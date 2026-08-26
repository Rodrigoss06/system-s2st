"""Genera los WAV de referencia de tests/fixtures/ con Fish TTS.

No es un modulo del motor: es utilidad de pruebas. El fixture se genera en vez de
grabarse para que cada corrida de F1 sea identica, que es la razon por la que F1 usa
archivo y no microfono. Un fixture sintetico NO valida STT sobre voz real: eso lo cubre
F2 con microfono.

Uso: .venv/bin/python tests/make_fixture.py --lang es --out tests/fixtures/es_30s.wav
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import aiohttp
import numpy as np
import soundfile as sf
from dotenv import load_dotenv
from livekit.plugins import fishaudio

# Seis clausulas. Incluyen numeros, fechas y nombres propios porque F5 los usa como
# criterio de preservacion y conviene que el fixture sea el mismo.
SCRIPTS: dict[str, list[str]] = {
    "es": [
        "Buenos días a todos y gracias por conectarse a la reunión de esta mañana.",
        "Hoy vamos a revisar el informe del tercer trimestre y los números de la región andina.",
        "Las ventas subieron un doce por ciento respecto al mismo periodo del año pasado.",
        "El equipo de Lima cerró tres contratos nuevos y el de Arequipa cerró dos.",
        "Necesitamos el presupuesto aprobado antes del quince de marzo de dos mil veintisiete.",
        "Si no hay preguntas cerramos aquí y seguimos por correo electrónico.",
    ],
}

# Clip de enrolamiento. El documento tecnico pide 15 a 20 s del hablante en SU idioma
# nativo: Fish transfiere el timbre al idioma destino. Texto variado en fonemas para que
# el modelo tenga material suficiente.
REFERENCE_SCRIPTS: dict[str, list[str]] = {
    "es": [
        "Hola, me llamo Rodrigo y estoy grabando este clip de referencia para el motor "
        "de doblaje.",
        "Trabajo desde Arequipa, en el sur del Perú, y hoy es veintiséis de agosto.",
        "Quiero que mi voz suene igual cuando el sistema la traduzca al inglés, al "
        "portugués o al japonés.",
        "Uno, dos, tres, cuatro, cinco. Que la fuerza te acompañe.",
    ],
}

TARGET_SAMPLE_RATE = 16_000
GAP_S = 0.6
# Un clip de enrolamiento continuo funciona mejor que uno troceado con silencios largos.
REFERENCE_GAP_S = 0.25


async def synth_clause(
    api_key: str, text: str, model: str, voice_id: str | None = None
) -> tuple[np.ndarray, int]:
    session = aiohttp.ClientSession()
    kwargs: dict = {
        "api_key": api_key,
        "model": model,
        "latency_mode": "normal",
        "sample_rate": TARGET_SAMPLE_RATE,
        "http_session": session,
    }
    if voice_id:
        kwargs["voice_id"] = voice_id
    tts = fishaudio.TTS(**kwargs)
    pieces: list[np.ndarray] = []
    rate = TARGET_SAMPLE_RATE
    try:
        async for audio in tts.synthesize(text):
            f = audio.frame
            pcm = np.frombuffer(f.data, dtype=np.int16)
            if f.num_channels > 1:
                pcm = pcm.reshape(-1, f.num_channels)[:, 0]
            if pcm.size:
                pieces.append(pcm)
                rate = f.sample_rate
    finally:
        await tts.aclose()
        await session.close()
    return (np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16)), rate


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="es")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="s2.1-pro-free")
    ap.add_argument(
        "--kind",
        choices=("fixture", "reference", "matrix"),
        default="fixture",
        help="fixture = WAV de 30 s (F1); reference = clip de enrolamiento (F3); "
        "matrix = guion de 10 frases por idioma (F5)",
    )
    ap.add_argument(
        "--voice-id",
        default=None,
        help="voz de Fish con la que generar. Para el clip de referencia conviene una "
        "distinta de la neutra, o el contraste A/B de F3 no se oye",
    )
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    api_key = os.getenv("FISH_API_KEY", "")
    if not api_key:
        print("ERROR: FISH_API_KEY vacia", file=sys.stderr)
        return 1
    if args.kind == "reference":
        scripts = REFERENCE_SCRIPTS
    elif args.kind == "matrix":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from matrix_scripts import MATRIX_SCRIPTS, SOURCE_VOICES

        scripts = MATRIX_SCRIPTS
        if args.voice_id is None:
            args.voice_id = SOURCE_VOICES.get(args.lang)
    else:
        scripts = SCRIPTS
    gap = REFERENCE_GAP_S if args.kind == "reference" else GAP_S
    if args.lang not in scripts:
        print(f"ERROR: sin guion {args.kind} para {args.lang}", file=sys.stderr)
        return 1

    out_pieces: list[np.ndarray] = []
    rate = TARGET_SAMPLE_RATE
    for i, text in enumerate(scripts[args.lang], 1):
        pcm, rate = await synth_clause(api_key, text, args.model, args.voice_id)
        print(f"  {i}. {pcm.size / rate:5.2f}s  {text[:60]}")
        out_pieces.append(pcm)
        # Pausa entre clausulas: da a Deepgram una frontera clara y evita que
        # smart_format pegue dos frases en una.
        out_pieces.append(np.zeros(int(gap * rate), dtype=np.int16))

    pcm = np.concatenate(out_pieces)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(args.out), pcm, rate, format="WAV", subtype="PCM_16")
    print(f"\n{args.out}  {pcm.size / rate:.2f}s  {rate} Hz mono")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
