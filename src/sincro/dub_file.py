"""make dub-file - F1. Cascada offline sobre archivo: WAV entra, WAV doblado sale.

Sin microfono, sin VAD, sin streaming. Con un WAV fijo cada corrida es identica: si la
traduccion cambia, la culpa es del LLM y no del driver de audio. El orquestador asincrono
completo (engine.py) es F2.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .adapters.file_io import (
    AudioFormatError,
    probe_wav,
    read_wav_frames,
    write_dubbed_wav,
)
from .committer import PunctuationCommitter
from .config import Settings, load_settings, uses_byte_budget
from .contracts import DubbedChunk
from .drift import DriftController, render_curve
from .synthesizer import FishSynthesizer
from .telemetry import SegmentRecord, TelemetryWriter, llm_cost_usd, tts_cost_usd
from .transcriber import DeepgramTranscriber
from .translator import GroqTranslator

logger = logging.getLogger(__name__)


async def _maybe_synth(
    synth: FishSynthesizer, tr: Any, speed: float, dropped: bool
) -> AsyncIterator[DubbedChunk]:
    """Un segmento descartado no se sintetiza: gastar TTS en audio que no suena es
    coste puro, y el objetivo del descarte es justamente recuperar tiempo."""
    if dropped:
        return
    async for chunk in synth.synthesize(tr, reference_id="", speed=speed):
        yield chunk


async def dub_file(
    in_path: Path, out_path: Path, s: Settings, show_curve: bool = False
) -> int:
    profile = s.profile
    sample_rate, channels, duration = probe_wav(in_path)
    print(f"input      : {in_path}  {duration:.2f}s  {sample_rate} Hz  {channels}ch")
    print(f"pair       : {profile.src} -> {profile.dst}")
    print(
        f"llm        : {s.llm_model}  reasoning_effort={s.llm_reasoning_effort}"
        f"  temp={s.llm_temperature}  max_tokens={s.llm_max_tokens}"
    )
    print(f"tts        : {s.tts_model}  (voz por defecto; la clonacion es F3)")
    budget_note = profile.expansion if uses_byte_budget(profile) else "sin presupuesto (ja)"
    print(f"expansion  : {budget_note}")
    print()

    transcriber = DeepgramTranscriber(s.deepgram_api_key, profile.src, profile.deepgram_code)
    committer = PunctuationCommitter()
    translator = GroqTranslator(
        s.groq_api_key,
        s.llm_model,
        profile,
        reasoning_effort=s.llm_reasoning_effort,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
    )
    synth = FishSynthesizer(s.fish_api_key, model=s.tts_model)

    writer = TelemetryWriter()
    chunks: list[DubbedChunk] = []
    drift_ctl = DriftController()
    n_seg = 0

    t_read_done = time.monotonic()
    events = transcriber.stream(read_wav_frames(in_path))

    # En offline el archivo se transcribe de una vez, asi que t_speech_end es comun a
    # todos los segmentos: el instante en que el audio dejo de entrar. El ttfa_ms que
    # sale de aqui NO es comparable con el objetivo de F2, que mide por turno en vivo.
    try:
        async for seg in committer.commit(events):
            n_seg += 1
            t_stt = time.monotonic()
            rec = SegmentRecord(
                seg_id=seg.seg_id,
                lang_src=profile.src,
                lang_dst=profile.dst,
                trigger=seg.trigger,
            )
            rec.mark("t_speech_end", t_read_done)
            rec.mark("t_stt_final", t_stt)

            budget = translator.budget_for(seg)
            tr = await translator.translate(seg, budget)
            rec.mark("t_llm_first_token", translator.t_first_token)
            rec.mark("t_llm_done", translator.t_done)

            # Una pausa larga borra la deuda antes de decidir nada.
            drift_ctl.note_gap(seg)
            dropped = drift_ctl.should_drop(seg)
            # El ratio que espera speed_for es de DURACION, no de bytes: el documento
            # define el mecanismo dos como comparar la duracion generada con la del
            # segmento fuente. Pasarle un ratio de bytes empujaba speed a 1.10 cuando
            # el ingles ya salia corto, y agravaba la deriva negativa.
            speed = drift_ctl.speed_for(seg, drift_ctl.duration_ratio())

            audio_duration = 0.0
            first = True
            if dropped:
                logger.info(
                    "seg %d dropped: drift %.2fs > %.2fs, trigger=%s, %d chars",
                    seg.seg_id,
                    drift_ctl.drift,
                    drift_ctl.THRESHOLD_HARD,
                    seg.trigger,
                    len(seg.text),
                )
            async for chunk in _maybe_synth(synth, tr, speed, dropped):
                if first:
                    rec.mark("t_tts_first_byte", synth.t_first_byte)
                    first = False
                audio_duration += chunk.audio_duration
                chunks.append(chunk)
            if first:
                # Segmento vaciado por [[SKIP]]: no hubo audio. Se marca igual para que la
                # linea JSONL no quede incompleta y make report pueda contarlo.
                now = time.monotonic()
                rec.mark("t_tts_first_byte", now)
            rec.mark("t_audio_out")

            if not dropped:
                drift_ctl.observe(seg, audio_duration, speed)
            drift_ctl.update(seg, audio_duration, speed, dropped)
            rec.source_duration_s = round(seg.source_duration, 3)
            rec.audio_duration_s = round(audio_duration, 3)
            rec.speed_applied = round(speed, 3)
            rec.drift_s = round(drift_ctl.drift, 3)
            rec.bytes_in = len(seg.text.encode("utf-8"))
            rec.bytes_out = tr.byte_actual
            rec.byte_budget = tr.byte_budget
            rec.tokens_in = translator.tokens_in
            rec.tokens_out = translator.tokens_out
            rec.cost_usd = round(
                llm_cost_usd(translator.tokens_in, translator.tokens_out, s.llm_model)
                + tts_cost_usd(tr.byte_actual, s.tts_model),
                8,
            )
            writer.write(rec)

            flag = "  [DROP]" if dropped else ("  [SKIP]" if not tr.text else "")
            print(
                f"seg {seg.seg_id:>2} [{seg.trigger:<11}] "
                f"{seg.t_start:>6.2f}-{seg.t_end:>6.2f}s{flag}"
            )
            print(f"   src: {seg.text}")
            print(f"   dst: {tr.text or '(vacio)'}")
            print(
                f"   bytes {rec.bytes_in} -> {rec.bytes_out} (budget {rec.byte_budget})"
                f"  speed {rec.speed_applied:.3f}"
                f"  audio {rec.audio_duration_s:.2f}s vs {rec.source_duration_s:.2f}s"
                f"  drift {rec.drift_s:+.2f}s"
            )
            print()


    finally:
        await synth.aclose()
    writer.close()

    if n_seg == 0:
        print("ERROR: no segments were produced. Nothing to write.", file=sys.stderr)
        return 1

    path, out_duration, samples = write_dubbed_wav(out_path, chunks)
    print(f"segments   : {n_seg}")
    print(f"skipped    : {translator.skipped}")
    print(f"leaked     : {translator.leaked} segments with control markers")
    print(f"partials   : {committer.dropped_partials} discarded (is_final gate)")
    print(f"dubbed wav : {path}  {out_duration:.2f}s  {samples} samples")
    print(f"telemetry  : {writer.path}")
    print(
        f"drift      : final {drift_ctl.drift:+.2f}s   |max| {drift_ctl.max_abs_drift:.2f}s"
        f"   max atraso {drift_ctl.max_positive:+.2f}s"
        f"   resets {drift_ctl.resets}   drops {drift_ctl.drops}"
    )
    print(f"ratio dur. : {drift_ctl.duration_ratio():.3f} (1.000 = isocrono)")
    if show_curve:
        print()
        print("  curva de deriva (s)")
        print(render_curve(drift_ctl.curve))
    print(
        f"source     : {duration:.2f}s   dubbed: {out_duration:.2f}s"
        f"   delta: {out_duration - duration:+.2f}s"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="dub-file", description="F1 offline cascade")
    ap.add_argument("--in", dest="in_path", required=True, type=Path)
    ap.add_argument("--out", dest="out_path", type=Path, default=None)
    ap.add_argument("--curve", action="store_true", help="grafica la curva de deriva")
    args = ap.parse_args()

    s = load_settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )

    out = args.out_path or Path("out") / f"dubbed-{time.strftime('%Y%m%dT%H%M%S')}.wav"
    try:
        return asyncio.run(dub_file(args.in_path, out, s, show_curve=args.curve))
    except AudioFormatError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
