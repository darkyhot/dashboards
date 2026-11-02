"""Воспроизводимая локальная приёмка на пяти независимых панелях.

python3 -m ptf.validation --output output/watch_validation
Код возврата 1 означает невыполненный критерий, а не успешную приёмку.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import acceptance, bench


def run(out_dir="output/watch_validation", n_orgs=1200, per_cell=6, per_size=3):
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    records = []
    profiles = [("standard", 191, 1., False, False), ("multiple_gosb", 211, 1., True, False),
                ("heavy_noise", 227, 1.6, False, False), ("events_in_history", 241, 1., True, True),
                ("varying_reasons", 257, 1.6, True, False)]
    for name, seed, scale, spread, history in profiles:
        print(f"\nПанель {name}; seed={seed}", flush=True)
        p = bench.simulate(seed=seed, n_orgs=n_orgs, gosb_per_tb=4, noise_scale=scale)
        if name == "varying_reasons":
            p = bench.vary_departure_reasons(p, seed=seed + 3)
        if spread:
            p = bench.spread_organizations(p, seed=seed + 1)
        if history:
            p = bench.with_history_events(p, seed=seed + 2)
        report = acceptance.run(p, folder / name, compare=True, known_clean=not history,
                                max_details=6, seed=seed + 1000, per_cell=per_cell, per_size=per_size)
        data = report["data"]
        for check in data["checks"].to_dict("records"):
            records.append({"profile": name, **check})
        print(data["checks"].to_string(index=False), flush=True)
        # Промежуточный итог остаётся доступен при прерывании длинного прогона.
        (folder / "summary.json").write_text(json.dumps(acceptance._plain(records),
            ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/watch_validation")
    parser.add_argument("--organizations", type=int, default=1200)
    parser.add_argument("--per-cell", type=int, default=6)
    parser.add_argument("--per-size", type=int, default=3)
    args = parser.parse_args()
    checks = run(args.output, args.organizations, args.per_cell, args.per_size)
    raise SystemExit(0 if all(row["пройдена"] for row in checks) else 1)
