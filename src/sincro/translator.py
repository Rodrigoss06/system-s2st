"""M5 - Translator. Groq via el plugin de OpenAI, contexto rodante y presupuesto de bytes.

Groq no cachea el system prompt en Llama 3.3 70B: se paga entero en cada clausula. De ahi
el limite de 200 tokens del prompt y el contexto acotado a 3 turnos (riesgo R6).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Final

from livekit.agents import llm as lkllm
from livekit.plugins import openai

from .config import LANG_NAME, uses_byte_budget
from .contracts import LanguageProfile, Segment, Translation

logger = logging.getLogger(__name__)

GROQ_BASE_URL: Final[str] = "https://api.groq.com/openai/v1"
PROMPT_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "prompts" / "translate.md"

# Valvula de seguridad del prompt: sin ella el modelo inventa una traduccion para ruido
# de fondo transcrito por error, y esa alucinacion se sintetiza y se reproduce.
SKIP_TOKEN: Final[str] = "[[SKIP]]"

CONTEXT_TURNS: Final[int] = 3


# Marcadores que algunos modelos filtran al contenido cuando el razonamiento sigue
# activo o el formato de chat se rompe.
CONTROL_MARKERS: Final[tuple[str, ...]] = (
    "<think>",
    "</think>",
    "<|return|>",
    "<|channel|>",
    "<|start|>",
    "<|end|>",
    "<|message|>",
    "analysisfinal",
)


class TranslationError(RuntimeError):
    pass


def _leaked_markers(text: str) -> list[str]:
    low = text.lower()
    return [m for m in CONTROL_MARKERS if m in low]


def load_prompt_template(path: Path = PROMPT_PATH) -> str:
    if not path.is_file():
        raise TranslationError(f"missing system prompt: {path}")
    return path.read_text(encoding="utf-8")


class GroqTranslator:
    """Implementa el Protocol Translator.

    El plugin de OpenAI apunta a la URL de Groq: `openai.LLM.with_groq` fue retirado en
    livekit-plugins-openai 1.7.0. Ver D8 en DECISIONS.md.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        profile: LanguageProfile,
        template: str | None = None,
        reasoning_effort: str = "none",
        temperature: float = 0.2,
        max_tokens: int = 200,
    ) -> None:
        if not api_key:
            raise TranslationError("GROQ_API_KEY is empty")
        self.model = model
        self.profile = profile
        self.reasoning_effort = reasoning_effort
        self._template = template if template is not None else load_prompt_template()
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": api_key,
            "base_url": GROQ_BASE_URL,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }
        # El auto-default del plugin solo cubre los gpt-5.x, asi que para qwen hay que
        # pasarlo explicito o el modelo razona por defecto.
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        self._llm = openai.LLM(**kwargs)
        self._context: deque[str] = deque(maxlen=CONTEXT_TURNS)
        self.tokens_in = 0
        self.tokens_out = 0
        self.t_first_token = 0.0
        self.t_done = 0.0
        self.skipped = 0
        self.leaked = 0

    def budget_for(self, seg: Segment) -> int:
        """Presupuesto en bytes. Cero significa: no restringir por bytes.

        Los pares con japones se controlan solo por duracion: la relacion bytes-duracion
        se rompe con kanji.
        """
        if not uses_byte_budget(self.profile):
            return 0
        return int(len(seg.text.encode("utf-8")) * self.profile.expansion)

    def _render(self, budget: int) -> str:
        context = "\n".join(self._context) if self._context else "(none)"
        return (
            self._template.replace("{SRC}", LANG_NAME[self.profile.src])
            .replace("{DST}", LANG_NAME[self.profile.dst])
            .replace("{BUDGET}", str(budget) if budget else "no hard limit")
            .replace("{FORMALITY}", self.profile.formality)
            .replace("{CONTEXT}", context)
        )

    async def translate(self, seg: Segment, budget: int) -> Translation:
        ctx = lkllm.ChatContext.empty()
        ctx.add_message(role="system", content=self._render(budget))
        ctx.add_message(role="user", content=seg.text)

        out = ""
        first: float | None = None
        t0 = time.monotonic()
        try:
            async with self._llm.chat(chat_ctx=ctx) as stream:
                async for chunk in stream:
                    if chunk.delta and chunk.delta.content:
                        if first is None:
                            first = time.monotonic()
                        out += chunk.delta.content
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        self.tokens_in = getattr(usage, "prompt_tokens", 0)
                        self.tokens_out = getattr(usage, "completion_tokens", 0)
        except Exception as e:
            raise TranslationError(f"groq call failed for seg {seg.seg_id}: {e}") from e

        self.t_first_token = round(first if first is not None else t0, 3)
        self.t_done = round(time.monotonic(), 3)

        text = out.strip()
        leaked = _leaked_markers(text)
        if leaked:
            # El prompt exige solo la traduccion. Si aparecen marcadores de control o un
            # bloque de razonamiento, la configuracion del modelo esta mal y el audio
            # acabaria diciendolo en voz alta.
            logger.error(
                "seg %d: model leaked control markers %s into content: %r",
                seg.seg_id,
                leaked,
                text[:120],
            )
            self.leaked += 1

        if SKIP_TOKEN in text:
            self.skipped += 1
            logger.info("seg %d skipped by the model: %r", seg.seg_id, seg.text[:60])
            text = ""
        else:
            # El contexto guarda solo traducciones utiles: un turno vacio no ayuda a
            # resolver pronombres ni concordancia.
            self._context.append(text)

        return Translation(
            seg_id=seg.seg_id,
            text=text,
            lang=self.profile.dst,
            byte_budget=budget,
            byte_actual=len(text.encode("utf-8")),
        )
