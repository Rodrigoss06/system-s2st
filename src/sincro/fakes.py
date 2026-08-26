"""Implementaciones Fake deterministas de cada Protocol.

Ningun fake toca la red ni el disco. Existen desde F0 para que cualquier fase se
pruebe en aislamiento y para que un proveedor caido no bloquee el avance.

Deterministas quiere decir: misma entrada, misma salida, byte a byte. No usan reloj
de pared ni random sin semilla.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from typing import Final

import numpy as np

from .contracts import (
    DubbedChunk,
    Lang,
    Segment,
    SpeechFrame,
    TranscriptEvent,
    Translation,
    Trigger,
    Word,
)

SAMPLE_RATE: Final[int] = 16_000

# Un caracter de texto destino equivale a este tiempo de audio sintetico. Fija la
# proporcion texto-duracion de FakeSynthesizer.
CHARS_PER_SECOND: Final[float] = 15.0

# Guion por defecto de FakeTranscriber: seis clausulas, como el WAV de 30 s de F1.
DEFAULT_SCRIPT: Final[tuple[str, ...]] = (
    "Buenos dias a todos.",
    "Hoy vamos a revisar el informe del trimestre.",
    "Las ventas subieron un 12 por ciento.",
    "El equipo de Lima cerro tres contratos.",
    "Necesitamos el presupuesto antes del 15 de marzo.",
    "Gracias por su tiempo.",
)


def _stable_float(seed: str, lo: float, hi: float) -> float:
    """Valor reproducible en [lo, hi) derivado de una cadena. No usa random."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    unit = int.from_bytes(digest[:8], "big") / 2**64
    return lo + unit * (hi - lo)


class FakeAudioGate:
    """M2. Passthrough con puerta dura. Cuenta las llamadas para verificar el anti-eco."""

    def __init__(self) -> None:
        self.muted = False
        self.mute_calls = 0
        self.unmute_calls = 0
        self.frames_dropped = 0

    async def process(
        self, frames: AsyncIterator[SpeechFrame]
    ) -> AsyncIterator[SpeechFrame]:
        async for frame in frames:
            if self.muted:
                self.frames_dropped += 1
                continue
            yield frame

    def mute(self) -> None:
        self.muted = True
        self.mute_calls += 1

    def unmute(self) -> None:
        self.muted = False
        self.unmute_calls += 1


# Puntuacion que cierra frase en los cinco idiomas. speech_final del fake se deriva de
# ella, no de is_final: son senales distintas y el fake tiene que poder disociarlas.
SENTENCE_FINAL: Final[tuple[str, ...]] = (".", "?", "!", "\u3002", "\uff1f", "\uff01")

# Retardo entre el fin de la palabra y la emision del evento. Tramo bajo del presupuesto
# de latencia (seccion 4: STT final 250-450 ms). Fijo: los fakes no usan reloj de pared.
STT_EMIT_LATENCY_S: Final[float] = 0.25


def _words_for(text: str, t_start: float, t_end: float) -> list[Word]:
    """Reparte la clausula en palabras con timestamps proporcionales a su longitud.

    El primer start y el ultimo end caen exactamente en los bordes de la clausula, de
    modo que t_end - t_start reproduce la duracion fuente sin error de redondeo.
    """
    tokens = text.split()
    if not tokens:
        return []
    weights = [len(tok) for tok in tokens]
    total = float(sum(weights))
    span = t_end - t_start
    words: list[Word] = []
    cum = 0
    for i, tok in enumerate(tokens):
        start = t_start if i == 0 else t_start + span * cum / total
        cum += weights[i]
        end = t_end if i == len(tokens) - 1 else t_start + span * cum / total
        words.append(
            Word(
                text=tok,
                start=round(start, 3),
                end=round(end, 3),
                confidence=round(_stable_float(f"{i}:{tok}", 0.80, 0.99), 3),
            )
        )
    return words


class FakeTranscriber:
    """M3. Emite un parcial y un final por clausula del guion, con timestamps sinteticos.

    Los parciales existen para que M4 pueda probar que nunca traduce is_final=False, y
    llegan con words vacio, igual que en Deepgram.
    """

    def __init__(
        self,
        lang: Lang = "es",
        script: Sequence[str] = DEFAULT_SCRIPT,
        seconds_per_clause: float = 3.0,
    ) -> None:
        self.lang = lang
        self.script = tuple(script)
        self.seconds_per_clause = seconds_per_clause

    async def stream(
        self, frames: AsyncIterator[SpeechFrame]
    ) -> AsyncIterator[TranscriptEvent]:
        t = 0.0
        async for _ in frames:
            pass  # el fake ignora el audio: su salida depende solo del guion
        for text in self.script:
            t_end = t + self.seconds_per_clause
            head = text[: max(1, len(text) // 2)]
            yield TranscriptEvent(
                text=head,
                lang=self.lang,
                is_final=False,
                speech_final=False,
                words=[],
                t_emit=round(t + self.seconds_per_clause / 2, 3),
            )
            yield TranscriptEvent(
                text=text,
                lang=self.lang,
                is_final=True,
                # Una clausula que no cierra frase deja speech_final en falso: es lo que
                # permite a M4 distinguir la compuerta del endpointing de Deepgram.
                speech_final=text.rstrip().endswith(SENTENCE_FINAL),
                words=_words_for(text, t, t_end),
                t_emit=round(t_end + STT_EMIT_LATENCY_S, 3),
            )
            t = t_end


class FakeSegmentCommitter:
    """M4. Solo compromete is_final. El trigger se deriva de la puntuacion del texto."""

    MAX_LEN_S: Final[float] = 12.0

    def __init__(self, first_seg_id: int = 1) -> None:
        self.next_seg_id = first_seg_id

    def _trigger_for(self, ev: TranscriptEvent) -> Trigger:
        if ev.t_end - ev.t_start > self.MAX_LEN_S:
            return "max_len"
        if ev.text.rstrip().endswith((".", "?", "!", "。", "？", "！")):  # noqa: RUF001
            return "eou"
        if ev.text.rstrip().endswith((",", ";", ":", "、")):
            return "punctuation"
        return "timeout"

    async def commit(self, events: AsyncIterator[TranscriptEvent]) -> AsyncIterator[Segment]:
        async for ev in events:
            if not ev.is_final:
                continue
            seg = Segment(
                seg_id=self.next_seg_id,
                text=ev.text,
                lang=ev.lang,
                t_start=ev.t_start,
                t_end=ev.t_end,
                trigger=self._trigger_for(ev),
            )
            self.next_seg_id += 1
            yield seg


class FakeTranslator:
    """M5. Invierte el texto. Es traduccion falsa pero reversible, lo que hace trivial
    verificar que el segmento correcto llego al sintetizador."""

    def __init__(self, dst_lang: Lang = "en") -> None:
        self.dst_lang = dst_lang
        self.tokens_in = 0
        self.tokens_out = 0

    async def translate(self, seg: Segment, budget: int) -> Translation:
        text = seg.text[::-1]
        # Aproximacion de 4 caracteres por token, mas 200 de system prompt: Groq no
        # cachea el prompt, se paga entero en cada clausula.
        self.tokens_in = 200 + len(seg.text) // 4
        self.tokens_out = len(text) // 4
        return Translation(
            seg_id=seg.seg_id,
            text=text,
            lang=self.dst_lang,
            byte_budget=budget,
            byte_actual=len(text.encode("utf-8")),
        )


class FakeVoiceRegistry:
    """M6. reference_id derivado del speaker_id, cacheado en memoria. No lee el WAV:
    la huella vocal es dato biometrico y el fake no debe tocarla."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    async def enroll(self, speaker_id: str, wav_path: str) -> str:
        ref = "fake-" + hashlib.sha256(speaker_id.encode("utf-8")).hexdigest()[:16]
        self._cache[speaker_id] = ref
        return ref

    def get(self, speaker_id: str) -> str | None:
        return self._cache.get(speaker_id)


class FakeSynthesizer:
    """M7. Tono senoidal de duracion proporcional a la longitud del texto.

    La frecuencia se deriva del reference_id, de modo que voz clonada y voz neutra
    suenan distinto y el contraste de F3 es audible incluso con fakes.
    """

    CHUNK_S: Final[float] = 0.2

    def __init__(self, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate

    def duration_for(self, text: str, speed: float) -> float:
        return len(text) / CHARS_PER_SECOND / speed

    async def synthesize(
        self, tr: Translation, reference_id: str, speed: float
    ) -> AsyncIterator[DubbedChunk]:
        duration = self.duration_for(tr.text, speed)
        freq = _stable_float(reference_id, 180.0, 300.0)
        total = int(duration * self.sample_rate)
        chunk_n = max(1, int(self.CHUNK_S * self.sample_rate))
        emitted = 0
        while emitted < total:
            n = min(chunk_n, total - emitted)
            t = (np.arange(emitted, emitted + n, dtype=np.float64)) / self.sample_rate
            pcm = (np.sin(2 * np.pi * freq * t) * 0.3 * 32767).astype(np.int16)
            emitted += n
            yield DubbedChunk(
                seg_id=tr.seg_id,
                pcm=pcm,
                sample_rate=self.sample_rate,
                speed_applied=speed,
                audio_duration=n / self.sample_rate,
            )


def fake_frames(
    seconds: float = 1.0, sample_rate: int = SAMPLE_RATE, frame_ms: int = 20
) -> AsyncIterator[SpeechFrame]:
    """Generador de SpeechFrame silenciosos, para alimentar gate y transcriber."""

    async def _gen() -> AsyncIterator[SpeechFrame]:
        n = int(sample_rate * frame_ms / 1000)
        count = int(seconds * 1000 / frame_ms)
        for i in range(count):
            yield SpeechFrame(
                pcm=np.zeros(n, dtype=np.int16),
                sample_rate=sample_rate,
                t_capture=i * frame_ms / 1000,
            )

    return _gen()
