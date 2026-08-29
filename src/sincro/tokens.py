"""S1 - tokens firmados de un solo uso (contrato de conexion, seccion 8).

Emitidos por el Dispatcher en `POST /v1/sessions`, validados por el Worker en `hello`.
Llevan `session_id`, `user_id`, `lang` y `reference_id`. **El idioma viaja aqui, no en
el hello**: es la unica forma de que el cliente no pueda cambiar de idioma a mitad de
sesion (decision de diseno desde v3; D52 dejo esto anotado como deuda de G2, cerrada
en este archivo + el cambio correspondiente en `call_serve.py`).

Firma HMAC-SHA256 (JWT via PyJWT, ya dependencia transitiva). Dispatcher y Worker son
procesos distintos en produccion (Container Apps separadas, ver ACLARACION en
DECISIONS.md), asi que el secreto viaja por variable de entorno compartida, no por
memoria de proceso.

De un solo uso: el Worker lleva su propio registro de `jti` ya canjeados (`TokenStore`)
y lo marca usado en el momento en que ACEPTA la conexion en `hello`, no antes -- si la
validacion fallara a medio camino por otra razon, no se quema un token que el cliente
podria necesitar reintentar.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Final

import jwt

from .contracts import Lang

TOKEN_TTL_S: Final[int] = 60  # contrato, seccion 8
_ALGORITHM: Final[str] = "HS256"
_ENV_SECRET: Final[str] = "SINCRO_TOKEN_SECRET"


class TokenError(ValueError):
    pass


def _secret() -> str:
    secret = os.getenv(_ENV_SECRET)
    if not secret:
        raise TokenError(
            f"{_ENV_SECRET} no esta configurado: Dispatcher y Worker necesitan el "
            "mismo secreto para firmar y validar tokens"
        )
    return secret


@dataclass(frozen=True)
class TokenClaims:
    session_id: str
    user_id: str
    lang: Lang
    reference_id: str
    jti: str


def issue_token(session_id: str, user_id: str, lang: Lang, reference_id: str) -> str:
    now = int(time.time())
    payload = {
        "session_id": session_id,
        "user_id": user_id,
        "lang": lang,
        "reference_id": reference_id,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + TOKEN_TTL_S,
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def decode_token(token: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expirado") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"token invalido: {exc}") from exc
    try:
        lang = payload["lang"]
        if lang not in ("es", "en", "pt-BR", "fr", "ja"):
            raise TokenError(f"token con lang invalido: {lang!r}")
        return TokenClaims(
            session_id=payload["session_id"],
            user_id=payload["user_id"],
            lang=lang,
            reference_id=payload.get("reference_id", ""),
            jti=payload["jti"],
        )
    except KeyError as exc:
        raise TokenError(f"token sin campo obligatorio: {exc}") from exc


class TokenStore:
    """Registro de tokens ya canjeados, en memoria, del lado del Worker.

    Un solo proceso Worker por ahora (G2). Si G4 reparte llamadas entre varios workers,
    esto necesita un backend compartido (Redis) para que un token no se pueda reusar
    contra un worker distinto al que lo canjeo primero -- anotado, no resuelto aqui.
    """

    def __init__(self) -> None:
        self._used: set[str] = set()

    def redeem(self, claims: TokenClaims) -> None:
        if claims.jti in self._used:
            raise TokenError(f"token ya usado: jti={claims.jti}")
        self._used.add(claims.jti)
