"""make soak - F6. Sesion larga sin intervencion, con corte de red simulado.

El criterio de F6 pide 20 minutos sin tocar nada y recuperacion automatica de un corte de
5 s. Para poder ejecutarlo sin una persona hablando 20 minutos seguidos, la fuente es un
WAV que se repite en bucle a velocidad de reloj. El resto del pipeline es identico al de
`make live`: mismo VAD, mismo WebSocket, mismos triggers, mismo TTS.

El corte no toca la red del sistema: cierra el WebSocket de Deepgram y bloquea la
reconexion durante N segundos, que es exactamente lo que el motor ve cuando la red cae
(riesgo R5). Hacerlo asi lo vuelve reproducible y no necesita permisos de root.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .adapters.file_io import read_wav_frames_realtime, write_dubbed_wav
from .committer import StreamingCommitter
from .config import Settings, load_settings, uses_byte_budget
from .contracts import DubbedChunk, LanguageProfile, Segment, SpeechFrame, Translation
from .engine import DubbingEngine, TurnResult
from .fakes import FakeTranslator
from .gate import DEFAULT_MIN_SILENCE, SileroGate
from .report import percentile
from .synthesizer import FishSynthesizer
from .telemetry import TelemetryWriter
from .transcriber import DeepgramStreamTranscriber
from .translator import GroqTranslator
from .voices import FishVoiceRegistry

logger = logging.getLogger(__name__)

TTS_SAMPLE_RATE = 44_100


class _OfflineTranslator(FakeTranslator):
    """FakeTranslator con la interfaz que el motor espera de M5.

    Existe solo para `--offline-llm`: permite correr la sesion larga cuando la cuota
    diaria de Groq esta agotada (D30), sin tocar el resto del pipeline.
    """

    def __init__(self, profile: LanguageProfile) -> None:
        super().__init__(dst_lang=profile.dst)
        self.profile = profile
        self.t_first_token = 0.0
        self.t_done = 0.0
        # El motor lee estos contadores en cada turno; FakeTranslator no los tiene.
        self.leaked = 0
        self.skipped = 0

    def budget_for(self, seg: Segment) -> int:
        if not uses_byte_budget(self.profile):
            return 0
        return int(len(seg.text.encode("utf-8")) * self.profile.expansion)

    async def translate(self, seg: Segment, budget: int) -> Translation:
        self.t_first_token = round(time.monotonic(), 3)
        tr = await super().translate(seg, budget)
        self.t_done = round(time.monotonic(), 3)
        return tr


async def looping_wav(
    path: str, gate: SileroGate, stop: asyncio.Event
) -> AsyncIterator[SpeechFrame]:
    """Repite el WAV hasta que se pida parar, pausando mientras la puerta esta cerrada.

    La pausa simula al hablante que se calla para escuchar el doblaje. Sin ella se pierde
    todo lo dicho durante la reproduccion y los segmentos salen fusionados.
    """
    loops = 0
    offset = 0.0
    last = 0.0
    while not stop.is_set():
        async for f in read_wav_frames_realtime(path):
            if stop.is_set():
                return
            await gate.wait_unmuted()
            last = f.t_capture
            # t_capture debe crecer de forma monotona entre vueltas o el mapeo de
            # tiempos del transcriptor retrocede y el TTFA sale negativo.
            yield SpeechFrame(
                pcm=f.pcm, sample_rate=f.sample_rate, t_capture=offset + f.t_capture
            )
        loops += 1
        offset += last + 0.5
        logger.info("soak: vuelta %d del WAV completada", loops)


async def run_soak(
    s: Settings,
    minutes: float,
    wav: str,
    cut_at_min: float,
    cut_s: float,
    min_silence: float,
    offline_llm: bool = False,
) -> int:
    profile = s.profile
    gate = SileroGate(profile, min_silence_duration=min_silence)
    print("  cargando turn-detector...")
    gate.load_eou()

    transcriber = DeepgramStreamTranscriber(s.deepgram_api_key, profile.src, profile.deepgram_code)
    committer = StreamingCommitter(gate, profile.turn_detector_code)
    translator: Any
    if offline_llm:
        # Prueba de resistencia sin gastar cuota de Groq. Mide estabilidad, reconexion y
        # anti-eco durante toda la sesion; NO mide calidad de traduccion, que se verifica
        # aparte con el traductor real.
        translator = _OfflineTranslator(profile)
    else:
        translator = GroqTranslator(
            s.groq_api_key,
            s.llm_model,
            profile,
            reasoning_effort=s.llm_reasoning_effort,
            temperature=s.llm_temperature,
            max_tokens=s.llm_max_tokens,
        )
    synth = FishSynthesizer(s.fish_api_key, model=s.tts_model, sample_rate=TTS_SAMPLE_RATE)
    writer = TelemetryWriter()

    registry = FishVoiceRegistry(s.fish_api_key)
    reference_id = ""
    if s.voice_id:
        registry.remember("speaker-0", s.voice_id)
        reference_id = s.voice_id

    captured: list[DubbedChunk] = []

    async def on_audio(chunk: DubbedChunk) -> None:
        captured.append(chunk)

    engine = DubbingEngine(
        s,
        gate,
        transcriber,
        committer,
        translator,
        synth,
        writer,
        on_audio=on_audio,
        reference_id=reference_id,
    )

    t0 = time.monotonic()
    events: list[str] = []

    def on_turn(r: TurnResult) -> None:
        tag = ""
        if r.dropped:
            tag = " [DROP]"
        elif r.tts_failed:
            tag = " [SUBTITULO]"
        el = time.monotonic() - t0
        print(
            f"  [{el / 60:5.1f}m] {r.seg.trigger:<11} TTFA {r.ttfa_ms:>5}ms{tag}"
            f"  {r.text_dst[:60]}"
        )

    print()
    print(f"  duracion   : {minutes:.0f} min")
    print(f"  fuente     : {wav} en bucle")
    print(f"  corte de red: a los {cut_at_min:.0f} min, {cut_s:.0f} s")
    print(f"  traductor  : {'FALSO (--offline-llm)' if offline_llm else s.llm_model}")
    print(f"  telemetry  : {writer.path}")
    print()

    stop = asyncio.Event()

    async def cutter() -> None:
        await asyncio.sleep(cut_at_min * 60)
        el = time.monotonic() - t0
        print(f"\n  >>> [{el / 60:5.1f}m] CORTE DE RED SIMULADO, {cut_s:.0f}s <<<\n")
        events.append(f"corte a los {el / 60:.1f} min")
        transcriber.simulate_network_drop(cut_s)

    runner = asyncio.create_task(engine.run(looping_wav(wav, gate, stop), on_turn=on_turn))
    cut = asyncio.create_task(cutter())
    done, pending = await asyncio.wait({runner}, timeout=minutes * 60)

    stop.set()
    for t in (cut, *pending):
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    failure: BaseException | None = None
    if runner in done and not runner.cancelled():
        failure = runner.exception()

    elapsed = time.monotonic() - t0
    await engine.aclose()
    if captured:
        path, dur, _ = write_dubbed_wav(
            Path("out") / f"soak-{time.strftime('%Y%m%dT%H%M%S')}.wav", captured
        )
        print(f"\n  dubbed wav : {path}  {dur:.1f}s")

    st = engine.stats
    ttfa = [float(x) for x in st.ttfa_ms]
    print("\n" + "=" * 74)
    print("  SOAK - resultado")
    print("=" * 74)
    print(f"  duracion real   : {elapsed / 60:.1f} min")
    print(f"  turnos          : {st.turns}")
    if ttfa:
        print(f"  TTFA            : P50 {percentile(ttfa, 0.50):.0f} ms   "
              f"P90 {percentile(ttfa, 0.90):.0f} ms   P99 {percentile(ttfa, 0.99):.0f} ms")
    print(f"  triggers        : {st.triggers}")
    print(f"  deriva          : final {engine.drift:+.2f}s   max atraso "
          f"{engine.drift_ctl.max_positive:+.2f}s")
    print(f"  reconexiones    : {transcriber.reconnects}   "
          f"sin socket {transcriber.downtime_s:.1f}s   "
          f"frames reenviados {transcriber.frames_replayed}")
    print(f"  buffer          : {transcriber.frames_dropped_overflow} frames descartados "
          f"por desbordamiento")
    print(f"  TTS degradado   : {st.tts_failures} turnos a subtitulo")
    print(f"  gate            : {gate.stats}")
    print(f"  telemetry       : {writer.path}")
    print()

    # Criterio de F6, comprobado punto por punto.
    ok_duracion = elapsed >= minutes * 60 * 0.95
    ok_sin_caida = failure is None
    ok_recuperado = transcriber.reconnects >= 1 and st.turns > 0
    hubo_turnos_tras_corte = st.turns > 0

    print("  CRITERIO F6")
    print(f"    sesion completa sin intervencion : {'SI' if ok_duracion else 'NO'}")
    print(f"    la sesion no se cayo             : {'SI' if ok_sin_caida else 'NO'}")
    print(f"    corte de red recuperado solo     : "
          f"{'SI' if ok_recuperado else 'NO'} ({transcriber.reconnects} reconexiones)")
    print(f"    turnos despues del corte         : {'SI' if hubo_turnos_tras_corte else 'NO'}")
    if failure is not None:
        print(f"\n  ERROR: {type(failure).__name__}: {failure}", file=sys.stderr)
    print("=" * 74)

    return 0 if (ok_duracion and ok_sin_caida and ok_recuperado) else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="soak", description="F6 sesion larga con corte de red")
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--wav", default="tests/fixtures/es_30s.wav")
    ap.add_argument("--cut-at", type=float, default=10.0, help="minuto del corte")
    ap.add_argument("--cut-seconds", type=float, default=5.0)
    ap.add_argument("--min-silence", type=float, default=DEFAULT_MIN_SILENCE)
    ap.add_argument(
        "--offline-llm",
        action="store_true",
        help="usa el traductor falso determinista. Prueba resistencia sin gastar "
        "cuota de Groq; NO mide calidad de traduccion",
    )
    args = ap.parse_args()

    s = load_settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not Path(args.wav).is_file():
        print(f"ERROR: no existe {args.wav}", file=sys.stderr)
        return 1
    try:
        return asyncio.run(
            run_soak(
                s,
                args.minutes,
                args.wav,
                args.cut_at,
                args.cut_seconds,
                args.min_silence,
                args.offline_llm,
            )
        )
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
