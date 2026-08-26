"""Contratos del motor. Tipos y Protocols, sin logica de negocio.

Fuente de verdad: Notion, "SINCRO Motor v3 - Documentacion tecnica", seccion 2.
Ningun modulo bajo src/sincro/ salvo adapters/ puede importar transporte de audio.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np

Lang = Literal["es", "en", "pt-BR", "fr", "ja"]
Formality = Literal["neutral", "formal", "informal"]
Trigger = Literal["eou", "punctuation", "timeout", "max_len"]


@dataclass(frozen=True)
class LanguageProfile:  # M1
    src: Lang
    dst: Lang
    deepgram_code: str  # es | en-US | pt-BR | fr | ja
    turn_detector_code: str  # es | en | pt | fr | ja
    expansion: float  # bytes destino / bytes fuente, calibrado en F5
    formality: Formality = "neutral"


@dataclass
class SpeechFrame:
    pcm: np.ndarray  # int16 mono
    sample_rate: int
    t_capture: float  # monotonic, para telemetria


@dataclass
class Segment:  # M4 -> M5
    seg_id: int
    text: str
    lang: Lang
    t_start: float
    t_end: float
    trigger: Trigger

    @property
    def source_duration(self) -> float:
        return self.t_end - self.t_start


@dataclass
class Translation:  # M5 -> M7
    seg_id: int
    text: str
    lang: Lang
    byte_budget: int  # objetivo de isocronia
    byte_actual: int


@dataclass
class DubbedChunk:  # M7 -> salida
    seg_id: int
    pcm: np.ndarray
    sample_rate: int
    speed_applied: float
    audio_duration: float


@dataclass
class Word:  # timestamp por palabra, viene de Deepgram
    text: str
    start: float  # s desde el inicio del stream
    end: float
    confidence: float


@dataclass
class TranscriptEvent:  # M3 -> M4   (cerrado por D1)
    text: str  # texto acumulado del turno en curso
    lang: Lang
    is_final: bool  # Deepgram cerro este fragmento
    speech_final: bool  # el endpointing de Deepgram detecto fin de habla
    words: list[Word]  # vacio en parciales
    t_emit: float  # monotonic, para telemetria

    # Los timestamps salen de words, nunca de la hora de llegada del paquete: el jitter
    # de red entraria en source_duration, y de ahi al presupuesto de bytes y a la deriva.
    @property
    def t_start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def t_end(self) -> float:
        return self.words[-1].end if self.words else 0.0


# ---- Protocolos ----


class AudioGate(Protocol):  # M2
    async def process(
        self, frames: AsyncIterator[SpeechFrame]
    ) -> AsyncIterator[SpeechFrame]: ...

    def mute(self) -> None: ...  # lo llama Synthesizer al reproducir (anti-eco)

    def unmute(self) -> None: ...


class Transcriber(Protocol):  # M3
    async def stream(
        self, frames: AsyncIterator[SpeechFrame]
    ) -> AsyncIterator[TranscriptEvent]: ...


class SegmentCommitter(Protocol):  # M4
    async def commit(self, events: AsyncIterator[TranscriptEvent]) -> AsyncIterator[Segment]: ...


class Translator(Protocol):  # M5
    async def translate(self, seg: Segment, budget: int) -> Translation: ...


class VoiceRegistry(Protocol):  # M6
    async def enroll(self, speaker_id: str, wav_path: str) -> str: ...

    def get(self, speaker_id: str) -> str | None: ...


class Synthesizer(Protocol):  # M7
    async def synthesize(
        self, tr: Translation, reference_id: str, speed: float
    ) -> AsyncIterator[DubbedChunk]: ...
