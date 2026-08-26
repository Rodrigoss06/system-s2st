"""M7 - Synthesizer. Fish Audio via livekit-plugins-fishaudio.

En F1 se usa la voz por defecto de Fish: la clonacion de timbre es F3. `speed` se acota
al rango que no degrada el timbre; el valor que se pasa lo decide M8 a partir de F4.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Final, Literal

import aiohttp
import numpy as np
from livekit.plugins import fishaudio

from .contracts import DubbedChunk, Translation

logger = logging.getLogger(__name__)

# Fish acepta 0.5 a 2.0, pero fuera de este rango el timbre clonado se degrada de forma
# audible. Documentacion tecnica, seccion 6.
SPEED_MIN: Final[float] = 0.95
SPEED_MAX: Final[float] = 1.25

DEV_MODEL: Final[str] = "s2.1-pro-free"
# Para tiempo real, balanced. Documentacion tecnica, seccion 11.
LATENCY_MODE: Final[Literal["normal", "balanced", "low"]] = "balanced"
CHUNK_LENGTH: Final[int] = 200


class SynthesisError(RuntimeError):
    pass


def clamp_speed(speed: float) -> float:
    return max(SPEED_MIN, min(SPEED_MAX, speed))


class FishSynthesizer:
    """Implementa el Protocol Synthesizer.

    No importa livekit.rtc: convierte el AudioFrame del plugin a np.ndarray en la
    frontera, de modo que ningun tipo de transporte entra en los contratos.
    """

    def __init__(self, api_key: str, model: str = DEV_MODEL, sample_rate: int = 44_100) -> None:
        if not api_key:
            raise SynthesisError("FISH_API_KEY is empty")
        self._api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.t_first_byte = 0.0
        self.requests = 0
        # Fuera del agent worker de LiveKit el plugin no tiene sesion HTTP: hay que
        # darle una y gestionar su ciclo de vida. Es la via soportada para uso standalone.
        self._session: aiohttp.ClientSession | None = None
        self._tts_instance: fishaudio.TTS | None = None

    def _tts(self, reference_id: str, speed: float) -> fishaudio.TTS:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        if self._tts_instance is None:
            self._tts_instance = fishaudio.TTS(
                api_key=self._api_key,
                model=self.model,
                latency_mode=LATENCY_MODE,
                chunk_length=CHUNK_LENGTH,
                sample_rate=self.sample_rate,
                speed=speed,
                http_session=self._session,
            )
        # Sin reference_id se usa la voz por defecto del plugin. F3 lo puebla.
        if reference_id:
            self._tts_instance.update_options(voice_id=reference_id, speed=speed)
        else:
            self._tts_instance.update_options(speed=speed)
        return self._tts_instance

    async def aclose(self) -> None:
        if self._tts_instance is not None:
            await self._tts_instance.aclose()
            self._tts_instance = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def synthesize_stream(
        self, tr: Translation, reference_id: str, speed: float
    ) -> AsyncIterator[DubbedChunk]:
        """F2. WebSocket de Fish con latency balanced: emite chunks segun se generan.

        `synthesize()` espera a que el texto entero este listo antes de mandarlo; este
        camino empuja el texto al socket y consume audio en cuanto llega, que es lo que
        recorta el TTFB del presupuesto de latencia.
        """
        if not tr.text.strip():
            logger.info("seg %d has empty text, skipping synthesis", tr.seg_id)
            return

        applied = clamp_speed(speed)
        tts = self._tts(reference_id, applied)
        self.requests += 1
        first = True
        stream = tts.stream()
        try:
            stream.push_text(tr.text)
            stream.flush()
            stream.end_input()
            async for audio in stream:
                frame = audio.frame
                pcm = np.frombuffer(frame.data, dtype=np.int16)
                if frame.num_channels > 1:
                    pcm = pcm.reshape(-1, frame.num_channels)[:, 0]
                if pcm.size == 0:
                    continue
                if first:
                    self.t_first_byte = round(time.monotonic(), 3)
                    first = False
                yield DubbedChunk(
                    seg_id=tr.seg_id,
                    pcm=pcm,
                    sample_rate=frame.sample_rate,
                    speed_applied=applied,
                    audio_duration=pcm.size / frame.sample_rate,
                )
        except Exception as e:
            raise SynthesisError(f"fish tts stream failed for seg {tr.seg_id}: {e}") from e
        finally:
            await stream.aclose()

    async def synthesize(
        self, tr: Translation, reference_id: str, speed: float
    ) -> AsyncIterator[DubbedChunk]:
        if not tr.text.strip():
            # Segmento vaciado por el token de escape de M5: no hay nada que sintetizar
            # y llamar a Fish con texto vacio gasta una peticion para nada.
            logger.info("seg %d has empty text, skipping synthesis", tr.seg_id)
            return

        applied = clamp_speed(speed)
        if applied != speed:
            logger.warning("seg %d: speed %.3f clamped to %.3f", tr.seg_id, speed, applied)

        tts = self._tts(reference_id, applied)
        self.requests += 1
        first = True
        try:
            stream = tts.synthesize(tr.text)
            async for audio in stream:
                frame = audio.frame
                pcm = np.frombuffer(frame.data, dtype=np.int16)
                if frame.num_channels > 1:
                    pcm = pcm.reshape(-1, frame.num_channels)[:, 0]
                if pcm.size == 0:
                    continue
                if first:
                    self.t_first_byte = round(time.monotonic(), 3)
                    first = False
                yield DubbedChunk(
                    seg_id=tr.seg_id,
                    pcm=pcm,
                    sample_rate=frame.sample_rate,
                    speed_applied=applied,
                    audio_duration=pcm.size / frame.sample_rate,
                )
        except Exception as e:
            raise SynthesisError(f"fish tts failed for seg {tr.seg_id}: {e}") from e
