"""Оркестратор: разведка → рабочий набор → выгрузки → расчёты → HTML, документ, CSV."""
from __future__ import annotations

import json
import time
from datetime import datetime

import pandas as pd

from . import analyze as A
from . import config, db, fetch, probe, progress
from . import months as M
from . import queries as Q


LOOKBACK = 3


def _months_plan(report_month, n_months: int, avail: set[str]) -> dict:
    last = M.parse(report_month)
    report = [M.shift(last, -i) for i in range(n_months - 1, -1, -1)]
    # Набор: от LOOKBACK месяцев до первого отчётного (стаж ушедших в первом
    # переходе: пришли в b, в b−1 или раньше) до месяца после последнего (возврат).
    work = []
    for off in (0, -12):
        for m in M.span(M.shift(report[0], -LOOKBACK + off), M.shift(report[-1], 1 + off)):
            if M.iso(m) in avail:
                work.append(m)
    missing = [M.label(m) for off in (0, -12)
               for m in M.span(M.shift(report[0], -LOOKBACK + off), M.shift(report[-1], off))
               if M.iso(m) not in avail]
    return {"report": report, "work": sorted(set(work)), "missing": missing}


def run(conn: str | None = None, schema: str | None = None,
        report_month: str = "2026-08", n_months: int = 3,
        history_months: int = 25, amt_min: int = Q.AMT_MIN, codes=Q.CODES,
        org_opts: dict | None = None, use_cache: bool = True,
        sql_timeout_min: int = config.SQL_TIMEOUT_MIN,
        verbose: bool = True, show_sql: bool = False) -> dict:
    t_start = time.time()
    progress.enable(verbose, show_sql)
    config.set_schema(schema)
    config.ensure_dirs()
    org_opts = {"min_base": 20, "min_real": 10, "min_share": 0.05,
                "reorg_min_share": 0.30, "reorg_min_movers": 5, "max_rows": 3000,
                **(org_opts or {})}
    engine = db.get_engine(config.db_url(conn), int(sql_timeout_min))
    db.ping(engine)
    progress.done(f"БД доступна, схема {config.SCHEMA}")

    last = M.parse(report_month)
    hist = M.span(M.shift(last, -(history_months - 1)), last)
    res: dict = {"report_month": M.iso(last), "schema": config.SCHEMA, "amt_min": amt_min,
                 "codes": list(codes), "org_opts": org_opts,
                 "generated": datetime.now().strftime("%Y-%m-%d %H:%M")}

    with db.session(engine) as cx:
        pr = probe.run(cx, min(hist[0], M.shift(last, -15)), M.shift(last, 1))
        res["probe"] = pr
        plan = _months_plan(last, n_months, set(pr["months"]))
        if plan["missing"]:
            raise probe.ProbeError(f"в витрине нет месяцев, нужных разбору: {plan['missing']}")
        report = plan["report"]
        res["report"] = [M.iso(m) for m in report]
        res["work"] = [M.iso(m) for m in plan["work"]]
        nxt = M.iso(M.shift(last, 1))
        res["has_next"] = nxt in pr["months"]
        if not res["has_next"]:
            progress.warn(f"{M.label(nxt)} не загружен — проверка «вернулись в следующем "
                          f"месяце» для {M.label(last)} не строится")

        ws = fetch.Workspace(cx, plan["work"], {"codes": list(codes), "amt_min": int(amt_min)})
        try:
            ws.build()
            fp = {"schema": config.SCHEMA, "amt_min": amt_min, "codes": list(codes),
                  "code_col": db.CODE_COL, "epk": pr["epk"]}
            # Число строк партиции — в ключе кэша СВОЕГО месяца (см. month_cached):
            # перезагрузка месяца меняет его ключ и только его.
            fp["rows"] = pr["months"]
            series_raw, load_raw = fetch.series(
                ws, [m for m in hist if M.iso(m) in pr["months"]], fp, use_cache)
            res["series_raw"], res["load_raw"] = series_raw, load_raw

            progress.step("Итоги месяцев набора")
            res["multi_bank_raw"] = ws.sql("multi_bank", Q.MULTI_BANK)
            res["multi_seg_raw"] = ws.sql("multi_seg", Q.MULTI_SEG)
            res["tb_seg_raw"] = ws.opt("tb_seg", Q.TB_SEG)
            res["tb_dim"] = ws.opt("tb_dim", Q.TB_DIM)
            progress.done("совместительство и территория")

            progress.step("Переходы: год к году и месяц к месяцу в обоих годах")
            fl, tr = {}, {}
            for m in report:
                fl[("yoy", M.iso(m))] = fetch.fl_flow(ws, M.shift(m, -12), m, f"yoy_{M.iso(m)}")
                tr[("yoy", M.iso(m))] = fetch.triple_flow(ws, M.shift(m, -12), m, f"yoy_{M.iso(m)}")
                for off, y in ((0, "cur"), (-12, "prev")):
                    c = M.shift(m, off)
                    fl[(y, M.iso(m))] = fetch.fl_flow(ws, M.shift(c, -1), c, f"mom_{M.iso(c)}")
                    tr[(y, M.iso(m))] = fetch.triple_flow(ws, M.shift(c, -1), c, f"mom_{M.iso(c)}")
                progress.done(f"{M.label(m)}: год к году и два месячных перехода")
            res["fl_raw"], res["tr_raw"] = fl, tr

            progress.step("Когорты пришедших, сезонность, зарплатные коды")
            coh = {}
            for off in (0, -12):
                for k in M.span(M.shift(report[0], -1 + off), M.shift(report[-1], off)):
                    coh[("mom", k)] = fetch.cohort(ws, k, M.shift(k, -1), f"mom_{M.iso(k)}")
            for k in report:
                coh[("yoy", k)] = fetch.cohort(ws, k, M.shift(k, -12), f"yoy_{M.iso(k)}")
            res["cohort_raw"] = coh
            res["seasonal_raw"] = {M.iso(m): fetch.seasonal(ws, m) for m in report}
            code_ms = sorted({M.shift(m, k) for m in report for k in (0, -1, -12, -13)})
            res["codes_raw"] = fetch.code_months(ws, code_ms, fp, use_cache)
            progress.done("когорты, сезонность, коды")

            progress.step("Организации: где численность реально сократилась")
            res["orgs_raw"] = {}
            for m in report:
                res["orgs_raw"][M.iso(m)] = fetch.orgs(ws, M.shift(m, -12), m, org_opts, M.iso(m))
                n = len(res["orgs_raw"][M.iso(m)]["list"])
                progress.done(f"{M.label(m)}: в списке {n:,} организаций")
            res["shown"] = dict(ws.shown)
            res["timing"] = dict(ws.timing)
        finally:
            ws.drop()

    compute(res)
    _write(res)
    progress.done(f"готово за {(time.time() - t_start) / 60:.1f} мин")
    return res


# --------------------------------------------------------------------------- #
def compute(res: dict) -> None:
    """Все расчёты поверх сырых свёрток. Отдельно от выгрузки: так их можно
    перезапустить на сохранённых свёртках без обращения к БД."""
    progress.step("Расчёты и проверки сходимости")
    report = [M.parse(m) for m in res["report"]]
    checks: list[dict] = []

    sf = A.series_frame(res["series_raw"])
    res["series"] = sf
    res["bank_series"] = A.bank_series(sf) if not sf.empty else pd.DataFrame()
    res["seg_pv"] = A.seg_pivot(sf) if not sf.empty else pd.DataFrame()
    res["seg_yoy_pct"] = A.yoy_pct_table(res["seg_pv"]) if not sf.empty else pd.DataFrame()
    res["seg_yoy_abs"] = A.yoy_abs_table(res["seg_pv"]) if not sf.empty else pd.DataFrame()
    res["mom_profile"] = A.mom_profile(res["bank_series"])
    res["steps"], res["steps_measured"] = A.steps(res["bank_series"])
    res["load"] = A.load_health(res["load_raw"])
    res["thresholds"] = (A.thresholds(res["bank_series"], report, res["amt_min"])
                         if not res["bank_series"].empty else pd.DataFrame())

    mb = A.multi_bank(res["multi_bank_raw"])
    ms = A.multi_seg(res["multi_seg_raw"])
    res["multi_bank"], res["multi_seg"] = mb, ms
    res["multi_yoy"] = A.multi_yoy(mb, ms, report)
    tot = mb.set_index("report_dt")["n_triples"]
    tot_epk = mb.set_index("report_dt")["n_epk"]

    # Ряд по витрине и рабочий набор считаются РАЗНЫМИ запросами — они обязаны
    # совпасть в общих месяцах, иначе один из двух путей считает не то.
    if not res["bank_series"].empty:
        bs = res["bank_series"].set_index("report_dt")["n_triples"]
        common = [d for d in tot.index if d in bs.index]
        checks.append(A.check("ряд по витрине = рабочий набор (получатели)",
                              float(sum(abs(bs[d] - tot[d]) for d in common)),
                              detail=f"месяцев сверено: {len(common)}"))

    # Год к году по отчётным месяцам и за месяц до первого — для разницы разниц.
    yoy = {}
    for m in [M.shift(report[0], -1)] + report:
        a, b = M.iso(m), M.iso(M.shift(m, -12))
        if a in tot.index and b in tot.index:
            yoy[a] = float(tot[a] - tot[b])
    res["yoy"] = yoy
    res["yoy_epk"] = {M.iso(m): float(tot_epk[M.iso(m)] - tot_epk[M.iso(M.shift(m, -12))])
                      for m in report}
    res["pattern"] = A.pattern({M.iso(m): yoy[M.iso(m)] for m in report})

    seg_tot = ms.pivot_table(index="seg", columns="report_dt", values="n_triples", aggfunc="sum").fillna(0)

    comp: dict = {}
    for (kind, m), fl in res["fl_raw"].items():
        md = M.parse(m)
        c = md if kind != "prev" else M.shift(md, -12)
        b = M.shift(c, -12) if kind == "yoy" else M.shift(c, -1)
        d = A.decomp(fl)
        tag = f"{kind} {M.label(b)}→{M.label(c)}"
        checks.append(A.check(f"разложение сходится ({tag}, получатели)", d["residual_tr"]))
        checks.append(A.check(f"разложение сходится ({tag}, ФЛ)", d["residual_epk"]))
        checks.append(A.check(f"итог разложения = итог набора ({tag})",
                              d["delta_tr"] - float(tot[M.iso(c)] - tot[M.iso(b)])))
        sd = A.seg_decomp(res["tr_raw"][(kind, m)],
                          seg_tot[M.iso(b)] if M.iso(b) in seg_tot else pd.Series(dtype=float),
                          seg_tot[M.iso(c)] if M.iso(c) in seg_tot else pd.Series(dtype=float))
        checks.append(A.check(f"сегменты сходятся ({tag})", float(sd["residual"].abs().sum())))
        checks.append(A.check(f"перестали: Σ сегментов = банк ({tag})",
                              float(-sd["stopped"].sum() - d["lost_tr"])))
        checks.append(A.check(f"начали: Σ сегментов = банк ({tag})",
                              float(sd["started"].sum() - d["gained_tr"])))
        checks.append(A.check(f"совместительство: Σ сегментов = банк ({tag})",
                              float(sd["other_seg"].sum() + sd["inside"].sum() - d["inside_tr"])))
        mx = A.flow_matrix(fl)
        checks.append(A.check(f"матрица перетоков замкнута ({tag})",
                              float(mx.drop(index=A.OUTSIDE).sum().sum() - d["base_epk"]
                                    + mx.drop(columns=A.OUTSIDE).sum().sum() - d["cur_epk"])))
        comp[(kind, m)] = {"b": M.iso(b), "c": M.iso(c), "d": d, "seg": sd, "mx": mx,
                           "seg_fl": A.seg_fl(mx), "rows": A.decomposition(d, b, c),
                           "causes": A.causes(d)}
    res["comp"] = comp

    did = {}
    for m in report:
        mi = M.iso(m)
        dc, dp = comp[("cur", mi)]["d"], comp[("prev", mi)]["d"]
        tab = A.did_components(dc, dp, m, M.shift(m, -12))
        expect = yoy.get(mi, float("nan")) - yoy.get(M.iso(M.shift(m, -1)), float("nan"))
        got = float(tab[tab["sub"].isna()]["diff"].sum())
        checks.append(A.check(f"разница разниц = ΔYoY({M.label(m)}) − ΔYoY({M.label(M.shift(m, -1))})",
                              got - expect))
        for g in ("lost", "gained", "inside"):
            x = tab[tab["group"] == g]
            checks.append(A.check(f"разница разниц: подстроки = группа ({g}, {M.label(m)})",
                                  float(x[x["sub"].notna()]["diff"].sum() - x[x["sub"].isna()]["diff"].sum())))
        back = {"cur": dc.get("lost_back_tr"), "prev": dp.get("lost_back_tr"),
                "cur_share": dc.get("lost_back_tr") / dc["lost_tr"] if dc.get("lost_tr") else None,
                "prev_share": dp.get("lost_back_tr") / dp["lost_tr"] if dp.get("lost_tr") else None,
                "has_next": bool(dc.get("has_next"))}
        did[mi] = {"table": tab, "expect": expect, "back": back,
                   "causes": A.did_lost_causes(dc, dp),
                   "seg": A.did_segments(comp[("cur", mi)]["seg"], comp[("prev", mi)]["seg"]),
                   "drivers": A.drivers(tab)}
    res["did"] = did

    res["cohorts"] = A.cohort_table(res["cohort_raw"])
    res["dissolved"] = A.dissolved(res["cohorts"], report)
    res["cohort_seg"] = {M.iso(k): A.cohort_seg(res["cohort_raw"], "mom", k,
                                                (report[-1].year * 12 + report[-1].month)
                                                - (k.year * 12 + k.month))
                         for k in report[:-1]}
    have_next = {M.iso(m): M.iso(M.shift(m, 1)) in res["work"] for m in report}
    res["seasonal"] = A.seasonal_table(res["seasonal_raw"], have_next)
    res["code_tables"] = {M.iso(m): A.code_table(res["codes_raw"], m) for m in report}
    res["tb"] = {M.iso(m): A.tb_table(res["tb_seg_raw"], res["tb_dim"], m) for m in report}

    res["orgs"] = {}
    for m in report:
        mi = M.iso(m)
        raw = res["orgs_raw"][mi]
        lst = A.org_list(raw["list"], res["tb_dim"])
        smr = A.org_summary(raw["summary"])
        if not lst.empty:
            checks.append(A.check(f"организации: реальное ≤ перестали ({M.label(m)})",
                                  float((lst["real_cut"] - lst["out_stopped"]).clip(lower=0).sum())))
        if not smr.empty and not lst.empty:
            n_cut = float(smr.loc[smr["cls"] == "cut", "n_orgs"].sum())
            checks.append(A.check(f"организации: в списке = класс «реальное сокращение» ({M.label(m)})",
                                  n_cut - float(lst["n_picked"].iloc[0])))
        if not smr.empty:
            b = M.iso(M.shift(m, -12))
            pairs = float(mb.set_index("report_dt").loc[b, "n_pairs"]) if b in set(mb["report_dt"]) else float("nan")
            checks.append(A.check(f"организации покрывают всю базу ({M.label(m)})",
                                  float(smr["base_fl"].sum()) - pairs,
                                  detail="Σ ФЛ по ИНН = Σ пар ФЛ×ИНН набора"))
        res["orgs"][mi] = {"list": lst, "summary": smr, "summary_seg": raw["summary"],
                           "reorg": raw["reorg"]}
    res["checks"] = checks
    bad = [c for c in checks if not c["ok"]]
    if bad:
        for c in bad:
            progress.warn(f"НЕ СХОДИТСЯ: {c['check']} — невязка {c['residual']}")
    else:
        progress.done(f"проверки сходимости: все {len(checks)} сошлись")


# --------------------------------------------------------------------------- #
def _write(res: dict) -> None:
    from . import report_md, view
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = res["report_month"][:7].replace("-", "")
    base = config.OUTPUT_DIR / f"payroll_bank_yoy_{tag}_{stamp}"
    progress.step("Запись результатов")
    html = view.render(res)
    p_html = base.with_suffix(".html")
    p_html.write_text(html, encoding="utf-8")
    res["path"] = str(p_html)
    progress.done(f"HTML: {p_html.name} ({len(html) / 1e6:.1f} МБ)")

    doc, leaks = report_md.render(res)
    res["doc_leaks"] = leaks
    if leaks:
        res["doc"] = None
        progress.warn(f"обезличенный документ НЕ записан: просочились {leaks[:5]}")
    else:
        p_md = base.with_suffix(".md")
        p_md.write_text(doc, encoding="utf-8")
        res["doc"] = str(p_md)
        progress.done(f"документ: {p_md.name}")

    csv_paths = []
    for m, o in res["orgs"].items():
        if o["list"].empty:
            continue
        p = config.OUTPUT_DIR / f"orgs_real_cut_{m[:7].replace('-', '')}_{stamp}.csv"
        o["list"].to_csv(p, index=False, encoding="utf-8-sig", sep=";")
        csv_paths.append(str(p))
    res["csv"] = csv_paths
    if csv_paths:
        progress.done(f"CSV со списками организаций: {len(csv_paths)}")

    (config.OUTPUT_DIR / "probe.json").write_text(
        json.dumps(res["probe"], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    cfg = {k: res[k] for k in ("report_month", "schema", "amt_min", "codes", "org_opts",
                               "report", "work", "generated")}
    cfg["timing_sec"] = res.get("timing", {})
    (config.OUTPUT_DIR / "run_config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
