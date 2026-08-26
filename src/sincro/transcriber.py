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
        # (segundos de audio enviados, t_capture de ese frame). El gate descarta silencio
        # y frames muteados, asi que el reloj de Deepgram, que cuenta audio recibido, va
        # mas lento que el de pared. Sin este mapeo el TTFA sale inflado.
        self._sent_s: list[float] = []
        self._capture_s: list[float] = []

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
        words = [
            Word(
                text=w.punctuated_word or w.word,
                start=float(w.start),
                end=float(w.end),
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
        """Abre el WebSocket y multiplexa envio de audio y recepcion de eventos.

        La reconexion con buffer es F6 (R5). Aqui se reintenta la apertura con backoff,
        pero una caida a mitad de sesion todavia termina el stream.
        """
        from deepgram import AsyncDeepgramClient

        client = AsyncDeepgramClient(api_key=self._api_key)
        sample_rate = 16_000

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
            self.t_stream_start = time.monotonic()
            logger.info(
                "deepgram ws open: model=%s language=%s endpointing=%dms",
                MODEL,
                self.deepgram_code,
                DG_ENDPOINTING_MS,
            )

            sent = 0.0

            async def pump_audio() -> None:
                nonlocal sent
                try:
                    async for f in frames:
                        self._sent_s.append(sent)
                        self._capture_s.append(f.t_capture)
                        sent += f.pcm.size / f.sample_rate
                        await conn.send_media(f.pcm.tobytes())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("audio pump failed")
                finally:
                    with contextlib.suppress(Exception):
                        await conn.send_close_stream()

            pump = asyncio.create_task(pump_audio())
            try:
                async for msg in conn:
                    ev = self._to_event(msg)
                    if ev is None:
                        continue
                    if ev.is_final:
                        self.finals += 1
                    else:
                        self.partials += 1
                    yield ev
            finally:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
