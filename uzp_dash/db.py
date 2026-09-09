"""Слой доступа к БД. Все дэши и скрипты ходят в БД только отсюда.

Одинаково работает с локальным Postgres (открытый контур) и пром-БД
(закрытый контур) — различается только URL подключения.
"""
from __future__ import annotations

from functools import lru_cache

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from . import config, progress

# Правило 13: сервер сам снимает запрос, который висит дольше лимита. Полагаться
# только на клиентский таймаут нельзя — соединение отвалится, а запрос продолжит
# занимать ресурсы базы.
SQL_TIMEOUT_MS = 10 * 60 * 1000

# Правило 10: протухший тикет выглядит как невнятная ошибка драйвера. Маркеры
# ищутся в тексте исключения — универсального класса ошибки у драйверов нет.
_KERBEROS_MARKERS = (
    "kerberos", "gssapi", "gss_", "krb5", "ticket expired",
    "credentials cache", "no credentials",
    "server not found in kerberos database",
)


class KerberosTicketError(RuntimeError):
    pass


def raise_if_kerberos(ex: Exception) -> None:
    """Распознать отказ по Kerberos и остановить работу с инструкцией.

    Останавливаемся намеренно: продолжать бессмысленно — ни один следующий запрос
    не пройдёт, а сыпать одинаковыми ошибками в тетрадку только мешает.
    """
    text_ = f"{type(ex).__name__}: {ex}".lower()
    if any(m in text_ for m in _KERBEROS_MARKERS):
        print("=" * 70, flush=True)
        print("ОСТАНОВЛЕНО: недействительный или отсутствующий Kerberos ticket.", flush=True)
        print("Обновите Kerberos ticket — выполните `kinit` в консоли,", flush=True)
        print("затем перезапустите ячейку.", flush=True)
        print("=" * 70, flush=True)
        raise KerberosTicketError(
            "Обновите Kerberos ticket: выполните kinit в консоли") from ex


@lru_cache(maxsize=8)
def get_engine(url: str) -> Engine:
    """SQLAlchemy engine (кэшируется по URL).

    pool_pre_ping — соединение проверяется перед выдачей: длинная сессия тетрадки
    переживает разрыв на стороне сервера, а не падает на первом же запросе.
    statement_timeout передаётся ЧЕРЕЗ options драйвера, а не отдельным SET: у
    engine несколько соединений в пуле, и SET на одном из них не действует на
    остальные — лимит оказался бы включён через раз.
    """
    return create_engine(
        url,
        pool_pre_ping=True,
        future=True,
        connect_args={"options": f"-c statement_timeout={SQL_TIMEOUT_MS}"},
    )


def read_sql(engine: Engine, sql: str, params: dict | None = None) -> pd.DataFrame:
    """Выполнить SELECT и вернуть DataFrame.

    В SQL используйте плейсхолдеры {schema} (основная витринная схема) и
    {schema_t} (схема пайплайна) — они подставляются автоматически,
    и именованные параметры :name (безопасная подстановка значений).
    """
    sql = sql.format(schema=config.SCHEMA, schema_t=config.SCHEMA_T)
    progress.sql(sql, params)
    try:
        with engine.connect() as conn:
            return pd.read_sql(text(sql), conn, params=params or {})
    except Exception as ex:
        raise_if_kerberos(ex)
        raise


def ping(engine: Engine) -> bool:
    """Проверка соединения — первое, что запускается в тетрадке."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as ex:
        raise_if_kerberos(ex)
        raise
