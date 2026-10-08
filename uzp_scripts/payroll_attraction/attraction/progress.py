"""Прогресс в консоль тетрадки. `flush=True` обязателен: без него вывод копится в
буфере и появляется пачкой в конце, и молчащий прогон неотличим от зависшего."""
from __future__ import annotations

import sys
import time

ENABLED = True
SHOW_SQL = False
_t0: float | None = None


def enable(verbose: bool = True, show_sql: bool = False) -> None:
    global ENABLED, SHOW_SQL, _t0
    ENABLED = bool(verbose)
    SHOW_SQL = bool(show_sql)
    _t0 = time.time()


def _ts() -> str:
    return f"{time.time() - _t0:6.1f}s" if _t0 else "   -  "


def step(msg: str) -> None:
    if ENABLED:
        print(f"[{_ts()}] → {msg}", flush=True, file=sys.stdout)


def done(msg: str) -> None:
    if ENABLED:
        print(f"[{_ts()}]   ✓ {msg}", flush=True, file=sys.stdout)


def warn(msg: str) -> None:
    # Предупреждения печатаются ВСЕГДА, даже при verbose=False: это то, что
    # нельзя пропустить (невязка, отключённый раздел, обрезанная выборка).
    print(f"[{_ts()}]   ⚠ {msg}", flush=True, file=sys.stdout)


def sql(query: str, params: dict | None = None) -> None:
    if not (ENABLED and SHOW_SQL):
        return
    print("        ┌─ SQL" + (f"  params={params}" if params else ""), flush=True)
    for ln in query.strip("\n").splitlines():
        if ln.strip():
            print("        │ " + ln.rstrip(), flush=True)
    print("        └─", flush=True)
