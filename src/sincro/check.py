"""make check - F0. Valida las tres credenciales y emite un JSONL de prueba con fakes.

Dos comprobaciones independientes:
  1. Una llamada minima de solo lectura a cada proveedor. Distingue credencial
     ausente, credencial rechazada y proveedor inalcanzable.
  2. La cascada completa de fakes, que debe producir una linea JSONL por segmento
     con las seis marcas de etapa del esquema.

Sale 0 solo si ambas pasan. Sin salida ejecutada no hay criterio cumplido.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Final

import httpx

from .config import Settings, load_settings, uses_byte_budget
from .fakes import (
    FakeAudioGate,
    FakeSegmentCommitter,
    FakeSynthesizer,
    FakeTranscriber,
    FakeTranslator,
    FakeVoiceRegistry,
    fake_frames,
)
from .telemetry import (
    STAGE_MARKS,
    SegmentRecord,
    TelemetryWriter,
    llm_cost_usd,
    tts_cost_usd,
)

TIMEOUT_S: Final[float] = 15.0
OK: Final[str] = "PASS"
BAD: Final[str] = "FAIL"


@dataclass
class ProbeResult:
    provider: str
    ok: bool
    detail: str


async def _probe(
    client: httpx.AsyncClient, provider: str, url: str, headers: dict[str, str], key: str
) -> ProbeResult:
    if not key:
        return ProbeResult(provider, False, "missing key in environment")
    try:
        r = await client.get(url, headers=headers, timeout=TIMEOUT_S)
    except httpx.HTTPError as e:
        return ProbeResult(provider, False, f"unreachable: {type(e).__name__}: {e}")
    if r.status_code in (401, 403):
        return ProbeResult(provider, False, f"credential rejected (HTTP {r.status_code})")
    if r.status_code >= 400:
        return ProbeResult(provider, False, f"unexpected HTTP {r.status_code}: {r.text[:120]}")
    return ProbeResult(provider, True, f"HTTP {r.status_code}")


async def probe_credentials(s: Settings) -> list[ProbeResult]:
    async with httpx.AsyncClient() as client:
        return list(
            await asyncio.gather(
                _probe(
                    client,
                    "deepgram",
                    "https://api.deepgram.com/v1/projects",
                    {"Authorization": f"Token {s.deepgram_api_key}"},
                    s.deepgram_api_key,
                ),
                _probe(
                    client,
                    "groq",
                    "https://api.groq.com/openai/v1/models",
                    {"Authorization": f"Bearer {s.groq_api_key}"},
                    s.groq_api_key,
                ),
                # /model de Fish responde 200 sin credencial valida; /wallet si autentica.
                _probe(
                    client,
                    "fish",
                    "https://api.fish.audio/wallet/self/api-credit",
                    {"Authorization": f"Bearer {s.fish_api_key}"},
                    s.fish_api_key,
                ),
            )
        )


async def run_fake_pipeline(s: Settings) -> tuple[TelemetryWriter, list[SegmentRecord]]:
    """Cascada completa con fakes: gate -> stt -> commit -> translate -> tts.

    Marca las seis etapas con reloj monotonico real. Los tiempos son pequenos porque
    los fakes no tocan la red; lo que se verifica aqui es el camino, no la latencia.
    """
    profile = s.profile
    gate = FakeAudioGate()
    transcriber = FakeTranscriber(lang=profile.src)
    committer = FakeSegmentCommitter()
    translator = FakeTranslator(dst_lang=profile.dst)
    voices = FakeVoiceRegistry()
    synth = FakeSynthesizer()

    reference_id = await voices.enroll("speaker-0", s.voice_ref)

    writer = TelemetryWriter()
    records: list[SegmentRecord] = []
    drift = 0.0

    gated = gate.process(fake_frames(seconds=0.2))
    events = transcriber.stream(gated)

    async for seg in committer.commit(events):
        rec = SegmentRecord(
            seg_id=seg.seg_id,
            lang_src=profile.src,
            lang_dst=profile.dst,
            trigger=seg.trigger,
        )
        rec.mark("t_speech_end")
        rec.mark("t_stt_final")

        bytes_in = len(seg.text.encode("utf-8"))
        budget = int(bytes_in * profile.expansion) if uses_byte_budget(profile) else 0

        rec.mark("t_llm_first_token")
        tr = await translator.translate(seg, budget)
        rec.mark("t_llm_done")

        # Anti-eco: la puerta se cierra mientras el sintetizador reproduce.
        gate.mute()
        audio_duration = 0.0
        first_byte = False
        async for chunk in synth.synthesize(tr, reference_id, speed=1.0):
            if not first_byte:
                rec.mark("t_tts_first_byte")
                first_byte = True
            audio_duration += chunk.audio_duration
        rec.mark("t_audio_out")
        gate.unmute()

        drift += audio_duration - seg.source_duration

        rec.source_duration_s = round(seg.source_duration, 3)
        rec.audio_duration_s = round(audio_duration, 3)
        rec.speed_applied = 1.0
        rec.drift_s = round(drift, 3)
        rec.bytes_in = bytes_in
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
        records.append(rec)

    writer.close()

    if gate.mute_calls != len(records) or gate.unmute_calls != len(records):
        raise AssertionError(
            f"anti-echo gate unbalanced: {gate.mute_calls} mute / {gate.unmute_calls} unmute "
            f"for {len(records)} segments"
        )
    return writer, records


async def main() -> int:
    s = load_settings()
    print("SINCRO Engine v3 - make check (F0)")
    print(f"  pair       : {s.src_lang} -> {s.dst_lang}")
    print(f"  tts model  : {s.tts_model}")
    print(f"  expansion  : {s.profile.expansion}"
          f"{'' if uses_byte_budget(s.profile) else '  (byte budget disabled: ja pair)'}")
    print()

    print("[1/2] credentials")
    t0 = time.monotonic()
    probes = await probe_credentials(s)
    for p in probes:
        print(f"  {OK if p.ok else BAD}  {p.provider:<9} {p.detail}")
    creds_ok = all(p.ok for p in probes)
    print(f"  ({time.monotonic() - t0:.2f}s)")
    print()

    print("[2/2] fake pipeline telemetry")
    try:
        writer, records = await run_fake_pipeline(s)
    except Exception as e:  # el check reporta el fallo, no lo propaga
        print(f"  {BAD}  pipeline raised {type(e).__name__}: {e}")
        return 1

    incomplete = [r for r in records if r.missing_marks]
    if not records:
        print(f"  {BAD}  no segments produced")
        return 1
    if incomplete:
        for r in incomplete:
            print(f"  {BAD}  seg {r.seg_id} missing marks: {', '.join(r.missing_marks)}")
        return 1

    print(f"  {OK}  {len(records)} segments, {len(STAGE_MARKS)}/6 stage marks each")
    print(f"  {OK}  jsonl -> {writer.path}")
    total_cost = sum(r.cost_usd for r in records)
    triggers = sorted({r.trigger for r in records})
    print(f"        triggers={','.join(triggers)}  cost_usd={total_cost:.8f}")
    print()

    if not creds_ok:
        print("RESULT: FAIL - telemetry path is green, credentials are not.")
        print("        Fill .env from .env.example; it lists where each key comes from.")
        return 1

    print("RESULT: PASS - 3/3 credentials valid, telemetry JSONL emitted.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
