"""Применение схемы и загрузка справочников-CSV как есть (открытый контур)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from uzp_dash import config

SCHEMA_SQL = Path(__file__).with_name("schema.sql")


def apply_schema(engine: Engine) -> None:
    """Создать схемы и таблицы (DROP+CREATE). Схем две: витринная и пайплайна."""
    sql = (SCHEMA_SQL.read_text(encoding="utf-8")
           .replace("__SCHEMA_T__", config.SCHEMA_T)
           .replace("__SCHEMA__", config.SCHEMA))
    with engine.begin() as conn:
        for stmt in _split_statements(sql):
            conn.execute(text(stmt))


def _split_statements(sql: str) -> list[str]:
    out, buf = [], []
    for line in sql.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        buf.append(line)
        if s.endswith(";"):
            out.append("\n".join(buf).rstrip().rstrip(";"))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return [s for s in out if s.strip()]


def _to_bool(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.lower().map(
        {"true": True, "false": False, "1": True, "0": False}
    ).astype("boolean")


def _to_int(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("Int64")


def load_reference_csvs(engine: Engine) -> dict[str, int]:
    """Загрузить uzp_dim_gosb и uzp_dim_metric из CSV как есть."""
    counts = {}

    gosb = pd.read_csv(config.CSV_GOSB)
    for c in ["tb_id", "old_gosb_id", "new_gosb_id", "isu_branch_id", "web_gosb_id",
              "pirs_gosb_id", "utc_timezone", "timezone_violation_msk", "region_id"]:
        gosb[c] = _to_int(gosb[c])
    gosb["inserted_dttm"] = pd.to_datetime(gosb["inserted_dttm"], errors="coerce")
    counts["uzp_dim_gosb"] = _bulk(engine, gosb, "uzp_dim_gosb")

    metric = pd.read_csv(config.CSV_METRIC)
    for c in ["metric_id", "owner_saphr_id", "dev_saphr_id"]:
        metric[c] = _to_int(metric[c])
    for c in ["is_active", "is_rank", "is_infopanel", "is_navigator", "is_sbolpro"]:
        metric[c] = _to_bool(metric[c])
    metric["modified_dt"] = pd.to_datetime(metric["modified_dt"], errors="coerce")
    metric["inserted_dttm"] = pd.to_datetime(metric["inserted_dttm"], errors="coerce")
    counts["uzp_dim_metric"] = _bulk(engine, metric, "uzp_dim_metric")

    return counts


def _bulk(engine: Engine, df: pd.DataFrame, table: str, schema: str | None = None) -> int:
    df = df.where(pd.notnull(df), None)
    df.to_sql(table, engine, schema=schema or config.SCHEMA, if_exists="append",
              index=False, method="multi", chunksize=500)
    return len(df)
