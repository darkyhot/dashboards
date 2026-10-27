"""Прогон: ведомости за окно → временные таблицы → сводка `yva_ptf_month_org`.

Одна сессия БД на весь прогон (в ней живут временные таблицы). Сводка строится в
`yva_ptf_month_org_new`; старая подменяется, ТОЛЬКО если сошлись все самопроверки.
Итоги каждого прогона дописываются в `yva_ptf_runs`.
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from . import config, db, progress
from . import months as M
from . import sql as Q

TOL = 0.01      # допуск сумм ФЛ в пересчёте (дроби 1/k)


class Builder:
    def __init__(self, conn, gp: bool, params: dict, nch: int) -> None:
        self.conn, self.gp, self.params, self.nch = conn, gp, params, max(1, int(nch))
        self.timing: dict[str, float] = {}

    def _ddl(self, name: str, body: str) -> str:
        if self.gp:
            return f"CREATE TEMP TABLE {name} AS\n{body}\nDISTRIBUTED BY ({Q.DIST[name]})"
        return f"CREATE TEMP TABLE {name} AS\n{body}"

    def create(self, name: str, body: str, params: dict | None = None, analyze: bool = True) -> int:
        t0 = time.time()
        db.execute(self.conn, f"DROP TABLE IF EXISTS {name}")
        n = db.execute(self.conn, self._ddl(name, body), {**self.params, **(params or {})})
        if analyze:
            db.execute(self.conn, f"ANALYZE {name}")
        self.timing[name] = time.time() - t0
        return n

    def insert(self, name: str, body: str, params: dict | None = None) -> int:
        return db.execute(self.conn, f"INSERT INTO {name}\n{body}", {**self.params, **(params or {})})

    def chunked(self, name: str, body: str, params: dict | None = None) -> int:
        """Тяжёлый запрос кусками по людям: CREATE по первому куску, INSERT по остальным."""
        t0 = time.time()
        n = 0
        for ch in range(self.nch):
            p = {**(params or {}), "nch": self.nch, "ch": ch}
            n += (self.create(name, body, p, analyze=False) if ch == 0 else self.insert(name, body, p))
            if self.nch > 1:
                progress.done(f"{name}: кусок {ch + 1}/{self.nch}")
        db.execute(self.conn, f"ANALYZE {name}")
        self.timing[name] = time.time() - t0
        return n


def run(conn: str = "", report_month: str = "", schema: str | None = None,
        schema_t: str | None = None, payroll: str | None = None, seg_hist: str | None = None,
        n_months: int = 36, epk_chunks: int = 1, amt_min: int = Q.AMT_MIN,
        exc_holding: str = Q.EXC_HOLDING, sql_timeout_min: int = config.SQL_TIMEOUT_MIN,
        verbose: bool = True, show_sql: bool = False) -> dict:
    t_start = time.time()
    progress.enable(verbose, show_sql)
    if not report_month:
        raise ValueError("REPORT_MONTH задаётся явно, 'ГГГГ-ММ'")
    config.setup(schema, schema_t, payroll, seg_hist)
    last_m = M.parse(report_month)
    months = [M.iso(m) for m in M.span(M.shift(last_m, -(n_months - 1)), last_m)]
    last = len(months) - 1
    first_day = months[0][:8] + "01"
    progress.step(f"Окно: {M.label(months[0])} … {M.label(months[-1])} ({len(months)} мес.); "
                  f"ведомости {config.SCHEMA}.{config.PAYROLL}; запись в {config.SCHEMA_T}")

    engine = db.get_engine(config.db_url(conn or None), sql_timeout_min)
    with db.session(engine) as c:
        gp = db.is_greenplum(c)
        progress.done("Greenplum" if gp else "PostgreSQL (без DISTRIBUTED BY и сжатия)")
        params = {"amt_min": amt_min, "exc_holding": exc_holding, "last": last,
                  "flap_min": Q.FLAP_MIN, "w_first_day": first_day, "w_to": months[-1]}
        b = Builder(c, gp, params, epk_chunks)

        # 1. Справочники
        progress.step("Справочники")
        b.create("t_months", Q.t_months(months))
        n = b.create("t_tik", Q.T_TIK)
        progress.done(f"t_tik: {n:,} организаций ТИК/ЦИК — исключены целиком")
        n = b.create("t_exc", Q.T_EXC)
        progress.done(f"t_exc: {n:,} организаций без порога")
        n = b.create("t_org", Q.T_ORG)
        progress.done(f"t_org: {n:,} организаций ЕПК")
        n = b.create("t_seg", Q.T_SEG)
        progress.done(f"t_seg: {n:,} пар «месяц × организация × ГОСБ» с историческим сегментом")

        # 2. Ведомости → тройки, по месяцу на оператор (единственное чтение витрины)
        progress.step(f"Ведомости → тройки (t_tr), {len(months)} операторов")
        rows_by_month = {}
        for i, m in enumerate(months):
            t0 = time.time()
            p = {"m": m, "mi": i}
            n = (b.create("t_tr", Q.T_TR_MONTH, p, analyze=False) if i == 0
                 else b.insert("t_tr", Q.T_TR_MONTH, p))
            rows_by_month[m] = n
            progress.done(f"{M.label(m)}: {n:,} троек, {time.time() - t0:.0f} с")
            if n == 0:
                progress.warn(f"{M.label(m)}: в ведомостях нет данных — месяц пуст, цифры по нему неверны")
        db.execute(c, "ANALYZE t_tr")

        # 3. Люди и истории
        progress.step("Люди и истории троек")
        for name, body in [("t_pm", Q.T_PM), ("t_person", Q.T_PERSON), ("t_thist", Q.T_THIST),
                           ("t_po", Q.T_PO), ("t_ph", Q.T_PH), ("t_orgm", Q.T_ORGM),
                           ("t_flip", Q.T_FLIP), ("t_flap", Q.T_FLAP)]:
            n = b.create(name, body)
            progress.done(f"{name}: {n:,} строк")

        # 4. Метки движений по трём базам
        for base, k in Q.BASES.items():
            progress.step(f"Метки движений, база {base} (M−{k})")
            b.chunked(f"t_in_{base}_part", Q.LAB_IN, {"k": k})
            b.create(f"t_in_{base}", Q.regroup(f"t_in_{base}_part", Q.IN_LABELS + ["fl_in"]))
            b.chunked(f"t_out_{base}_part", Q.LAB_OUT, {"k": k})
            b.create(f"t_out_{base}", Q.regroup(f"t_out_{base}_part",
                                                Q.OUT_LABELS + ["fl_out", "out_temp"]))
            progress.done(f"t_in_{base}, t_out_{base}")

        # 5. Остатки, ключи, сегменты
        progress.step("Остатки месяца, ключи сводки, сегменты")
        b.chunked("t_s_part", Q.STOCK)
        b.create("t_s", Q.regroup("t_s_part", Q.STOCK_SUMS, extra=", min(tb_id) AS tb_id"))
        n = b.create("t_keys", Q.T_KEYS)
        b.create("t_kseg", Q.T_KSEG)
        progress.done(f"ключей сводки: {n:,}")

        # 6. Сводка в _new, проверки, подмена
        progress.step("Сводка yva_ptf_month_org_new")
        db.execute(c, Q.DROP_NEW)
        body = Q.final_select()
        if gp:
            ddl = (f"CREATE TABLE {Q.TARGET}_new {Q.GP_STORAGE} AS\n{body}\n"
                   f"DISTRIBUTED BY (org_id, sys_gosb_id)")
        else:
            ddl = f"CREATE TABLE {Q.TARGET}_new AS\n{body}"
        n_rows = db.execute(c, ddl, params)
        progress.done(f"{n_rows:,} строк")

        progress.step("Самопроверки")
        checks = _checks(c)
        ok = all(x["ok"] for x in checks)
        for x in checks:
            (progress.done if x["ok"] else progress.warn)(f"{x['name']}: {x['detail']}")

        totals = db.read_sql(c, Q.MONTH_TOTALS)
        db.execute(c, Q.CREATE_RUNS)
        prev = db.read_sql(c, Q.PREV_RUN)
        if ok:
            for stmt in Q.SWAP:
                db.execute(c, stmt)
            progress.done("сводка подменена: yva_ptf_month_org")
        else:
            progress.warn("ПРОВЕРКИ НЕ СОШЛИСЬ: старая сводка не тронута, новая оставлена в "
                          "yva_ptf_month_org_new для разбора")

        run_ts = datetime.now()
        run_id = int(run_ts.strftime("%Y%m%d%H%M%S"))
        dur = (time.time() - t_start) / 60
        totals["report_dt"] = pd.to_datetime(totals["report_dt"]).dt.date.astype(str)
        for _, r in totals.sort_values("report_dt").iterrows():
            mi = months.index(r["report_dt"])
            db.execute(c, Q.INSERT_RUNS, {
                "run_id": run_id, "run_ts": run_ts.isoformat(sep=" "), "report_dt": r["report_dt"],
                "window_from": months[0], "window_to": months[-1],
                "pf_recip": int(r["pf_recip"]), "pf_fl": float(r["pf_fl"]),
                "fot_amt": float(r["fot_amt"]), "any_recip": int(r["any_recip"]),
                "raw_rows": int(_raw_rows(c, mi)),
                "seg_hist_share": float(r["pf_hist"]) / float(r["pf_recip"]) if r["pf_recip"] else None,
                "one_time_prelim": mi > last - 2, "checks_ok": ok, "duration_min": round(dur, 2)})
        revisions = _revisions(totals, prev)
        if len(revisions):
            progress.warn(f"пересчёт прошлого: {len(revisions)} мес. изменились против прогона "
                          f"{int(prev['run_id'].iloc[0])}")
        progress.done(f"журнал yva_ptf_runs: прогон {run_id}, {dur:.1f} мин")

    return {"ok": ok, "run_id": run_id, "months": months, "checks": checks,
            "totals": totals, "revisions": revisions, "rows": n_rows,
            "rows_by_month": rows_by_month, "timing": b.timing, "duration_min": dur}


_RAW_CACHE: dict = {}


def _raw_rows(c, mi: int) -> int:
    if not _RAW_CACHE:
        df = db.read_sql(c, "SELECT mi, sum(n_rows) AS n FROM t_tr GROUP BY mi")
        _RAW_CACHE.update({int(a): int(n) for a, n in zip(df["mi"], df["n"])})
    return _RAW_CACHE.get(mi, 0)


def _checks(c) -> list[dict]:
    """Тождества по каждой строке + сверка итогов сводки с временными таблицами."""
    _RAW_CACHE.clear()
    out = []
    rc = db.read_sql(c, Q.row_checks()).iloc[0]
    for name, v in rc.items():
        out.append({"name": f"строки: {name}", "ok": int(v or 0) == 0,
                    "detail": "нарушений нет" if not v else f"нарушено в {int(v):,} строках"})
    tot = db.read_sql(c, Q.MONTH_TOTALS)
    tr = db.read_sql(c, Q.TR_TOTALS)
    df = tot.merge(tr, on="report_dt", how="outer").fillna(0)
    bad_pf = df[df["pf_recip"] != df["n_pf"]]
    out.append({"name": "портфель = тройки в портфеле", "ok": bad_pf.empty,
                "detail": "сходится по всем месяцам" if bad_pf.empty else f"расходится в {len(bad_pf)} мес."})
    bad_fl = df[(df["pf_fl"] - df["n_fl"]).abs() > TOL]
    out.append({"name": "ФЛ в пересчёте = число людей", "ok": bad_fl.empty,
                "detail": "сходится по всем месяцам" if bad_fl.empty else f"расходится в {len(bad_fl)} мес."})
    worst = max(df[["m_conc", "q_conc", "y_conc"]].abs().max().max(), 0)
    out.append({"name": "совместительство по банку = 0", "ok": worst <= TOL,
                "detail": f"наибольшее отклонение {worst:.4f}"})
    return out


def _revisions(totals: pd.DataFrame, prev: pd.DataFrame) -> pd.DataFrame:
    if prev.empty:
        return pd.DataFrame(columns=["report_dt", "было", "стало", "разница"])
    p = prev.copy()
    p["report_dt"] = pd.to_datetime(p["report_dt"]).dt.date.astype(str)
    df = totals[["report_dt", "pf_recip"]].merge(p[["report_dt", "pf_recip"]], on="report_dt",
                                                 suffixes=("_new", "_old"))
    df = df[df["pf_recip_new"] != df["pf_recip_old"]]
    return pd.DataFrame({"report_dt": df["report_dt"], "было": df["pf_recip_old"],
                         "стало": df["pf_recip_new"],
                         "разница": df["pf_recip_new"] - df["pf_recip_old"]})
