"""DubbingEngine - orquestador asincrono, F2.

Agnostico del transporte por contrato: recibe SpeechFrame y emite DubbedChunk mas
eventos. No sabe si el audio viene de un microfono o de una pista de LiveKit.

Dos lazos concurrentes:

  captura : frames -> gate(VAD) -> transcriber(WS) -> committer -> cola de segmentos
  salida  : cola -> translator -> synthesizer -> altavoz, con la puerta cerrada

Estan separados a proposito. Si fueran uno solo, el motor dejaria de escuchar mientras
sintetiza, y el hablante perderia todo lo dicho durante la reproduccion.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from .committer import StreamingCommitter
from .config import Settings, uses_byte_budget
from .contracts import DubbedChunk, Segment
from .drift import DriftController
from .gate import UNMUTE_GUARD_S, SileroGate
from .synthesizer import FishSynthesizer
from .telemetry import SegmentRecord, TelemetryWriter, llm_cost_usd, tts_cost_usd
from .transcriber import DeepgramStreamTranscriber
from .translator import GroqTranslator

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    seg: Segment
    text_dst: str
    ttfa_ms: int
    audio_duration: float
    dropped: bool = False


@dataclass
class EngineStats:
    turns: int = 0
    skipped: int = 0
    dropped: int = 0
    leaked: int = 0
    ttfa_ms: list[int] = field(default_factory=list)
    triggers: dict[str, int] = field(default_factory=dict)


class DubbingEngine:
    def __init__(
        self,
        settings: Settings,
        gate: SileroGate,
        transcriber: DeepgramStreamTranscriber,
        committer: StreamingCommitter,
        translator: GroqTranslator,
        synthesizer: FishSynthesizer,
        writer: TelemetryWriter,
        on_audio: Callable[[DubbedChunk], Any] | None = None,
        reference_id: str = "",
    ) -> None:
        self.s = settings
        self.gate = gate
        self.transcriber = transcriber
        self.committer = committer
        self.translator = translator
        self.synth = synthesizer
        self.writer = writer
        self.on_audio = on_audio
        self.reference_id = reference_id
        self.stats = EngineStats()
        self.drift_ctl = DriftController()
        self.t_capture_start = 0.0
        self._segments: asyncio.Queue[Segment | None] = asyncio.Queue()
        # Sin referencia fuerte el GC puede matar el unmute a medias y dejar el
        # microfono cerrado para siempre.
        self._pending_tasks: set[asyncio.Task[None]] = set()

    # ---- lazo de captura ----

    async def _capture(self, frames: AsyncIterator[Any]) -> None:
        async def stamped() -> AsyncIterator[Any]:
            async for f in frames:
                if self.t_capture_start == 0.0:
                    self.t_capture_start = time.monotonic() - f.t_capture
                yield f

        gated = self.gate.process(stamped())
        events = self.transcriber.stream(gated)
        try:
            async for seg in self.committer.commit(events):
                await self._segments.put(seg)
        finally:
            await self._segments.put(None)

    # ---- lazo de salida ----

    def _speech_end_monotonic(self, seg: Segment) -> float:
        """Convierte el final del segmento a reloj monotonico.

        seg.t_end esta en la linea temporal de Deepgram, que cuenta **audio recibido**.
        El gate descarta silencio y frames muteados, asi que ese reloj va mas lento que el
        de pared y no se puede sumar directamente al instante de apertura: eso inflaba el
        TTFA. Se traduce primero a t_capture, que si es tiempo real del microfono.

        Se usa el timestamp de la palabra y no la hora de llegada del paquete para que el
        jitter de red Arequipa-US no entre en la medida.
        """
        capture_t = self.transcriber.capture_time_for(seg.t_end)
        if capture_t is None or self.t_capture_start == 0.0:
            return self.transcriber.t_stream_start + seg.t_end
        return self.t_capture_start + capture_t

    async def _process(self, seg: Segment) -> TurnResult:
        rec = SegmentRecord(
            seg_id=seg.seg_id,
            lang_src=self.s.src_lang,
            lang_dst=self.s.dst_lang,
            trigger=seg.trigger,
        )
        t_speech_end = self._speech_end_monotonic(seg)
        rec.mark("t_speech_end", t_speech_end)
        rec.mark("t_stt_final")

        budget = self.translator.budget_for(seg)
        tr = await self.translator.translate(seg, budget)
        rec.mark("t_llm_first_token", self.translator.t_first_token)
        rec.mark("t_llm_done", self.translator.t_done)

        # Una pausa larga borra la deuda antes de decidir velocidad o descarte.
        self.drift_ctl.note_gap(seg)
        dropped = self.drift_ctl.should_drop(seg)
        # Ratio de duracion, no de bytes. Ver dub_file.py y D22.
        speed = self.drift_ctl.speed_for(seg, self.drift_ctl.duration_ratio())
        if dropped:
            logger.info(
                "seg %d dropped: drift %.2fs > %.2fs, trigger=%s, %d chars",
                seg.seg_id,
                self.drift_ctl.drift,
                self.drift_ctl.THRESHOLD_HARD,
                seg.trigger,
                len(seg.text),
            )

        # Anti-eco: la puerta se cierra ANTES del primer sample y se abre 150 ms despues
        # del ultimo, no antes.
        # Un segmento descartado no cierra la puerta: no hay nada que reproducir, y
        # cerrarla dejaria al hablante sin microfono sin motivo.
        if not dropped:
            self.gate.mute()
        audio_duration = 0.0
        first = True
        t_audio_out = time.monotonic()
        try:
            async for chunk in self._synth_or_skip(tr, speed, dropped):
                if first:
                    rec.mark("t_tts_first_byte", self.synth.t_first_byte)
                    t_audio_out = time.monotonic()
                    first = False
                audio_duration += chunk.audio_duration
                if self.on_audio is not None:
                    await self.on_audio(chunk)
        finally:
            if first:
                # Sin audio: [[SKIP]] o texto vacio. Se marca igual para no dejar la
                # linea JSONL incompleta.
                rec.mark("t_tts_first_byte")
                t_audio_out = time.monotonic()
            rec.mark("t_audio_out", t_audio_out)
            if not dropped:
                task = asyncio.create_task(self.gate.unmute_after_guard(UNMUTE_GUARD_S))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)

        if not dropped:
            self.drift_ctl.observe(seg, audio_duration, speed)
        self.drift_ctl.update(seg, audio_duration, speed, dropped)
        rec.source_duration_s = round(seg.source_duration, 3)
        rec.audio_duration_s = round(audio_duration, 3)
        rec.speed_applied = round(speed, 3)
        rec.drift_s = round(self.drift_ctl.drift, 3)
        rec.bytes_in = len(seg.text.encode("utf-8"))
        rec.bytes_out = tr.byte_actual
        rec.byte_budget = tr.byte_budget
        rec.tokens_in = self.translator.tokens_in
        rec.tokens_out = self.translator.tokens_out
        rec.cost_usd = round(
            llm_cost_usd(self.translator.tokens_in, self.translator.tokens_out, self.s.llm_model)
            + tts_cost_usd(tr.byte_actual, self.s.tts_model),
            8,
        )
        self.writer.write(rec)

        self.stats.turns += 1
        self.stats.ttfa_ms.append(rec.ttfa_ms)
        self.stats.triggers[seg.trigger] = self.stats.triggers.get(seg.trigger, 0) + 1
        self.stats.skipped = self.translator.skipped
        self.stats.leaked = self.translator.leaked

        self.stats.dropped = self.drift_ctl.drops

        return TurnResult(
            seg=seg,
            text_dst=tr.text,
            ttfa_ms=rec.ttfa_ms,
            audio_duration=audio_duration,
            dropped=dropped,
        )

    async def _synth_or_skip(
        self, tr: Any, speed: float, dropped: bool
    ) -> AsyncIterator[DubbedChunk]:
        if dropped:
            return
        async for chunk in self.synth.synthesize_stream(tr, self.reference_id, speed=speed):
            yield chunk

    async def _output(self, on_turn: Callable[[TurnResult], None] | None) -> None:
        while True:
            seg = await self._segments.get()
            if seg is None:
                return
            try:
                result = await self._process(seg)
            except Exception:
                logger.exception("turn failed for seg %d, continuing", seg.seg_id)
                # Degradacion: un turno roto no tumba la sesion. La version completa,
                # con subtitulo en terminal cuando cae el TTS, es F6.
                continue
            if on_turn is not None:
                on_turn(result)

    # ---- ciclo de vida ----

    async def run(
        self,
        frames: AsyncIterator[Any],
        on_turn: Callable[[TurnResult], None] | None = None,
    ) -> EngineStats:
        capture = asyncio.create_task(self._capture(frames))
        output = asyncio.create_task(self._output(on_turn))
        try:
            await asyncio.gather(capture, output)
        except asyncio.CancelledError:
            raise
        finally:
            for t in (capture, output):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        return self.stats

    async def aclose(self) -> None:
        await self.synth.aclose()
        self.writer.close()

    @property
    def drift(self) -> float:
        return self.drift_ctl.drift

    @property
    def budget_note(self) -> str:
        p = self.s.profile
        return str(p.expansion) if uses_byte_budget(p) else "sin presupuesto (ja)"
