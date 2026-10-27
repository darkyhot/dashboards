"""Прогон: ведомости за окно → временные таблицы → сводка `yva_ptf_month_org`.

Одна сессия БД на весь прогон (в ней живут временные таблицы). Сводка и потоки
строятся в `_new` и подменяются ОДНОЙ транзакцией, только если сошлись все
самопроверки. Каждый прогон дописывает журнал `yva_ptf_journal`, а успешный — ещё и
поколение `yva_ptf_vintage`.
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from . import config, db, progress
from . import months as M
from . import sql as Q

TOL = 0.01                  # допуск сумм ФЛ в пересчёте (дроби 1/k во float8)
SEG_FULL = 0.9              # месяц истории сегментов «полный»: покрытие ≥ 90% медианы окна
UNDERLOAD = 0.10            # недогруз ТБ: отношение к прошлому месяцу хуже банка на столько


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
        self.timing[name] = self.timing.get(name, 0) + time.time() - t0
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


def window(report_month: str, n_months: int) -> list[str]:
    """Окно: последние n_months, но не раньше 2023-11 (раньше отбор relev не определён)."""
    last = M.parse(report_month)
    first = max(M.parse(Q.FIRST_MONTH), M.shift(last, -(n_months - 1)))
    months = [M.iso(m) for m in M.span(first, last)]
    if len(months) > Q.MAX_MONTHS:
        raise ValueError(f"окно {len(months)} мес. > {Q.MAX_MONTHS}: битовые маски bigint не вместят")
    return months


def cmp0(months: list[str]) -> int:
    """Номер первого месяца, с которым можно сравнивать (2024-01: с него relev в нынешнем виде)."""
    return next((i for i, m in enumerate(months) if m >= Q.CMP_MONTH), len(months))


def run(conn: str = "", report_month: str = "", schema: str | None = None,
        schema_t: str | None = None, payroll: str | None = None, seg_hist: str | None = None,
        n_months: int = 36, epk_chunks: int = 1, amt_min: int = Q.AMT_MIN,
        exc_holding: str = Q.EXC_HOLDING, pair_min: int = 10, vint_org_min: int = 1000,
        sql_timeout_min: int = 60, verbose: bool = True, show_sql: bool = False) -> dict:
    t_start = time.time()
    progress.enable(verbose, show_sql)
    if not report_month:
        raise ValueError("REPORT_MONTH задаётся явно, 'ГГГГ-ММ'")
    config.setup(schema, schema_t, payroll, seg_hist)
    months = window(report_month, n_months)
    last = len(months) - 1
    c0 = cmp0(months)
    progress.step(f"Окно: {M.label(months[0])} … {M.label(months[-1])} ({len(months)} мес.; "
                  f"сравнения — с {M.label(months[min(c0, last)])}); ведомости "
                  f"{config.SCHEMA}.{config.PAYROLL}; запись в {config.SCHEMA_T}")

    engine = db.get_engine(config.db_url(conn or None), sql_timeout_min)
    with db.session(engine) as c:
        gp = db.is_greenplum(c)
        progress.done("Greenplum" if gp else "PostgreSQL (без DISTRIBUTED BY и сжатия)")
        params = {"amt_min": amt_min, "exc_holding": exc_holding, "last": last, "cmp0": c0,
                  "flap_min": Q.FLAP_MIN, "pair_min": pair_min,
                  "w_first_day": months[0][:8] + "01", "w_to": months[-1]}
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
        rows_by_month, empty = {}, []
        for i, m in enumerate(months):
            t0 = time.time()
            p = {"m": m, "mi": i}
            n = (b.create("t_tr", Q.T_TR_MONTH, p, analyze=False) if i == 0
                 else b.insert("t_tr", Q.T_TR_MONTH, p))
            rows_by_month[m] = n
            progress.done(f"{M.label(m)}: {n:,} троек, {time.time() - t0:.0f} с")
            if n == 0:
                empty.append(m)
                progress.warn(f"{M.label(m)}: в ведомостях нет данных — сводка не будет подменена")
        db.execute(c, "ANALYZE t_tr")

        # 3. Люди и истории
        progress.step("Люди и маски истории")
        for name, body in [("t_pm", Q.T_PM), ("t_thist", Q.T_THIST), ("t_pbits", Q.T_PBITS),
                           ("t_po", Q.T_PO), ("t_ph", Q.T_PH), ("t_orgm", Q.T_ORGM)]:
            n = b.create(name, body)
            progress.done(f"{name}: {n:,} строк")

        # 4. Метки движений по базам
        for base, k in Q.BASES.items():
            progress.step(f"Метки движений, база {base} (M−{k})")
            b.chunked(f"t_in_{base}_part", Q.LAB_IN, {"k": k})
            b.create(f"t_in_{base}", Q.agg_in(f"t_in_{base}_part"))
            b.chunked(f"t_out_{base}_part", Q.LAB_OUT, {"k": k})
            b.create(f"t_out_{base}", Q.agg_out(f"t_out_{base}_part"))
            progress.done(f"t_in_{base}, t_out_{base}")

        # 5. Остатки, ключи, сегменты
        progress.step("Остатки месяца, ключи сводки, сегменты")
        b.chunked("t_s_part", Q.STOCK)
        b.create("t_s", Q.regroup("t_s_part", Q.STOCK_COLS, extra=", min(tb_id) AS tb_id"))
        n = b.create("t_keys", Q.T_KEYS)
        cov = db.read_sql(c, Q.SEG_COVERAGE).sort_values("mi")
        cur_mi, seg_note = _seg_cur_month(cov)
        progress.done(seg_note)
        b.create("t_seg_cur", Q.T_SEG_CUR, {"cur_mi": cur_mi})
        b.create("t_kseg", Q.T_KSEG)
        progress.done(f"ключей сводки: {n:,}")

        # 6. Сводка и потоки в _new
        progress.step("Сводка yva_ptf_month_org_new и потоки «организация → организация»")
        for stmt in Q.DROP_NEW:
            db.execute(c, stmt)
        n_rows = db.execute(c, _create_persistent(gp, f"{Q.TARGET}_new", Q.final_select(),
                                                  "org_id, sys_gosb_id"), params)
        n_pairs = db.execute(c, _create_persistent(gp, f"{Q.PAIRS}_new", Q.FLOW_PAIRS,
                                                   "from_org_id, to_org_id"), params)
        progress.done(f"сводка {n_rows:,} строк, потоков {n_pairs:,} пар")

        # 7. Проверки
        progress.step("Самопроверки")
        checks = _checks(c, empty)
        ok = all(x["ok"] for x in checks)
        for x in checks:
            (progress.done if x["ok"] else progress.warn)(f"{x['name']}: {x['detail']}")
        totals = db.read_sql(c, Q.MONTH_TOTALS)
        totals["report_dt"] = pd.to_datetime(totals["report_dt"]).dt.date.astype(str)

        # 8. Журнал (временные таблицы — до подмены, пока есть _new)
        b.create("t_pslice", Q.T_PSLICE)
        b.create("t_jpers", Q.T_JPERS)
        b.create("t_jrows", Q.T_JROWS)
        db.execute(c, Q.CREATE_JOURNAL)
        prev = db.read_sql(c, Q.PREV_RUN)

        run_ts = datetime.now()
        run_id = int(run_ts.strftime("%Y%m%d%H%M%S"))
        if ok:
            db.execute_tx(c, Q.SWAP)
            progress.done("сводка и потоки подменены одной транзакцией")
            db.execute(c, Q.CREATE_VINTAGE)
            nv = db.execute(c, Q.insert_vintage(), {"run_id": run_id, "def_ver": Q.DEF_VER,
                                                    "vint_org_min": vint_org_min})
            progress.done(f"поколение {run_id}: {nv:,} строк в yva_ptf_vintage")
        else:
            progress.warn("ПРОВЕРКИ НЕ СОШЛИСЬ: старая сводка не тронута, новая оставлена в "
                          "yva_ptf_month_org_new для разбора")
        dur = (time.time() - t_start) / 60
        db.execute(c, Q.INSERT_JOURNAL, {
            "run_id": run_id, "run_ts": run_ts.isoformat(sep=" "), "def_ver": Q.DEF_VER,
            "window_from": months[0], "window_to": months[-1], "seg_cur_dt": months[cur_mi],
            "checks_ok": ok, "duration_min": round(dur, 2)})
        journal = db.read_sql(c, "SELECT * FROM {schema_t}.yva_ptf_journal WHERE run_id = :r",
                              {"r": run_id})
        revisions = _revisions(totals, prev)
        if len(revisions):
            progress.warn(f"пересчёт прошлого: {len(revisions)} мес. изменились против прогона "
                          f"{int(prev['run_id'].iloc[0])}")
        under = _underload(journal)
        for u in under:
            progress.warn(u)
        progress.done(f"журнал yva_ptf_journal: прогон {run_id}, {dur:.1f} мин")

    return {"ok": ok, "run_id": run_id, "months": months, "cmp0": c0, "checks": checks,
            "totals": totals, "journal": journal, "revisions": revisions, "underload": under,
            "rows": n_rows, "pairs": n_pairs, "seg_cur_dt": months[cur_mi], "seg_coverage": cov,
            "rows_by_month": rows_by_month, "timing": b.timing, "duration_min": dur}


def _create_persistent(gp: bool, name: str, body: str, dist: str) -> str:
    if gp:
        return f"CREATE TABLE {name} {Q.GP_STORAGE} AS\n{body}\nDISTRIBUTED BY ({dist})"
    return f"CREATE TABLE {name} AS\n{body}"


def _seg_cur_month(cov: pd.DataFrame) -> tuple[int, str]:
    """Последний «полный» месяц истории сегментов: покрытие ≥ SEG_FULL × медиана окна."""
    cov = cov[cov["pf"] > 0].copy()
    if cov.empty:
        return 0, "истории сегментов нет — сегменты из ЕПК"
    cov["share"] = cov["pf_hist"] / cov["pf"]
    med = cov["share"].median()
    full = cov[cov["share"] >= SEG_FULL * med]
    mi = int(full["mi"].max()) if len(full) else int(cov["mi"].max())
    last = int(cov["mi"].max())
    note = (f"текущая сегментация — по месяцу №{mi} (покрытие {cov.set_index('mi')['share'][mi]:.0%}, "
            f"медиана {med:.0%})")
    if mi < last:
        note += f"; последние {last - mi} мес. истории неполные"
    return mi, note


def _checks(c, empty: list[str]) -> list[dict]:
    """Тождества по каждой строке + сверка итогов сводки с временными таблицами."""
    out = [{"name": "все месяцы окна загружены", "ok": not empty,
            "detail": "да" if not empty else f"пустые: {', '.join(empty)}"}]
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
    worst = max(df[["m_conc", "y_conc"]].abs().max().max(), 0)
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


def _underload(journal: pd.DataFrame) -> list[str]:
    """Недогруз последнего месяца: у ТБ отношение «получатели M / M−1» хуже, чем по банку."""
    if journal.empty:
        return []
    j = journal.copy()
    j["report_dt"] = pd.to_datetime(j["report_dt"])
    ms = sorted(j["report_dt"].unique())
    if len(ms) < 2:
        return []
    cur, prv = ms[-1], ms[-2]

    def ratio(df):
        a = df[df["report_dt"] == cur].set_index("slice_value")["any_recip"]
        b = df[df["report_dt"] == prv].set_index("slice_value")["any_recip"]
        return (a / b).dropna(), b

    bank, _ = ratio(j[j["slice_type"] == "bank"])
    tb, base = ratio(j[j["slice_type"] == "tb"])
    if bank.empty:
        return []
    r0 = float(bank.iloc[0])
    return [f"возможен недогруз: ТБ {k} — получатели {v:.1%} от прошлого месяца при {r0:.1%} по банку"
            for k, v in tb.items() if v < r0 - UNDERLOAD and base.get(k, 0) >= 1000]
