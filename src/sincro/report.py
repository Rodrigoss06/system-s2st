"""make report - agrega el JSONL de telemetria.

P50 P90 P99 de TTFA, distribucion de triggers, curva de deriva y coste acumulado.
Sin este agregado no se puede verificar ningun criterio de aceptacion.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

TARGET_DISTRIBUTION = {
    "eou": (0.55, 0.70),
    "punctuation": (0.20, 0.35),
    "timeout": (0.0, 0.10),
    "max_len": (0.0, 0.03),
}


def percentile(values: list[float], p: float) -> float:
    """Percentil por interpolacion lineal. Sin numpy: report no necesita el coste."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def latest_jsonl(out_dir: Path) -> Path | None:
    files = sorted(out_dir.glob("telemetry-*.jsonl"))
    return files[-1] if files else None


def load(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def render(path: Path, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"{path}: sin filas."

    ttfa = [float(r["ttfa_ms"]) for r in rows]
    src = sum(float(r["source_duration_s"]) for r in rows)
    dub = sum(float(r["audio_duration_s"]) for r in rows)
    cost = sum(float(r["cost_usd"]) for r in rows)
    tin = sum(int(r["tokens_in"]) for r in rows)
    tout = sum(int(r["tokens_out"]) for r in rows)
    bin_ = sum(int(r["bytes_in"]) for r in rows)
    bout = sum(int(r["bytes_out"]) for r in rows)
    speeds = [float(r["speed_applied"]) for r in rows]
    drifts = [float(r["drift_s"]) for r in rows]
    triggers = Counter(str(r["trigger"]) for r in rows)
    n = len(rows)

    lines: list[str] = []
    a = lines.append
    a(f"SINCRO report - {path}")
    a(f"{n} segmentos   {rows[0]['lang_src']} -> {rows[0]['lang_dst']}")
    a("")
    a("TTFA")
    a(f"  P50 {percentile(ttfa, 0.50):>9.0f} ms")
    a(f"  P90 {percentile(ttfa, 0.90):>9.0f} ms")
    a(f"  P99 {percentile(ttfa, 0.99):>9.0f} ms")
    a(f"  min {min(ttfa):>9.0f} ms    max {max(ttfa):.0f} ms")
    a("")
    a("Triggers")
    for name in ("eou", "punctuation", "timeout", "max_len"):
        c = triggers.get(name, 0)
        pct = c / n
        lo, hi = TARGET_DISTRIBUTION[name]
        mark = "ok " if lo <= pct <= hi else "!! "
        a(f"  {mark}{name:<12} {c:>3}  {pct:>6.1%}   objetivo {lo:.0%}-{hi:.0%}")
    for name in sorted(set(triggers) - set(TARGET_DISTRIBUTION)):
        a(f"     {name:<12} {triggers[name]:>3}")
    if not triggers.get("eou"):
        # En F1 no hay turn-detector, asi que eou no puede dispararse y la distribucion
        # objetivo todavia no aplica. Sin esta nota los !! se leen como un fallo.
        a("     nota: sin eou -> turn-detector inactivo (F2). La distribucion")
        a("           objetivo se evalua a partir de F2.")
    a("")
    a("Isocronia y deriva")
    a(f"  audio fuente    {src:>8.2f} s")
    a(f"  audio doblado   {dub:>8.2f} s")
    a(f"  deriva final    {drifts[-1]:>+8.2f} s")
    a(f"  deriva max      {max(drifts, key=abs):>+8.2f} s")
    a(f"  speed  min/max  {min(speeds):.3f} / {max(speeds):.3f}")
    a(f"  bytes  in/out   {bin_} / {bout}"
      + (f"   ratio {bout / bin_:.3f}" if bin_ else ""))
    a("")
    a("Coste")
    a(f"  tokens in/out   {tin} / {tout}")
    a(f"  total           ${cost:.6f}")
    if src > 0:
        a(f"  por minuto      ${cost / (src / 60):.6f}/min de habla")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(prog="report", description="Agrega el JSONL de telemetria")
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=Path("out"))
    args = ap.parse_args()

    path = args.file or latest_jsonl(args.out_dir)
    if path is None or not path.is_file():
        print(
            f"ERROR: no hay telemetria en {args.out_dir}/. Ejecuta make dub-file primero.",
            file=sys.stderr,
        )
        return 1
    print(render(path, load(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
