"""M10 - CallSession. Dos DubbingEngine (uno por direccion) mas M9, coordinados.

Ningun motor conoce al otro (CLAUDE.md): CallSession es el unico objeto que ve las dos
mitades. Posee los dos `DubbingEngine` completos -- contexto rodante, reloj de deriva,
`reference_id` y contador de segmentos son estado POR HABLANTE, nunca compartido -- y
los dos `EchoGate` (M9) que los cruzan.

No implementa el `hello` ni el timeout de 60 s de emparejamiento del contrato: eso es
responsabilidad de `call_serve.py`, que arma un `CallSession` solo cuando ya tiene dos
participantes con `hello` valido. `CallSession.run()` asume que la llamada ya existe.

Mensajes de control (contrato, seccion 5) que este modulo emite:

- `state` (`speaking` | `translating` | `idle`): aproximado sin tocar el nucleo. El
  nucleo no expone un callback de inicio/fin de habla ni de "segmento recien
  comprometido"; se obtienen envolviendo `EchoGate.is_speaking` (subclase de
  SileroGate, ver D47) y sub-clasando `StreamingCommitter.commit()` para observar cada
  `Segment` en cuanto se cierra, antes de traducir. Es composicion en este archivo, no
  un cambio a `gate.py` ni a `committer.py`.
- `dub_start` / `dub_end`: `dub_start` en el primer chunk de `on_audio` de un segmento
  (streaming real, sin esperar la sintesis completa); `dub_end` cuando `on_turn` cierra
  ese segmento. `duration_ms` de `dub_start` no puede conocerse de antemano sin
  esperar la sintesis entera -- eso es justo lo que CLAUDE.md prohibe -- asi que se
  manda en 0; ningun consumidor de este commit depende todavia de ese numero.

G2 anade las senales de la tabla de degradacion (contrato, seccion 7) que hacia falta
enganchar (la politica en si ya existia en el nucleo de F6, o se anade aqui por
composicion, nunca editando M1-M8):

- **TTS caido**: `engine.py` ya degrada a subtitulo (`TurnResult.tts_failed`); `on_turn`
  aqui abajo lo traduce en `error` no fatal + `translation` hacia quien escuchaba.
- **STT desconectado**: `transcriber.py` (M3) ya reconecta con backoff desde F6 y
  expone `self.connected: bool` en tiempo real; `_DirectionState` lo sondea igual que
  `EchoGate.is_speaking` y manda `state: idle` mientras dura, sin tocar M3.
- **LLM agotado por rate limit**: `translator.py` (M5) no reintenta -- cualquier fallo
  de Groq revienta `TranslationError` de una. `_ResilientTranslator` (subclase de
  GroqTranslator, mismo patron de herencia que `EchoGate` sobre `SileroGate`, D47) le
  anade reintento con backoff, y si persiste pasa el texto fuente sin traducir en vez
  de tirar la sesion, con `error` no fatal hacia el que escuchaba.
- **WebSocket del cliente caido**: NO implementado en G2. El contrato pide que la
  sesion sobreviva 30 s esperando reconexion con el mismo token; hoy `CallSession.run()`
  cierra toda la llamada en cuanto un lado se desconecta. Es un cambio de arquitectura
  del tamano de M9/M10 (re-conectar un socket nuevo a un `DubbingEngine` ya corriendo),
  no una extension de una fila existente -- que se decida antes de construirlo.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from .adapters.ws_io import WebSocketAudioSink, WebSocketAudioSource, enable_tcp_nodelay
from .committer import StreamingCommitter
from .config import Settings
from .contracts import DubbedChunk, Lang, Segment, TranscriptEvent, Translation
from .echo_gate import EchoGate, make_pair
from .engine import DubbingEngine, EngineStats, TurnResult
from .gate import DEFAULT_MIN_SILENCE
from .synthesizer import FishSynthesizer
from .telemetry import TelemetryWriter
from .transcriber import DeepgramStreamTranscriber
from .translator import GroqTranslator, TranslationError
from .voices import FishVoiceRegistry

logger = logging.getLogger(__name__)

# El contrato exige 16 kHz mono en las dos direcciones del socket (D44, heredado de G0).
WS_SAMPLE_RATE = 16_000

STATE_POLL_S = 0.12
LLM_MAX_RETRIES = 2
LLM_BACKOFF_BASE_S = 1.0

OnSegment = Callable[[Segment], None]
OnDegraded = Callable[[str], None]


class _ResilientTranslator(GroqTranslator):
    """Fila 3 de la tabla de degradacion: `translator.py` (M5) no reintenta, cualquier
    fallo de Groq revienta `TranslationError` de una. Subclase, mismo patron que
    `EchoGate` sobre `SileroGate` (D47): satisface el tipo concreto que
    `DubbingEngine.__init__` ya exige, sin tocar `translator.py` ni `engine.py`.

    Reintenta con backoff SOLO si el fallo huele a limite de tasa (Groq devuelve HTTP
    429; D30 ya documento el mensaje exacto: "429 - Rate limit reached"). Si persiste
    tras los reintentos, pasa el texto fuente sin traducir en vez de tirar la sesion --
    la llamada sigue, degradada, en vez de caerse entera por un fallo de una etapa
    (CLAUDE.md, la misma politica de F6 elevada a nivel de llamada).
    """

    def __init__(self, *args: Any, on_degraded: OnDegraded | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_degraded = on_degraded

    async def translate(self, seg: Segment, budget: int) -> Translation:
        last_exc: TranslationError | None = None
        for attempt in range(LLM_MAX_RETRIES + 1):
            try:
                return await super().translate(seg, budget)
            except TranslationError as exc:
                last_exc = exc
                if "429" not in str(exc):
                    raise  # no es rate limit: no es esta fila de la tabla
                if attempt < LLM_MAX_RETRIES:
                    await asyncio.sleep(LLM_BACKOFF_BASE_S * 2**attempt)

        logger.warning(
            "seg %d: LLM rate-limited tras %d reintentos, se pasa sin traducir: %s",
            seg.seg_id, LLM_MAX_RETRIES, last_exc,
        )
        if self._on_degraded is not None:
            self._on_degraded(f"llm rate limited after {LLM_MAX_RETRIES} retries: {last_exc}")
        return Translation(
            seg_id=seg.seg_id,
            text=seg.text,
            lang=self.profile.dst,
            byte_budget=budget,
            byte_actual=len(seg.text.encode("utf-8")),
        )


@dataclass
class Participant:
    connection: ServerConnection
    lang: Lang
    reference_id: str = ""
    user_id: str = ""


async def _send_json(connection: ServerConnection, payload: dict[str, object]) -> None:
    try:
        await connection.send(json.dumps(payload))
    except ConnectionClosed:
        logger.warning("call_session: no se pudo mandar %s, socket cerrado", payload.get("t"))


class _ObservingCommitter(StreamingCommitter):
    """Subclase de StreamingCommitter (M4): mismo comportamiento, mas un callback que
    ve cada Segment en cuanto se cierra, antes de traducir. No cambia committer.py."""

    def __init__(self, gate: EchoGate, lang_code: str, on_segment: OnSegment) -> None:
        super().__init__(gate, lang_code)
        self._on_segment = on_segment

    async def commit(self, events: AsyncIterator[TranscriptEvent]) -> AsyncIterator[Segment]:
        async for seg in super().commit(events):
            self._on_segment(seg)
            yield seg


class _DirectionState:
    """Rastrea speaking/translating/idle para UNA direccion y manda `state` al que
    escucha. Aproximado (ver docstring del modulo): sondea `EchoGate.is_speaking` y
    `transcriber.connected` cada STATE_POLL_S en vez de recibir un evento del nucleo,
    que no existe."""

    def __init__(
        self,
        gate: EchoGate,
        transcriber: DeepgramStreamTranscriber,
        listener: ServerConnection,
    ) -> None:
        self._gate = gate
        self._transcriber = transcriber
        self._listener = listener
        self._state = "idle"
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._poll_task = asyncio.create_task(self._poll())
        # Sin referencia fuerte el GC puede matar un envio de `state` a medias (mismo
        # riesgo que el unmute diferido de v3 en engine.py).
        self._pending: set[asyncio.Task[None]] = set()

    def _fire(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send(self, state: str) -> None:
        async with self._lock:
            if state == self._state:
                return
            self._state = state
        await _send_json(self._listener, {"t": "state", "peer": state})

    async def _poll(self) -> None:
        try:
            while not self._stop.is_set():
                # Fila 2 de la tabla de degradacion: STT desconectado -> `state: idle`
                # mientras dura. Se comprueba antes que `is_speaking`: si Deepgram esta
                # cayendo, no hay forma real de saber si el hablante habla o no.
                if not self._transcriber.connected:
                    if self._state != "idle":
                        await self._send("idle")
                elif self._gate.is_speaking and self._state != "speaking":
                    await self._send("speaking")
                await asyncio.sleep(STATE_POLL_S)
        except asyncio.CancelledError:
            pass

    def on_segment_committed(self, seg: Segment) -> None:
        # El commit YA es la senal de fin de turno (eou/punctuation/timeout/max_len);
        # no se condiciona a `gate.is_speaking`, que tarda min_silence_duration (550 ms
        # por defecto) en bajar y casi siempre sigue en True cuando el commit dispara.
        # Guardarlo aqui casi nunca mandaba `translating` -- medido en `make call-test`,
        # no supuesto (ver D48).
        self._fire(self._send("translating"))

    def on_turn(self, result: TurnResult) -> None:
        self._fire(self._send("idle"))

    async def aclose(self) -> None:
        self._stop.set()
        self._poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._poll_task
        for t in list(self._pending):
            with contextlib.suppress(asyncio.CancelledError):
                await t


@dataclass
class _Direction:
    """Un motor completo, su sink, su seguimiento de estado y el `on_turn` que hay que
    pasarle a `engine.run()` -- `DubbingEngine.__init__` no acepta `on_turn`, solo
    `engine.run(frames, on_turn=...)` lo hace (ver `live.py`)."""

    label: str
    engine: DubbingEngine
    source: WebSocketAudioSource
    sink: WebSocketAudioSink
    state: _DirectionState
    on_turn: Callable[[TurnResult], None]


def _build_direction(
    label: str,
    base: Settings,
    speaker: Participant,
    listener: Participant,
    gate: EchoGate,
    writer: TelemetryWriter,
    observer: Callable[[TurnResult], None] | None = None,
) -> _Direction:
    """Motor completo de `speaker` hacia `listener`: STT en el idioma de `speaker`
    (perfil ya resuelto en `gate.profile`), TTS en el idioma de `listener`, timbre de
    `listener` -- es quien lo va a escuchar."""
    profile = gate.profile

    source = WebSocketAudioSource(speaker.connection)
    sink = WebSocketAudioSink(listener.connection)

    transcriber = DeepgramStreamTranscriber(
        base.deepgram_api_key, profile.src, profile.deepgram_code
    )
    state = _DirectionState(gate, transcriber, listener.connection)
    committer = _ObservingCommitter(gate, profile.turn_detector_code, state.on_segment_committed)

    def on_llm_degraded(detail: str) -> None:
        fire(
            _send_json(
                listener.connection,
                {"t": "error", "code": "llm_rate_limited", "fatal": False, "detail": detail},
            )
        )

    translator = _ResilientTranslator(
        base.groq_api_key,
        base.llm_model,
        profile,
        reasoning_effort=base.llm_reasoning_effort,
        temperature=base.llm_temperature,
        max_tokens=base.llm_max_tokens,
        on_degraded=on_llm_degraded,
    )
    synth = FishSynthesizer(base.fish_api_key, model=base.tts_model, sample_rate=WS_SAMPLE_RATE)
    registry = FishVoiceRegistry(base.fish_api_key)
    reference_id = ""
    if listener.reference_id:
        registry.remember(f"listener-{label}", listener.reference_id)
        reference_id = listener.reference_id

    dub_open = False
    pending: set[asyncio.Task[None]] = set()

    def fire(coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coro)
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def on_audio(chunk: DubbedChunk) -> None:
        nonlocal dub_open
        if not dub_open:
            dub_open = True
            await _send_json(
                listener.connection,
                {"t": "dub_start", "seg_id": chunk.seg_id, "duration_ms": 0},
            )
        await sink.play(chunk)

    def on_turn(result: TurnResult) -> None:
        nonlocal dub_open
        sink.end_utterance(result)
        if dub_open:
            dub_open = False
            fire(_send_json(listener.connection, {"t": "dub_end", "seg_id": result.seg.seg_id}))
        if result.tts_failed:
            # Fila 1 de la tabla de degradacion: engine.py (F6) ya siguio sin audio;
            # aqui se traduce en la senal del contrato -- error no fatal, y la
            # traduccion como subtitulo para quien iba a escuchar el doblaje.
            fire(
                _send_json(
                    listener.connection,
                    {"t": "error", "code": "tts_unavailable", "fatal": False},
                )
            )
            fire(
                _send_json(
                    listener.connection,
                    {"t": "translation", "who": "peer", "text": result.text_dst,
                     "seg_id": result.seg.seg_id},
                )
            )
        state.on_turn(result)
        if observer is not None:
            observer(result)

    engine = DubbingEngine(
        base, gate, transcriber, committer, translator, synth, writer,
        on_audio=on_audio, reference_id=reference_id,
    )
    return _Direction(
        label=label, engine=engine, source=source, sink=sink, state=state, on_turn=on_turn
    )


class CallSession:
    """Posee los dos DubbingEngine y los dos EchoGate. Ningun motor conoce al otro."""

    def __init__(
        self,
        base: Settings,
        participant_a: Participant,
        participant_b: Participant,
        writer: TelemetryWriter,
        on_turn_ab: Callable[[TurnResult], None] | None = None,
        on_turn_ba: Callable[[TurnResult], None] | None = None,
    ) -> None:
        """`on_turn_ab`/`on_turn_ba` son puramente de observacion externa (p.ej.
        `call_test.py` verificando M9): se llaman despues de que CallSession ya hizo su
        propio trabajo con el turno, nunca lo condicionan."""
        self.a = participant_a
        self.b = participant_b
        enable_tcp_nodelay(participant_a.connection)
        enable_tcp_nodelay(participant_b.connection)

        profile_ab = dataclasses.replace(
            base, src_lang=participant_a.lang, dst_lang=participant_b.lang
        ).profile
        profile_ba = dataclasses.replace(
            base, src_lang=participant_b.lang, dst_lang=participant_a.lang
        ).profile
        gate_ab, gate_ba = make_pair(
            profile_ab, profile_ba, min_silence_duration=DEFAULT_MIN_SILENCE
        )

        self.dir_ab = _build_direction(
            "A->B", base, participant_a, participant_b, gate_ab, writer, observer=on_turn_ab
        )
        self.dir_ba = _build_direction(
            "B->A", base, participant_b, participant_a, gate_ba, writer, observer=on_turn_ba
        )

    async def run(self) -> tuple[EngineStats, EngineStats]:
        for direction in (self.dir_ab, self.dir_ba):
            direction.engine.gate.load_eou()

        await _send_json(self.a.connection, {"t": "ready", "peer_lang": self.b.lang})
        await _send_json(self.b.connection, {"t": "ready", "peer_lang": self.a.lang})

        task_ab = asyncio.create_task(
            self.dir_ab.engine.run(self.dir_ab.source.frames(), on_turn=self.dir_ab.on_turn)
        )
        task_ba = asyncio.create_task(
            self.dir_ba.engine.run(self.dir_ba.source.frames(), on_turn=self.dir_ba.on_turn)
        )
        done, pending = await asyncio.wait({task_ab, task_ba}, return_when=asyncio.FIRST_COMPLETED)

        # Un lado colgo: el otro no tiene con quien seguir. peer_left y cierre limpio.
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        survivor = self.b.connection if task_ab in done else self.a.connection
        await _send_json(survivor, {"t": "peer_left"})

        await self.dir_ab.state.aclose()
        await self.dir_ba.state.aclose()
        await self.dir_ab.sink.close()
        await self.dir_ba.sink.close()
        await self.dir_ab.engine.aclose()
        await self.dir_ba.engine.aclose()

        return self.dir_ab.engine.stats, self.dir_ba.engine.stats
