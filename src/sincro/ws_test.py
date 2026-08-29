"""make ws-test - G0. Un socket, un motor, contra el fixture de F1.

Arranca el servidor de `ws_serve.py` en localhost, le manda el fixture con un cliente
WebSocket (misma logica que `tests/ws_client.py`, sin cruzar el paquete `sincro` con
`tests/`) y reporta la telemetria con el mismo formato que `make report`. El criterio de
G0 se verifica comparando esta salida, etapa por etapa, contra la telemetria de F2 --
esta herramienta mide, no decide si pasa.
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
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .adapters.file_io import read_wav_frames_realtime
from .adapters.ws_io import (
    SAMPLE_RATE,
    SAMPLES_PER_FRAME,
    FrameError,
    decode_frame,
    encode_frame,
    pad_to_frame,
)
from .config import load_settings
from .report import load, render
from .telemetry import TelemetryWriter
from .ws_serve import run_server

logger = logging.getLogger(__name__)

DEFAULT_FIXTURE = "tests/fixtures/es_30s.wav"
DEFAULT_PORT = 8765


async def _send_and_receive(
    url: str, wav_path: str, out_path: str, tail_s: float
) -> dict[str, object]:
    async with connect(url, compression=None) as ws:

        async def receive() -> tuple[np.ndarray, int, int]:
            chunks: list[np.ndarray] = []
            frames = 0
            bad = 0
            try:
                async for message in ws:
                    if isinstance(message, str):
                        continue
                    try:
                        frame = decode_frame(message)
                    except FrameError as exc:
                        bad += 1
                        logger.warning("ws-test: frame invalido recibido: %s", exc)
                        continue
                    chunks.append(frame.pcm)
                    frames += 1
            except ConnectionClosed:
                pass
            pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
            return pcm, frames, bad

        recv_task = asyncio.create_task(receive())
        t0 = time.monotonic()
        seq = 0
        async for frame in read_wav_frames_realtime(wav_path):
            fin = frame.pcm.size != SAMPLES_PER_FRAME
            pcm = frame.pcm if not fin else pad_to_frame(frame.pcm)
            await ws.send(encode_frame(pcm, seq, int(frame.t_capture * 1000), fin=fin))
            seq = (seq + 1) & 0xFFFF
        send_elapsed = time.monotonic() - t0

        await asyncio.sleep(tail_s)
        await ws.close()
        pcm, frames_received, bad_frames = await recv_task

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), pcm, SAMPLE_RATE, subtype="PCM_16")
    return {
        "frames_sent": seq,
        "frames_received": frames_received,
        "bad_frames": bad_frames,
        "send_elapsed_s": round(send_elapsed, 2),
        "out_duration_s": round(pcm.size / SAMPLE_RATE, 2),
        "out_path": str(out),
    }


async def main_async(fixture: str, port: int, tail_s: float) -> int:
    s = load_settings()
    writer = TelemetryWriter()
    server = await run_server("localhost", port, s, writer)
    print(f"  servidor    : ws://localhost:{port}")
    print(f"  fixture     : {fixture}")
    print(f"  telemetry   : {writer.path}")
    print()

    out_wav = f"out/ws-test-{time.strftime('%Y%m%dT%H%M%S')}.wav"
    try:
        result = await _send_and_receive(f"ws://localhost:{port}", fixture, out_wav, tail_s)
    finally:
        server.close()
        await server.wait_closed()

    print(f"  frames tx   : {result['frames_sent']}")
    print(f"  frames rx   : {result['frames_received']}  ({result['bad_frames']} invalidos)")
    print(f"  wav salida  : {result['out_path']}  {result['out_duration_s']}s")
    print()

    rows = load(writer.path)
    if not rows:
        print("ERROR: el servidor no produjo ningun segmento.", file=sys.stderr)
        return 1
    print(render(writer.path, rows))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ws_test", description="G0: una direccion end to end")
    ap.add_argument("--fixture", default=DEFAULT_FIXTURE)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--tail-s", type=float, default=15.0)
    args = ap.parse_args()

    if not Path(args.fixture).is_file():
        print(f"ERROR: no existe {args.fixture}", file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(main_async(args.fixture, args.port, args.tail_s))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        logger.exception("ws-test failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
