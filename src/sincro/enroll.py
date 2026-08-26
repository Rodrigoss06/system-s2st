"""make enroll - F3. Registra el timbre del hablante y devuelve el reference_id."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import load_settings
from .voices import EnrollmentError, FishVoiceRegistry, consent_prompt

logger = logging.getLogger(__name__)


async def run_enroll(ref: Path, speaker_id: str, yes: bool, keep: bool) -> int:
    s = load_settings()
    registry = FishVoiceRegistry(s.fish_api_key)

    # El consentimiento va antes de leer el clip, no despues.
    if not consent_prompt(ref, interactive=not yes):
        return 1

    print("\n  subiendo clip a Fish Audio...")
    reference_id = await registry.enroll(speaker_id, str(ref))

    print()
    print("  " + "=" * 68)
    print(f"  reference_id : {reference_id}")
    print(f"  speaker_id   : {speaker_id}")
    print("  visibilidad  : private")
    print("  " + "=" * 68)
    print()
    print("  Para usarlo en la sesion en vivo:")
    print(f"    SINCRO_VOICE_ID={reference_id} make live")
    print()
    print("  El motor NO lo persiste: la cache de M6 vive solo en memoria del proceso.")
    if not keep:
        print("  Para borrar la huella de Fish cuando termines:")
        print(f"    make enroll-delete ID={reference_id}")
    return 0


async def run_delete(reference_id: str) -> int:
    s = load_settings()
    registry = FishVoiceRegistry(s.fish_api_key)
    ok = await registry.delete(reference_id)
    print(f"  {'borrado' if ok else 'NO se pudo borrar'}: {reference_id}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="enroll", description="F3 enrolamiento de timbre")
    ap.add_argument("--ref", type=Path, default=None, help="WAV de referencia")
    ap.add_argument("--speaker", default="speaker-0")
    ap.add_argument("--yes", action="store_true", help="salta la confirmacion (no recomendado)")
    ap.add_argument("--keep", action="store_true", help="no sugiere el borrado al terminar")
    ap.add_argument("--delete", default=None, help="borra un reference_id de Fish")
    args = ap.parse_args()

    s = load_settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.delete:
            return asyncio.run(run_delete(args.delete))
        ref = args.ref or Path(s.voice_ref)
        return asyncio.run(run_enroll(ref, args.speaker, args.yes, args.keep))
    except EnrollmentError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n  Cancelado.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
