"""Adaptador de consola, F2. sounddevice: microfono de entrada, altavoz de salida.

Unico modulo junto a file_io.py que puede importar sounddevice. El motor no lo conoce:
recibe SpeechFrame y devuelve DubbedChunk. En v4 este archivo se sustituye por una pista
de LiveKit sin tocar M1-M8.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import AsyncIterator
from typing import Any, Final

import numpy as np
import sounddevice as sd

from ..contracts import SpeechFrame

logger = logging.getLogger(__name__)

SAMPLE_RATE: Final[int] = 16_000
FRAME_MS: Final[int] = 20
BLOCKSIZE: Final[int] = SAMPLE_RATE * FRAME_MS // 1000

# Si la cola de captura crece, el consumidor va mas lento que el microfono. Se descartan
# los frames mas viejos: en tiempo real el audio viejo ya no sirve para nada.
MAX_QUEUED_FRAMES: Final[int] = 100

HEADPHONES_WARNING: Final[str] = """
================================================================================
  AURICULARES OBLIGATORIOS

  El microfono y el altavoz estan en la misma maquina. Sin auriculares el motor
  transcribe su propia salida traducida, la vuelve a traducir y entra en bucle.

  La puerta dura de AudioGate cierra el microfono mientras se reproduce, pero es
  la segunda defensa, no la primera. Ponte los auriculares.
================================================================================
"""


def list_devices() -> str:
    return str(sd.query_devices())


def warn_headphones(interactive: bool = True) -> bool:
    """Devuelve False si el usuario aborta."""
    print(HEADPHONES_WARNING)
    if not interactive:
        return True
    try:
        answer = input("  Llevas auriculares puestos? [s/N] ").strip().lower()
    except EOFError:
        return False
    if answer not in ("s", "si", "sí", "y", "yes"):
        print("  Abortado. Ponte los auriculares y vuelve a ejecutar make live.")
        return False
    return True


class MicrophoneSource:
    """Microfono a SpeechFrame de 20 ms, int16 mono 16 kHz."""

    def __init__(self, device: int | str | None = None) -> None:
        self.device = device
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._stream: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.frames_captured = 0
        self.frames_dropped = 0
        self.overflows = 0

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        # Corre en el hilo de PortAudio: nada bloqueante aqui dentro.
        if status:
            self.overflows += 1
        if self._loop is None:
            return
        pcm = np.frombuffer(bytes(indata), dtype=np.int16).copy()
        if self._queue.qsize() >= MAX_QUEUED_FRAMES:
            self.frames_dropped += 1
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, pcm)

    async def frames(self) -> AsyncIterator[SpeechFrame]:
        self._loop = asyncio.get_running_loop()
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCKSIZE,
            device=self.device,
            channels=1,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()
        t0 = time.monotonic()
        logger.info("microphone open: %d Hz, %d ms frames", SAMPLE_RATE, FRAME_MS)
        try:
            while True:
                pcm = await self._queue.get()
                self.frames_captured += 1
                yield SpeechFrame(
                    pcm=pcm,
                    sample_rate=SAMPLE_RATE,
                    t_capture=round(time.monotonic() - t0, 6),
                )
        finally:
            self._stream.stop()
            self._stream.close()
            logger.info(
                "microphone closed: %d frames, %d dropped, %d overflows",
                self.frames_captured,
                self.frames_dropped,
                self.overflows,
            )


class SpeakerSink:
    """Altavoz. Reproduce los DubbedChunk segun llegan, sin esperar al segmento entero."""

    def __init__(self, sample_rate: int, device: int | str | None = None) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self._stream: Any = None
        self.samples_played = 0

    def open(self) -> None:
        self._stream = sd.RawOutputStream(
            samplerate=self.sample_rate,
            device=self.device,
            channels=1,
            dtype="int16",
        )
        self._stream.start()
        logger.info("speaker open: %d Hz", self.sample_rate)

    async def play(self, pcm: np.ndarray) -> None:
        if self._stream is None:
            self.open()
        # write() bloquea hasta que hay hueco en el buffer de PortAudio: va a un hilo
        # para no congelar la captura ni el resto del pipeline.
        await asyncio.to_thread(self._stream.write, pcm.tobytes())
        self.samples_played += pcm.size

    async def drain(self) -> None:
        """Espera a que el buffer del altavoz se vacie de verdad.

        Sin esto el unmute llega mientras la sala todavia suena, y el microfono captura
        la cola de nuestra propia salida.
        """
        if self._stream is None:
            return
        latency = float(getattr(self._stream, "latency", 0.0) or 0.0)
        await asyncio.sleep(latency)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def check_devices() -> tuple[bool, str]:
    try:
        default_in, default_out = sd.default.device
        devs = sd.query_devices()
        name_in = devs[default_in]["name"] if default_in is not None else "?"
        name_out = devs[default_out]["name"] if default_out is not None else "?"
        return True, f"in='{name_in}'  out='{name_out}'"
    except Exception as e:
        print(f"ERROR: no audio devices: {e}", file=sys.stderr)
        return False, str(e)
