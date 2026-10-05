"""Доступ к БД. Одинаково работает с локальным Postgres и с Greenplum на ядре 9.4 —
различается только URL.

Плейсхолдеры `{schema}` и `{code_col}` подставляются ЗАМЕНОЙ ПО ИМЕНИ, а не
`str.format()`: так квантификатор регулярки `{1,12}` в тексте SQL не нужно
удваивать, и ловушка «IndexError: Replacement index out of range» исключена.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from . import config, progress

# Имя колонки кода зачисления определяет разведка (на проме — `enrollment_type`,
# в постановке — `enrollment_type_id`). До разведки стоит прод-имя.
CODE_COL = "enrollment_type"

_KERBEROS_MARKERS = (
    "kerberos", "gssapi", "gss_", "krb5", "ticket expired",
    "credentials cache", "no credentials",
    "server not found in kerberos database",
)


class KerberosTicketError(RuntimeError):
    pass


def raise_if_kerberos(ex: Exception) -> None:
    """Протухший тикет выглядит как невнятная ошибка драйвера — распознаём и
    останавливаемся с инструкцией: ни один следующий запрос всё равно не пройдёт."""
    t = f"{type(ex).__name__}: {ex}".lower()
    if any(m in t for m in _KERBEROS_MARKERS):
        print("=" * 70, flush=True)
        print("ОСТАНОВЛЕНО: недействительный или отсутствующий Kerberos ticket.", flush=True)
        print("Обновите Kerberos ticket — выполните `kinit` в консоли,", flush=True)
        print("затем перезапустите ячейку.", flush=True)
        print("=" * 70, flush=True)
        raise KerberosTicketError("Обновите Kerberos ticket: выполните kinit в консоли") from ex


@lru_cache(maxsize=8)
def get_engine(url: str, timeout_min: int = config.SQL_TIMEOUT_MIN) -> Engine:
    """statement_timeout — через options драйвера, а не отдельным SET: в пуле
    несколько соединений, и SET на одном не действует на остальные."""
    ms = int(timeout_min) * 60 * 1000
    return create_engine(url, pool_pre_ping=True, future=True,
                         connect_args={"options": f"-c statement_timeout={ms}"})


@contextmanager
def session(engine: Engine):
    """Одно соединение на весь прогон: в нём живут временные таблицы рабочего
    набора. Из пула на каждый запрос они были бы не видны."""
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()


def render(sql: str) -> str:
    """Подставить схему и колонку кода — ровно так, как перед исполнением.

    Этот же текст показывается читателю у блока отчёта: собирать показ вторым
    способом нельзя, он разъедется с исполняемым молча.
    """
    return sql.replace("{schema}", config.SCHEMA).replace("{code_col}", CODE_COL)


def execute(conn: Connection, sql: str, params: dict | None = None) -> int:
    """Оператор без результата (CREATE/INSERT/ANALYZE/DROP) с коммитом.

    Коммит обязателен: SQLAlchemy 2.x сам открывает транзакцию и без коммита
    откатит её при закрытии — временная таблица исчезла бы, когда понадобилась.
    """
    sql = render(sql)
    progress.sql(sql, params)
    try:
        res = conn.execute(text(sql), params or {})
        conn.commit()
        return int(res.rowcount or 0)
    except Exception as ex:
        raise_if_kerberos(ex)
        raise


def read_sql(conn: Connection, sql: str, params: dict | None = None) -> pd.DataFrame:
    sql = render(sql)
    progress.sql(sql, params)
    try:
        return pd.read_sql(text(sql), conn, params=params or {})
    except Exception as ex:
        raise_if_kerberos(ex)
        raise


def rollback(conn: Connection) -> None:
    """Откат после упавшего оператора. Без него КАЖДЫЙ следующий запрос сессии
    падает с «current transaction is aborted», и ошибка одного блока молча уносит
    все разделы после него (так уже было на проме). Временные таблицы откат
    переживают — они закоммичены при создании."""
    try:
        conn.rollback()
    except Exception as ex:                                  # noqa: BLE001
        progress.warn(f"откат не удался ({type(ex).__name__}: {str(ex)[:120]})")


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception as ex:
        raise_if_kerberos(ex)
        raise
