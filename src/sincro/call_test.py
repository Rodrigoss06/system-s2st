"""make call-test - G1. Llamada bidireccional simulada con dos clientes de prueba.

Verifica los tres criterios de G1:
  1. mute_calls == unmute_calls en AMBOS motores (M9 no se queda a medias)
  2. ningun segmento del doblaje aparece transcrito como entrada del otro motor
     (la prueba real de que la puerta cruzada funciona, no solo que existe)
  3. los mensajes `state` llegan en un orden valido (nunca dos veces el mismo estado
     seguido, nunca `idle` antes del primer `speaking`)
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

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
from .call_serve import run_call_server
from .call_session import CallSession
from .config import load_settings
from .engine import TurnResult
from .telemetry import TelemetryWriter
from .tokens import issue_token

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8766
ECHO_SIMILARITY_THRESHOLD = 0.82


async def _client(
    url: str, session_id: str, user_id: str, lang: str, wav_path: str, out_path: str, tail_s: float
) -> dict[str, Any]:
    async with connect(url, compression=None) as ws:
        token = issue_token(session_id, user_id, lang, "")  # type: ignore[arg-type]
        await ws.send(json.dumps({"t": "hello", "token": token}))

        async def receive() -> tuple[np.ndarray, list[dict[str, Any]], int, int]:
            chunks: list[np.ndarray] = []
            messages: list[dict[str, Any]] = []
            frames = 0
            bad = 0
            try:
                async for message in ws:
                    if isinstance(message, str):
                        messages.append(json.loads(message))
                        continue
                    try:
                        frame = decode_frame(message)
                    except FrameError:
                        bad += 1
                        continue
                    chunks.append(frame.pcm)
                    frames += 1
            except ConnectionClosed:
                pass
            pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
            return pcm, messages, frames, bad

        recv_task = asyncio.create_task(receive())
        seq = 0
        async for frame in read_wav_frames_realtime(wav_path):
            fin = frame.pcm.size != SAMPLES_PER_FRAME
            pcm = frame.pcm if not fin else pad_to_frame(frame.pcm)
            await ws.send(encode_frame(pcm, seq, int(frame.t_capture * 1000), fin=fin))
            seq = (seq + 1) & 0xFFFF

        await asyncio.sleep(tail_s)
        await ws.close()
        pcm, messages, frames_received, bad_frames = await recv_task

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), pcm, SAMPLE_RATE, subtype="PCM_16")
    return {
        "lang": lang,
        "frames_sent": seq,
        "frames_received": frames_received,
        "bad_frames": bad_frames,
        "out_path": str(out),
        "messages": messages,
    }


def _echo_hits(
    segments_in: list[str], dub_out: list[str], threshold: float = ECHO_SIMILARITY_THRESHOLD
) -> list[tuple[str, str, float]]:
    """Busca texto doblado (`dub_out`) dentro de las transcripciones de entrada del
    motor CONTRARIO (`segments_in`). Similitud, no solo igualdad exacta: el STT puede
    variar puntuacion o capitalizacion sobre el mismo contenido de audio."""
    hits = []
    for dub_text in dub_out:
        if not dub_text.strip():
            continue
        for seg_text in segments_in:
            if not seg_text.strip():
                continue
            ratio = difflib.SequenceMatcher(None, dub_text.lower(), seg_text.lower()).ratio()
            if ratio >= threshold:
                hits.append((dub_text, seg_text, ratio))
    return hits


def _check_state_order(messages: list[dict[str, Any]]) -> list[str]:
    """Ordenes invalidos: dos `state` iguales seguidos, o `idle` antes de cualquier
    `speaking`. Devuelve la lista de problemas encontrados (vacia si esta todo bien)."""
    problems = []
    prev: str | None = None
    seen_speaking = False
    for msg in messages:
        if msg.get("t") != "state":
            continue
        state = msg.get("peer")
        if state == prev:
            problems.append(f"state repetido seguido: {state!r}")
        if state == "idle" and not seen_speaking:
            problems.append("idle antes de cualquier speaking")
        if state == "speaking":
            seen_speaking = True
        prev = state
    return problems


async def main_async(wav_en: str, wav_es: str, port: int, tail_s: float) -> int:
    base = load_settings()
    writer = TelemetryWriter()

    turns_ab: list[TurnResult] = []
    turns_ba: list[TurnResult] = []
    sessions: list[CallSession] = []

    server = await run_call_server(
        "localhost",
        port,
        base,
        writer,
        on_turn_ab=turns_ab.append,
        on_turn_ba=turns_ba.append,
        on_session_done=sessions.append,
    )
    print(f"  servidor    : ws://localhost:{port}")
    print(f"  telemetry   : {writer.path}")
    print()

    stamp = time.strftime("%Y%m%dT%H%M%S")
    session_id = f"s_test_{stamp}"
    url = f"ws://localhost:{port}"
    out_en = f"out/call-test-{stamp}-en.wav"
    out_es = f"out/call-test-{stamp}-es.wav"
    try:
        result_en, result_es = await asyncio.gather(
            _client(url, session_id, "u_en", "en", wav_en, out_en, tail_s),
            _client(url, session_id, "u_es", "es", wav_es, out_es, tail_s),
        )
    finally:
        server.close()
        await server.wait_closed()

    for label, r in (("EN", result_en), ("ES", result_es)):
        print(
            f"  {label}: tx={r['frames_sent']:5d}  rx={r['frames_received']:5d}"
            f"  ({r['bad_frames']} invalidos)  wav={r['out_path']}"
        )
    print()

    if not sessions:
        print(
            "ERROR: la llamada no se emparejo (revisa el hello de los dos clientes).",
            file=sys.stderr,
        )
        return 1
    session = sessions[0]

    ok = True

    # --- criterio 1: mute_calls == unmute_calls en ambos motores ---
    stats_ab = session.dir_ab.engine.gate.stats
    stats_ba = session.dir_ba.engine.gate.stats
    print("criterio 1 - mute_calls == unmute_calls")
    for label, stats in (("A->B", stats_ab), ("B->A", stats_ba)):
        balanced = stats["mute_calls"] == stats["unmute_calls"]
        ok = ok and balanced
        tag = "OK" if balanced else "FALLA"
        print(
            f"  [{tag}] {label}: mute_calls={stats['mute_calls']}  "
            f"unmute_calls={stats['unmute_calls']}"
        )
    print()

    # --- criterio 2: nada del doblaje aparece transcrito como entrada del otro motor ---
    print("criterio 2 - sin eco (texto doblado transcrito como entrada)")
    texts_in_ab = [t.seg.text for t in turns_ab]  # lo que A->B transcribio de A
    texts_in_ba = [t.seg.text for t in turns_ba]  # lo que B->A transcribio de B
    dub_to_a = [t.text_dst for t in turns_ba]  # el doblaje que sono en el altavoz de A
    dub_to_b = [t.text_dst for t in turns_ab]  # el doblaje que sono en el altavoz de B

    hits_a = _echo_hits(texts_in_ab, dub_to_a)  # doblaje-para-A que A "dijo" de vuelta
    hits_b = _echo_hits(texts_in_ba, dub_to_b)  # doblaje-para-B que B "dijo" de vuelta
    for label, hits in (("A", hits_a), ("B", hits_b)):
        if hits:
            ok = False
            print(f"  [FALLA] eco detectado del lado de {label}:")
            for dub_text, seg_text, ratio in hits:
                print(f"      doblaje  : {dub_text!r}")
                print(f"      entrada  : {seg_text!r}  (similitud {ratio:.2f})")
        else:
            print(f"  [OK] {label}: {len(dub_to_a if label == 'A' else dub_to_b)} doblajes, "
                  f"0 coincidencias en {len(texts_in_ab if label == 'A' else texts_in_ba)} "
                  f"transcripciones de entrada")
    print()

    # --- criterio 3: orden de los mensajes state ---
    print("criterio 3 - orden de los mensajes state")
    for label, r in (("EN", result_en), ("ES", result_es)):
        problems = _check_state_order(r["messages"])
        states = [m["peer"] for m in r["messages"] if m.get("t") == "state"]
        if problems:
            ok = False
            print(f"  [FALLA] {label}: {states}")
            for p in problems:
                print(f"      {p}")
        else:
            print(f"  [OK] {label}: {states}")
    print()

    print("=" * 72)
    print(f"  RESULTADO G1: {'PASA' if ok else 'FALLA'}")
    print("=" * 72)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="call_test", description="G1: llamada bidireccional simulada")
    ap.add_argument("--wav-en", required=True)
    ap.add_argument("--wav-es", required=True)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--tail-s", type=float, default=15.0)
    args = ap.parse_args()

    for path in (args.wav_en, args.wav_es):
        if not Path(path).is_file():
            print(f"ERROR: no existe {path}", file=sys.stderr)
            return 1

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(main_async(args.wav_en, args.wav_es, args.port, args.tail_s))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        logger.exception("call-test failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
