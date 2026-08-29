"""S2 - VoiceRepository con persistencia (contrato, seccion 2 y 6).

Crear el modelo de voz en Fish tarda segundos: no puede pasar al iniciar la llamada.
Se hace una vez en onboarding (`POST /v1/voices`) y se reutiliza consultando esta
tabla (`GET /v1/voices/{user_id}`, o el propio cliente antes de pedir una sesion).

Envuelve `FishVoiceRegistry` (M6, `voices.py`) sin modificarlo: la subida a Fish sigue
igual. Esto solo anade la fila persistente `{user_id, lang, reference_id, consent_at}`
en SQLite, porque el `reference_id` tiene que sobrevivir a un reinicio del proceso --
la cache en memoria de M6 (deliberada: el dato biometrico no debe sobrevivir al MOTOR)
no alcanza para el Dispatcher, que si necesita recordarlo entre despliegues.

El audio del enrolamiento por HTTP llega como bytes en la peticion, nunca como un
archivo en disco: se llama directo al metodo de bajo nivel de `FishVoiceRegistry`
(`_post_model`, ya sube bytes) en vez de a `enroll()` (que exige una ruta de archivo,
pensada para `make enroll` por consola). Es la misma regla de "no toca disco" de M6,
aplicada al camino HTTP.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .contracts import Lang
from .voices import EnrollmentError, FishVoiceRegistry

DEFAULT_DB_PATH: str = "out/voices.db"


class ConsentError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceRecord:
    user_id: str
    lang: Lang
    reference_id: str
    consent_at: str


class VoiceRepository:
    def __init__(self, registry: FishVoiceRegistry, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._registry = registry
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voices (
                user_id TEXT NOT NULL,
                lang TEXT NOT NULL,
                reference_id TEXT NOT NULL,
                consent_at TEXT NOT NULL,
                PRIMARY KEY (user_id, lang)
            )
            """
        )
        self._conn.commit()

    async def enroll(
        self,
        user_id: str,
        lang: Lang,
        audio: bytes,
        filename: str,
        consent: bool,
        consent_at: str,
    ) -> VoiceRecord:
        if not consent:
            # El 400 lo traduce la capa HTTP (dispatcher.py); esta excepcion es el
            # limite real: sin consentimiento, no hay subida, aqui ni mas adentro.
            raise ConsentError("consent es obligatorio: la huella vocal es dato biometrico")

        speaker_id = f"{user_id}-{lang}"
        cached = self._registry.get(speaker_id)
        if cached:
            reference_id = cached
        else:
            # Metodo "privado" de M6: no toca disco, sube bytes directo. Ver docstring.
            payload = await self._registry._post_model(
                f"{self._registry.title_prefix}-{speaker_id}", audio, filename
            )
            raw_id = payload.get("_id") or payload.get("id")
            if not raw_id:
                raise EnrollmentError(f"fish returned no model id: {str(payload)[:200]}")
            reference_id = str(raw_id)
            self._registry.remember(speaker_id, reference_id)

        self._conn.execute(
            "INSERT OR REPLACE INTO voices (user_id, lang, reference_id, consent_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, lang, reference_id, consent_at),
        )
        self._conn.commit()
        return VoiceRecord(
            user_id=user_id, lang=lang, reference_id=reference_id, consent_at=consent_at
        )

    def get(self, user_id: str, lang: Lang | None = None) -> list[VoiceRecord]:
        if lang is not None:
            cur = self._conn.execute(
                "SELECT user_id, lang, reference_id, consent_at FROM voices "
                "WHERE user_id = ? AND lang = ?",
                (user_id, lang),
            )
        else:
            cur = self._conn.execute(
                "SELECT user_id, lang, reference_id, consent_at FROM voices WHERE user_id = ?",
                (user_id,),
            )
        return [VoiceRecord(*row) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
