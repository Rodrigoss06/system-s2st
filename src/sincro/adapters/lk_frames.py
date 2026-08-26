"""Frontera con los tipos de audio de LiveKit. Unico archivo que importa livekit.rtc.

Silero necesita `rtc.AudioFrame` en `push_frame`. La regla de arquitectura prohibe que
un modulo de M1-M8 importe `livekit.rtc`, asi que la conversion vive aqui, en adapters/,
que es exactamente la frontera que esa regla define. Los contratos siguen usando
`np.ndarray` y ningun tipo de LiveKit entra en ellos.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from livekit import rtc

from ..contracts import SpeechFrame


def to_lk_frame(frame: SpeechFrame) -> Any:
    pcm = np.ascontiguousarray(frame.pcm, dtype=np.int16)
    return rtc.AudioFrame(
        data=pcm.tobytes(),
        sample_rate=frame.sample_rate,
        num_channels=1,
        samples_per_channel=len(pcm),
    )


def from_lk_frame(frame: Any, t_capture: float) -> SpeechFrame:
    pcm = np.frombuffer(frame.data, dtype=np.int16)
    if frame.num_channels > 1:
        pcm = pcm.reshape(-1, frame.num_channels)[:, 0]
    return SpeechFrame(pcm=pcm, sample_rate=frame.sample_rate, t_capture=t_capture)
