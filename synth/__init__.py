"""Сборка синтетической БД открытого контура (одной командой)."""
from __future__ import annotations

from sqlalchemy.engine import Engine

from uzp_dash import config
from uzp_dash.db import get_engine

from .generate import generate_all
from .reference import apply_schema, load_reference_csvs


def build(conn: str | None = None) -> dict:
    """Пересоздать схему, загрузить справочники, сгенерировать синтетику.

    conn — SQLAlchemy URL локального Postgres (иначе берётся UZP_DB_URL из .env).
    Возвращает словарь {таблица: число строк}.
    """
    engine: Engine = get_engine(config.db_url(conn))
    apply_schema(engine)
    counts = {}
    counts.update(load_reference_csvs(engine))
    counts.update(generate_all(engine))
    return counts


__all__ = ["build", "apply_schema", "load_reference_csvs", "generate_all"]
