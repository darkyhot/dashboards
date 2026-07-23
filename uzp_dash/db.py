"""Слой доступа к БД. Все дэши ходят в БД только отсюда.

Одинаково работает с локальным Postgres (открытый контур) и пром-БД
(закрытый контур) — различается только URL подключения.
"""
from __future__ import annotations

from functools import lru_cache

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from . import config


@lru_cache(maxsize=8)
def get_engine(url: str) -> Engine:
    """SQLAlchemy engine (кэшируется по URL)."""
    return create_engine(url, pool_pre_ping=True, future=True)


def read_sql(engine: Engine, sql: str, params: dict | None = None) -> pd.DataFrame:
    """Выполнить SELECT и вернуть DataFrame.

    В SQL используйте плейсхолдер {schema} — он подставляется автоматически,
    и именованные параметры :name (безопасная подстановка значений).
    """
    sql = sql.format(schema=config.SCHEMA)
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def ping(engine: Engine) -> bool:
    """Проверка соединения."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
