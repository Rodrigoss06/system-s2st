"""M1 - LanguageProfile y la tabla de los cinco idiomas.

Idioma declarado por variable de entorno. Sin autodeteccion, sin language=multi.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, get_args

from dotenv import load_dotenv

from .contracts import Formality, Lang, LanguageProfile

SUPPORTED_LANGS: Final[tuple[Lang, ...]] = get_args(Lang)

# Codigos por proveedor. Documentacion tecnica, seccion 3.
DEEPGRAM_CODE: Final[dict[Lang, str]] = {
    "es": "es",
    "en": "en-US",
    "pt-BR": "pt-BR",
    "fr": "fr",
    "ja": "ja",
}

TURN_DETECTOR_CODE: Final[dict[Lang, str]] = {
    "es": "es",
    "en": "en",
    "pt-BR": "pt",
    "fr": "fr",
    "ja": "ja",
}

LANG_NAME: Final[dict[Lang, str]] = {
    "es": "Spanish",
    "en": "English",
    "pt-BR": "Brazilian Portuguese",
    "fr": "French",
    "ja": "Japanese",
}

# Groq deprecio llama-3.3-70b-versatile el 17/06/2026 y dejo de servirlo en agosto.
# Todo lo del LLM se parametriza para que un cambio de modelo no exija tocar codigo (D8).
DEFAULT_LLM_MODEL: Final[str] = "qwen/qwen3.6-27b"

# qwen3.6-27b solo acepta "none" o "default". Con "default" el modelo emite un bloque
# <think> dentro del propio content y agota max_tokens. Traducir una clausula no necesita
# razonamiento, y esos tokens son latencia pura antes del TTS.
DEFAULT_LLM_REASONING_EFFORT: Final[str] = "none"
DEFAULT_LLM_TEMPERATURE: Final[float] = 0.2
# Red de seguridad: si algo reactiva el razonamiento, la generacion se corta en vez de
# comerse el presupuesto de latencia en silencio.
DEFAULT_LLM_MAX_TOKENS: Final[int] = 200

# El japones rompe la relacion bytes-duracion por los kanji: se controla solo por
# duracion medida. Documentacion tecnica, seccion 3.
BYTE_BUDGET_DISABLED: Final[float] = 0.0

# Factores medidos/declarados en el documento tecnico. Son primera aproximacion:
# F5 los recalibra midiendo duracion de audio real, que es la senal fiable.
EXPANSION_EXPLICIT: Final[dict[tuple[Lang, Lang], float]] = {
    ("en", "es"): 1.20,
    ("es", "en"): 0.85,
    ("es", "pt-BR"): 1.00,
    ("es", "fr"): 1.10,
}

# Factores medidos en F5 con make matrix-test, sobre duracion de audio real. Sustituyen
# a los teoricos donde existen. El archivo lo escribe `make matrix-test --apply`; si no
# existe, se usan los valores del documento.
CALIBRATION_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "out" / "expansion.json"


def _load_calibrated() -> dict[tuple[Lang, Lang], float]:
    if not CALIBRATION_PATH.is_file():
        return {}
    try:
        raw = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[tuple[Lang, Lang], float] = {}
    for key, value in raw.get("pairs", {}).items():
        src, _, dst = key.partition("->")
        if src in SUPPORTED_LANGS and dst in SUPPORTED_LANGS:
            out[(src, dst)] = float(value)
    return out


EXPANSION_MEASURED: Final[dict[tuple[Lang, Lang], float]] = _load_calibrated()

# Peso de verbosidad relativo, derivado de los factores explicitos, para los pares
# que el documento no tabula. Solo se usa si F5 aun no midio ese par.
_VERBOSITY: Final[dict[Lang, float]] = {
    "en": 1.00,
    "es": 1.20,
    "pt-BR": 1.20,
    "fr": 1.32,
}


def expansion_for(src: Lang, dst: Lang) -> float:
    """Factor de expansion del par. 0.0 significa: no usar presupuesto de bytes.

    Prioridad: medido en F5 > tabulado en el documento > derivado de verbosidad.
    """
    if dst == "ja" or src == "ja":
        return BYTE_BUDGET_DISABLED
    measured = EXPANSION_MEASURED.get((src, dst))
    if measured is not None:
        return measured
    explicit = EXPANSION_EXPLICIT.get((src, dst))
    if explicit is not None:
        return explicit
    return round(_VERBOSITY[dst] / _VERBOSITY[src], 3)


def uses_byte_budget(profile: LanguageProfile) -> bool:
    return profile.expansion != BYTE_BUDGET_DISABLED


def profile_for(src: Lang, dst: Lang, formality: Formality = "neutral") -> LanguageProfile:
    if src not in SUPPORTED_LANGS:
        raise ValueError(f"unsupported source language: {src!r}; expected one of {SUPPORTED_LANGS}")
    if dst not in SUPPORTED_LANGS:
        raise ValueError(f"unsupported target language: {dst!r}; expected one of {SUPPORTED_LANGS}")
    if src == dst:
        raise ValueError(f"source and target language are the same: {src!r}")
    return LanguageProfile(
        src=src,
        dst=dst,
        deepgram_code=DEEPGRAM_CODE[src],
        turn_detector_code=TURN_DETECTOR_CODE[src],
        expansion=expansion_for(src, dst),
        formality=formality,
    )


DIRECTED_PAIRS: Final[tuple[tuple[Lang, Lang], ...]] = tuple(
    (s, d) for s in SUPPORTED_LANGS for d in SUPPORTED_LANGS if s != d
)


@dataclass(frozen=True)
class Settings:
    src_lang: Lang
    dst_lang: Lang
    llm_model: str
    llm_reasoning_effort: str
    llm_temperature: float
    llm_max_tokens: int
    tts_model: str
    voice_ref: str
    voice_id: str
    log_level: str
    deepgram_api_key: str
    groq_api_key: str
    fish_api_key: str

    @property
    def profile(self) -> LanguageProfile:
        return profile_for(self.src_lang, self.dst_lang)


def _read_lang(var: str, default: str) -> Lang:
    raw = os.getenv(var, default)
    if raw not in SUPPORTED_LANGS:
        raise ValueError(f"{var}={raw!r} is not supported; expected one of {SUPPORTED_LANGS}")
    return raw


def load_settings(dotenv_path: str | None = None) -> Settings:
    load_dotenv(dotenv_path, override=False)
    return Settings(
        src_lang=_read_lang("SINCRO_SRC_LANG", "es"),
        dst_lang=_read_lang("SINCRO_DST_LANG", "en"),
        llm_model=os.getenv("SINCRO_LLM_MODEL", DEFAULT_LLM_MODEL),
        llm_reasoning_effort=os.getenv(
            "SINCRO_LLM_REASONING_EFFORT", DEFAULT_LLM_REASONING_EFFORT
        ),
        llm_temperature=float(os.getenv("SINCRO_LLM_TEMPERATURE", DEFAULT_LLM_TEMPERATURE)),
        llm_max_tokens=int(os.getenv("SINCRO_LLM_MAX_TOKENS", DEFAULT_LLM_MAX_TOKENS)),
        tts_model=os.getenv("SINCRO_TTS_MODEL", "s2.1-pro-free"),
        voice_ref=os.getenv("SINCRO_VOICE_REF", "tests/fixtures/voz_referencia.wav"),
        voice_id=os.getenv("SINCRO_VOICE_ID", ""),
        log_level=os.getenv("SINCRO_LOG_LEVEL", "INFO"),
        deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
        fish_api_key=os.getenv("FISH_API_KEY", ""),
    )
