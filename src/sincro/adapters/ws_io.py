"""Adaptador WebSocket, v4 G0. Sustituye a console_io.py: sin sounddevice, sin mic real.

Unico modulo (junto a ws_serve.py y ws_client.py) que puede importar `websockets`. El
motor no lo sabe: recibe SpeechFrame y llama a on_audio con DubbedChunk, igual que con
un microfono. Formato binario exacto de Notion, "SINCRO Motor v4 - Contrato de conexion",
seccion 5.

Frame binario, 8 bytes de cabecera + 640 bytes de PCM (16 kHz mono s16le, 20 ms):

    ver(u8) flags(u8) seq(u16 BE) timestamp_ms(u32 BE) | PCM little-endian, 320 muestras

`flags` bit0 = fin de emision. `seq` es circular (envuelve en 65536); el receptor lo usa
para detectar perdida, nunca para reordenar (el contrato deja eso para cuando haga falta).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final

import numpy as np
from websockets.asyncio.connection import Connection
from websockets.exceptions import ConnectionClosed

from ..contracts import DubbedChunk, SpeechFrame
from ..engine import TurnResult

logger = logging.getLogger(__name__)

SAMPLE_RATE: Final[int] = 16_000
FRAME_MS: Final[int] = 20
SAMPLES_PER_FRAME: Final[int] = SAMPLE_RATE * FRAME_MS // 1000  # 320
PCM_BYTES_PER_FRAME: Final[int] = SAMPLES_PER_FRAME * 2  # 640, int16
FRAME_VERSION: Final[int] = 1
FLAG_FIN: Final[int] = 0x01

# ver, flags, seq, timestamp_ms. Red = big-endian; el PCM que sigue es little-endian,
# tal como especifica el contrato (son dos cosas distintas, no lo unifiques).
_HEADER = struct.Struct(">BBHI")
HEADER_BYTES: Final[int] = _HEADER.size  # 8
FRAME_BYTES: Final[int] = HEADER_BYTES + PCM_BYTES_PER_FRAME  # 648

# ~500 ms de audio de salida en cola. Bastante para absorber jitter del cliente sin
# acumular retardo si de verdad no da abasto.
MAX_QUEUED_OUT_FRAMES: Final[int] = 25


class FrameError(ValueError):
    pass


@dataclass(frozen=True)
class DecodedFrame:
    seq: int
    timestamp_ms: int
    fin: bool
    pcm: np.ndarray


def encode_frame(pcm: np.ndarray, seq: int, timestamp_ms: int, fin: bool = False) -> bytes:
    if pcm.size != SAMPLES_PER_FRAME:
        raise FrameError(f"frame debe tener {SAMPLES_PER_FRAME} muestras, llegaron {pcm.size}")
    flags = FLAG_FIN if fin else 0
    header = _HEADER.pack(FRAME_VERSION, flags, seq & 0xFFFF, timestamp_ms & 0xFFFFFFFF)
    return header + pcm.astype("<i2", copy=False).tobytes()


def pad_to_frame(pcm: np.ndarray) -> np.ndarray:
    """Rellena con silencio hasta SAMPLES_PER_FRAME.

    El ultimo frame de un WAV o de una emision rara vez cae justo en el borde de 320
    muestras; el contrato no admite frames de tamano variable, asi que se completa con
    ceros en vez de mandar un frame corto.
    """
    if pcm.size == SAMPLES_PER_FRAME:
        return pcm
    if pcm.size > SAMPLES_PER_FRAME:
        raise FrameError(f"no se puede rellenar: {pcm.size} muestras exceden el frame")
    pad = SAMPLES_PER_FRAME - pcm.size
    return np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])


def decode_frame(data: bytes) -> DecodedFrame:
    if len(data) != FRAME_BYTES:
        raise FrameError(f"se esperaban {FRAME_BYTES} bytes, llegaron {len(data)}")
    ver, flags, seq, timestamp_ms = _HEADER.unpack(data[:HEADER_BYTES])
    if ver != FRAME_VERSION:
        raise FrameError(f"version de frame no soportada: {ver}")
    pcm = np.frombuffer(data[HEADER_BYTES:], dtype="<i2").copy()
    return DecodedFrame(seq=seq, timestamp_ms=timestamp_ms, fin=bool(flags & FLAG_FIN), pcm=pcm)


def enable_tcp_nodelay(connection: Connection) -> None:
    """Nagle coalesce frames de 640 bytes cada 20 ms y anade decenas de ms invisibles.

    La libreria no expone un parametro para esto (`websockets.serve` no tiene
    `tcp_nodelay`); `connection.transport` es el `asyncio.Transport` real de
    `connection_made`, documentado por uso en la propia libreria (ver
    `local_address`/`remote_address` en `websockets/asyncio/connection.py`).
    """
    sock = connection.transport.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class WebSocketAudioSource:
    """Entrada v4. Decodifica frames binarios del contrato en SpeechFrame.

    Mismo contrato de salida que `MicrophoneSource.frames()`: un AsyncIterator[SpeechFrame]
    liso. Ninguna logica de VAD ni de gate aqui, igual que en console_io.py: eso lo hace
    `gate.process()` sobre lo que este adaptador produce.
    """

    def __init__(self, connection: Connection) -> None:
        self._ws = connection
        self.frames_received = 0
        self.frames_lost = 0  # huecos detectados por seq, nunca reordenados (contrato S5)
        self.bad_frames = 0  # tamano o version invalidos: descartados, jamas fallo silencioso
        self._expected_seq: int | None = None

    async def frames(self) -> AsyncIterator[SpeechFrame]:
        t0 = time.monotonic()
        logger.info("ws source open: %d Hz, %d ms frames", SAMPLE_RATE, FRAME_MS)
        try:
            async for message in self._ws:
                if isinstance(message, str):
                    # Frame de control JSON (hello/mute/unmute/bye): no es audio, y no es
                    # responsabilidad de este adaptador. Ver EventSink en el contrato.
                    continue
                try:
                    frame = decode_frame(message)
                except FrameError as exc:
                    self.bad_frames += 1
                    logger.warning("ws source: frame descartado: %s", exc)
                    continue
                if self._expected_seq is not None and frame.seq != self._expected_seq:
                    gap = (frame.seq - self._expected_seq) & 0xFFFF
                    self.frames_lost += gap
                    logger.warning(
                        "ws source: %d frame(s) perdidos antes de seq %d", gap, frame.seq
                    )
                self._expected_seq = (frame.seq + 1) & 0xFFFF
                self.frames_received += 1
                yield SpeechFrame(
                    pcm=frame.pcm,
                    sample_rate=SAMPLE_RATE,
                    t_capture=round(time.monotonic() - t0, 6),
                )
        finally:
            logger.info(
                "ws source closed: %d frames, %d perdidos, %d invalidos",
                self.frames_received,
                self.frames_lost,
                self.bad_frames,
            )

    @property
    def stats(self) -> dict[str, int]:
        return {
            "received": self.frames_received,
            "lost": self.frames_lost,
            "bad": self.bad_frames,
        }


class WebSocketAudioSink:
    """Salida v4. Trocea DubbedChunk en frames del contrato y los reenvia segun llegan.

    `play()` es el `on_audio` del motor: se llama una vez por chunk de Fish, streaming.
    Cada chunk se corta en frames de exactamente 320 muestras y se encola para envio
    inmediato; el sobrante entre llamadas se guarda para la siguiente, nunca se espera
    a la sintesis completa del segmento (la regla mas cara de romper, CLAUDE.md).

    `end_utterance()` es el `on_turn` del motor: marca el ultimo frame del segmento con
    `flags.fin`, rellenando con silencio si el sobrante no llega a 320 muestras. Sin este
    enganche el contrato no tendria forma de saber donde termina una emision, porque
    `on_audio` por si solo no distingue "chunk de en medio" de "chunk final".
    """

    def __init__(self, connection: Connection, max_queued: int = MAX_QUEUED_OUT_FRAMES) -> None:
        self._ws = connection
        self._leftover: np.ndarray = np.empty(0, dtype=np.int16)
        self._seq = 0
        self._t0 = time.monotonic()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_queued)
        self.frames_sent = 0
        self.frames_dropped = 0
        self._closed = False
        self._sender_task = asyncio.create_task(self._sender())

    async def _sender(self) -> None:
        # Si el socket ya murio seguimos vaciando la cola (marcando task_done) sin
        # mandar nada, hasta ver el centinela: es lo que permite que `drain()` use
        # `queue.join()` en vez de sondear con sleep.
        while True:
            data = await self._queue.get()
            try:
                if data is None:
                    return
                if self._closed:
                    continue
                try:
                    await self._ws.send(data)
                    self.frames_sent += 1
                except ConnectionClosed:
                    logger.warning("ws sink: conexion cerrada, se detiene el envio")
                    self._closed = True
            finally:
                self._queue.task_done()

    def _enqueue(self, pcm: np.ndarray, fin: bool) -> None:
        if self._closed:
            return
        data = encode_frame(pcm, self._seq, int((time.monotonic() - self._t0) * 1000), fin)
        self._seq = (self._seq + 1) & 0xFFFF
        try:
            self._queue.put_nowait(data)
            return
        except asyncio.QueueFull:
            pass
        # Backpressure por descarte (CLAUDE.md): si el cliente no da abasto, se tira el
        # frame en cola MAS VIEJO, nunca se encola sin limite ni se bloquea el productor.
        with contextlib.suppress(asyncio.QueueEmpty):
            self._queue.get_nowait()
            self._queue.task_done()
            self.frames_dropped += 1
            logger.warning("ws sink: cola llena, se descarta el frame mas viejo")
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(data)

    async def play(self, chunk: DubbedChunk) -> None:
        buf = np.concatenate([self._leftover, chunk.pcm.astype(np.int16, copy=False)])
        n_full = buf.size // SAMPLES_PER_FRAME
        for i in range(n_full):
            self._enqueue(buf[i * SAMPLES_PER_FRAME : (i + 1) * SAMPLES_PER_FRAME], fin=False)
        self._leftover = buf[n_full * SAMPLES_PER_FRAME :]

    def end_utterance(self, result: TurnResult | None = None) -> None:
        if self._leftover.size == 0:
            return  # nada que cerrar: skip, drop o TTS caido no produjeron audio
        self._enqueue(pad_to_frame(self._leftover), fin=True)
        self._leftover = np.empty(0, dtype=np.int16)

    async def drain(self) -> None:
        await self._queue.join()

    async def close(self) -> None:
        await self.drain()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        await self._sender_task

    @property
    def stats(self) -> dict[str, int]:
        return {"sent": self.frames_sent, "dropped": self.frames_dropped}
