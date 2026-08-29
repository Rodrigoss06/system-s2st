"""S1 - Dispatcher HTTP (contrato de conexion, seccion 4).

Servicio aparte del Worker (ver ACLARACION en DECISIONS.md, 2026-08-29): el contrato
describe dos Container Apps distintas. Este archivo es el Dispatcher -- FastAPI, para
que `POST`/`DELETE` con cuerpo JSON funcionen y para generar el OpenAPI ejecutable que
pide la tarea. El Worker (`call_serve.py`, `websockets.serve()` crudo) no cambia de
libreria: eso ya se decidio y no se revisita aqui.

Rutas: `POST /v1/sessions`, `DELETE /v1/sessions/{id}`, `POST /v1/voices`,
`GET /v1/voices/{user_id}`. `GET /healthz` y `GET /readyz` NO estan aqui: son del
Worker, GET puro, van por `process_request` en `call_serve.py`.

Para G2 hay un solo Worker fijo (`SINCRO_WORKER_WS_URL`): asignar entre varios workers
y llevar el cupo de slots es S3/G4, no esto.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Annotated, Final

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import metrics
from .config import SUPPORTED_LANGS
from .contracts import Lang
from .tokens import TOKEN_TTL_S, issue_token
from .voice_repository import ConsentError, VoiceRecord, VoiceRepository
from .voices import EnrollmentError, FishVoiceRegistry

DEFAULT_WORKER_WS_URL: Final[str] = "ws://localhost:8080/v1/stream"


def _worker_ws_url() -> str:
    return os.getenv("SINCRO_WORKER_WS_URL", DEFAULT_WORKER_WS_URL)


class ParticipantIn(BaseModel):
    user_id: str
    lang: Lang
    reference_id: str = ""


class SessionsIn(BaseModel):
    participants: list[ParticipantIn] = Field(min_length=2, max_length=2)


class SessionsOut(BaseModel):
    session_id: str
    ws_url: str
    tokens: dict[str, str]
    expires_in: int


class VoiceOut(BaseModel):
    user_id: str
    lang: Lang
    reference_id: str
    consent_at: str


class VoicesOut(BaseModel):
    user_id: str
    voices: list[VoiceOut]


def create_app(fish_api_key: str, db_path: str = "out/voices.db") -> FastAPI:
    """Fabrica en vez de un `app` a nivel de modulo: `make api-test` necesita una base
    de datos de prueba propia y no pisar el estado de otra corrida."""
    app = FastAPI(
        title="SINCRO Dispatcher",
        version="1.0.0",
        description="Contrato de conexion v1 -- crea llamadas y enrola timbres. "
        "El WebSocket de audio y health/ready viven en el Worker, no aqui.",
    )
    registry = FishVoiceRegistry(fish_api_key)
    repo = VoiceRepository(registry, db_path=db_path)
    sessions: dict[str, dict[str, object]] = {}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sessions", status_code=201, response_model=SessionsOut)
    async def create_session(body: SessionsIn) -> SessionsOut:
        session_id = f"s_{uuid.uuid4().hex[:12]}"
        tokens = {
            p.user_id: issue_token(session_id, p.user_id, p.lang, p.reference_id)
            for p in body.participants
        }
        sessions[session_id] = {
            "status": "pending",
            "created_at": time.time(),
            "participants": [p.user_id for p in body.participants],
        }
        metrics.dispatcher_sessions_created_total.inc()
        return SessionsOut(
            session_id=session_id,
            ws_url=_worker_ws_url(),
            tokens=tokens,
            expires_in=TOKEN_TTL_S,
        )

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str) -> None:
        if session_id not in sessions:
            raise HTTPException(status_code=404, detail="session not found")
        # Libera el registro del Dispatcher. Los tokens ya emitidos son de un solo uso
        # y expiran solos en TOKEN_TTL_S; si alguno ya se canjeo, la llamada ya esta
        # corriendo en el Worker, y cortarla desde aca necesitaria un canal
        # Dispatcher->Worker que todavia no existe (S3, junto con el dispatcher real
        # de N workers). Anotado, no resuelto en G2.
        del sessions[session_id]
        metrics.dispatcher_sessions_deleted_total.inc()

    @app.post("/v1/voices", status_code=201, response_model=VoiceOut)
    async def enroll_voice(
        user_id: Annotated[str, Form()],
        lang: Annotated[str, Form()],
        audio: Annotated[UploadFile, File()],
        consent: Annotated[bool, Form()] = False,
        consent_at: Annotated[str, Form()] = "",
    ) -> VoiceOut:
        if lang not in SUPPORTED_LANGS:
            metrics.dispatcher_voices_rejected_total.inc()
            raise HTTPException(status_code=400, detail=f"lang invalido: {lang!r}")
        if not consent_at:
            consent_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        audio_bytes = await audio.read()
        try:
            record: VoiceRecord = await repo.enroll(
                user_id=user_id,
                lang=lang,
                audio=audio_bytes,
                filename=audio.filename or "voice.wav",
                consent=consent,
                consent_at=consent_at,
            )
        except ConsentError as exc:
            metrics.dispatcher_voices_rejected_total.inc()
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except EnrollmentError as exc:
            metrics.dispatcher_voices_rejected_total.inc()
            raise HTTPException(status_code=502, detail=f"fish enrollment failed: {exc}") from None
        metrics.dispatcher_voices_enrolled_total.inc()
        return VoiceOut(
            user_id=record.user_id,
            lang=record.lang,
            reference_id=record.reference_id,
            consent_at=record.consent_at,
        )

    @app.get("/v1/voices/{user_id}", response_model=VoicesOut)
    async def get_voices(user_id: str) -> VoicesOut:
        records = repo.get(user_id)
        if not records:
            raise HTTPException(status_code=404, detail="no voice registered for this user")
        return VoicesOut(
            user_id=user_id,
            voices=[
                VoiceOut(
                    user_id=r.user_id, lang=r.lang, reference_id=r.reference_id,
                    consent_at=r.consent_at,
                )
                for r in records
            ],
        )

    app.state.repo = repo
    app.state.sessions = sessions
    return app


if __name__ == "__main__":
    import argparse
    import os
    import sys

    import uvicorn

    from .config import load_settings

    ap = argparse.ArgumentParser(
        prog="dispatcher", description="Dispatcher HTTP (contrato de conexion, seccion 4)"
    )
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    s = load_settings()
    db_path = os.getenv("SINCRO_VOICES_DB", "out/voices.db")
    app = create_app(s.fish_api_key, db_path=db_path)
    print(f"Dispatcher listening on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
