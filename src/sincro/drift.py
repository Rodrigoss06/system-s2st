"""M8 - DriftController. El mecanismo que de verdad sostiene la isocronia.

    drift = tiempo total de audio doblado - tiempo total de habla fuente
    Positivo significa que el doblaje va atrasado.

De los tres mecanismos encadenados del documento tecnico, seccion 6, este es el tercero
y el unico que corrige de verdad. Los otros dos (presupuesto de bytes en el prompt y
correccion de velocidad) son orientativos.

`RESET_SILENCE` es lo que impide que la deriva crezca sin limite: cada pausa natural del
hablante devuelve el reloj a cero. Sin el, una sesion de 20 minutos acumula un desfase
irrecuperable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final

from .contracts import Segment
from .synthesizer import SPEED_MAX, SPEED_MIN

logger = logging.getLogger(__name__)

# Limites de la primera rama de speed_for. Mas suaves que los duros del sintetizador:
# mientras la deriva es tolerable no hace falta forzar el timbre.
SOFT_SPEED_MAX: Final[float] = 1.10

# Un segmento de relleno no lleva informacion. Por debajo de esta longitud y con trigger
# timeout, descartarlo recupera tiempo sin perder contenido.
FILLER_MAX_CHARS: Final[int] = 25


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class DriftSample:
    seg_id: int
    drift: float
    speed: float
    dropped: bool
    reset: bool


@dataclass
class DriftController:
    THRESHOLD_SOFT: float = 0.8  # s -> empieza a acelerar
    THRESHOLD_HARD: float = 2.0  # s -> descarta segmentos de relleno
    RESET_SILENCE: float = 1.5  # s de silencio -> drift vuelve a cero

    drift: float = 0.0
    max_positive: float = 0.0
    resets: int = 0
    drops: int = 0
    curve: list[DriftSample] = field(default_factory=list)
    _last_speech_end: float | None = None
    # Media movil de duracion_audio_a_speed_1 / duracion_fuente. Es la senal del
    # mecanismo dos del documento: comparar duracion generada con duracion fuente.
    # Arranca en 1.0 y se calibra sola con los primeros segmentos.
    _ratio_ema: float = 1.0
    _ratio_n: int = 0

    def duration_ratio(self) -> float:
        """Velocidad que haria el segmento isocrono, segun lo medido hasta ahora.

        Si el idioma destino sale sistematicamente mas corto (ES->EN da ~0.82), este
        valor baja de 1.0 y pide hablar mas despacio para llenar la misma ventana.
        """
        return self._ratio_ema

    def observe(self, seg: Segment, audio_duration: float, speed: float) -> None:
        if seg.source_duration <= 0 or audio_duration <= 0 or speed <= 0:
            return
        # Se normaliza a speed 1.0 para que la medida no arrastre la correccion previa.
        natural = (audio_duration * speed) / seg.source_duration
        self._ratio_n += 1
        alpha = 1.0 / min(self._ratio_n, 10)
        self._ratio_ema += alpha * (natural - self._ratio_ema)

    def speed_for(self, seg: Segment, budget_ratio: float) -> float:
        """Factor de velocidad para este segmento.

        Por debajo del umbral blando se respeta el presupuesto de bytes, acotado a 1.10.
        Por encima se acelera en proporcion al exceso, con el techo duro de 1.25 que
        impide que el timbre clonado se degrade de forma audible.
        """
        if self.drift < self.THRESHOLD_SOFT:
            return clamp(budget_ratio, SPEED_MIN, SOFT_SPEED_MAX)
        excess = self.drift - self.THRESHOLD_SOFT
        return clamp(1.0 + excess * 0.15, SPEED_MIN, SPEED_MAX)

    def should_drop(self, seg: Segment) -> bool:
        """Solo se descartan segmentos cortos sin contenido informativo."""
        return (
            self.drift > self.THRESHOLD_HARD
            and seg.trigger == "timeout"
            and len(seg.text) < FILLER_MAX_CHARS
        )

    def note_gap(self, seg: Segment) -> bool:
        """Mira el silencio entre el segmento anterior y este. Devuelve True si reseteo.

        Se llama ANTES de decidir velocidad o descarte: una pausa larga borra la deuda
        acumulada, y con la deuda a cero no hay que acelerar ni descartar nada.
        """
        reset = False
        if self._last_speech_end is not None:
            gap = seg.t_start - self._last_speech_end
            if gap >= self.RESET_SILENCE:
                if self.drift != 0.0:
                    logger.info(
                        "drift reset by %.2fs of silence: %.2fs -> 0.00s", gap, self.drift
                    )
                self.drift = 0.0
                self.resets += 1
                reset = True
        return reset

    def update(self, seg: Segment, audio_duration: float, speed: float, dropped: bool) -> None:
        """Acumula la deuda del turno y cierra el registro de la curva."""
        if not dropped:
            self.drift += audio_duration - seg.source_duration
        else:
            # El segmento no se reproduce: su duracion fuente sigue corriendo y esa es
            # justamente la deuda que se recupera.
            self.drift -= seg.source_duration
            self.drops += 1
        self.max_positive = max(self.max_positive, self.drift)
        self._last_speech_end = seg.t_end
        self.curve.append(
            DriftSample(
                seg_id=seg.seg_id,
                drift=round(self.drift, 3),
                speed=round(speed, 3),
                dropped=dropped,
                reset=False,
            )
        )

    @property
    def max_abs_drift(self) -> float:
        return max((abs(s.drift) for s in self.curve), default=0.0)


def _cell(value: float, level: float) -> str:
    """Rellena hacia arriba para deriva positiva y hacia abajo para negativa."""
    if level >= 0:
        return "#" if value >= level > 0 or (level == 0 and value >= 0) else " "
    return "#" if value <= level else " "


def render_curve(curve: list[DriftSample], width: int = 64, height: int = 15) -> str:
    """Grafica la deriva en ASCII. Sin dependencias: make report corre en cualquier sitio."""
    if not curve:
        return "(sin datos de deriva)"
    values = [s.drift for s in curve]
    lo, hi = min(values), max(values)
    if hi - lo < 0.2:
        mid = (hi + lo) / 2
        lo, hi = mid - 0.1, mid + 0.1

    # Submuestreo a `width` columnas conservando el extremo de cada tramo.
    n = len(values)
    cols: list[float] = []
    for i in range(min(width, n)):
        a = i * n // min(width, n)
        b = max(a + 1, (i + 1) * n // min(width, n))
        chunk = values[a:b]
        cols.append(max(chunk, key=abs))

    lines: list[str] = []
    for row in range(height, -1, -1):
        level = lo + (hi - lo) * row / height
        marks = "".join(_cell(v, level) for v in cols)
        axis = "  0 |" if abs(level) < (hi - lo) / (2 * height) else f"{level:+5.2f}|"
        lines.append(f"  {axis}{marks}")
    lines.append("       " + "-" * len(cols))
    lines.append(f"       segmento 1 .. {curve[-1].seg_id}")
    return "\n".join(lines)
