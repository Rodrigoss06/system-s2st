"""M4 - SegmentCommitter. Convierte el evento de M3 en segmentos estables.

F1 implementa dos de los cuatro triggers: `punctuation` y `max_len`. `eou` necesita el
turn-detector y `timeout` necesita streaming; ambos son F2. No se simulan aqui: un
trigger inventado falsea la distribucion que la telemetria usa como indicador de salud.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Final

from .contracts import Segment, TranscriptEvent, Trigger, Word

logger = logging.getLogger(__name__)

# Cierre de frase en los cinco idiomas soportados.
SENTENCE_FINAL: Final[tuple[str, ...]] = (".", "?", "!", "。", "？", "！")  # noqa: RUF001
# Coma fuerte: corta solo si el segmento ya tiene cuerpo suficiente.
SOFT_BREAK: Final[tuple[str, ...]] = (",", ";", ":", "、")

# Idiomas que no separan palabras con espacio. Deepgram devuelve el japones en
# morfemas sueltos; unirlos con espacio produce "20 2 6" en vez de "2026" y "リ マ" en
# vez de "リマ", y ese texto roto es el que llegaria al traductor.
NO_SPACE_LANGS: Final[frozenset[str]] = frozenset({"ja"})


def join_words(words: list[Word], lang: str) -> str:
    sep = "" if lang in NO_SPACE_LANGS else " "
    return sep.join(w.text for w in words).strip()


MAX_SEGMENT_S: Final[float] = 12.0
MIN_SOFT_BREAK_S: Final[float] = 2.5


class PunctuationCommitter:
    """Implementa el Protocol SegmentCommitter cortando por puntuacion.

    Los limites del segmento salen de los timestamps de `words`, nunca de la hora de
    llegada del evento: alimentan source_duration, y de ahi el presupuesto de bytes y la
    deriva. Ver D1.
    """

    def __init__(self, first_seg_id: int = 1) -> None:
        self.next_seg_id = first_seg_id
        self.dropped_partials = 0

    def _emit(self, words: list[Word], lang: str, trigger: Trigger) -> Segment:
        seg = Segment(
            seg_id=self.next_seg_id,
            text=join_words(words, lang),
            lang=lang,  # type: ignore[arg-type]
            t_start=words[0].start,
            t_end=words[-1].end,
            trigger=trigger,
        )
        self.next_seg_id += 1
        return seg

    async def commit(self, events: AsyncIterator[TranscriptEvent]) -> AsyncIterator[Segment]:
        async for ev in events:
            if not ev.is_final:
                # is_final es la compuerta, no un trigger. Nunca se traduce un parcial.
                self.dropped_partials += 1
                continue
            if not ev.words:
                logger.warning("final event with no words, nothing to commit: %r", ev.text[:60])
                continue

            pending: list[Word] = []
            for w in ev.words:
                pending.append(w)
                span = pending[-1].end - pending[0].start
                text = w.text.rstrip()

                if text.endswith(SENTENCE_FINAL):
                    yield self._emit(pending, ev.lang, "punctuation")
                    pending = []
                elif text.endswith(SOFT_BREAK) and span >= MIN_SOFT_BREAK_S:
                    yield self._emit(pending, ev.lang, "punctuation")
                    pending = []
                elif span > MAX_SEGMENT_S:
                    yield self._emit(pending, ev.lang, "max_len")
                    pending = []

            if pending:
                # Cola sin puntuacion de cierre: en F1 el archivo se acabo, asi que es un
                # cierre legitimo, no un timeout.
                yield self._emit(pending, ev.lang, "punctuation")


# ---------------------------------------------------------------------------
# F2 - los cuatro triggers
# ---------------------------------------------------------------------------

# El documento tecnico, seccion 5, fija 800 ms. Medido a 400 ms sobre el fixture de
# 10 min, dos corridas de ~45 turnos cada una: P50 3616 -> 2890 ms y P90 4752 -> 4097
# ms, con las fronteras de segmento **identicas**. Ver D36.
# Se probo tambien alargarlo a 2.5 s cuando el turn-detector dice que la idea no
# cierra: empeoro el P90 a 4291 ms. Revertido.
TIMEOUT_S: Final[float] = 0.400

# Guardas de no-corte. Si el fragmento termina en una de estas, el hablante no ha
# terminado la idea aunque Deepgram haya cerrado el fragmento y aunque haya silencio.
# Cortar aqui produce medias frases, que es el fallo que el criterio de F2 mide.
NO_CUT_WORDS: Final[dict[str, frozenset[str]]] = {
    "es": frozenset(
        {
            "y", "e", "o", "u", "ni", "que", "pero", "porque", "aunque", "si", "cuando",
            "mientras", "como", "donde", "cual", "cuyo", "pues", "sino",
            "de", "del", "a", "al", "en", "con", "por", "para", "sin", "sobre", "entre",
            "hasta", "desde", "hacia", "tras", "ante", "bajo", "segun",
            "el", "la", "los", "las", "un", "una", "unos", "unas", "lo",
            "mi", "tu", "su", "mis", "tus", "sus", "nuestro", "nuestra",
            "muy", "mas", "menos", "tan", "tanto",
        }
    ),
    "en": frozenset(
        {
            "and", "or", "but", "nor", "so", "yet", "because", "although", "though",
            "if", "when", "while", "whereas", "that", "which", "who", "whose", "as",
            "of", "to", "in", "on", "at", "by", "for", "with", "from", "into", "onto",
            "about", "over", "under", "between", "through", "during", "without",
            "the", "a", "an", "my", "your", "his", "her", "its", "our", "their",
            "very", "more", "less", "most",
        }
    ),
    "pt": frozenset(
        {
            "e", "ou", "mas", "nem", "que", "porque", "embora", "se", "quando",
            "enquanto", "como", "onde", "pois",
            "de", "do", "da", "dos", "das", "a", "ao", "em", "no", "na", "com", "por",
            "para", "sem", "sobre", "entre", "ate", "desde",
            "o", "os", "as", "um", "uma", "uns", "umas",
            "meu", "minha", "seu", "sua", "muito", "mais", "menos",
        }
    ),
    "fr": frozenset(
        {
            "et", "ou", "mais", "ni", "que", "parce", "bien", "si", "quand",
            "pendant", "comme", "car", "donc",
            "de", "du", "des", "a", "au", "aux", "en", "dans", "avec", "par", "pour",
            "sans", "sur", "entre", "vers", "chez", "depuis",
            "le", "la", "les", "un", "une", "mon", "ton", "son", "ma", "ta", "sa",
            "mes", "tes", "ses", "notre", "votre", "leur", "tres", "plus", "moins",
        }
    ),
    # El japones es aglutinante y las particulas van pegadas: una lista de palabras
    # sueltas no aplica igual. Se deja vacia y se corta solo por puntuacion y eou.
    "ja": frozenset(),
}


def _ends_with_incomplete_number(text: str) -> bool:
    """Un numero suelto al final casi siempre continua: '15 de', '3 contratos', '2027'."""
    tail = text.rstrip().rstrip(".,;:").split()
    return bool(tail) and tail[-1].isdigit()


class StreamingCommitter:
    """M4 en streaming. Acumula fragmentos con is_final y decide cuando cerrar.

    `is_final` es la **compuerta**, no un trigger: ningun trigger dispara sobre un
    parcial. `speech_final` corrobora `eou` y `punctuation`, nunca dispara solo, porque
    su umbral no se configura desde el motor.

    Orden de evaluacion: max_len, guardas de no-corte, eou, punctuation. El orden importa
    para la distribucion objetivo del documento: eou se evalua antes que punctuation, de
    modo que un cierre semantico se atribuye a eou y no a la coma que lo acompana.
    """

    def __init__(
        self,
        gate: Any,
        lang_code: str,
        first_seg_id: int = 1,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self.gate = gate
        self.lang_code = lang_code
        self.next_seg_id = first_seg_id
        self.timeout_s = timeout_s
        self.dropped_partials = 0
        self.no_cut_holds = 0
        self._last_eou: float = 1.0
        self.trigger_counts: dict[str, int] = {
            "eou": 0,
            "punctuation": 0,
            "timeout": 0,
            "max_len": 0,
        }

    def _no_cut(self, words: list[Word]) -> bool:
        if not words:
            return True
        stop = NO_CUT_WORDS.get(self.lang_code, frozenset())
        last = words[-1].text.strip().rstrip(".,;:!?").lower()
        if last in stop:
            return True
        return _ends_with_incomplete_number(" ".join(w.text for w in words))

    def _emit(self, words: list[Word], lang: str, trigger: Trigger) -> Segment:
        seg = Segment(
            seg_id=self.next_seg_id,
            text=join_words(words, lang),
            lang=lang,  # type: ignore[arg-type]
            t_start=words[0].start,
            t_end=words[-1].end,
            trigger=trigger,
        )
        self.next_seg_id += 1
        self.trigger_counts[trigger] += 1
        return seg

    async def _decide(
        self, pending: list[Word], speech_final: bool, timed_out: bool
    ) -> Trigger | None:
        if not pending:
            return None
        span = pending[-1].end - pending[0].start
        text = join_words(pending, self.lang_code)

        # max_len va primero y se salta las guardas: es la valvula de seguridad contra un
        # hablante que encadena sin pausas. Si aparece seguido, revisar el umbral de Silero.
        if span > MAX_SEGMENT_S:
            return "max_len"

        if self._no_cut(pending):
            self.no_cut_holds += 1
            # Ni siquiera el timeout rompe la guarda: cortar en "y" produce media frase.
            return None

        # El umbral del turn-detector es un *unlikely_threshold*: por debajo, el hablante
        # casi seguro NO ha terminado y hay que esperar. No es una compuerta de commit por
        # si sola; usarla asi cortaba a media frase.
        eou_p = await self.gate.predict_eou(text)
        self._last_eou = eou_p
        finished = eou_p >= self.gate.eou_threshold

        # eou: Deepgram detecto que el hablante paro Y el modelo dice que la idea cierra.
        # speech_final corrobora, nunca dispara solo: su umbral no se configura desde aqui.
        if speech_final and finished:
            return "eou"

        # punctuation: smart_format cerro la frase. Signo fuerte por si mismo.
        if text.rstrip().endswith(SENTENCE_FINAL) and finished:
            return "punctuation"
        if (
            speech_final
            and text.rstrip().endswith(SOFT_BREAK)
            and span >= MIN_SOFT_BREAK_S
            and finished
        ):
            return "punctuation"

        # timeout: 800 ms sin texto final nuevo. Ultimo recurso; si sube del 10 % el
        # endpointing esta mal calibrado.
        if timed_out:
            return "timeout"
        return None

    async def commit(self, events: AsyncIterator[TranscriptEvent]) -> AsyncIterator[Segment]:
        queue: asyncio.Queue[TranscriptEvent | None] = asyncio.Queue()

        async def feed() -> None:
            try:
                async for ev in events:
                    await queue.put(ev)
            finally:
                await queue.put(None)

        feeder = asyncio.create_task(feed())
        pending: list[Word] = []
        lang = self.lang_code
        speech_final = False
        try:
            while True:
                timed_out = False
                try:
                    # El timeout solo corre si hay algo pendiente: sin texto acumulado no
                    # hay nada que cerrar y no tiene sentido despertar cada 800 ms.
                    ev = await asyncio.wait_for(
                        queue.get(), timeout=self.timeout_s if pending else None
                    )
                except TimeoutError:
                    timed_out = True
                    ev = None

                if ev is None and not timed_out:
                    break

                if ev is not None:
                    if not ev.is_final:
                        self.dropped_partials += 1
                        continue
                    if not ev.words:
                        continue
                    pending.extend(ev.words)
                    lang = ev.lang
                    speech_final = ev.speech_final

                trigger = await self._decide(pending, speech_final, timed_out)
                if trigger is not None:
                    yield self._emit(pending, lang, trigger)
                    pending = []
                    speech_final = False

            if pending:
                yield self._emit(pending, lang, "timeout")
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder
