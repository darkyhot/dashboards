"""Простой прогресс-логгер для тетрадки: печатает шаги, (опц.) SQL и вызовы LLM.

Включается из generate_dashboard(verbose=..., show_sql=..., show_llm=...).
Полные промпты/ответы LLM пишутся в файлы (output/llm_logs/), в тетрадку идёт
короткая выжимка: размер промпта, finish_reason, usage, покрытие. При пустом или
битом ответе кусок сырого тела печатается ВСЕГДА — иначе причину не понять.
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path

ENABLED = False
SHOW_SQL = False
SHOW_LLM = False
LOG_DIR: Path | None = None
_t0 = None
_seq = 0

HEAD_TAIL = 1500      # сколько симв. промпта печатать в тетрадку при show_llm
RAW_HEAD = 500        # сколько симв. сырого ответа печатать при сбое


def enable(verbose: bool = True, show_sql: bool = False, show_llm: bool = False,
           llm_log_dir: "Path | str | None" = ...) -> None:
    global ENABLED, SHOW_SQL, SHOW_LLM, LOG_DIR, _t0, _seq
    ENABLED = bool(verbose)
    SHOW_SQL = bool(show_sql)
    SHOW_LLM = bool(show_llm)
    if llm_log_dir is ...:                     # по умолчанию — output/llm_logs
        from .config import OUTPUT_DIR
        LOG_DIR = OUTPUT_DIR / "llm_logs"
    else:
        LOG_DIR = Path(llm_log_dir) if llm_log_dir else None
    _t0 = time.time()
    _seq = 0


def _ts() -> str:
    return f"{time.time() - _t0:5.1f}s" if _t0 else "  -  "


def step(msg: str) -> None:
    if ENABLED:
        print(f"[{_ts()}] → {msg}", flush=True, file=sys.stdout)


def done(msg: str) -> None:
    if ENABLED:
        print(f"[{_ts()}]   ✓ {msg}", flush=True, file=sys.stdout)


def warn(msg: str) -> None:
    if ENABLED:
        print(f"[{_ts()}]   ⚠ {msg}", flush=True, file=sys.stdout)


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


# --------------------------------------------------------------------------- #
def _clip(text: str, head: int = HEAD_TAIL) -> str:
    """Голова и хвост длинного текста (середина промпта в тетрадке не нужна)."""
    text = text or ""
    if len(text) <= head * 2:
        return text
    return f"{text[:head]}\n… [пропущено {len(text) - head * 2} симв.] …\n{text[-head:]}"


def llm_dump(label: str, prompt: str, response: str, meta: dict | None = None) -> str | None:
    """Записать полный запрос/ответ в файл. Возвращает путь (или None)."""
    if LOG_DIR is None:
        return None
    global _seq
    _seq += 1
    seq = _seq
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w.-]+", "_", label)[:60]
        path = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{seq:03d}_{slug}.txt"
        path.write_text(
            f"=== META ===\n{meta}\n\n"
            f"=== REQUEST ({len(prompt or '')} симв.) ===\n{prompt}\n\n"
            f"=== RESPONSE ({len(response or '')} симв.) ===\n{response}\n",
            encoding="utf-8",
        )
        return str(path)
    except Exception as ex:      # логи не должны ронять генерацию
        warn(f"не удалось записать лог LLM: {type(ex).__name__}: {ex}")
        return None


def llm_request(label: str, prompt: str, note: str = "") -> None:
    """Краткая строка о запросе всегда; полный текст — при show_llm."""
    if not ENABLED:
        return
    extra = f" · {note}" if note else ""
    print(f"[{_ts()}]   → LLM [{label}]: запрос {len(prompt or '')} симв.{extra}", flush=True)
    if SHOW_LLM:
        _block(f"LLM запрос [{label}]", _clip(prompt))


def llm_response(label: str, resp: str, meta: dict | None = None, ok: bool = True) -> None:
    """Ответ LLM: длина + finish_reason + usage. При сбое — кусок сырого тела."""
    if not ENABLED:
        return
    meta = meta or {}
    n = len(resp or "")
    tail = ""
    if meta:
        tail = (f" · finish_reason={meta.get('finish_reason')}"
                f" · usage={meta.get('usage')}"
                f" · reasoning={meta.get('reasoning_len')} симв."
                f" · {meta.get('elapsed')}s")
    print(f"[{_ts()}]   ✓ LLM [{label}]: ответ {n} симв.{tail}", flush=True)
    if SHOW_LLM:
        _block(f"LLM ответ [{label}]", _clip(resp, 3000))
    elif not ok or n == 0:
        # Именно этого не хватало при разборе пустых ответов на проме.
        _block(f"LLM сырой ответ [{label}] (первые {RAW_HEAD} симв.)",
               (resp or "")[:RAW_HEAD] or "(пусто)")
        if meta.get("raw_head"):
            _block(f"LLM тело HTTP [{label}]", meta["raw_head"])


def llm_error(label: str, err) -> None:
    """Ошибка LLM (в т.ч. текст ошибки API) — печатается всегда при verbose."""
    if ENABLED:
        print(f"[{_ts()}]   ⚠ LLM [{label}] ОШИБКА: {err}", flush=True)
