"""Реестр дэшбордов. Дэш регистрируется декоратором @dashboard("имя").

generate_dashboard() — единственная точка входа для управляющего ноутбука.
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config, llm, progress
from .db import get_engine
from .llm import complete

# Контекст, который прокидывается в генератор дэша.
@dataclass
class Context:
    engine: object          # SQLAlchemy Engine
    llm: Callable[..., str] # llm.complete
    params: dict
    output_dir: Path


_REGISTRY: dict[str, Callable[[Context], str]] = {}
_LOADED = False


def dashboard(name: str):
    """Декоратор регистрации. Функция принимает Context, возвращает HTML-строку."""
    def deco(fn: Callable[[Context], str]):
        _REGISTRY[name] = fn
        fn._dashboard_name = name  # type: ignore[attr-defined]
        return fn
    return deco


def _ensure_loaded() -> None:
    """Импортировать все модули пакета dashboards, чтобы сработали декораторы."""
    global _LOADED
    if _LOADED:
        return
    from . import dashboards as pkg
    for mod in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg.__name__}.{mod.name}")
    _LOADED = True


def list_dashboards() -> list[str]:
    _ensure_loaded()
    return sorted(_REGISTRY)


def generate_dashboard(name: str, conn: str | None = None, params: dict | None = None,
                       contour: str | None = None, verbose: bool = True,
                       show_sql: bool = False, show_llm: bool = False,
                       llm_opts: dict | None = None) -> str:
    """Сгенерировать дэш по имени. Возвращает путь к .html.

    conn     — SQLAlchemy URL (если не задан, берётся из .env UZP_DB_URL).
    params   — параметры конкретного дэша (напр. {"tb": "ЮЗБ", "llm_top_n": 20}).
    contour  — 'open' (DeepSeek) | 'closed' (GLM/Qwen). Управляется из тетрадки.
    verbose  — печатать прогресс генерации (для тетрадки). По умолчанию True.
    show_sql — дополнительно печатать SQL-запросы.
    show_llm — дополнительно печатать текст запросов/ответов LLM.
    llm_opts — настройки вызова LLM: {"max_tokens": 4000, "extra": {...},
               "timeout": (10, 120)}. Полные логи всегда пишутся в output/llm_logs/.
    """
    _ensure_loaded()
    if name not in _REGISTRY:
        raise KeyError(f"Дэш '{name}' не найден. Доступные: {list_dashboards()}")

    progress.enable(verbose=verbose, show_sql=show_sql, show_llm=show_llm)
    config.set_contour(contour)
    opts = llm.configure(**(llm_opts or {}))
    model = llm._model_for(config.CONTOUR, None)
    progress.done(f"LLM: модель {model}"
                  + ("" if opts.get("model") else " (из .env, llm_opts['model'] не задан)")
                  + f" · max_tokens={opts['max_tokens']} · extra={opts['extra']} · "
                  f"timeout={opts['timeout']} · логи → {progress.LOG_DIR}")
    progress.step(f"Дэш «{name}» · контур {config.CONTOUR} · подключение к БД")
    engine = get_engine(config.db_url(conn))
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ctx = Context(engine=engine, llm=complete, params=params or {}, output_dir=config.OUTPUT_DIR)

    html = _REGISTRY[name](ctx)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.OUTPUT_DIR / f"{name}_{ts}.html"
    out.write_text(html, encoding="utf-8")
    progress.done(f"Готово: {out}")
    return str(out)
