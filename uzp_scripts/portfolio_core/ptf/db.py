"""Доступ к БД: локальный Postgres и Greenplum на ядре 9.4 — различается только URL.

Плейсхолдеры `{schema}`, `{schema_t}`, `{payroll}`, `{seg_hist}`, `{code_col}`
подставляются заменой по имени (не `str.format`): квантификатор `{1,12}` в
регулярках не нужно экранировать.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from . import config, progress

CODE_COL = "enrollment_type"      # колонка вида зачисления в ведомостях

_KERBEROS = ("kerberos", "gssapi", "gss_", "krb5", "ticket expired",
             "credentials cache", "no credentials")


class KerberosTicketError(RuntimeError):
    pass


def raise_if_kerberos(ex: Exception) -> None:
    t = f"{type(ex).__name__}: {ex}".lower()
    if any(m in t for m in _KERBEROS):
        print("=" * 70, flush=True)
        print("ОСТАНОВЛЕНО: недействительный Kerberos ticket. Выполните `kinit` в консоли", flush=True)
        print("и перезапустите ячейку.", flush=True)
        print("=" * 70, flush=True)
        raise KerberosTicketError("Обновите Kerberos ticket: kinit") from ex


@lru_cache(maxsize=8)
def get_engine(url: str, timeout_min: int = config.SQL_TIMEOUT_MIN) -> Engine:
    ms = int(timeout_min) * 60 * 1000
    return create_engine(url, pool_pre_ping=True, future=True,
                         connect_args={"options": f"-c statement_timeout={ms}"})


@contextmanager
def session(engine: Engine):
    """Одно соединение на прогон: в нём живут временные таблицы."""
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()


def render(sql: str) -> str:
    return (sql.replace("{schema}", config.SCHEMA).replace("{schema_t}", config.SCHEMA_T)
            .replace("{payroll}", config.PAYROLL).replace("{seg_hist}", config.SEG_HIST)
            .replace("{code_col}", CODE_COL))


def execute(conn: Connection, sql: str, params: dict | None = None) -> int:
    """Оператор без результата, с коммитом (иначе SQLAlchemy 2 откатит его)."""
    sql = render(sql)
    progress.sql(sql, params)
    try:
        res = conn.execute(text(sql), params or {})
        conn.commit()
        return int(res.rowcount or 0)
    except Exception as ex:
        raise_if_kerberos(ex)
        raise


def execute_tx(conn: Connection, statements: list[str], params: dict | None = None) -> None:
    """Несколько операторов ОДНОЙ транзакцией: либо все, либо ни одного."""
    try:
        for s in statements:
            s = render(s)
            progress.sql(s, params)
            conn.execute(text(s), params or {})
        conn.commit()
    except Exception as ex:
        rollback(conn)
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


def scalar(conn: Connection, sql: str, params: dict | None = None):
    sql = render(sql)
    try:
        return conn.execute(text(sql), params or {}).scalar()
    except Exception as ex:
        raise_if_kerberos(ex)
        raise


def rollback(conn: Connection) -> None:
    try:
        conn.rollback()
    except Exception as ex:                                  # noqa: BLE001
        progress.warn(f"откат не удался ({type(ex).__name__}: {str(ex)[:120]})")


def is_greenplum(conn: Connection) -> bool:
    v = str(conn.execute(text("SELECT version()")).scalar() or "")
    conn.commit()
    return "greenplum" in v.lower()
