"""Cliente de prueba para llamadas bidireccionales, v4 G1.

Manda `hello`, espera `ready`/`waiting_for_peer`, emite un WAV en frames de 20 ms a
ritmo real (igual que `ws_client.py`), y ademas registra los mensajes de control
(`state`, `dub_start`, `dub_end`, `peer_left`, ...) en el orden en que llegan -- es lo
que `call_test.py` usa para verificar el criterio de G1 sobre el orden de `state`.

No sustituye a `ws_client.py`: G0 no tiene hello ni control JSON. Este es su equivalente
para G1, donde SI hacen falta.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
    seq = 0
    async for frame in read_wav_frames_realtime(wav_path):
        fin = frame.pcm.size != SAMPLES_PER_FRAME
        pcm = frame.pcm if not fin else pad_to_frame(frame.pcm)
        await ws.send(encode_frame(pcm, seq, int(frame.t_capture * 1000), fin=fin))
        seq = (seq + 1) & 0xFFFF
    return seq


async def receive(ws: ClientConnection) -> tuple[np.ndarray, list[dict[str, object]], int, int]:
    """Junta audio y mensajes de control hasta que el socket se cierra.

    Devuelve (pcm, mensajes_de_control_en_orden, frames_audio, frames_invalidos).
    """
    chunks: list[np.ndarray] = []
    messages: list[dict[str, object]] = []
    frames = 0
    bad = 0
    try:
        async for message in ws:
            if isinstance(message, str):
                messages.append(json.loads(message))
                continue
            try:
                frame = decode_frame(message)
            except FrameError as exc:
                bad += 1
                logger.warning("call client: frame invalido recibido: %s", exc)
                continue
            chunks.append(frame.pcm)
            frames += 1
    except ConnectionClosed:
        pass
    pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
    return pcm, messages, frames, bad


async def run_call_client(
    url: str,
    token: str,
    wav_path: str,
    out_path: str,
    tail_s: float,
) -> dict[str, object]:
    """`token` es el que devuelve `POST /v1/sessions` del Dispatcher para este
    participante -- desde G2 el `hello` no lleva `lang` suelto (contrato, seccion 8;
    D52). Para pruebas locales sin Dispatcher, `python -m sincro.tokens` (o
    `issue_token` directo) genera uno con el mismo `SINCRO_TOKEN_SECRET`."""
    async with connect(url, compression=None) as ws:
        await ws.send(
            json.dumps(
                {"t": "hello", "token": token, "audio": {"rate": SAMPLE_RATE, "frame_ms": 20}}
            )
        )

        recv_task = asyncio.create_task(receive(ws))
        t0 = time.monotonic()
        frames_sent = await send_wav(ws, wav_path)
        send_elapsed = time.monotonic() - t0

        # El doblaje del ultimo segmento sigue llegando despues de que el WAV termina.
        await asyncio.sleep(tail_s)
        await ws.close()
        pcm, messages, frames_received, bad_frames = await recv_task

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
        "messages": messages,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="call_client", description="Cliente de prueba de llamada (G1/G2)"
    )
    ap.add_argument("--url", required=True)
    ap.add_argument(
        "--token", required=True, help="token de POST /v1/sessions para este participante"
    )
    ap.add_argument("--wav", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--tail-s", type=float, default=15.0)
    args = ap.parse_args()

    out_path = args.out or f"out/call_client-{time.strftime('%Y%m%dT%H%M%S')}.wav"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    result = asyncio.run(run_call_client(args.url, args.token, args.wav, out_path, args.tail_s))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
