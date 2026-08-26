"""M6 - VoiceRegistry. Enrolamiento de timbre contra Fish Audio.

El clip de referencia es **dato biometrico**. Tres reglas que no se relajan:

1. El `reference_id` se cachea en memoria del proceso. Nunca toca disco.
2. Los bytes del WAV no se guardan en ningun atributo: se leen, se suben y se sueltan.
3. Subirlo exige consentimiento explicito del hablante. El paso de captura del clip es
   tambien el paso de consentimiento.

El modelo se crea con `visibility=private`. En el plugin de LiveKit el parametro se llama
`voice_id`; en la API de Fish es `reference_id`. Es el mismo valor.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

FISH_MODEL_URL: Final[str] = "https://api.fish.audio/model"
TIMEOUT_S: Final[float] = 120.0
MAX_RETRIES: Final[int] = 3
BACKOFF_BASE_S: Final[float] = 1.0

# El documento tecnico pide 15 a 20 s. Por debajo de 10 s el timbre sale pobre; por
# encima de 60 s Fish no mejora y la subida se alarga.
MIN_CLIP_S: Final[float] = 10.0
MAX_CLIP_S: Final[float] = 60.0

CONSENT_NOTICE: Final[str] = """
================================================================================
  CONSENTIMIENTO - HUELLA VOCAL

  Se va a subir un clip de voz a Fish Audio para crear un modelo de timbre.

    clip     : {path}
    duracion : {duration:.1f} s
    destino  : api.fish.audio  (servidor externo, fuera de esta maquina)
    modelo   : privado, asociado a tu cuenta de Fish

  La huella vocal es dato biometrico. Con ella se puede sintetizar habla que
  suena como el hablante diciendo cosas que nunca dijo.

  Sube el clip solo si eres el hablante, o si tienes su permiso explicito.

  El reference_id se guarda solo en memoria de este proceso y se pierde al salir.
  El clip NO se copia a disco por el motor.
================================================================================
"""


class EnrollmentError(RuntimeError):
    pass


def clip_duration(wav_path: str | Path) -> float:
    import soundfile as sf

    return float(sf.info(str(wav_path)).duration)


def consent_prompt(wav_path: str | Path, interactive: bool = True) -> bool:
    """Devuelve False si no hay consentimiento. Se llama ANTES de leer el clip."""
    p = Path(wav_path)
    if not p.is_file():
        raise EnrollmentError(f"reference clip not found: {p}")
    duration = clip_duration(p)
    print(CONSENT_NOTICE.format(path=p, duration=duration))

    if duration < MIN_CLIP_S:
        print(f"  AVISO: el clip dura {duration:.1f} s; el documento pide 15 a 20 s.")
        print("         Con menos de 10 s el timbre clonado sale pobre.")
    elif duration > MAX_CLIP_S:
        print(f"  AVISO: el clip dura {duration:.1f} s; por encima de 60 s no mejora.")

    if not interactive:
        logger.warning("consent prompt skipped by flag")
        return True
    try:
        answer = input("  Autorizas subir este clip? [s/N] ").strip().lower()
    except EOFError:
        return False
    if answer not in ("s", "si", "sí", "y", "yes"):
        print("  Enrolamiento cancelado. No se subio nada.")
        return False
    return True


class FishVoiceRegistry:
    """Implementa el Protocol VoiceRegistry."""

    def __init__(self, api_key: str, title_prefix: str = "sincro") -> None:
        if not api_key:
            raise EnrollmentError("FISH_API_KEY is empty")
        self._api_key = api_key
        self.title_prefix = title_prefix
        # speaker_id -> reference_id. Solo en memoria: se pierde al salir del proceso,
        # que es justo lo que se quiere con un dato biometrico.
        self._cache: dict[str, str] = {}

    def get(self, speaker_id: str) -> str | None:
        return self._cache.get(speaker_id)

    def remember(self, speaker_id: str, reference_id: str) -> None:
        """Registra un reference_id ya existente sin volver a subir el clip."""
        self._cache[speaker_id] = reference_id

    def forget(self, speaker_id: str) -> None:
        self._cache.pop(speaker_id, None)

    async def _post_model(self, title: str, audio: bytes, filename: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        data = {
            "title": title,
            "type": "tts",
            "train_mode": "fast",
            "visibility": "private",
        }
        last: Exception | None = None
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            for attempt in range(MAX_RETRIES):
                try:
                    r = await client.post(
                        FISH_MODEL_URL,
                        headers=headers,
                        data=data,
                        files={"voices": (filename, audio, "audio/wav")},
                    )
                    if r.status_code in (200, 201):
                        payload: dict[str, Any] = r.json()
                        return payload
                    if r.status_code < 500 and r.status_code != 429:
                        raise EnrollmentError(
                            f"fish rejected the enrollment: HTTP {r.status_code} {r.text[:200]}"
                        )
                    last = EnrollmentError(f"fish HTTP {r.status_code}: {r.text[:200]}")
                except httpx.HTTPError as e:
                    last = e
                delay = BACKOFF_BASE_S * 2**attempt
                logger.warning(
                    "fish enroll attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    last,
                    delay,
                )
                await asyncio.sleep(delay)
        raise EnrollmentError(f"fish enrollment failed after {MAX_RETRIES} attempts: {last}")

    async def enroll(self, speaker_id: str, wav_path: str) -> str:
        """Sube el clip y devuelve el reference_id. El consentimiento se pide ANTES,
        en `consent_prompt`, no aqui: este metodo ya asume autorizacion."""
        cached = self._cache.get(speaker_id)
        if cached:
            logger.info("speaker %r already enrolled: %s", speaker_id, cached)
            return cached

        p = Path(wav_path)
        if not await asyncio.to_thread(p.is_file):
            raise EnrollmentError(f"reference clip not found: {p}")

        # Los bytes viven en una local y mueren al salir del metodo. No se guardan.
        # La lectura va a un hilo: bloquear el bucle aqui congelaria el pipeline.
        audio = await asyncio.to_thread(p.read_bytes)
        payload = await self._post_model(f"{self.title_prefix}-{speaker_id}", audio, p.name)
        del audio

        reference_id = payload.get("_id") or payload.get("id")
        if not reference_id:
            raise EnrollmentError(f"fish returned no model id: {str(payload)[:200]}")
        state = payload.get("state")
        if state not in (None, "trained"):
            logger.warning("fish model %s state=%r, expected 'trained'", reference_id, state)

        self._cache[speaker_id] = str(reference_id)
        logger.info("enrolled speaker %r -> %s (state=%s)", speaker_id, reference_id, state)
        return str(reference_id)

    async def delete(self, reference_id: str) -> bool:
        """Borra el modelo en Fish. La huella no debe quedar viva mas de lo necesario."""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.delete(f"{FISH_MODEL_URL}/{reference_id}", headers=headers)
        ok = r.status_code in (200, 204)
        if ok:
            for k, v in list(self._cache.items()):
                if v == reference_id:
                    del self._cache[k]
        else:
            logger.warning("could not delete model %s: HTTP %d", reference_id, r.status_code)
        return ok
