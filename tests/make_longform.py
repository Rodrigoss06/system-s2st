"""Genera el WAV de habla continua de 10 min que necesita make drift-test.

El guion se compone por plantillas con relleno variado (numeros, fechas, nombres,
ciudades) para que el contexto rodante de M5 no vea la misma frase una y otra vez, lo
que falsearia tanto la traduccion como el coste en tokens.

Uso: .venv/bin/python tests/make_longform.py --minutes 10 --out tests/fixtures/es_10min.wav
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

SAMPLE_RATE = 16_000
# Habla continua: la pausa es la justa para que Deepgram separe clausulas, muy por
# debajo del RESET_SILENCE de 1.5 s. Si fuera mayor, la deriva se reseteria sola en cada
# frase y el test no mediria nada.
GAP_S = 0.35
CONCURRENCY = 3

CIUDADES = ["Lima", "Arequipa", "Cusco", "Trujillo", "Piura", "Iquitos", "Tacna", "Ica"]
AREAS = ["ventas", "operaciones", "soporte", "logística", "finanzas", "producto"]
MESES = ["enero", "marzo", "abril", "junio", "agosto", "octubre", "noviembre"]
NOMBRES = ["Carla", "Diego", "Mariana", "Joaquín", "Lucía", "Andrés", "Paola", "Rubén"]

PLANTILLAS = [
    "El equipo de {area} en {ciudad} cerró {n} contratos durante el último trimestre.",
    "Las cifras de {area} subieron un {n} por ciento respecto al mismo periodo anterior.",
    "{nombre} presentó el informe de {ciudad} el {d} de {mes} y quedó aprobado.",
    "Necesitamos revisar el presupuesto de {area} antes del {d} de {mes} de dos mil veintisiete.",
    "En {ciudad} atendimos {n} solicitudes nuevas y resolvimos casi todas el mismo día.",
    "{nombre} propuso adelantar la reunión de {area} al {d} de {mes} por la mañana.",
    "El margen de {area} bajó {n} puntos, y eso se explica sobre todo por el tipo de cambio.",
    "Durante {n} semanas seguidas el equipo de {ciudad} superó su objetivo mensual.",
    "Quedamos en que {nombre} coordina con {ciudad} y nos responde antes del viernes.",
    "La proyección de {area} para {mes} depende de que cerremos {n} acuerdos más.",
]


def build_script(target_seconds: float, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    # ~14 caracteres por segundo de habla en espanol, medido sobre el fixture de F1.
    chars_needed = target_seconds * 14
    out: list[str] = []
    total = 0
    while total < chars_needed:
        t = PLANTILLAS[len(out) % len(PLANTILLAS)]
        line = t.format(
            area=rng.choice(AREAS),
            ciudad=rng.choice(CIUDADES),
            nombre=rng.choice(NOMBRES),
            mes=rng.choice(MESES),
            n=rng.randint(2, 40),
            d=rng.randint(1, 28),
        )
        out.append(line)
        total += len(line)
    return out


async def synth_one(
    sem: asyncio.Semaphore, api_key: str, text: str, model: str, voice_id: str | None
) -> np.ndarray:
    async with sem:
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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="s2.1-pro-free")
    ap.add_argument("--voice-id", default=None)
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    api_key = os.getenv("FISH_API_KEY", "")
    if not api_key:
        print("ERROR: FISH_API_KEY vacia", file=sys.stderr)
        return 1

    script = build_script(args.minutes * 60)
    print(f"  {len(script)} clausulas para ~{args.minutes:.0f} min, concurrencia {CONCURRENCY}")

    sem = asyncio.Semaphore(CONCURRENCY)
    # gather conserva el orden del guion; as_completed no, y el audio quedaria barajado.
    clips = await asyncio.gather(
        *(synth_one(sem, api_key, t, args.model, args.voice_id) for t in script)
    )

    gap = np.zeros(int(GAP_S * SAMPLE_RATE), dtype=np.int16)
    pieces: list[np.ndarray] = []
    for clip in clips:
        pieces.append(clip)
        pieces.append(gap)
    pcm = np.concatenate(pieces)

    await asyncio.to_thread(args.out.parent.mkdir, parents=True, exist_ok=True)
    sf.write(str(args.out), pcm, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    dur = pcm.size / SAMPLE_RATE
    print(f"\n  {args.out}  {dur:.1f}s ({dur / 60:.2f} min)  {SAMPLE_RATE} Hz mono")
    print(f"  pausa entre clausulas: {GAP_S}s (RESET_SILENCE es 1.5s, no se disparara)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
