"""Adaptador de archivo, F1. Unico lugar de M1-M8 que toca el sistema de archivos de audio.

El motor no conoce este modulo: recibe SpeechFrame y devuelve DubbedChunk. En F2 se
sustituye por console_io.py sin tocar ningun modulo.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Final

import numpy as np
import soundfile as sf

from ..contracts import DubbedChunk, SpeechFrame

EXPECTED_SAMPLE_RATE: Final[int] = 16_000
FRAME_MS: Final[int] = 20


class AudioFormatError(ValueError):
    """El WAV de entrada no cumple el formato que el motor espera."""


def probe_wav(path: str | Path) -> tuple[int, int, float]:
    info = sf.info(str(path))
    return info.samplerate, info.channels, info.duration


def read_wav_pcm(path: str | Path) -> tuple[np.ndarray, int]:
    """Lee el WAV completo como int16 mono. Valida formato antes de gastar creditos."""
    p = Path(path)
    if not p.is_file():
        raise AudioFormatError(f"input file not found: {p}")
    pcm, sample_rate = sf.read(str(p), dtype="int16", always_2d=True)
    channels = pcm.shape[1]
    if channels != 1:
        raise AudioFormatError(
            f"{p}: expected mono, got {channels} channels. "
            f"Convert with: ffmpeg -i {p} -ac 1 -ar {EXPECTED_SAMPLE_RATE} out.wav"
        )
    if sample_rate != EXPECTED_SAMPLE_RATE:
        raise AudioFormatError(
            f"{p}: expected {EXPECTED_SAMPLE_RATE} Hz, got {sample_rate} Hz. "
            f"Convert with: ffmpeg -i {p} -ac 1 -ar {EXPECTED_SAMPLE_RATE} out.wav"
        )
    return pcm[:, 0], sample_rate


async def read_wav_frames(
    path: str | Path, frame_ms: int = FRAME_MS
) -> AsyncIterator[SpeechFrame]:
    """Entrega el WAV como SpeechFrame de frame_ms.

    t_capture es el desplazamiento dentro del archivo, no el reloj de pared: en F1 el
    archivo ES la linea temporal, y asi cada corrida sobre el mismo WAV es identica.
    """
    pcm, sample_rate = read_wav_pcm(path)
    n = int(sample_rate * frame_ms / 1000)
    for i in range(0, len(pcm), n):
        yield SpeechFrame(
            pcm=pcm[i : i + n],
            sample_rate=sample_rate,
            t_capture=round(i / sample_rate, 6),
        )


def frames_to_wav_bytes(pcm: np.ndarray, sample_rate: int) -> bytes:
    import io

    buf = io.BytesIO()
    sf.write(buf, pcm, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def write_dubbed_wav(
    path: str | Path,
    chunks: Iterable[DubbedChunk],
    fallback_sample_rate: int = EXPECTED_SAMPLE_RATE,
) -> tuple[Path, float, int]:
    """Concatena los DubbedChunk en un WAV. Devuelve ruta, duracion y numero de muestras."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pieces: list[np.ndarray] = []
    sample_rate = fallback_sample_rate
    for c in chunks:
        if c.pcm.size == 0:
            continue
        if pieces and c.sample_rate != sample_rate:
            raise AudioFormatError(
                f"mixed sample rates in dubbed output: {sample_rate} then {c.sample_rate}"
            )
        sample_rate = c.sample_rate
        pieces.append(c.pcm)
    pcm = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16)
    sf.write(str(p), pcm, sample_rate, format="WAV", subtype="PCM_16")
    return p, len(pcm) / sample_rate if sample_rate else 0.0, len(pcm)


def silence(seconds: float, sample_rate: int) -> np.ndarray:
    return np.zeros(int(seconds * sample_rate), dtype=np.int16)


async def read_wav_frames_realtime(
    path: str | Path, frame_ms: int = FRAME_MS
) -> AsyncIterator[SpeechFrame]:
    """Igual que read_wav_frames pero a velocidad de reloj, como un microfono.

    Existe para poder ejercitar el pipeline de F2 sin hablar: mismo VAD, mismo WebSocket,
    mismos triggers. **No sustituye al criterio de aceptacion de F2**, que exige voz real
    por microfono; sirve para medir de forma repetible el efecto de cada ajuste.
    """
    import asyncio

    pcm, sample_rate = read_wav_pcm(path)
    n = int(sample_rate * frame_ms / 1000)
    period = frame_ms / 1000
    start = time.monotonic()
    for idx, i in enumerate(range(0, len(pcm), n)):
        target = start + idx * period
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        yield SpeechFrame(
            pcm=pcm[i : i + n],
            sample_rate=sample_rate,
            t_capture=round(i / sample_rate, 6),
        )
