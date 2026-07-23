"""Простой прогресс-логгер для тетрадки: печатает шаги и (опц.) SQL.

Включается из generate_dashboard(verbose=..., show_sql=...). По умолчанию выключен,
чтобы не шуметь в не-интерактивных вызовах.
"""
from __future__ import annotations

import sys
import time

ENABLED = False
SHOW_SQL = False
SHOW_LLM = False
_t0 = None


def enable(verbose: bool = True, show_sql: bool = False, show_llm: bool = False) -> None:
    global ENABLED, SHOW_SQL, SHOW_LLM, _t0
    ENABLED = bool(verbose)
    SHOW_SQL = bool(show_sql)
    SHOW_LLM = bool(show_llm)
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


def _block(title: str, body: str) -> None:
    print(f"        ┌─ {title}", flush=True)
    for ln in str(body).splitlines() or [""]:
        print("        │ " + ln, flush=True)
    print("        └─", flush=True)


def llm_request(label: str, prompt: str) -> None:
    """Показать текст запроса к LLM — только при show_llm."""
    if ENABLED and SHOW_LLM:
        _block(f"LLM запрос [{label}]", prompt)


def llm_response(label: str, resp: str) -> None:
    """Показать ответ LLM. При show_llm — полностью, иначе — краткий статус."""
    if not ENABLED:
        return
    if SHOW_LLM:
        _block(f"LLM ответ [{label}]", resp)
    else:
        n = len(resp or "")
        print(f"[{_ts()}]   ✓ LLM [{label}]: ответ {n} симв.", flush=True)


def llm_error(label: str, err) -> None:
    """Ошибка LLM (в т.ч. текст ошибки API) — печатается всегда при verbose."""
    if ENABLED:
        print(f"[{_ts()}]   ⚠ LLM [{label}] ОШИБКА: {err}", flush=True)
