"""Оркестратор: разведка → рабочий набор → выгрузки → расчёты → HTML."""
from __future__ import annotations

import time
from datetime import date, datetime

from . import analyze as A
from . import config, db, fetch, progress
from . import months as M
from . import queries as Q

# Витрины отчёта: (схема, таблица). Нет хоть одной — прогон останавливается сразу.
TABLES = [("s", "mis_data_payroll_m"), ("s", "uzp_dim_enrollment_type"),
          ("s", "uzp_dim_education_organization"), ("s", "uzp_data_epk_consolidation"),
          ("s", "uzp_data_nfl_channel"), ("t", "bkv_bkv_uzp_data_nfl_channel"),
          ("s", "asm_data_operation"), ("t", "ml_ksa_clickstream_events_oaa"),
          ("s", "uzp_dwh_sale_funnel_task"), ("s", "uzp_dwh_sap_staff_emp"), ("s", "uzp_dim_gosb")]


class ProbeError(RuntimeError):
    pass


def probe(cx) -> None:
    progress.step("Разведка витрин")
    miss = []
    for sch, t in TABLES:
        schema = config.SCHEMA if sch == "s" else config.SCHEMA_T
        n = int(db.read_sql(cx, Q.PROBE_TABLE, {"schema": schema, "table": t}).iloc[0, 0])
        if not n:
            miss.append(f"{schema}.{t}")
    if miss:
        raise ProbeError(f"нет витрин: {', '.join(miss)}")
    progress.done(f"все {len(TABLES)} витрин на месте")
    try:
        db.execute(cx, Q.PROBE_TEMP)
        db.execute(cx, Q.DROP_PROBE_TEMP)
    except Exception as ex:                                      # noqa: BLE001
        db.rollback(cx)
        raise ProbeError(f"временные таблицы запрещены ({type(ex).__name__}: {str(ex)[:160]})") from ex
    progress.done("временные таблицы разрешены")


def run(conn: str | None = None, schema: str | None = None, schema_t: str | None = None,
        report_month: str = "2026-08", amt_min: int = Q.AMT_MIN,
        nfl_split: str = "2026-03-31", exc_holding: str = "МИНОБОРОНЫ",
        staff_pos: str = Q.MZP_POS, top_rows: int = 100, break_max: int = 30,
        sql_timeout_min: int = config.SQL_TIMEOUT_MIN,
        verbose: bool = True, show_sql: bool = False) -> dict:
    t_start = time.time()
    progress.enable(verbose, show_sql)
    config.set_schemas(schema, schema_t)
    config.ensure_dirs()
    engine = db.get_engine(config.db_url(conn), int(sql_timeout_min))
    db.ping(engine)
    progress.done(f"БД доступна, схемы {config.SCHEMA}, {config.SCHEMA_T}")

    last = M.parse(report_month)
    m_cur = M.span(date(last.year, 1, 31), last)
    m_prev = [M.shift(m, -12) for m in m_cur]
    cells = [M.shift(last, -24), M.shift(last, -12), last]
    act_from = date(m_prev[0].year - 1, 11, 1)          # окно действия: +2 месяца до января
    params = {"amt_min": int(amt_min), "exc_holding": exc_holding, "nfl_split": nfl_split,
              "nfl_months": [M.iso(m) for m in m_prev + m_cur], "m_cur": [M.iso(m) for m in m_cur],
              "act_from": act_from.isoformat(), "act_to": M.iso(last),
              "c_prev2": M.iso(cells[0]), "c_prev": M.iso(cells[1]), "c_cur": M.iso(cells[2]),
              "cell_months": [M.iso(m) for m in cells],
              "staff_dates": [M.iso(M.shift(last, -12)), M.iso(last)], "staff_pos": staff_pos,
              "top_rows": int(top_rows)}
    res: dict = {"report_month": M.iso(last), "m_cur": [M.iso(m) for m in m_cur],
                 "m_prev": [M.iso(m) for m in m_prev], "cells": [M.iso(m) for m in cells],
                 "schema": config.SCHEMA, "schema_t": config.SCHEMA_T, "params": params,
                 "break_max": int(break_max),
                 "generated": datetime.now().strftime("%Y-%m-%d %H:%M")}

    with db.session(engine) as cx:
        probe(cx)
        ws = fetch.Workspace(cx, params)
        try:
            ws.build(cells)
            progress.step("Выгрузки")
            raw = {}
            for name, sql in (("cell_sum", Q.CELL_SUM), ("cell_tot", Q.CELL_TOT), ("cell_dim", Q.CELL_DIM),
                              ("cell_top", Q.CELL_TOP), ("nfl_month", Q.NFL_MONTH),
                              ("nfl_overlap", Q.NFL_OVERLAP), ("nfl_lag", Q.NFL_LAG),
                              ("nfl_cell", Q.NFL_CELL), ("nfl_step", Q.NFL_STEP), ("nfl_dim", Q.NFL_DIM),
                              ("deal_month", Q.DEAL_MONTH), ("deal_match", Q.DEAL_MATCH),
                              ("staff", Q.STAFF), ("staff_tot", Q.STAFF_TOT), ("tb_dim", Q.TB_DIM), ("nfl_no_epk", Q.NFL_NO_EPK)):
                raw[name] = ws.opt(name, sql)
            progress.done(f"выгрузок: {len(raw)}")
            res["raw"] = raw
            res["shown"] = dict(ws.shown)
            res["timing"] = dict(ws.timing)
            res["rows"] = dict(ws.rows)
        finally:
            ws.drop()

    A.compute(res)
    _write(res)
    progress.done(f"готово за {(time.time() - t_start) / 60:.1f} мин")
    return res


def _write(res: dict) -> None:
    from . import view
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = res["report_month"][:7].replace("-", "")
    path = config.OUTPUT_DIR / f"payroll_attraction_{tag}_{stamp}.html"
    progress.step("Запись HTML")
    html = view.render(res)
    path.write_text(html, encoding="utf-8")
    res["path"] = str(path)
    progress.done(f"HTML: {path.name} ({len(html) / 1e6:.1f} МБ)")
