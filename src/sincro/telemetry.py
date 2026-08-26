"""M8 - Telemetria. Una linea JSONL por segmento en out/telemetry-TIMESTAMP.jsonl.

Esquema exacto: Documentacion tecnica, seccion 8. Sin este archivo no se puede
verificar ningun criterio de aceptacion, por eso existe desde F0.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal, Self

from .contracts import Lang, Trigger

logger = logging.getLogger(__name__)

# Las seis marcas de etapa del esquema, en orden cronologico. make check exige las seis.
STAGE_MARKS: Final[tuple[str, ...]] = (
    "t_speech_end",
    "t_stt_final",
    "t_llm_first_token",
    "t_llm_done",
    "t_tts_first_byte",
    "t_audio_out",
)

Stage = Literal[
    "t_speech_end",
    "t_stt_final",
    "t_llm_first_token",
    "t_llm_done",
    "t_tts_first_byte",
    "t_audio_out",
]

# Tarifas USD por millon de tokens, (entrada, salida). Unica fuente: cambiar un precio
# o anadir un modelo se hace aqui y en ningun otro sitio. Tomadas de la respuesta de
# GET https://api.groq.com/openai/v1/models, campo `pricing`, el 2026-08-26.
# Los tokens de razonamiento se facturan como tokens de salida.
LLM_PRICES: Final[dict[str, tuple[float, float]]] = {
    "qwen/qwen3.6-27b": (0.60, 3.00),
    "qwen/qwen3.8-27b": (0.80, 4.00),
    "openai/gpt-oss-120b": (0.15, 0.60),
    # Retirado por Groq el 17/06/2026. Se conserva para poder releer telemetria vieja.
    "llama-3.3-70b-versatile": (0.59, 0.79),
}
FALLBACK_LLM_PRICE: Final[tuple[float, float]] = (0.60, 3.00)

FISH_USD_PER_M_BYTES: Final[float] = 15.0
FISH_FREE_MODELS: Final[frozenset[str]] = frozenset({"s2.1-pro-free"})


def llm_price_for(model: str) -> tuple[float, float]:
    price = LLM_PRICES.get(model)
    if price is None:
        logger.warning(
            "no price entry for LLM %r; cost_usd is an estimate using %s. "
            "Add it to LLM_PRICES from the /models endpoint.",
            model,
            FALLBACK_LLM_PRICE,
        )
        return FALLBACK_LLM_PRICE
    return price


def llm_cost_usd(tokens_in: int, tokens_out: int, model: str) -> float:
    usd_in, usd_out = llm_price_for(model)
    return (tokens_in * usd_in + tokens_out * usd_out) / 1_000_000


def tts_cost_usd(bytes_out: int, tts_model: str) -> float:
    if tts_model in FISH_FREE_MODELS:
        return 0.0
    return bytes_out * FISH_USD_PER_M_BYTES / 1_000_000


@dataclass
class SegmentRecord:
    """Una linea del JSONL. El orden de los campos es el del documento tecnico."""

    seg_id: int
    lang_src: Lang
    lang_dst: Lang
    trigger: Trigger
    t_speech_end: float = 0.0
    t_stt_final: float = 0.0
    t_llm_first_token: float = 0.0
    t_llm_done: float = 0.0
    t_tts_first_byte: float = 0.0
    t_audio_out: float = 0.0
    ttfa_ms: int = 0
    source_duration_s: float = 0.0
    audio_duration_s: float = 0.0
    speed_applied: float = 1.0
    drift_s: float = 0.0
    bytes_in: int = 0
    bytes_out: int = 0
    byte_budget: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    _marked: set[str] = field(default_factory=set, repr=False, compare=False)

    def mark(self, stage: Stage, t: float | None = None) -> None:
        if stage not in STAGE_MARKS:
            raise ValueError(f"unknown stage: {stage!r}")
        setattr(self, stage, round(t if t is not None else time.monotonic(), 3))
        self._marked.add(stage)

    @property
    def missing_marks(self) -> tuple[str, ...]:
        return tuple(s for s in STAGE_MARKS if s not in self._marked)

    def finalize(self) -> None:
        """Deriva ttfa_ms de las marcas. TTFA = fin de clausula -> primer audio doblado."""
        self.ttfa_ms = round((self.t_audio_out - self.t_speech_end) * 1000)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_marked", None)
        return d


class TelemetryWriter:
    """Escritor JSONL. Flush por linea: una sesion interrumpida conserva su evidencia."""

    def __init__(self, out_dir: str | os.PathLike[str] = "out", path: Path | None = None) -> None:
        if path is None:
            stamp = time.strftime("%Y%m%dT%H%M%S")
            path = Path(out_dir) / f"telemetry-{stamp}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8")
        self.count = 0

    def write(self, record: SegmentRecord) -> None:
        record.finalize()
        self._fh.write(json.dumps(record.to_json(), ensure_ascii=False) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
