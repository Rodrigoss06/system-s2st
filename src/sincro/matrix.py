"""make matrix-test - F5. Recorre los 20 pares dirigidos y recalibra la expansion.

Un WAV fijo por idioma fuente. La transcripcion NO depende del idioma destino, asi que se
hace una vez por fuente y se reutiliza para los cuatro destinos: 5 llamadas a Deepgram en
vez de 20.

La sintesis se hace de una pieza por par, a `speed=1.0`, para medir la relacion de
duracion **natural** entre destino y fuente. Ese es el numero con el que se recalibran los
factores de expansion: el documento tecnico dice que la duracion de audio real es la senal
fiable, no los bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import numpy as np

from .adapters.file_io import read_wav_frames, write_dubbed_wav
from .committer import PunctuationCommitter
from .config import DEEPGRAM_CODE, SUPPORTED_LANGS, Settings, load_settings, profile_for
from .contracts import DubbedChunk, Lang, Segment
from .synthesizer import FishSynthesizer
from .transcriber import DeepgramTranscriber
from .translator import GroqTranslator

logger = logging.getLogger(__name__)

FIXTURE_TMPL: Final[str] = "tests/fixtures/matrix_{lang}.wav"

# Invariantes del guion fijo: numeros, fechas, cantidades y nombres propios que la
# traduccion debe preservar en cualquier par. Viven aqui y no en tests/ porque el motor
# no puede depender del arbol de pruebas.
INVARIANTS: Final[tuple[str, ...]] = (
    "12", "2026", "3", "7", "15", "2027", "9", "30", "48291", "5400", "2", "28", "4",
    "Rodrigo", "Arequipa", "Lima", "Cusco", "Mariana", "Diego",
)
DEEPGRAM_URL: Final[str] = "https://api.deepgram.com/v1/listen"
TTS_SAMPLE_RATE: Final[int] = 44_100


# El japones escribe los numeros en kanji y los nombres propios en katakana. Sin
# traducirlos, el comparador de invariantes da falsos negativos y culpa al traductor de
# fallos que no existen. Primera version de F5 puntuo es->ja como 13/19 por esto.
_KANJI_DIGITS: Final[dict[str, int]] = {
    # \u3007 es el cero ideografico, no la letra O.
    "\u3007": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_KANJI_UNITS: Final[dict[str, int]] = {"十": 10, "百": 100, "千": 1000}
_KANJI_BIG: Final[dict[str, int]] = {"万": 10_000, "億": 100_000_000}
_KANJI_ALL: Final[str] = "".join(_KANJI_DIGITS) + "".join(_KANJI_UNITS) + "".join(_KANJI_BIG)

# Transliteraciones que produce Deepgram en japones. Se aceptan variantes: el STT escribe
# a veces ロディゴ en vez de ロドリゴ.
_KATAKANA_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "Rodrigo": ("ロドリゴ", "ロディゴ"),
    "Arequipa": ("アレキパ", "アレキーパ"),
    "Lima": ("リマ",),
    "Cusco": ("クスコ",),
    "Mariana": ("マリアナ",),
    "Diego": ("ディエゴ", "ディゴ"),
}


def _kanji_to_int(token: str) -> int | None:
    """Convierte un numero en kanji a entero. Devuelve None si no lo es."""
    total = 0
    section = 0
    current = 0
    seen = False
    for ch in token:
        if ch in _KANJI_DIGITS:
            current = _KANJI_DIGITS[ch]
            seen = True
        elif ch in _KANJI_UNITS:
            section += (current or 1) * _KANJI_UNITS[ch]
            current = 0
            seen = True
        elif ch in _KANJI_BIG:
            total += (section + current) * _KANJI_BIG[ch]
            section = current = 0
            seen = True
        else:
            return None
    return total + section + current if seen else None


def expand_kanji_numbers(text: str) -> str:
    """Anade la forma arabiga de cada numero en kanji, conservando el original."""
    out: list[str] = [text]
    run = ""
    for ch in text + " ":
        if ch in _KANJI_ALL:
            run += ch
        else:
            if run:
                value = _kanji_to_int(run)
                if value is not None:
                    out.append(str(value))
                run = ""
    return " ".join(out)


# Fin del bloque latino extendido. Por encima, un signo combinante ya no es un acento
# que se pueda tirar: en japones el dakuten distingue ロドリゴ de ロトリコ.
_LATIN_MAX: Final[int] = 0x024F


def normalize(text: str) -> str:
    """Minusculas y sin acentos latinos, conservando los diacriticos del japones."""
    out: list[str] = []
    base_is_latin = False
    for ch in unicodedata.normalize("NFKD", text.lower()):
        if unicodedata.combining(ch):
            if not base_is_latin:
                out.append(ch)
            continue
        base_is_latin = ord(ch) <= _LATIN_MAX
        out.append(ch)
    return unicodedata.normalize("NFKC", "".join(out))


def find_invariants(text: str, invariants: tuple[str, ...]) -> set[str]:
    """Cuales de los invariantes aparecen, contando kanji y katakana como equivalentes."""
    # NFKC pasa los digitos de ancho completo a ASCII; luego se anaden los kanji.
    wide = unicodedata.normalize("NFKC", text)
    hay = normalize(expand_kanji_numbers(wide))
    found = set()
    for inv in invariants:
        if normalize(inv) in hay:
            found.add(inv)
            continue
        if any(alias in text for alias in _KATAKANA_ALIASES.get(inv, ())):
            found.add(inv)
    return found


@dataclass
class PairResult:
    src: Lang
    dst: Lang
    segments: int = 0
    src_speech_s: float = 0.0
    dst_audio_s: float = 0.0
    bytes_in: int = 0
    bytes_out: int = 0
    tokens_out: int = 0
    skipped: int = 0
    leaked: int = 0
    roundtrip_conf: float = 0.0
    roundtrip_text: str = ""
    invariants_src: set[str] = field(default_factory=set)
    invariants_dst: set[str] = field(default_factory=set)
    error: str = ""

    @property
    def duration_ratio(self) -> float:
        return self.dst_audio_s / self.src_speech_s if self.src_speech_s else 0.0

    @property
    def byte_ratio(self) -> float:
        return self.bytes_out / self.bytes_in if self.bytes_in else 0.0

    @property
    def invariants_kept(self) -> tuple[int, int]:
        """Solo cuentan los invariantes que sobrevivieron al STT de la fuente.

        Si 'Arequipa' se perdio al transcribir el WAV origen, perderlo en el destino no
        es culpa del traductor y no debe puntuar en su contra.
        """
        expected = self.invariants_src
        return len(expected & self.invariants_dst), len(expected)


async def transcribe_source(s: Settings, lang: Lang, path: Path) -> tuple[list[Segment], float]:
    tr = DeepgramTranscriber(s.deepgram_api_key, lang, DEEPGRAM_CODE[lang])
    committer = PunctuationCommitter()
    segments = [seg async for seg in committer.commit(tr.stream(read_wav_frames(path)))]
    speech = sum(seg.source_duration for seg in segments)
    return segments, speech


async def roundtrip(api_key: str, wav: bytes, lang: Lang) -> tuple[float, str]:
    params = {
        "model": "nova-3",
        "language": DEEPGRAM_CODE[lang],
        "punctuate": "true",
        "smart_format": "true",
    }
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "audio/wav"}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(DEEPGRAM_URL, params=params, headers=headers, content=wav)
    if r.status_code != 200:
        return 0.0, f"(STT HTTP {r.status_code})"
    data: dict[str, Any] = r.json()
    ch = data.get("results", {}).get("channels", [])
    if not ch or not ch[0].get("alternatives"):
        return 0.0, ""
    alt = ch[0]["alternatives"][0]
    return float(alt.get("confidence", 0.0)), str(alt.get("transcript", ""))


async def run_pair(
    s: Settings,
    src: Lang,
    dst: Lang,
    segments: list[Segment],
    src_speech: float,
    src_invariants: set[str],
    synth: FishSynthesizer,
    out_dir: Path,
) -> PairResult:
    res = PairResult(src=src, dst=dst, src_speech_s=src_speech, invariants_src=src_invariants)
    profile = profile_for(src, dst)
    translator = GroqTranslator(
        s.groq_api_key,
        s.llm_model,
        profile,
        reasoning_effort=s.llm_reasoning_effort,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
    )

    parts: list[str] = []
    for seg in segments:
        budget = translator.budget_for(seg)
        tr = await translator.translate(seg, budget)
        res.bytes_in += len(seg.text.encode("utf-8"))
        res.bytes_out += tr.byte_actual
        res.tokens_out += translator.tokens_out
        if tr.text:
            parts.append(tr.text)
    res.segments = len(segments)
    res.skipped = translator.skipped
    res.leaked = translator.leaked

    dst_text = " ".join(parts)
    res.invariants_dst = find_invariants(dst_text, INVARIANTS)

    from .contracts import Translation

    whole = Translation(seg_id=0, text=dst_text, lang=dst, byte_budget=0, byte_actual=0)
    chunks: list[DubbedChunk] = []
    async for chunk in synth.synthesize(whole, reference_id="", speed=1.0):
        chunks.append(chunk)
    if not chunks:
        res.error = "no audio"
        return res
    res.dst_audio_s = sum(c.audio_duration for c in chunks)

    path = out_dir / f"matrix_{src}_to_{dst}.wav"
    write_dubbed_wav(path, chunks, TTS_SAMPLE_RATE)
    pcm = np.concatenate([c.pcm for c in chunks])
    import io

    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, pcm, chunks[0].sample_rate, format="WAV", subtype="PCM_16")
    res.roundtrip_conf, res.roundtrip_text = await roundtrip(
        s.deepgram_api_key, buf.getvalue(), dst
    )
    return res


def render_matrix(results: list[PairResult]) -> str:
    lines: list[str] = []
    a = lines.append
    a("MATRIZ DE LOS 20 PARES DIRIGIDOS")
    a("")
    a(f"{'par':<14}{'seg':>4}{'fuente s':>10}{'destino s':>10}{'ratio dur':>10}"
      f"{'ratio byt':>10}{'RT conf':>9}{'invar':>8}{'skip':>6}")
    a("-" * 81)
    for r in results:
        kept, total = r.invariants_kept
        inv = f"{kept}/{total}" if total else "-"
        a(f"{r.src + ' -> ' + r.dst:<14}{r.segments:>4}{r.src_speech_s:>10.2f}"
          f"{r.dst_audio_s:>10.2f}{r.duration_ratio:>10.3f}{r.byte_ratio:>10.3f}"
          f"{r.roundtrip_conf:>9.3f}{inv:>8}{r.skipped:>6}")
    return "\n".join(lines)


def recalibrate(results: list[PairResult]) -> list[tuple[Lang, Lang, float, float]]:
    """Nuevo factor de expansion por par, a partir de la duracion medida.

    El factor vigente produjo una relacion de duracion `r`. Para que el destino dure lo
    mismo que la fuente hay que escalar el presupuesto por 1/r. Los pares con japones no
    usan presupuesto de bytes, asi que no se recalibran.
    """
    out: list[tuple[Lang, Lang, float, float]] = []
    for r in results:
        if r.src == "ja" or r.dst == "ja" or r.duration_ratio <= 0:
            continue
        current = profile_for(r.src, r.dst).expansion
        out.append((r.src, r.dst, current, round(current / r.duration_ratio, 3)))
    return out


async def run_matrix(
    s: Settings, out_dir: Path, only: str | None, apply: bool = False
) -> int:
    await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
    synth = FishSynthesizer(s.fish_api_key, model=s.tts_model, sample_rate=TTS_SAMPLE_RATE)
    results: list[PairResult] = []
    source_texts: dict[str, str] = {}
    t0 = time.monotonic()

    try:
        for src in SUPPORTED_LANGS:
            if only and src != only:
                continue
            path = Path(FIXTURE_TMPL.format(lang=src))
            if not await asyncio.to_thread(path.is_file):
                print(f"  FALTA {path}, se omite {src}", file=sys.stderr)
                continue
            print(f"\n=== fuente {src}: transcribiendo {path.name} ===")
            segments, speech = await transcribe_source(s, src, path)
            src_text = " ".join(seg.text for seg in segments)
            src_inv = find_invariants(src_text, INVARIANTS)
            source_texts[src] = src_text
            print(f"  {len(segments)} segmentos, {speech:.2f}s de habla, "
                  f"{len(src_inv)}/{len(INVARIANTS)} invariantes sobreviven al STT")
            missing = sorted(set(INVARIANTS) - src_inv)
            if missing:
                print(f"  perdidos ya en la fuente: {', '.join(missing)}")

            for dst in SUPPORTED_LANGS:
                if dst == src:
                    continue
                print(f"  -> {dst} ...", end="", flush=True)
                r = await run_pair(s, src, dst, segments, speech, src_inv, synth, out_dir)
                results.append(r)
                kept, total = r.invariants_kept
                print(f" ratio {r.duration_ratio:.3f}  RT {r.roundtrip_conf:.3f}  "
                      f"invar {kept}/{total}")
    finally:
        await synth.aclose()

    print()
    print(render_matrix(results))
    print()
    print(f"  {len(results)} pares en {time.monotonic() - t0:.0f}s")

    calib = recalibrate(results)
    if calib:
        print()
        print("FACTORES DE EXPANSION RECALIBRADOS (por duracion medida)")
        for csrc, cdst, old, new in calib:
            print(f"  {csrc + ' -> ' + cdst:<14} {old:.3f} -> {new:.3f}")

    payload = {
        "pairs": [
            {
                "src": r.src,
                "dst": r.dst,
                "segments": r.segments,
                "src_speech_s": round(r.src_speech_s, 3),
                "dst_audio_s": round(r.dst_audio_s, 3),
                "duration_ratio": round(r.duration_ratio, 4),
                "byte_ratio": round(r.byte_ratio, 4),
                "roundtrip_confidence": round(r.roundtrip_conf, 4),
                "roundtrip_text": r.roundtrip_text,
                "invariants_kept": r.invariants_kept[0],
                "invariants_expected": r.invariants_kept[1],
                "skipped": r.skipped,
                "leaked": r.leaked,
                "error": r.error,
            }
            for r in results
        ],
        "source_texts": source_texts,
        "recalibrated": {f"{a}->{b}": {"old": o, "new": n} for a, b, o, n in calib},
    }
    report = out_dir / "matrix.json"
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  detalle -> {report}")

    if apply and calib:
        from .config import CALIBRATION_PATH

        CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATION_PATH.write_text(
            json.dumps(
                {
                    "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "llm_model": s.llm_model,
                    "note": "medidos sobre duracion de audio real, F5. Los pares con ja "
                    "no llevan presupuesto de bytes y no figuran.",
                    "pairs": {f"{a}->{b}": n for a, b, _o, n in calib},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  factores aplicados -> {CALIBRATION_PATH}")
    elif calib:
        print("\n  (para fijarlos: make matrix-test ARGS=--apply)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="matrix-test", description="F5 los 20 pares dirigidos")
    ap.add_argument("--out-dir", type=Path, default=Path("out/matrix"))
    ap.add_argument("--only", default=None, help="corre solo este idioma fuente")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="escribe out/expansion.json con los factores medidos, que config.py lee "
        "por delante de los teoricos",
    )
    args = ap.parse_args()

    s = load_settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(run_matrix(s, args.out_dir, args.only, args.apply))
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
