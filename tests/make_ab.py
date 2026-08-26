"""Genera el material del panel A/B de F3.

Produce dos muestras con el MISMO texto: una con el timbre clonado y otra con la voz
neutra de Fish. La unica variable es el timbre. El orden se baraja y la respuesta queda
en un archivo aparte, para que quien administra el panel pueda no verla.

Uso:
  .venv/bin/python tests/make_ab.py --reference-id <id>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from pathlib import Path

import aiohttp
import numpy as np
import soundfile as sf
from dotenv import load_dotenv
from livekit.plugins import fishaudio

SAMPLE_RATE = 44_100

# Las traducciones que produjo la cascada de F1. Usar el texto real del motor y no una
# frase inventada hace que el panel juzgue lo que el sistema realmente emite.
AB_TEXT = [
    "Good morning, everyone, and thanks for joining this morning's meeting.",
    "Today we will review the Q3 report and the Andean region figures.",
    "Sales rose 12% versus the same period last year.",
    "The Lima team closed 3 new contracts and Arequipa 2.",
    "We need the approved budget before March 15, 2027.",
]


async def synth(api_key: str, text: str, voice_id: str | None, model: str) -> np.ndarray:
    session = aiohttp.ClientSession()
    kwargs: dict = {
        "api_key": api_key,
        "model": model,
        "latency_mode": "normal",
        "sample_rate": SAMPLE_RATE,
        "http_session": session,
    }
    if voice_id:
        kwargs["voice_id"] = voice_id
    tts = fishaudio.TTS(**kwargs)
    pieces: list[np.ndarray] = []
    try:
        async for audio in tts.synthesize(text):
            f = audio.frame
            pcm = np.frombuffer(f.data, dtype=np.int16)
            if f.num_channels > 1:
                pcm = pcm.reshape(-1, f.num_channels)[:, 0]
            if pcm.size:
                pieces.append(pcm)
    finally:
        await tts.aclose()
        await session.close()
    return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16)


async def build(api_key: str, voice_id: str, model: str, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    gap = np.zeros(int(0.4 * SAMPLE_RATE), dtype=np.int16)

    variants = {}
    for label, vid in (("clonada", voice_id), ("neutra", None)):
        parts: list[np.ndarray] = []
        for text in AB_TEXT:
            parts.append(await synth(api_key, text, vid, model))
            parts.append(gap)
        variants[label] = np.concatenate(parts)
        print(f"  {label:<8} {variants[label].size / SAMPLE_RATE:5.2f}s")

    labels = ["clonada", "neutra"]
    random.shuffle(labels)
    mapping = {}
    for i, label in enumerate(labels, 1):
        path = out_dir / f"muestra_{i}.wav"
        sf.write(str(path), variants[label], SAMPLE_RATE, format="WAV", subtype="PCM_16")
        mapping[f"muestra_{i}.wav"] = label
        print(f"  -> {path}")

    key_path = out_dir / "RESPUESTA.txt"
    key_path.write_text(
        "Panel A/B de F3 - clave. NO abrir antes de que el panel responda.\n\n"
        + "".join(f"{k} = voz {v}\n" for k, v in sorted(mapping.items()))
        + f"\nreference_id clonado: {voice_id}\n",
        encoding="utf-8",
    )
    (out_dir / "INSTRUCCIONES.txt").write_text(
        "Panel A/B de F3 - clonacion de timbre\n"
        "=====================================\n\n"
        "1. Escucha primero referencia.wav. Esa es la voz del hablante original,\n"
        "   hablando en espanol.\n\n"
        "2. Escucha muestra_1.wav y muestra_2.wav. Las dos dicen el MISMO texto en\n"
        "   ingles. Una usa el timbre clonado del hablante; la otra una voz neutra\n"
        "   por defecto.\n\n"
        "3. Pregunta unica: cual de las dos suena como la persona de referencia.wav?\n\n"
        "4. Anota la respuesta de cada oyente ANTES de abrir RESPUESTA.txt.\n\n"
        "Criterio de F3: 3 de 3 oyentes aciertan.\n",
        encoding="utf-8",
    )
    print(f"  -> {key_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference-id", required=True)
    ap.add_argument("--model", default="s2.1-pro-free")
    ap.add_argument("--out", type=Path, default=Path("out/ab"))
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    api_key = os.getenv("FISH_API_KEY", "")
    if not api_key:
        print("ERROR: FISH_API_KEY vacia", file=sys.stderr)
        return 1
    if args.seed is not None:
        random.seed(args.seed)
    return asyncio.run(build(api_key, args.reference_id, args.model, args.out))


if __name__ == "__main__":
    sys.exit(main())
