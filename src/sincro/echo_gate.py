"""M9 - EchoGate. Puerta anti-eco cruzada entre los dos motores de una llamada.

En v3 el motor se silenciaba a si mismo: microfono y altavoz compartian maquina, y
`gate.mute()` bastaba. En v4 el eco cruza de motor: el altavoz de A reproduce el
doblaje de B->A, y si esa salida se filtra al microfono de A, quien la recibe es A->B,
un motor DISTINTO con su propio SileroGate. Ninguno de los dos gates ve al otro por su
cuenta -- por eso M9 vive en CallSession, no dentro de un motor (CLAUDE.md).

Diseno: `EchoGate` es una subclase de `SileroGate` (M2), no un envoltorio. Hereda VAD y
turn-detector sin tocar `gate.py`, y usa exactamente el mismo tipo que
`DubbingEngine.__init__` y `StreamingCommitter.__init__` ya esperan -- cero cambios en
el nucleo, cero `type: ignore`. Lo unico que se sobreescribe es `mute`/`unmute`/
`unmute_after_guard`, que en vez de actuar sobre si mismo actuan sobre el `target`: el
EchoGate del motor CONTRARIO.

Por que no `self.target.mute()`: `target` es OTRO EchoGate, cuyo `mute()` tambien esta
redirigido. Delegar con el metodo normal entra en llamada mutua infinita (A muta a B,
B muta a A, A muta a B...). La salida es llamar al metodo SIN LIGAR de `SileroGate`
directamente sobre `target` (`SileroGate.mute(self.target)`): eso ejecuta el "mutate a
ti mismo" real -- pone `target.muted = True` en el propio `target` -- sin volver a pasar
por el `mute()` sobreescrito de `target`.

Politica de interbloqueo (obligatoria documentarla, ver tarea de G1):

- El estado de mute es por DIRECCION: cada EchoGate tiene su propio `muted`/`mute_calls`
  heredados de SileroGate: no hay un mute global de la sesion.
- `mute()`/`unmute()` son escrituras de atributo, no locks. Si los dos motores emiten a
  la vez, cada uno escribe el `muted` del OTRO target sin esperar nada del otro lado:
  no hay espera circular posible, no hay deadlock en el sentido de bloqueo mutuo.
- Riesgo real, heredado sin cambios de `gate.py` (no arreglable sin tocar el nucleo):
  `unmute_after_guard()` llama a `unmute()` de forma incondicional tras dormir 150 ms,
  sin comprobar si un segundo segmento ya volvio a mutear el mismo gate mientras
  dormia. Si el motor B->A emite dos segmentos seguidos con menos de 150 ms entre el
  fin del primero y el mute del segundo, el guard del primero puede desmutear a mitad
  de la reproduccion del segundo. Ya existia en v3 (mismo mecanismo, self-mute); v4 solo
  lo reubica de "un motor se desmutea de mas pronto" a "el gate del OTRO participante se
  desmutea de mas pronto". Se vigila en `make call-test`, no se enmascara.
"""

from __future__ import annotations

import asyncio
import logging

from .contracts import LanguageProfile
from .gate import DEFAULT_ACTIVATION, DEFAULT_MIN_SILENCE, UNMUTE_GUARD_S, SileroGate

logger = logging.getLogger(__name__)


class EchoGate(SileroGate):
    """VAD y turn-detector propios (heredados); mute/unmute actuan sobre `target`."""

    def __init__(
        self,
        profile: LanguageProfile,
        min_silence_duration: float = DEFAULT_MIN_SILENCE,
        activation_threshold: float = DEFAULT_ACTIVATION,
    ) -> None:
        super().__init__(profile, min_silence_duration, activation_threshold)
        self.target: EchoGate | None = None

    def bind_target(self, target: EchoGate) -> None:
        """Enlaza los dos lados despues de construirlos: cada uno necesita al otro ya
        creado, asi que el enlace no puede pasar por el constructor."""
        self.target = target

    def mute(self) -> None:
        if self.target is None:
            logger.warning("EchoGate.mute() sin target enlazado, se ignora")
            return
        SileroGate.mute(self.target)

    def unmute(self) -> None:
        if self.target is None:
            logger.warning("EchoGate.unmute() sin target enlazado, se ignora")
            return
        SileroGate.unmute(self.target)

    async def unmute_after_guard(self, guard_s: float = UNMUTE_GUARD_S) -> None:
        if self.target is None:
            logger.warning("EchoGate.unmute_after_guard() sin target enlazado, se ignora")
            return
        await asyncio.sleep(guard_s)
        SileroGate.unmute(self.target)

    @property
    def is_speaking(self) -> bool:
        """Expone `_speaking` (mantenido por el watcher VAD heredado de SileroGate) para
        que CallSession pueda emitir `state: speaking` sin que el nucleo exponga un
        callback de inicio/fin de habla. Lectura de un atributo propio de la subclase,
        no una violacion de encapsulamiento de gate.py."""
        return self._speaking


def make_pair(
    profile_a: LanguageProfile,
    profile_b: LanguageProfile,
    min_silence_duration: float = DEFAULT_MIN_SILENCE,
    activation_threshold: float = DEFAULT_ACTIVATION,
) -> tuple[EchoGate, EchoGate]:
    """Los dos EchoGate de una llamada, ya enlazados entre si.

    `gate_a` es el gate del motor A->B (VAD y turn-detector con el perfil de A, procesa
    la entrada de A). `gate_a.mute()` -- que llama el motor A->B al emitir hacia el
    altavoz de B -- mutea a `gate_b`: la entrada de B, que es justo lo que hay que
    silenciar mientras B escucha el doblaje de A. Simetrico: `gate_b.mute()`, llamado
    por el motor B->A al emitir hacia el altavoz de A, mutea la entrada de A (`gate_a`).
    """
    gate_a = EchoGate(profile_a, min_silence_duration, activation_threshold)
    gate_b = EchoGate(profile_b, min_silence_duration, activation_threshold)
    gate_a.bind_target(gate_b)
    gate_b.bind_target(gate_a)
    return gate_a, gate_b
