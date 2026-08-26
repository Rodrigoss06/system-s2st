"""make live - F2. Microfono a altavoz por consola.

Imprime el aviso de auriculares ANTES de abrir el microfono, no despues.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

from .adapters.console_io import MicrophoneSource, SpeakerSink, check_devices, warn_headphones
from .adapters.file_io import read_wav_frames_realtime
from .committer import StreamingCommitter
from .config import Settings, load_settings, uses_byte_budget
from .contracts import DubbedChunk, SpeechFrame
from .engine import DubbingEngine, TurnResult
from .gate import DEFAULT_MIN_SILENCE, SileroGate
from .report import percentile
from .synthesizer import FishSynthesizer
from .telemetry import TelemetryWriter
from .transcriber import DeepgramStreamTranscriber
from .translator import GroqTranslator
from .voices import FishVoiceRegistry

logger = logging.getLogger(__name__)

TTS_SAMPLE_RATE = 44_100


async def run_live(
    s: Settings,
    min_silence: float,
    seconds: float | None,
    from_wav: str | None = None,
    neutral_voice: bool = False,
) -> int:
    profile = s.profile

    gate = SileroGate(profile, min_silence_duration=min_silence)
    print("  cargando turn-detector (local, primera vez tarda)...")
    gate.load_eou()

    transcriber = DeepgramStreamTranscriber(s.deepgram_api_key, profile.src, profile.deepgram_code)
    committer = StreamingCommitter(gate, profile.turn_detector_code)
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

    # Voz neutra = sin reference_id: el plugin usa su voz por defecto. Voz clonada =
    # el reference_id de M6. El contraste entre las dos es el demo de F3.
    registry = FishVoiceRegistry(s.fish_api_key)
    reference_id = ""
    if not neutral_voice and s.voice_id:
        registry.remember("speaker-0", s.voice_id)
        reference_id = s.voice_id
    voice_note = "neutra (por defecto de Fish)" if not reference_id else f"clonada {reference_id}"

    speaker: SpeakerSink | None = None
    captured: list[DubbedChunk] = []
    if from_wav is None:
        speaker = SpeakerSink(TTS_SAMPLE_RATE)
        speaker.open()

    async def on_audio(chunk: DubbedChunk) -> None:
        if speaker is not None:
            await speaker.play(chunk.pcm)
        else:
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

    def on_turn(r: TurnResult) -> None:
        tag = " [DROP]" if r.dropped else ""
        print(
            f"\n[{r.seg.trigger:<11}] TTFA {r.ttfa_ms:>5} ms   "
            f"drift {engine.drift:+.2f}s{tag}"
        )
        print(f"   {profile.src}: {r.seg.text}")
        print(f"   {profile.dst}: {r.text_dst or '(vacio)'}")

    mic = MicrophoneSource()
    if from_wav is not None:
        async def paced_wav() -> AsyncIterator[SpeechFrame]:
            """El WAV se detiene mientras la puerta esta cerrada.

            Un hablante real se calla para escuchar el doblaje. Un WAV no, y sin esto
            se pierde todo lo dicho durante la reproduccion: los segmentos salen
            fusionados y el TTFA medido no significa nada.
            """
            async for f in read_wav_frames_realtime(from_wav):
                await gate.wait_unmuted()
                yield f

        frames = paced_wav()
        source_note = f"WAV en tiempo real, pausado en mute: {from_wav}"
    else:
        frames = mic.frames()
        source_note = "microfono"
    budget = profile.expansion if uses_byte_budget(profile) else "sin presupuesto (ja)"
    print()
    print(f"  pair       : {profile.src} -> {profile.dst}")
    print(f"  llm        : {s.llm_model}  reasoning_effort={s.llm_reasoning_effort}")
    print(f"  tts        : {s.tts_model}")
    print(f"  voz        : {voice_note}")
    print(f"  expansion  : {budget}")
    print(f"  vad        : min_silence_duration={min_silence}s")
    print(f"  eou        : threshold={gate.eou_threshold} ({profile.turn_detector_code})")
    print(f"  telemetry  : {writer.path}")
    print(f"  fuente     : {source_note}")
    print()
    print("  Habla. Ctrl-C para terminar.")
    print()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    t0 = time.monotonic()
    runner = asyncio.create_task(engine.run(frames, on_turn=on_turn))
    waiter = asyncio.create_task(stop.wait())
    timeout = seconds if seconds else None
    _done, pending = await asyncio.wait(
        {runner, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    # Un fallo del pipeline no puede terminar como sesion correcta con 0 turnos.
    failure: BaseException | None = None
    if runner in _done and not runner.cancelled():
        failure = runner.exception()

    elapsed = time.monotonic() - t0
    await engine.aclose()
    if speaker is not None:
        speaker.close()
    elif captured:
        from .adapters.file_io import write_dubbed_wav

        out_path, dur, _ = write_dubbed_wav(
            Path("out") / f"live-{time.strftime('%Y%m%dT%H%M%S')}.wav", captured
        )
        print(f"\n  dubbed wav : {out_path}  {dur:.2f}s")

    st = engine.stats
    print("\n" + "=" * 72)
    print(f"  sesion     : {elapsed:.1f}s   turnos: {st.turns}")
    if st.ttfa_ms:
        v = [float(x) for x in st.ttfa_ms]
        p90 = percentile(v, 0.90)
        print(f"  TTFA       : P50 {percentile(v, 0.50):.0f} ms   "
              f"P90 {p90:.0f} ms   P99 {percentile(v, 0.99):.0f} ms")
        print(f"  criterio   : P90 < 2000 ms -> {'CUMPLE' if p90 < 2000 else 'NO CUMPLE'}")
    print(f"  triggers   : {st.triggers}")
    print(f"  deriva     : final {engine.drift:+.2f}s   max {engine.drift_ctl.max_abs_drift:.2f}s"
          f"   resets {engine.drift_ctl.resets}   drops {engine.drift_ctl.drops}")
    print(f"  no-cut     : {committer.no_cut_holds} cortes evitados por las guardas")
    print(f"  parciales  : {committer.dropped_partials} descartados (compuerta is_final)")
    print(f"  gate       : {gate.stats}")
    print(f"  stt        : {transcriber.partials} parciales, {transcriber.finals} finales")
    print(f"  telemetry  : {writer.path}")
    print("=" * 72)

    if failure is not None:
        print(f"\nERROR: el pipeline fallo: {type(failure).__name__}: {failure}", file=sys.stderr)
        logger.error("pipeline failed", exc_info=failure)
        return 1
    if st.turns == 0:
        print("\nERROR: la sesion no produjo ningun turno.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="live", description="F2 microfono a altavoz")
    ap.add_argument("--min-silence", type=float, default=DEFAULT_MIN_SILENCE)
    ap.add_argument("--seconds", type=float, default=None, help="corta solo tras N segundos")
    ap.add_argument("--yes", action="store_true", help="salta la confirmacion de auriculares")
    ap.add_argument(
        "--neutral-voice",
        action="store_true",
        help="usa la voz por defecto de Fish en vez del timbre clonado. Es el lado B "
        "del contraste A/B de F3",
    )
    ap.add_argument("--devices", action="store_true", help="lista dispositivos y sale")
    ap.add_argument(
        "--from-wav",
        default=None,
        help="alimenta el pipeline con un WAV a velocidad real en vez del microfono. "
        "Sirve para medir de forma repetible; NO es el criterio de aceptacion de F2",
    )
    args = ap.parse_args()

    s = load_settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.devices:
        from .adapters.console_io import list_devices

        print(list_devices())
        return 0

    if args.from_wav is None:
        # El aviso va antes de tocar el microfono.
        if not warn_headphones(interactive=not args.yes):
            return 1
        ok, info = check_devices()
        if not ok:
            return 1
        print(f"  devices    : {info}")

    try:
        return asyncio.run(
            run_live(s, args.min_silence, args.seconds, args.from_wav, args.neutral_voice)
        )
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        logger.exception("live session failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
