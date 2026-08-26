"""M2 - AudioGate. Silero VAD, endpointing semantico y puerta dura anti-eco.

Dos responsabilidades que el documento tecnico agrupa en M2:

- **VAD**: decide que frames contienen habla. Recorta el audio que se manda a Deepgram,
  que es lo que hace que el coste de STT sea por minuto de habla y no de reloj.
- **Endpointing**: el turn-detector multilingue puntua si un texto cierra turno. Es un
  modelo de TEXTO, no de audio, asi que aqui se expone como servicio y quien lo consume
  es M4, que es donde hay transcripcion. El modelo lo carga M2 porque el documento le
  asigna el endpointing.

La puerta dura es la segunda defensa anti-eco y no es opcional: mientras el sintetizador
reproduce, el microfono no entra. Nunca desactivarla "porque tengo auriculares".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, Final

from livekit.agents import vad as lkvad
from livekit.plugins import silero

# Runner local del turn-detector. Se instancia directo porque la clase publica
# MultilingualModel exige un inference_executor del agent worker, que no usamos. Ver D14.
from livekit.plugins.turn_detector.multilingual import _EUORunnerMultilingual

from .adapters.lk_frames import to_lk_frame
from .contracts import LanguageProfile, SpeechFrame

logger = logging.getLogger(__name__)

# Minimo exigido por el turn-detector de audio. Bajarlo corta al hablante a media frase.
MIN_SILENCE_FLOOR: Final[float] = 0.25
DEFAULT_MIN_SILENCE: Final[float] = 0.55
DEFAULT_ACTIVATION: Final[float] = 0.5

# Guarda anti-eco tras terminar la reproduccion. Documentacion tecnica y CLAUDE.md.
UNMUTE_GUARD_S: Final[float] = 0.150

# Frames de 20 ms que se retienen para no cortar el ataque de la primera palabra: el VAD
# decide a posteriori, asi que sin este colchon se pierde el inicio de cada intervencion.
PREFIX_FRAMES: Final[int] = 12
# Cola tras END_OF_SPEECH, para no cortar consonantes finales sordas.
HANGOVER_FRAMES: Final[int] = 8

# Umbrales de fin de turno por idioma, medidos por LiveKit (languages.json del modelo).
EOU_THRESHOLD: Final[dict[str, float]] = {
    "es": 0.0058,
    "en": 0.0110,
    "pt": 0.0069,
    "fr": 0.0078,
    "ja": 0.0096,
}
EOU_THRESHOLD_FALLBACK: Final[float] = 0.0100


class SileroGate:
    """Implementa el Protocol AudioGate."""

    def __init__(
        self,
        profile: LanguageProfile,
        min_silence_duration: float = DEFAULT_MIN_SILENCE,
        activation_threshold: float = DEFAULT_ACTIVATION,
    ) -> None:
        if min_silence_duration < MIN_SILENCE_FLOOR:
            raise ValueError(
                f"min_silence_duration={min_silence_duration} is below the {MIN_SILENCE_FLOOR}s "
                "floor required by the audio turn detector"
            )
        self.profile = profile
        self.min_silence_duration = min_silence_duration
        self.activation_threshold = activation_threshold

        self._vad = silero.VAD.load(
            min_silence_duration=min_silence_duration,
            activation_threshold=activation_threshold,
            sample_rate=16_000,
            force_cpu=True,
        )
        self._eou = _EUORunnerMultilingual()
        self._eou_ready = False

        self.muted = False
        self._unmuted = asyncio.Event()
        self._unmuted.set()
        self.mute_calls = 0
        self.unmute_calls = 0
        self.frames_dropped_muted = 0
        self.frames_dropped_silence = 0
        self.frames_forwarded = 0
        self.speech_events = 0
        self._speaking = False
        self._t_last_speech = 0.0

    # ---- endpointing (M4 lo consume) ----

    def load_eou(self) -> None:
        """Carga ONNX y tokenizer. Tarda unos segundos: se hace antes de abrir el micro."""
        if not self._eou_ready:
            t0 = time.monotonic()
            self._eou.initialize()
            self._eou_ready = True
            logger.info("turn detector loaded in %.2fs (local, CPU)", time.monotonic() - t0)

    @property
    def eou_threshold(self) -> float:
        return EOU_THRESHOLD.get(self.profile.turn_detector_code, EOU_THRESHOLD_FALLBACK)

    def _run_eou(self, text: str) -> float:
        payload = json.dumps({"chat_ctx": [{"role": "user", "content": text}]}).encode()
        raw = self._eou.run(payload)
        if raw is None:
            return 0.0
        result: dict[str, Any] = json.loads(raw.decode())
        return float(result.get("eou_probability", 0.0))

    async def predict_eou(self, text: str) -> float:
        """Probabilidad de fin de turno. La inferencia es CPU pura: va a un hilo para no
        bloquear el bucle de eventos mientras el microfono sigue entrando."""
        if not text.strip():
            return 0.0
        self.load_eou()
        return await asyncio.to_thread(self._run_eou, text)

    # ---- puerta dura anti-eco ----

    def mute(self) -> None:
        self.muted = True
        self._unmuted.clear()
        self.mute_calls += 1

    def unmute(self) -> None:
        self.muted = False
        self._unmuted.set()
        self.unmute_calls += 1

    async def wait_unmuted(self) -> None:
        await self._unmuted.wait()

    async def unmute_after_guard(self, guard_s: float = UNMUTE_GUARD_S) -> None:
        """El altavoz tiene cola: la sala sigue sonando despues del ultimo frame."""
        await asyncio.sleep(guard_s)
        self.unmute()

    # ---- VAD ----

    async def process(
        self, frames: AsyncIterator[SpeechFrame]
    ) -> AsyncIterator[SpeechFrame]:
        vad_stream = self._vad.stream()
        prefix: deque[SpeechFrame] = deque(maxlen=PREFIX_FRAMES)
        hangover = 0

        async def watch_vad() -> None:
            async for ev in vad_stream:
                if ev.type == lkvad.VADEventType.START_OF_SPEECH:
                    self._speaking = True
                    self.speech_events += 1
                elif ev.type == lkvad.VADEventType.END_OF_SPEECH:
                    self._speaking = False
                    self._t_last_speech = time.monotonic()

        watcher = asyncio.create_task(watch_vad())
        try:
            async for frame in frames:
                if self.muted:
                    # Puerta dura: el frame no entra ni al VAD. Si entrara, el VAD
                    # marcaria habla sobre nuestra propia salida.
                    self.frames_dropped_muted += 1
                    prefix.clear()
                    continue

                vad_stream.push_frame(to_lk_frame(frame))

                if self._speaking:
                    if prefix:
                        for buffered in prefix:
                            self.frames_forwarded += 1
                            yield buffered
                        prefix.clear()
                    hangover = HANGOVER_FRAMES
                    self.frames_forwarded += 1
                    yield frame
                elif hangover > 0:
                    hangover -= 1
                    self.frames_forwarded += 1
                    yield frame
                else:
                    prefix.append(frame)
                    self.frames_dropped_silence += 1
        finally:
            watcher.cancel()
            vad_stream.end_input()
            await vad_stream.aclose()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "forwarded": self.frames_forwarded,
            "dropped_muted": self.frames_dropped_muted,
            "dropped_silence": self.frames_dropped_silence,
            "speech_events": self.speech_events,
            "mute_calls": self.mute_calls,
            "unmute_calls": self.unmute_calls,
        }
