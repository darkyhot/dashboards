"""Простой прогресс-логгер для тетрадки: печатает шаги и (опц.) SQL.

Включается из generate_dashboard(verbose=..., show_sql=...). По умолчанию выключен,
чтобы не шуметь в не-интерактивных вызовах.
"""
from __future__ import annotations

import sys
import time

ENABLED = False
SHOW_SQL = False
_t0 = None


def enable(verbose: bool = True, show_sql: bool = False) -> None:
    global ENABLED, SHOW_SQL, _t0
    ENABLED = bool(verbose)
    SHOW_SQL = bool(show_sql)
    _t0 = time.time()


def _ts() -> str:
    return f"{time.time() - _t0:5.1f}s" if _t0 else "  -  "


def step(msg: str) -> None:
    if ENABLED:
        print(f"[{_ts()}] → {msg}", flush=True, file=sys.stdout)


def done(msg: str) -> None:
    if ENABLED:
        print(f"[{_ts()}]   ✓ {msg}", flush=True, file=sys.stdout)


def sql(query: str, params: dict | None = None) -> None:
    """Показать SQL (компактно) — только при show_sql."""
    if not (ENABLED and SHOW_SQL):
        return
    lines = [ln for ln in query.strip("\n").splitlines() if ln.strip()]
    print("        ┌─ SQL" + (f"  params={params}" if params else ""), flush=True)
    for ln in lines:
        print("        │ " + ln.rstrip(), flush=True)
    print("        └─", flush=True)
