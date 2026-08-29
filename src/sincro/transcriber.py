"""M3 - Transcriber. Deepgram Nova-3 Monolingual, idioma fijo.

F1 usa el modo pre-grabado: el WAV entero en una peticion. F2 migra a WebSocket.
La forma de TranscriptEvent no cambia entre las dos, que es el motivo de D1.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx
import numpy as np

from .contracts import Lang, SpeechFrame, TranscriptEvent, Word

logger = logging.getLogger(__name__)

DEEPGRAM_URL: Final[str] = "https://api.deepgram.com/v1/listen"
MODEL: Final[str] = "nova-3"
TIMEOUT_S: Final[float] = 120.0
MAX_RETRIES: Final[int] = 3
BACKOFF_BASE_S: Final[float] = 1.0


class TranscriptionError(RuntimeError):
    pass


def _words_from_alternative(alt: dict[str, Any]) -> list[Word]:
    """Nova-3 devuelve punctuated_word solo con punctuate/smart_format activos.

    Se prefiere sobre `word` porque M4 corta por puntuacion: sin ella no hay frontera.
    """
    return [
        Word(
            text=w.get("punctuated_word") or w["word"],
            start=float(w["start"]),
            end=float(w["end"]),
            confidence=float(w.get("confidence", 0.0)),
        )
        for w in alt.get("words", [])
    ]


class DeepgramTranscriber:
    """Implementa el Protocol Transcriber contra la API pre-grabada de Deepgram.

    No usa livekit-plugins-deepgram: su conversion a SpeechData descarta la confianza
    por palabra, que `Word` exige. Ver D7 en DECISIONS.md.
    """

    def __init__(self, api_key: str, lang: Lang, deepgram_code: str) -> None:
        if not api_key:
            raise TranscriptionError("DEEPGRAM_API_KEY is empty")
        self._api_key = api_key
        self.lang = lang
        self.deepgram_code = deepgram_code
        self.request_id: str | None = None
        self.audio_duration: float = 0.0

    @property
    def _params(self) -> dict[str, str]:
        return {
            "model": MODEL,
            "language": self.deepgram_code,
            "punctuate": "true",
            "smart_format": "true",
            "filler_words": "false",
        }

    async def _post(self, wav: bytes) -> dict[str, Any]:
        headers = {
            "Authorization": f"Token {self._api_key}",
            "Content-Type": "audio/wav",
        }
        last: Exception | None = None
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            for attempt in range(MAX_RETRIES):
                try:
                    r = await client.post(
                        DEEPGRAM_URL, params=self._params, headers=headers, content=wav
                    )
                    if r.status_code == 200:
                        data: dict[str, Any] = r.json()
                        return data
                    # 4xx que no sea 429 no se reintenta: reintentar no lo arregla.
                    if r.status_code < 500 and r.status_code != 429:
                        raise TranscriptionError(
                            f"deepgram rejected the request: HTTP {r.status_code} {r.text[:200]}"
                        )
                    last = TranscriptionError(f"deepgram HTTP {r.status_code}: {r.text[:200]}")
                except httpx.HTTPError as e:
                    last = e
                delay = BACKOFF_BASE_S * 2**attempt
                logger.warning(
                    "deepgram attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    last,
                    delay,
                )
                await asyncio.sleep(delay)
        raise TranscriptionError(f"deepgram failed after {MAX_RETRIES} attempts: {last}")

    async def stream(
        self, frames: AsyncIterator[SpeechFrame]
    ) -> AsyncIterator[TranscriptEvent]:
        """Acumula el audio y emite un unico evento final con todas las palabras.

        En pre-grabado no hay parciales: Deepgram devuelve el resultado cerrado. M4 hace
        el corte por puntuacion sobre las palabras de este evento.
        """
        from .adapters.file_io import frames_to_wav_bytes

        buf: list[np.ndarray] = []
        sample_rate = 0
        async for f in frames:
            buf.append(f.pcm)
            sample_rate = f.sample_rate
        if not buf or sample_rate == 0:
            raise TranscriptionError("no audio frames received")

        pcm = np.concatenate(buf)
        self.audio_duration = len(pcm) / sample_rate
        wav = frames_to_wav_bytes(pcm, sample_rate)
        logger.info(
            "deepgram prerecorded: model=%s language=%s duration=%.2fs bytes=%d",
            MODEL,
            self.deepgram_code,
            self.audio_duration,
            len(wav),
        )

        data = await self._post(wav)
        self.request_id = data.get("metadata", {}).get("request_id")

        channels = data.get("results", {}).get("channels", [])
        if not channels or not channels[0].get("alternatives"):
            raise TranscriptionError("deepgram returned no alternatives")
        alt = channels[0]["alternatives"][0]

        words = _words_from_alternative(alt)
        transcript = alt.get("transcript", "")
        if not transcript.strip():
            logger.warning("deepgram returned an empty transcript")

        t_emit = asyncio.get_running_loop().time()
        yield TranscriptEvent(
            text=transcript,
            lang=self.lang,
            is_final=True,
            # Pre-grabado: el archivo termino, no hay endpointing en juego. En F2 este
            # valor viene del campo speech_final del WebSocket.
            speech_final=True,
            words=words,
            t_emit=round(t_emit, 3),
        )


# ---------------------------------------------------------------------------
# F2 - streaming por WebSocket
# ---------------------------------------------------------------------------

# Endpointing propio de Deepgram, en ms. Es deliberadamente corto: el corte real lo
# decide M4 con el turn-detector. Aqui solo se quiere que Deepgram cierre fragmentos
# pronto para que haya texto final con el que puntuar.
DG_ENDPOINTING_MS: Final[int] = 300
DG_UTTERANCE_END_MS: Final[int] = 1000
WS_MAX_RETRIES: Final[int] = 5
WS_BACKOFF_BASE_S: Final[float] = 0.5
WS_BACKOFF_MAX_S: Final[float] = 8.0

# Audio que se guarda mientras el socket esta caido. Con 30 s se cubre de sobra el corte
# de 5 s del criterio de F6; mas alla, el audio viejo ya no sirve para doblar en vivo.
WS_BUFFER_S: Final[float] = 30.0
# Ventana que se reenvia al reconectar: el audio posterior al ultimo `is_final` recibido.
# Deepgram no confirma que proceso, asi que sin este reenvio el segmento a medias se
# pierde, que es justo lo que el criterio prohibe.
WS_REPLAY_S: Final[float] = 10.0

# El gate (M2) no manda nada mientras no hay habla: es deliberado, paga STT por minuto
# de habla y no de reloj. Pero un silencio real (el hablante piensa, escucha al otro)
# deja el socket sin nada que enviar, y Deepgram lo cierra por inactividad (1011
# net0001), medido en vivo, no supuesto. Un KeepAlive sin audio real cada
# WS_KEEPALIVE_INTERVAL_S evita el cierre sin fingir habla que no hubo.
WS_KEEPALIVE_INTERVAL_S: Final[float] = 5.0


class DeepgramStreamTranscriber:
    """M3 en streaming. SDK oficial de Deepgram, no el plugin de LiveKit (D13).

    Emite parciales y finales. `speech_final` sale del campo homonimo del WebSocket, que
    es el endpointing interno de Deepgram, distinto de `is_final`. Los timestamps por
    palabra vienen en la linea temporal del stream, en segundos desde que se abrio.
    """

    def __init__(self, api_key: str, lang: Lang, deepgram_code: str) -> None:
        if not api_key:
            raise TranscriptionError("DEEPGRAM_API_KEY is empty")
        self._api_key = api_key
        self.lang = lang
        self.deepgram_code = deepgram_code
        self.t_stream_start = 0.0
        self.partials = 0
        self.finals = 0
        self.reconnects = 0
        self.frames_buffered = 0
        self.frames_replayed = 0
        self.frames_dropped_overflow = 0
        self.keepalives_sent = 0
        self.downtime_s = 0.0
        self.connected = False
        # Segundos de audio enviados en total, sumando todas las conexiones. Deepgram
        # reinicia sus timestamps en cada socket nuevo, asi que hay que llevar el
        # desplazamiento aparte o el TTFA se descuadra despues de reconectar.
        self._sent_total = 0.0
        self._conn_offset = 0.0
        self._blocked_until = 0.0
        # (segundos de audio enviados, t_capture de ese frame). El gate descarta silencio
        # y frames muteados, asi que el reloj de Deepgram, que cuenta audio recibido, va
        # mas lento que el de pared. Sin este mapeo el TTFA sale inflado.
        self._sent_s: list[float] = []
        self._capture_s: list[float] = []

    def simulate_network_drop(self, seconds: float) -> None:
        """Corta el socket y bloquea la reconexion durante `seconds`.

        Es la inyeccion de fallo de `make soak`. No toca la red del sistema: reproduce
        exactamente lo que ve el motor cuando el WebSocket se cae, que es lo que el
        riesgo R5 describe.
        """
        self._blocked_until = time.monotonic() + seconds
        self.connected = False
        logger.warning("simulated network drop: blocking reconnect for %.1fs", seconds)

    def capture_time_for(self, stream_time: float) -> float | None:
        """Traduce un timestamp de la linea temporal de Deepgram a t_capture real."""
        if not self._sent_s:
            return None
        i = bisect.bisect_right(self._sent_s, stream_time)
        if i == 0:
            return self._capture_s[0]
        if i >= len(self._sent_s):
            return self._capture_s[-1]
        # Interpolacion dentro del tramo: dentro de un frame el audio si es continuo.
        s0, s1 = self._sent_s[i - 1], self._sent_s[i]
        c0, c1 = self._capture_s[i - 1], self._capture_s[i]
        if s1 == s0:
            return c1
        return c0 + (c1 - c0) * (stream_time - s0) / (s1 - s0)

    def _to_event(self, msg: Any) -> TranscriptEvent | None:
        # El socket multiplexa Results, Metadata, SpeechStarted y UtteranceEnd. Solo
        # Results lleva transcripcion; los demas tienen otra forma y romperian el acceso.
        if getattr(msg, "type", None) != "Results":
            return None
        channel = getattr(msg, "channel", None)
        alternatives = getattr(channel, "alternatives", None)
        if not alternatives:
            return None
        alt = alternatives[0]
        transcript = alt.transcript or ""
        if not transcript.strip():
            return None
        # Deepgram reinicia sus timestamps en cada socket. Sumar el desplazamiento de
        # la conexion los devuelve a una linea temporal continua entre reconexiones.
        off = self._conn_offset
        words = [
            Word(
                text=w.punctuated_word or w.word,
                start=float(w.start) + off,
                end=float(w.end) + off,
                confidence=float(w.confidence),
            )
            for w in (alt.words or [])
        ]
        return TranscriptEvent(
            text=transcript,
            lang=self.lang,
            is_final=bool(getattr(msg, "is_final", False)),
            speech_final=bool(getattr(msg, "speech_final", False)),
            words=words,
            t_emit=round(time.monotonic(), 3),
        )

    async def stream(
        self, frames: AsyncIterator[SpeechFrame]
    ) -> AsyncIterator[TranscriptEvent]:
        """Streaming con reconexion automatica y buffer de audio (F6, riesgo R5).

        Tres piezas cooperando:

        - un colector consume `frames` sin parar, tambien mientras el socket esta caido,
          para que el microfono no se atasque;
        - un buffer acotado guarda ese audio, descartando lo mas viejo si el corte se
          alarga, porque en tiempo real el audio viejo ya no sirve;
        - una ventana de reenvio conserva el audio posterior al ultimo `is_final`, y se
          reinyecta al reconectar para no perder el segmento que estaba a medias.
        """
        from deepgram import AsyncDeepgramClient

        client = AsyncDeepgramClient(api_key=self._api_key)
        sample_rate = 16_000

        pending: deque[SpeechFrame] = deque()
        replay: deque[SpeechFrame] = deque()
        arrived = asyncio.Event()
        source_done = False

        def buffer_seconds(q: deque[SpeechFrame]) -> float:
            return sum(f.pcm.size for f in q) / sample_rate

        async def collect() -> None:
            nonlocal source_done
            try:
                async for f in frames:
                    pending.append(f)
                    self.frames_buffered += 1
                    while buffer_seconds(pending) > WS_BUFFER_S:
                        pending.popleft()
                        self.frames_dropped_overflow += 1
                    arrived.set()
            finally:
                source_done = True
                arrived.set()

        collector = asyncio.create_task(collect())
        attempt = 0
        # Instante en que se perdio el socket. Se cierra al reconectar, no al abrir:
        # medirlo desde la apertura sumaba la vida entera de la conexion.
        down_since: float | None = None
        self.t_stream_start = time.monotonic()

        try:
            while not (source_done and not pending):
                now = time.monotonic()
                if now < self._blocked_until:
                    await asyncio.sleep(min(0.2, self._blocked_until - now))
                    continue

                try:
                    async with client.listen.v1.connect(
                        model=MODEL,
                        language=self.deepgram_code,
                        encoding="linear16",
                        sample_rate=sample_rate,
                        channels=1,
                        interim_results=True,
                        punctuate=True,
                        smart_format=True,
                        endpointing=DG_ENDPOINTING_MS,
                        utterance_end_ms=DG_UTTERANCE_END_MS,
                        vad_events=True,
                    ) as conn:
                        self.connected = True
                        if down_since is not None:
                            self.downtime_s += time.monotonic() - down_since
                            down_since = None
                        attempt = 0
                        self._conn_offset = self._sent_total
                        if replay:
                            # Se reinyecta lo no confirmado antes que el audio nuevo.
                            pending.extendleft(reversed(replay))
                            self.frames_replayed += len(replay)
                            logger.info(
                                "reconnected, replaying %.2fs of unconfirmed audio",
                                buffer_seconds(replay),
                            )
                            replay.clear()
                        logger.info(
                            "deepgram ws open: model=%s language=%s offset=%.2fs",
                            MODEL,
                            self.deepgram_code,
                            self._conn_offset,
                        )

                        async def pump() -> None:
                            while True:
                                if not pending:
                                    if source_done:
                                        with contextlib.suppress(Exception):
                                            await conn.send_close_stream()
                                        return
                                    arrived.clear()
                                    try:
                                        await asyncio.wait_for(
                                            arrived.wait(), timeout=WS_KEEPALIVE_INTERVAL_S
                                        )
                                    except TimeoutError:
                                        # Silencio real, no un corte: nada que mandar
                                        # salvo el KeepAlive. Si esto fallara, el error
                                        # sale por el `except Exception` de mas abajo,
                                        # que ya reconecta con backoff -- no se traga.
                                        await conn.send_keep_alive()
                                        self.keepalives_sent += 1
                                    continue
                                f = pending.popleft()
                                await conn.send_media(f.pcm.tobytes())
                                self._sent_s.append(self._sent_total)
                                self._capture_s.append(f.t_capture)
                                self._sent_total += f.pcm.size / f.sample_rate
                                replay.append(f)
                                while buffer_seconds(replay) > WS_REPLAY_S:
                                    replay.popleft()

                        pumper = asyncio.create_task(pump())
                        try:
                            async for msg in conn:
                                if time.monotonic() < self._blocked_until:
                                    raise TranscriptionError("simulated drop")
                                ev = self._to_event(msg)
                                if ev is None:
                                    continue
                                if ev.is_final:
                                    self.finals += 1
                                    # Confirmado: ya no hay que reenviarlo.
                                    replay.clear()
                                else:
                                    self.partials += 1
                                yield ev
                        finally:
                            pumper.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await pumper
                    if source_done and not pending:
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.connected = False
                    if down_since is None:
                        down_since = time.monotonic()
                    if source_done and not pending:
                        break
                    attempt += 1
                    self.reconnects += 1
                    if attempt > WS_MAX_RETRIES:
                        raise TranscriptionError(
                            f"deepgram ws failed after {WS_MAX_RETRIES} reconnects: {e}"
                        ) from e
                    delay = min(WS_BACKOFF_BASE_S * 2 ** (attempt - 1), WS_BACKOFF_MAX_S)
                    logger.warning(
                        "deepgram ws down (%s); reconnect %d/%d in %.1fs, "
                        "%.2fs buffered, %.2fs to replay",
                        e,
                        attempt,
                        WS_MAX_RETRIES,
                        delay,
                        buffer_seconds(pending),
                        buffer_seconds(replay),
                    )
                    await asyncio.sleep(delay)
        finally:
            self.connected = False
            collector.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await collector
