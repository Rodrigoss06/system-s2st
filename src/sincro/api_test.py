"""make api-test - G2. Cubre cada endpoint del contrato y cada fila de la tabla de
degradacion (Notion, "SINCRO Motor v4 - Contrato de conexion", secciones 4 y 7).

Levanta Dispatcher (FastAPI/uvicorn) y Worker (`call_serve.py`) en el mismo proceso, el
uno apuntando al otro por variable de entorno. En produccion son dos Container Apps
(ver ACLARACION en DECISIONS.md); esto es lo mas cercano a un extremo a extremo real
sin desplegar nada.

Las filas 1 y 3 de la tabla de degradacion (TTS caido, LLM con rate limit) se prueban
invocando la logica nueva directamente -- `on_turn` y `_ResilientTranslator` -- en vez
de forzar la condicion real contra Deepgram/Groq/Fish, que no es practico ni etico de
disparar a proposito contra un proveedor en vivo. La fila 2 (STT desconectado) se
prueba igual, sobre `_DirectionState` con un transcriber de mentira.

**Fila 4 (WebSocket del cliente caido, reconexion en 30 s con el mismo token) NO esta
implementada.** Este harness la reporta como PENDIENTE explicitamente, no la oculta ni
la cuenta como si pasara.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .adapters.file_io import read_wav_frames_realtime
from .adapters.ws_io import SAMPLES_PER_FRAME, encode_frame, pad_to_frame
from .call_serve import run_call_server
from .call_session import Participant, _build_direction
from .config import load_settings
from .contracts import Segment
from .dispatcher import create_app
from .echo_gate import make_pair
from .engine import TurnResult
from .telemetry import TelemetryWriter
from .translator import GroqTranslator, TranslationError

logger = logging.getLogger(__name__)

DISPATCHER_PORT = 8790
WORKER_PORT = 8791
WAV_EN = "tests/fixtures/matrix_en_35s.wav"
WAV_ES = "tests/fixtures/es_30s.wav"
VOICE_CLIP = "tests/fixtures/voz_referencia.wav"


class _Result:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))
        tag = "OK" if ok else "FALLA"
        print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""))

    def pending(self, name: str, detail: str) -> None:
        self.checks.append((name, None, detail))  # type: ignore[arg-type]
        print(f"  [PENDIENTE] {name} -- {detail}")

    @property
    def all_ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks if ok is not None)


class _FakeConnection:
    """Suficiente para on_turn/_DirectionState: solo necesitan `.send()`."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, data: Any) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        pass

    def get_extra_info(self, name: str) -> None:
        return None

    @property
    def transport(self) -> _FakeConnection:
        return self


def _sent_types(conn: _FakeConnection) -> list[str]:
    types = []
    for raw in conn.sent:
        if isinstance(raw, str):
            types.append(json.loads(raw).get("t"))
    return types


async def _wait_dispatcher(base_url: str, timeout_s: float = 10.0) -> None:
    async with httpx.AsyncClient() as client:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"{base_url}/openapi.json", timeout=1.0)
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise TimeoutError("dispatcher no respondio a tiempo")


async def _run_mini_call(
    dispatcher_url: str, worker_url: str, tail_s: float = 8.0
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Un intercambio corto real, de punta a punta: Dispatcher emite tokens, dos
    clientes hacen hello contra el Worker con ellos. Devuelve los mensajes de control
    que recibio cada uno."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{dispatcher_url}/v1/sessions",
            json={
                "participants": [
                    {"user_id": "u_api_en", "lang": "en", "reference_id": ""},
                    {"user_id": "u_api_es", "lang": "es", "reference_id": ""},
                ]
            },
        )
        r.raise_for_status()
        body = r.json()
        token_en = body["tokens"]["u_api_en"]
        token_es = body["tokens"]["u_api_es"]

    async def one_side(token: str, wav_path: str) -> dict[str, Any]:
        async with connect(worker_url, compression=None) as ws:
            await ws.send(json.dumps({"t": "hello", "token": token}))
            messages: list[dict[str, Any]] = []

            async def receive() -> None:
                try:
                    async for msg in ws:
                        if isinstance(msg, str):
                            messages.append(json.loads(msg))
                except ConnectionClosed:
                    pass

            recv_task = asyncio.create_task(receive())
            seq = 0
            async for frame in read_wav_frames_realtime(wav_path):
                fin = frame.pcm.size != SAMPLES_PER_FRAME
                pcm = frame.pcm if not fin else pad_to_frame(frame.pcm)
                await ws.send(encode_frame(pcm, seq, int(frame.t_capture * 1000), fin=fin))
                seq = (seq + 1) & 0xFFFF
            await asyncio.sleep(tail_s)
            await ws.close()
            await recv_task
        return {"messages": messages}

    return await asyncio.gather(one_side(token_en, WAV_EN), one_side(token_es, WAV_ES))


async def test_sessions_and_hello(result: _Result, dispatcher_url: str, worker_url: str) -> None:
    print("\n== POST /v1/sessions + hello con token (el idioma viaja en el token, D52) ==")
    result_en, result_es = await _run_mini_call(dispatcher_url, worker_url)
    types_en = _sent_types_from_messages(result_en)
    types_es = _sent_types_from_messages(result_es)
    result.check(
        "POST /v1/sessions -> 201 y hello con token producen 'ready' en ambos lados",
        "ready" in types_en and "ready" in types_es,
        f"EN vio {types_en[:3]}..., ES vio {types_es[:3]}...",
    )


def _sent_types_from_messages(r: dict[str, Any]) -> list[str]:
    return [m.get("t") for m in r["messages"]]


async def test_delete_session(result: _Result, dispatcher_url: str) -> None:
    print("\n== DELETE /v1/sessions/{id} ==")
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{dispatcher_url}/v1/sessions",
            json={
                "participants": [
                    {"user_id": "u_del_a", "lang": "en", "reference_id": ""},
                    {"user_id": "u_del_b", "lang": "es", "reference_id": ""},
                ]
            },
        )
        session_id = r.json()["session_id"]

        r_del = await client.delete(f"{dispatcher_url}/v1/sessions/{session_id}")
        result.check(
            "DELETE de una sesion existente -> 204",
            r_del.status_code == 204,
            str(r_del.status_code),
        )

        r_del2 = await client.delete(f"{dispatcher_url}/v1/sessions/{session_id}")
        result.check(
            "DELETE de una sesion ya borrada -> 404",
            r_del2.status_code == 404,
            str(r_del2.status_code),
        )


async def test_voices(result: _Result, dispatcher_url: str) -> None:
    print("\n== POST /v1/voices + GET /v1/voices/{user_id} ==")
    voice_bytes = await asyncio.to_thread(Path(VOICE_CLIP).read_bytes)
    async with httpx.AsyncClient(timeout=60.0) as client:
        r_no_consent = await client.post(
            f"{dispatcher_url}/v1/voices",
            data={"user_id": "u_voice_1", "lang": "es", "consent": "false"},
            files={"audio": ("voz.wav", voice_bytes, "audio/wav")},
        )
        result.check(
            "POST /v1/voices sin consent -> 400",
            r_no_consent.status_code == 400,
            str(r_no_consent.status_code),
        )

        r_enroll = await client.post(
            f"{dispatcher_url}/v1/voices",
            data={"user_id": "u_voice_1", "lang": "es", "consent": "true"},
            files={"audio": ("voz.wav", voice_bytes, "audio/wav")},
        )
        result.check(
            "POST /v1/voices con consent -> 201 y reference_id",
            r_enroll.status_code == 201 and bool(r_enroll.json().get("reference_id")),
            f"HTTP {r_enroll.status_code}: {r_enroll.text[:150]}",
        )

        r_get = await client.get(f"{dispatcher_url}/v1/voices/u_voice_1")
        result.check(
            "GET /v1/voices/{user_id} de un usuario enrolado -> 200",
            r_get.status_code == 200 and len(r_get.json().get("voices", [])) >= 1,
            f"HTTP {r_get.status_code}: {r_get.text[:150]}",
        )

        r_missing = await client.get(f"{dispatcher_url}/v1/voices/u_nadie_registrado")
        result.check(
            "GET /v1/voices/{user_id} de un usuario sin timbre -> 404",
            r_missing.status_code == 404,
            str(r_missing.status_code),
        )


async def test_healthz_readyz(result: _Result, worker_http_url: str) -> None:
    print("\n== GET /healthz y GET /readyz (Worker) ==")
    async with httpx.AsyncClient() as client:
        r_health = await client.get(f"{worker_http_url}/healthz")
        result.check("GET /healthz -> 200", r_health.status_code == 200, r_health.text)

        r_ready = await client.get(f"{worker_http_url}/readyz")
        body = r_ready.json()
        result.check(
            "GET /readyz -> 200, modelos cargados, credenciales validas, slots libres",
            r_ready.status_code == 200
            and body.get("models_loaded") is True
            and all(c["ok"] for c in body.get("credentials", []))
            and "slots_free" in body,
            f"HTTP {r_ready.status_code}: {body}",
        )


async def test_degradation_row1_tts_down(result: _Result) -> None:
    print("\n== Degradacion fila 1: TTS caido -> error no fatal + translation subtitulo ==")
    base = load_settings()
    profile_a = base.profile
    gate_a, _gate_b = make_pair(profile_a, profile_a)
    speaker = Participant(connection=_FakeConnection(), lang="es")  # type: ignore[arg-type]
    listener = Participant(connection=_FakeConnection(), lang="en")  # type: ignore[arg-type]
    writer = TelemetryWriter()
    direction = _build_direction("A->B", base, speaker, listener, gate_a, writer)

    seg = Segment(seg_id=7, text="hola", lang="es", t_start=0.0, t_end=1.0, trigger="eou")
    turn = TurnResult(seg=seg, text_dst="hello", ttfa_ms=0, audio_duration=0.0, tts_failed=True)
    direction.on_turn(turn)
    await asyncio.sleep(0.05)  # los envios van por tareas fire-and-forget

    types = _sent_types(listener.connection)  # type: ignore[arg-type]
    result.check(
        "TurnResult.tts_failed=True dispara error(tts_unavailable) + translation",
        "error" in types and "translation" in types,
        f"mensajes vistos: {types}",
    )
    await direction.sink.close()
    await direction.state.aclose()


async def test_degradation_row2_stt_reconnect(result: _Result) -> None:
    print("\n== Degradacion fila 2: STT desconectado -> state:idle mientras dura ==")
    from .call_session import _DirectionState

    class _FakeGate:
        is_speaking = True

    class _FakeTranscriber:
        connected = True

    listener_conn = _FakeConnection()
    gate = _FakeGate()
    transcriber = _FakeTranscriber()
    state = _DirectionState(gate, transcriber, listener_conn)  # type: ignore[arg-type]

    await asyncio.sleep(0.3)  # deja que el sondeo detecte "speaking"
    transcriber.connected = False
    await asyncio.sleep(0.3)  # deja que el sondeo detecte la caida

    types = _sent_types(listener_conn)
    result.check(
        "transcriber.connected=False hace que el sondeo mande state:idle",
        types[-1:] == ["state"] and json.loads(listener_conn.sent[-1])["peer"] == "idle",
        f"mensajes vistos: {types}",
    )
    await state.aclose()


async def test_degradation_row3_llm_rate_limit(result: _Result) -> None:
    print("\n== Degradacion fila 3: LLM con rate limit -> reintento, luego texto sin traducir ==")
    from .call_session import _ResilientTranslator

    base = load_settings()
    profile = base.profile
    calls = 0

    async def flaky_translate(self: GroqTranslator, seg: Segment, budget: int) -> Any:
        nonlocal calls
        calls += 1
        raise TranslationError("groq call failed: 429 - Rate limit reached")

    original = GroqTranslator.translate
    GroqTranslator.translate = flaky_translate  # type: ignore[method-assign]
    degraded_messages: list[str] = []
    try:
        translator = _ResilientTranslator(
            base.groq_api_key, base.llm_model, profile,
            reasoning_effort=base.llm_reasoning_effort,
            on_degraded=degraded_messages.append,
        )
        seg = Segment(
            seg_id=9, text="fuente sin traducir", lang="es",
            t_start=0.0, t_end=1.0, trigger="eou",
        )
        out = await translator.translate(seg, budget=100)
        result.check(
            "tras agotar reintentos, devuelve el texto FUENTE en vez de lanzar",
            out.text == seg.text and calls == 3 and len(degraded_messages) == 1,
            f"calls={calls} text={out.text!r} degraded={degraded_messages}",
        )
    finally:
        GroqTranslator.translate = original  # type: ignore[method-assign]


async def test_degradation_row4_client_reconnect(result: _Result) -> None:
    print("\n== Degradacion fila 4: WebSocket del cliente caido, reconexion en 30s ==")
    result.pending(
        "sesion sobrevive 30s y acepta reconexion con el mismo token",
        "no implementado en G2 -- CallSession.run() cierra la llamada entera en cuanto "
        "un lado se desconecta (asyncio.wait FIRST_COMPLETED). Requiere poder "
        "re-enchufar una conexion nueva a un DubbingEngine ya corriendo; alcance del "
        "tamano de M9/M10, no una extension de una fila existente. Ver DECISIONS.md.",
    )


async def main_async() -> int:
    base = load_settings()
    writer = TelemetryWriter()
    result = _Result()

    worker = await run_call_server("localhost", WORKER_PORT, base, writer)
    worker_ws_url = f"ws://localhost:{WORKER_PORT}"
    worker_http_url = f"http://localhost:{WORKER_PORT}"

    import os

    os.environ["SINCRO_WORKER_WS_URL"] = worker_ws_url
    app = create_app(base.fish_api_key, db_path=f"out/api-test-voices-{int(time.time())}.db")
    config = uvicorn.Config(app, host="localhost", port=DISPATCHER_PORT, log_level="warning")
    uv_server = uvicorn.Server(config)
    dispatcher_task = asyncio.create_task(uv_server.serve())
    dispatcher_url = f"http://localhost:{DISPATCHER_PORT}"

    try:
        await _wait_dispatcher(dispatcher_url)

        await test_sessions_and_hello(result, dispatcher_url, worker_ws_url)
        await test_delete_session(result, dispatcher_url)
        await test_voices(result, dispatcher_url)
        await test_healthz_readyz(result, worker_http_url)
        await test_degradation_row1_tts_down(result)
        await test_degradation_row2_stt_reconnect(result)
        await test_degradation_row3_llm_rate_limit(result)
        await test_degradation_row4_client_reconnect(result)
    finally:
        uv_server.should_exit = True
        await dispatcher_task
        worker.close()
        await worker.wait_closed()

    pending = [c for c in result.checks if c[1] is None]
    print("\n" + "=" * 72)
    print(f"  RESULTADO G2: {'PASA' if result.all_ok else 'FALLA'}"
          f"{' (' + str(len(pending)) + ' pendiente(s), ver arriba)' if pending else ''}")
    print("=" * 72)
    return 0 if result.all_ok else 1


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(main_async())
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        logger.exception("api-test failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
