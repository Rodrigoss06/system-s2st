"""G0 - sirve una direccion del motor sobre WebSocket crudo.

Esto NO es CallSession (G1): un socket, un motor, sin par, sin M9, sin API de control,
sin dispatcher. Es el mismo `DubbingEngine` de F2, con `adapters/ws_io.py` en vez de
`adapters/console_io.py`. Existe para medir el adaptador de transporte, no para producir.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from websockets.asyncio.server import Server, ServerConnection, serve

from .adapters.ws_io import WebSocketAudioSink, WebSocketAudioSource, enable_tcp_nodelay
from .committer import StreamingCommitter
from .config import Settings
from .engine import DubbingEngine, EngineStats, TurnResult
from .gate import DEFAULT_MIN_SILENCE, SileroGate
from .synthesizer import FishSynthesizer
from .telemetry import TelemetryWriter
from .transcriber import DeepgramStreamTranscriber
from .translator import GroqTranslator
from .voices import FishVoiceRegistry

logger = logging.getLogger(__name__)

# El contrato de conexion exige 16 kHz mono en las dos direcciones (Notion, seccion 5).
# F2/F3 usaban 44100 Hz porque el destino era un altavoz local; aqui el destino es el
# propio socket, y resamplear seria latencia y complejidad que Fish resuelve gratis con
# un parametro.
WS_SAMPLE_RATE = 16_000


def build_engine(
    s: Settings, writer: TelemetryWriter, sink: WebSocketAudioSink
) -> DubbingEngine:
    """Misma construccion que `live.run_live`, con el sink de WS como `on_audio`."""
    profile = s.profile
    gate = SileroGate(profile, min_silence_duration=DEFAULT_MIN_SILENCE)
    gate.load_eou()
    transcriber = DeepgramStreamTranscriber(s.deepgram_api_key, profile.src, profile.deepgram_code)
    committer = StreamingCommitter(gate, profile.turn_detector_code)
    translator = GroqTranslator(
        s.groq_api_key,
        s.llm_model,
        profile,
        reasoning_effort=s.llm_reasoning_effort,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
    )
    synth = FishSynthesizer(s.fish_api_key, model=s.tts_model, sample_rate=WS_SAMPLE_RATE)
    registry = FishVoiceRegistry(s.fish_api_key)
    reference_id = ""
    if s.voice_id:
        registry.remember("speaker-0", s.voice_id)
        reference_id = s.voice_id
    return DubbingEngine(
        s, gate, transcriber, committer, translator, synth, writer,
        on_audio=sink.play, reference_id=reference_id,
    )


async def serve_connection(
    connection: ServerConnection,
    s: Settings,
    writer: TelemetryWriter,
    on_turn: Callable[[TurnResult], None] | None = None,
) -> EngineStats:
    """Una llamada a esto = una direccion end to end. Termina cuando el socket se cierra."""
    enable_tcp_nodelay(connection)
    source = WebSocketAudioSource(connection)
    sink = WebSocketAudioSink(connection)
    engine = build_engine(s, writer, sink)

    def _on_turn(r: TurnResult) -> None:
        # Cierra el frame final del segmento (flags.fin) antes de que on_turn de arriba
        # vea el resultado: el contrato necesita ese marcador para saber donde termina
        # la emision, y `on_audio` por si solo no distingue chunk de en medio de final.
        sink.end_utterance(r)
        if on_turn is not None:
            on_turn(r)

    try:
        return await engine.run(source.frames(), on_turn=_on_turn)
    finally:
        await sink.close()
        await engine.aclose()
        logger.info("ws source stats: %s", source.stats)
        logger.info("ws sink stats: %s", sink.stats)


async def run_server(
    host: str,
    port: int,
    s: Settings,
    writer: TelemetryWriter,
    on_connection: Callable[[EngineStats], None] | None = None,
) -> Server:
    """Arranca el servidor y devuelve el objeto `Server` ya escuchando (`serve()` con
    `compression=None`: el PCM no comprime bien y le cuesta CPU y latencia por frame,
    CLAUDE.md, reglas del camino de audio)."""

    async def handler(connection: ServerConnection) -> None:
        stats = await serve_connection(connection, s, writer)
        if on_connection is not None:
            on_connection(stats)

    return await serve(handler, host, port, compression=None)


if __name__ == "__main__":
    import argparse
    import asyncio
    import sys

    from .config import load_settings

    ap = argparse.ArgumentParser(prog="ws_serve", description="Sirve el motor por WebSocket crudo")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    async def _main() -> int:
        s = load_settings()
        writer = TelemetryWriter()
        server = await run_server(args.host, args.port, s, writer)
        print(f"listening on ws://{args.host}:{args.port}  telemetry={writer.path}")
        async with server:
            await server.serve_forever()
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(_main()))
