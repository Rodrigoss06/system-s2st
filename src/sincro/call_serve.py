"""G1/G2 - empareja dos conexiones WebSocket en un CallSession.

Implementa el ciclo de vida de "la sesion existe" del contrato (seccion 3): existe
cuando AMBOS mandan `hello`; el primero recibe `waiting_for_peer`; si el segundo no
aparece en 60 s, la sesion expira y libera el slot.

G2 cierra la deuda que G1 dejo anotada (D52): el `hello` ya no declara `lang`
directamente. Lleva un **token firmado** (`tokens.py`) emitido por el Dispatcher en
`POST /v1/sessions`, y de ahi salen `session_id`, `user_id`, `lang` y `reference_id`
-- el cliente no puede cambiar de idioma a mitad de sesion porque el transporte ya no
le da esa puerta (contrato, seccion 8). El token se valida y se canjea (de un solo
uso) en `_read_hello`.

`_waiting` ya se indexa por `session_id` real (no por una clave fija): dos llamadas
distintas ya no pueden cruzar a sus participantes entre si. Sigue siendo un solo
proceso Worker sin cupo de N llamadas concurrentes -- eso es S3/G4.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from . import metrics
from .call_session import CallSession, Participant
from .check import probe_credentials
from .config import Settings
from .engine import TurnResult
from .gate import DEFAULT_MIN_SILENCE, SileroGate
from .telemetry import TelemetryWriter
from .tokens import TokenClaims, TokenError, TokenStore, decode_token

logger = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 10.0  # tiempo para mandar hello tras conectar, no el de emparejar
PAIR_TIMEOUT_S = 60.0  # contrato, seccion 3
READY_CACHE_S = 30.0  # no relanzar credenciales/modelos en cada poll de /readyz


@dataclass
class _Waiting:
    connection: ServerConnection
    claims: TokenClaims
    paired: asyncio.Event
    finished: asyncio.Future[None]


async def _read_hello(connection: ServerConnection, token_store: TokenStore) -> TokenClaims:
    raw = await asyncio.wait_for(connection.recv(), timeout=HELLO_TIMEOUT_S)
    if not isinstance(raw, str):
        raise ValueError("se esperaba hello (texto/JSON), llego binario")
    msg = json.loads(raw)
    if not isinstance(msg, dict):
        # "null", "[1,2]", "42" son JSON validos pero no un objeto: .get() sobre eso
        # no es un dict revienta con AttributeError, no con algo que el llamador espere.
        raise ValueError(f"se esperaba un objeto JSON, llego {type(msg).__name__}")
    if msg.get("t") != "hello":
        raise ValueError(f"se esperaba hello, llego {msg.get('t')!r}")
    token = msg.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("hello sin token")
    claims = decode_token(token)
    # De un solo uso: se canjea AQUI, al aceptar el hello (contrato, seccion 8), no
    # antes de saber que el token es valido y no antes de emparejar.
    token_store.redeem(claims)
    return claims


class Lobby:
    def __init__(
        self,
        base: Settings,
        writer: TelemetryWriter,
        on_turn_ab: Callable[[TurnResult], None] | None = None,
        on_turn_ba: Callable[[TurnResult], None] | None = None,
        on_session_done: Callable[[CallSession], None] | None = None,
    ) -> None:
        self._base = base
        self._writer = writer
        self._on_turn_ab = on_turn_ab
        self._on_turn_ba = on_turn_ba
        self._on_session_done = on_session_done
        self._waiting: dict[str, _Waiting] = {}
        self._lock = asyncio.Lock()
        self._tokens = TokenStore()

    async def handle(self, connection: ServerConnection) -> None:
        try:
            claims = await _read_hello(connection, self._tokens)
        except (TimeoutError, ValueError, ConnectionClosed, TokenError) as exc:
            logger.warning("call_serve: hello invalido o ausente: %s", exc)
            metrics.worker_tokens_rejected_total.inc()
            with contextlib.suppress(ConnectionClosed):
                await connection.close()
            return

        session_id = claims.session_id
        async with self._lock:
            entry = self._waiting.pop(session_id, None)
            if entry is None:
                entry = _Waiting(
                    connection=connection,
                    claims=claims,
                    paired=asyncio.Event(),
                    finished=asyncio.get_running_loop().create_future(),
                )
                self._waiting[session_id] = entry
                is_first = True
            else:
                is_first = False

        if is_first:
            # El handler del primero se queda vivo hasta que la llamada termine, no
            # solo hasta emparejar (bug D-critico de G1: dormir un `sleep` fijo
            # desconectaba a este participante a los 60 s de conectar, en plena
            # llamada; ver D51).
            await self._wait_for_peer(session_id, connection, entry)
        else:
            await self._start_session(entry, connection, claims)

    async def _wait_for_peer(
        self, session_id: str, connection: ServerConnection, entry: _Waiting
    ) -> None:
        with contextlib.suppress(ConnectionClosed):
            await connection.send(json.dumps({"t": "waiting_for_peer"}))

        try:
            await asyncio.wait_for(entry.paired.wait(), timeout=PAIR_TIMEOUT_S)
        except TimeoutError:
            async with self._lock:
                if self._waiting.get(session_id) is entry:
                    del self._waiting[session_id]
            metrics.worker_peer_timeouts_total.inc()
            logger.info("call_serve: nadie llego en %.0fs, se libera el slot", PAIR_TIMEOUT_S)
            with contextlib.suppress(ConnectionClosed):
                await connection.send(
                    json.dumps({"t": "error", "code": "peer_timeout", "fatal": True})
                )
                await connection.close()
            return

        # Emparejado: el handler sigue vivo mientras dure la sesion. `_start_session`
        # (que corre en el handler del OTRO participante) marca `finished` pase lo que
        # pase, incluida una excepcion, para no dejar a este colgado.
        await entry.finished

    async def _start_session(
        self, entry: _Waiting, conn_b: ServerConnection, claims_b: TokenClaims
    ) -> None:
        entry.paired.set()
        metrics.worker_sessions_total.inc()
        metrics.worker_sessions_active.inc()
        try:
            participant_a = _participant(entry.connection, entry.claims)
            participant_b = _participant(conn_b, claims_b)
            session = CallSession(
                self._base,
                participant_a,
                participant_b,
                self._writer,
                on_turn_ab=self._on_turn_ab,
                on_turn_ba=self._on_turn_ba,
            )
            await session.run()
            if self._on_session_done is not None:
                self._on_session_done(session)
        finally:
            metrics.worker_sessions_active.dec()
            if not entry.finished.done():
                entry.finished.set_result(None)

    @property
    def slots_free(self) -> int:
        """Un solo cupo por proceso en G2 (S3/G4 reparte entre N workers)."""
        return 0 if self._waiting else 1


def _participant(connection: ServerConnection, claims: TokenClaims) -> Participant:
    return Participant(
        connection=connection,
        lang=claims.lang,
        reference_id=claims.reference_id,
        user_id=claims.user_id,
    )


@dataclass
class _ReadyStatus:
    models_loaded: bool = False
    credentials: list[dict[str, object]] = field(default_factory=list)
    checked_at: float = 0.0

    @property
    def ready(self) -> bool:
        return self.models_loaded and all(bool(c["ok"]) for c in self.credentials)


class ReadinessProbe:
    """`GET /readyz` (contrato, seccion 4): modelos cargados, credenciales validas,
    slots libres.

    `models_loaded` se comprueba UNA vez, en `warm_up()`: cargar el turn-detector tarda
    segundos (ONNX), y una vez cargado no hay nada que revisar de nuevo -- no cambia en
    caliente. Repetirlo periodicamente seria trabajo caro sin ninguna senal nueva.

    `credentials` si puede cambiar en caliente (una key revocada, una cuenta
    suspendida), asi que se reprueba en segundo plano cada `READY_CACHE_S`. Corre en un
    task aparte, nunca dentro de la peticion: la primera version de esto refrescaba
    dentro de `status()`, y un poll de `/readyz` que llegaba justo cuando el cache
    vencia se quedaba esperando la vuelta de red de los tres proveedores -- un health
    check nunca debe poder bloquear en su propia peticion (medido con `make api-test`,
    no supuesto)."""

    def __init__(self, base: Settings, lobby: Lobby) -> None:
        self._base = base
        self._lobby = lobby
        self._status = _ReadyStatus()
        self._refresh_task: asyncio.Task[None] | None = None

    async def warm_up(self) -> None:
        """Se llama una vez al arrancar el Worker, antes de aceptar conexiones."""
        probe_gate = SileroGate(self._base.profile, min_silence_duration=DEFAULT_MIN_SILENCE)
        try:
            await asyncio.to_thread(probe_gate.load_eou)
            self._status.models_loaded = True
        except Exception:
            logger.exception("readyz: fallo cargando el turn-detector")
            self._status.models_loaded = False
        await self._refresh_credentials()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_credentials(self) -> None:
        results = await probe_credentials(self._base)
        self._status.credentials = [
            {"provider": r.provider, "ok": r.ok, "detail": r.detail} for r in results
        ]
        self._status.checked_at = time.monotonic()

    async def _refresh_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(READY_CACHE_S)
                await self._refresh_credentials()
        except asyncio.CancelledError:
            pass

    async def status(self) -> tuple[bool, dict[str, object]]:
        st = self._status
        return st.ready, {
            "models_loaded": st.models_loaded,
            "credentials": st.credentials,
            "slots_free": self._lobby.slots_free,
            "checked_at": round(st.checked_at, 3),
        }


def _json_response(status_code: int, reason: str, payload: dict[str, object]) -> Response:
    body = json.dumps(payload).encode("utf-8")
    headers = Headers(
        [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
    )
    return Response(status_code, reason, headers, body)


def _make_process_request(
    ready: ReadinessProbe,
) -> Callable[[ServerConnection, Request], Awaitable[Response | None]]:
    async def process_request(connection: ServerConnection, request: Request) -> Response | None:
        # Rutas GET del Worker (seccion 4 del contrato); todo lo demas sigue el
        # protocolo normal de upgrade a WebSocket. POST/DELETE no pasan por aqui: el
        # parser HTTP de `websockets` los rechaza antes de llegar a este hook (D50),
        # y por eso viven en el Dispatcher (`dispatcher.py`), no en el Worker.
        path = request.path.split("?", 1)[0]
        if path == "/healthz":
            return _json_response(200, "OK", {"status": "ok"})
        if path == "/readyz":
            is_ready, detail = await ready.status()
            reason = "OK" if is_ready else "Not Ready"
            return _json_response(200 if is_ready else 503, reason, detail)
        return None

    return process_request


async def run_call_server(
    host: str,
    port: int,
    base: Settings,
    writer: TelemetryWriter,
    on_turn_ab: Callable[[TurnResult], None] | None = None,
    on_turn_ba: Callable[[TurnResult], None] | None = None,
    on_session_done: Callable[[CallSession], None] | None = None,
) -> Server:
    lobby = Lobby(
        base, writer, on_turn_ab=on_turn_ab, on_turn_ba=on_turn_ba, on_session_done=on_session_done
    )
    ready = ReadinessProbe(base, lobby)
    await ready.warm_up()
    return await serve(
        lobby.handle, host, port, compression=None, process_request=_make_process_request(ready)
    )


if __name__ == "__main__":
    import argparse
    import asyncio
    import sys

    from .config import load_settings

    ap = argparse.ArgumentParser(
        prog="call_serve", description="Worker: empareja dos WebSocket en un CallSession"
    )
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    async def _main() -> int:
        s = load_settings()
        writer = TelemetryWriter()
        server = await run_call_server(args.host, args.port, s, writer)
        print(f"Worker listening on ws://{args.host}:{args.port}  telemetry={writer.path}")
        async with server:
            await server.serve_forever()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
