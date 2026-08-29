"""Cliente de prueba WebSocket, v4. Sustituto del microfono para todo v4.

Lee un WAV, lo emite en frames de 20 ms a ritmo real por el contrato binario de Notion
("SINCRO Motor v4 - Contrato de conexion", seccion 5), recibe el doblaje por el mismo
socket y lo escribe a otro WAV. No es parte del motor: es una herramienta de prueba,
reutilizable en G1 (dos instancias, llamada bidireccional) y en G3/G4 contra un servidor
real desplegado en Azure.

Uso: .venv/bin/python tests/ws_client.py --url ws://localhost:8765 --wav x.wav
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from sincro.adapters.file_io import read_wav_frames_realtime
from sincro.adapters.ws_io import (
    SAMPLE_RATE,
    SAMPLES_PER_FRAME,
    FrameError,
    decode_frame,
    encode_frame,
    pad_to_frame,
)

logger = logging.getLogger(__name__)


async def send_wav(ws: ClientConnection, wav_path: str) -> int:
    """Emite el WAV a ritmo real, un frame de 20 ms por vez. Devuelve frames enviados."""
    seq = 0
    async for frame in read_wav_frames_realtime(wav_path):
        # Solo el ultimo frame del WAV puede no caer justo en el borde de 320 muestras;
        # ese mismo relleno se usa como marca (imperfecta) de fin de emision.
        fin = frame.pcm.size != SAMPLES_PER_FRAME
        pcm = frame.pcm if not fin else pad_to_frame(frame.pcm)
        ts_ms = int(frame.t_capture * 1000)
        await ws.send(encode_frame(pcm, seq, ts_ms, fin=fin))
        seq = (seq + 1) & 0xFFFF
    return seq


async def receive_to_pcm(ws: ClientConnection) -> tuple[np.ndarray, int, int]:
    """Junta los frames binarios recibidos hasta que el socket se cierra.

    Devuelve (pcm, frames_recibidos, frames_invalidos).
    """
    chunks: list[np.ndarray] = []
    frames = 0
    bad = 0
    try:
        async for message in ws:
            if isinstance(message, str):
                continue  # control JSON: no es responsabilidad de este cliente
            try:
                frame = decode_frame(message)
            except FrameError as exc:
                bad += 1
                logger.warning("ws client: frame invalido recibido: %s", exc)
                continue
            chunks.append(frame.pcm)
            frames += 1
    except ConnectionClosed:
        pass
    pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
    return pcm, frames, bad


async def run_client(url: str, wav_path: str, out_path: str, tail_s: float) -> dict[str, object]:
    async with connect(url, compression=None) as ws:
        recv_task = asyncio.create_task(receive_to_pcm(ws))
        t0 = time.monotonic()
        frames_sent = await send_wav(ws, wav_path)
        send_elapsed = time.monotonic() - t0

        # El doblaje del ultimo segmento sigue llegando despues de que el WAV termina:
        # VAD, commit, traduccion y sintesis tardan tras la ultima palabra. Sin esta
        # espera se corta la cola de salida antes de que llegue el final.
        await asyncio.sleep(tail_s)
        await ws.close()
        pcm, frames_received, bad_frames = await recv_task

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), pcm, SAMPLE_RATE, subtype="PCM_16")

    return {
        "frames_sent": frames_sent,
        "frames_received": frames_received,
        "bad_frames": bad_frames,
        "send_elapsed_s": round(send_elapsed, 2),
        "out_duration_s": round(pcm.size / SAMPLE_RATE, 2),
        "out_path": str(out),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ws_client", description="Cliente de prueba WS, sustituto del microfono (v4)"
    )
    ap.add_argument("--url", required=True, help="ws://host:puerto")
    ap.add_argument("--wav", required=True, help="WAV de entrada, 16 kHz mono")
    ap.add_argument("--out", default=None, help="WAV de salida (default out/ws_client-<hora>.wav)")
    ap.add_argument("--tail-s", type=float, default=15.0, help="espera tras enviar todo el WAV")
    args = ap.parse_args()

    out_path = args.out or f"out/ws_client-{time.strftime('%Y%m%dT%H%M%S')}.wav"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    result = asyncio.run(run_client(args.url, args.wav, out_path, args.tail_s))
    print(f"frames enviados   : {result['frames_sent']}")
    print(f"frames recibidos  : {result['frames_received']}  ({result['bad_frames']} invalidos)")
    print(f"tiempo de envio   : {result['send_elapsed_s']}s")
    print(f"wav de salida     : {result['out_path']}  {result['out_duration_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
