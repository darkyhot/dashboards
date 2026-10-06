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
        org_opts: dict | None = None,
        focus_holding: str = "МИНОБОРОНЫ",
        adv_pattern: str = A.ADV_RE, sal_pattern: str = A.SAL_RE,
        sql_timeout_min: int = config.SQL_TIMEOUT_MIN,
        verbose: bool = True, show_sql: bool = False) -> dict:
    t_start = time.time()
    progress.enable(verbose, show_sql)
    config.set_schema(schema)
    config.ensure_dirs()
    org_opts = {"min_base": 20, "min_real": 10, "min_share": 0.05,
                "reorg_min_share": 0.30, "reorg_min_movers": 5, "max_rows": 3000,
                "hole_min_base": 20, "break_max": 40,
                **(org_opts or {})}
    engine = db.get_engine(config.db_url(conn), int(sql_timeout_min))
    db.ping(engine)
    progress.done(f"БД доступна, схема {config.SCHEMA}")

    last = M.parse(report_month)
    hist = M.span(M.shift(last, -(history_months - 1)), last)
    res: dict = {"report_month": M.iso(last), "schema": config.SCHEMA, "amt_min": amt_min,
                 "codes": list(codes), "org_opts": org_opts, "focus_holding": focus_holding,
                 "generated": datetime.now().strftime("%Y-%m-%d %H:%M")}

    with db.session(engine) as cx:
        pr = probe.run(cx)
        res["probe"] = pr
        # Месяцы копии: ряд, рабочий набор обоих лет и месяц после отчётного.
        first = M.shift(last, -(n_months - 1))
        raw_from = min(hist[0], M.shift(first, -LOOKBACK - 12))
        raw_months = M.span(raw_from, M.shift(last, 1))
        ws = fetch.Workspace(cx, [], {"codes": list(codes), "amt_min": int(amt_min),
                                      "focus_holding": focus_holding,
                                      "adv_codes": [-1], "sal_codes": [-1]})
        try:
            # Все строки — только месяцам рабочего набора (кандидатам: какие из них
            # загружены, станет известно по копии); остальным — зарплатные коды.
            full = {M.iso(m) for off in (0, -12)
                    for m in M.span(M.shift(first, -LOOKBACK + off), M.shift(last, 1 + off))}
            rows = ws.build_raw(raw_months, full)
            ws.params["all_rows"] = True       # для показанного SQL: копия рабочего набора
            # Аванс и зарплата — по названиям кодов из первого скопированного месяца.
            pc = A.classify_codes(ws.code_names, adv_pattern, sal_pattern)
            res["pay_codes"] = pc
            ws.params["adv_codes"] = pc["adv"] or [-1]
            ws.params["sal_codes"] = pc["sal"] or [-1]
            progress.done(f"коды аванса: {pc['adv'] or 'не найдены'}; зарплаты: {pc['sal'] or 'не найдены'}")
            avail = {m for m, n in rows.items() if n > 0}
            pr["months"] = rows
            if not avail:
                raise probe.ProbeError(
                    "в витрине нет ни одной строки за нужные месяцы — проверьте схему и что "
                    "report_dt в ведомостях — последний день месяца")
            plan = _months_plan(last, n_months, avail)
            if plan["missing"]:
                raise probe.ProbeError(f"в витрине нет месяцев, нужных разбору: {plan['missing']}")
            report = plan["report"]
            res["report"] = [M.iso(m) for m in report]
            res["work"] = [M.iso(m) for m in plan["work"]]
            nxt = M.iso(M.shift(last, 1))
            res["has_next"] = nxt in avail
            if res["has_next"]:
                # Ряд — до месяца после отчётного: отскок после провала виден на графике.
                hist = hist + [M.parse(nxt)]
            else:
                progress.warn(f"{M.label(nxt)} не загружен — проверка «вернулись в следующем "
                              f"месяце» для {M.label(last)} не строится")
            hist = [m for m in hist if M.iso(m) in avail]
            code_ms = sorted({M.shift(m, k) for m in report for k in (0, -1, -12, -13)})

            ws.months = res["work"]
            ws.pt_months = [M.shift(m, off) for m in report for off in (0, -12)]
            ws.build(hist, code_ms)
            res["series_focus_raw"] = ws.series_focus
            res["paytype_raw"] = ws.paytype
            res["focus_tot_raw"] = ws.opt("focus_tot", Q.FOCUS_TOT)
            nf = res["focus_tot_raw"]
            progress.done(f"холдинг «{focus_holding}»: " + (
                f"{int(nf['n_orgs'].max()):,} организаций с получателями" if not nf.empty
                else "организаций с получателями нет"))
            res["series_raw"] = (pd.concat(ws.series_rows, ignore_index=True)
                                 if ws.series_rows else pd.DataFrame())
            res["load_raw"] = ws.load
            res["codes_raw"] = ws.codes

            progress.step("Итоги месяцев набора")
            res["multi_bank_raw"] = ws.sql("multi_bank", Q.MULTI_BANK)
            res["multi_seg_raw"] = ws.sql("multi_seg", Q.MULTI_SEG)
            res["tb_seg_raw"] = ws.opt("tb_seg", Q.TB_SEG)
            res["tb_dim"] = ws.opt("tb_dim", Q.TB_DIM)
            progress.done("совместительство и территория")

            progress.step("Переходы: год к году и месяц к месяцу в обоих годах")
            fl, tr, stp, ff = {}, {}, {}, {}

            def flows(key, b, c, tag):
                fl[key] = fetch.fl_flow(ws, b, c, tag)
                tr[key] = fetch.triple_flow(ws, b, c, tag)
                # По той же t_tflow: отток B2C/B2B и разложение холдинга.
                stp[key] = fetch.stop_size(ws, b, c, tag)
                ff[key] = fetch.focus_flow(ws, b, c, tag)

            for m in report:
                flows(("yoy", M.iso(m)), M.shift(m, -12), m, f"yoy_{M.iso(m)}")
                for off, y in ((0, "cur"), (-12, "prev")):
                    c = M.shift(m, off)
                    flows((y, M.iso(m)), M.shift(c, -1), c, f"mom_{M.iso(c)}")
                progress.done(f"{M.label(m)}: год к году и два месячных перехода")
            res["fl_raw"], res["tr_raw"], res["stop_raw"], res["ff_raw"] = fl, tr, stp, ff

            progress.step("Когорты пришедших, сезонность")
            coh = {}
            for off in (0, -12):
                for k in M.span(M.shift(report[0], -1 + off), M.shift(report[-1], off)):
                    coh[("mom", k)] = fetch.cohort(ws, k, M.shift(k, -1), f"mom_{M.iso(k)}")
            for k in report:
                coh[("yoy", k)] = fetch.cohort(ws, k, M.shift(k, -12), f"yoy_{M.iso(k)}")
            res["cohort_raw"] = coh
            # Где растворились: когорты до последнего отчётного месяца, по разрезам.
            cd = {"cur": [], "prev": []}
            for k in M.span(M.shift(report[0], -1), M.shift(report[-1], -1)):
                for y, off in (("cur", 0), ("prev", -12)):
                    kk = M.shift(k, off)
                    cd[y].append(fetch.cohort_dim(ws, kk, M.shift(kk, -1), M.shift(last, off),
                                                  f"{M.iso(kk)}"))
            res["cohort_dim_raw"] = cd
            res["seasonal_raw"] = {M.iso(m): fetch.seasonal(ws, m) for m in report}
            progress.done("когорты и сезонность")

            progress.step("Организации: где численность реально сократилась")
            res["orgs_raw"] = {}
            for m in report:
                res["orgs_raw"][M.iso(m)] = fetch.orgs(ws, M.shift(m, -12), m, org_opts, M.iso(m))
                n = len(res["orgs_raw"][M.iso(m)]["list"])
                progress.done(f"{M.label(m)}: в списке {n:,} организаций")
            if res["has_next"]:
                progress.step(f"{M.label(last)}: потеря или перенос в {M.label(M.shift(last, 1))}")
                res["aug_raw"] = fetch.august(ws, last, org_opts["hole_min_base"], 300,
                                              org_opts["break_max"])
                progress.done("организации с провалом и подпись переноса")
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
        checks.append(A.check("ряд (t_stage) = рабочий набор (t_pairs), получатели",
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

    # Отток B2C / B2B: ступени обязаны сложиться ровно в «перестали получать».
    res["stop"] = {}
    for key, df in res.get("stop_raw", {}).items():
        if df is None or df.empty or not comp[key]["d"]:
            continue
        got = float(A.stop_buckets(df)["n_tr"].sum())
        checks.append(A.check(f"B2C + B2B = перестали получать ({key[0]} {M.label(key[1])})",
                              got - float(comp[key]["d"]["lost_tr"])))
    for m in report:
        mi = M.iso(m)
        sr = res.get("stop_raw", {})
        if sr.get(("cur", mi)) is None or sr[("cur", mi)].empty:
            continue
        res["stop"][mi] = {"did": A.stop_did(sr[("cur", mi)], sr.get(("prev", mi))),
                           "seg": A.stop_seg(sr[("cur", mi)], sr.get(("prev", mi))),
                           "yoy": A.stop_buckets(sr.get(("yoy", mi)))}

    _focus(res, checks, report)
    res["cohort_dims"] = A.cohort_dims(res["cohort_dim_raw"]["cur"], res["cohort_dim_raw"]["prev"]) \
        if res.get("cohort_dim_raw") else {}
    pt = res.get("paytype_raw")
    res["paytype"] = {}
    if pt is not None and not pt.empty:
        for m in report:
            res["paytype"][M.iso(m)] = {"table": A.paytype_table(pt, m), "mix": A.paytype_mix(pt, m),
                                        "seg": A.paytype_seg(pt, m),
                                        "focus": A.paytype_table(pt, m, focus_only=True)}

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
                                  detail="Σ ФЛ по id орг = Σ пар ФЛ×организация набора"))
        brk = A.org_break(raw.get("break"))
        if brk and not lst.empty:
            sg = brk["seg"]
            checks.append(A.check(f"организации: Σ сегментов разреза = список ({M.label(m)})",
                                  float(sg["n_orgs"].sum()) - float(lst["n_picked"].iloc[0])))
            checks.append(A.check(f"организации: реальное сокращение разреза = списка ({M.label(m)})",
                                  float(sg["real_cut"].sum()) - float(lst["sum_real_picked"].iloc[0])))
        foc = raw.get("focus")
        if foc is not None and not foc.empty:
            foc = A.org_list(foc, res["tb_dim"])
        res["orgs"][mi] = {"list": lst, "summary": smr, "summary_seg": raw["summary"],
                           "reorg": raw["reorg"], "break": brk,
                           "focus": foc if foc is not None else pd.DataFrame()}
    if res.get("aug_raw"):
        last = report[-1]
        aug = res["aug_raw"]
        for k in ("hole_cur", "hole_prev"):
            aug[k] = A.with_labels(aug[k])
        res["hole_dims"] = A.hole_dims(aug.get("hole_dim_cur"), aug.get("hole_dim_prev"))
        if res["hole_dims"]:
            checks.append(A.check(f"пропустившие месяц: Σ сегментов = всего ({M.label(last)})",
                                  float(res["hole_dims"]["seg"]["n_orgs_cur"].sum())
                                  - A.hole_summary(aug["hole_cur"])["n_orgs"]))
        res["bridge"] = A.bridge(mb, last)
        tp = A.temp_perm(comp[("cur", M.iso(last))]["d"], comp[("prev", M.iso(last))]["d"])
        res["temp_perm"] = tp
        checks.append(A.check(f"временное + постоянное = перестали ({M.label(last)})",
                              float(tp["diff"].iloc[:2].sum() - tp["diff"].iloc[2])))
        if not res["bridge"].empty:
            br = res["bridge"].set_index("key")
            nxt = M.iso(M.shift(last, 1))
            checks.append(A.check("мост: разница переходов в следующем месяце = ΔYoY(след.) − ΔYoY(отч.)",
                                  float(br.loc["did", "next"]
                                        - (br.loc["yoy", "next"] - br.loc["yoy", "cur"]))))
    res["checks"] = checks
    bad = [c for c in checks if not c["ok"]]
    if bad:
        for c in bad:
            progress.warn(f"НЕ СХОДИТСЯ: {c['check']} — невязка {c['residual']}")
    else:
        progress.done(f"проверки сходимости: все {len(checks)} сошлись")


# --------------------------------------------------------------------------- #
def _focus(res: dict, checks: list, report: list) -> None:
    """Выделенный холдинг: итоги, разложения переходов, разница разниц, отток B2C/B2B."""
    ft = A.focus_tot(res.get("focus_tot_raw"))
    res["focus_tot"] = ft
    res["focus"] = {"comp": {}, "did": {}, "stop": {}}
    sf = res.get("series_focus_raw")
    res["focus_series"] = (sf.assign(report_dt=sf["report_dt"].astype(str)).set_index("report_dt").sort_index()
                           if sf is not None and not sf.empty else pd.DataFrame())
    if ft.empty:
        return
    if not res["focus_series"].empty:
        fs = res["focus_series"]["n_triples"].astype(float)
        common = [d for d in ft.index if d in fs.index]
        checks.append(A.check("холдинг: ряд (t_stage) = рабочий набор (t_pairs)",
                              float(sum(abs(fs[d] - ft.loc[d, "n_triples"]) for d in common))))
    for (kind, m), df in res.get("ff_raw", {}).items():
        md = M.parse(m)
        c = md if kind != "prev" else M.shift(md, -12)
        b = M.shift(c, -12) if kind == "yoy" else M.shift(c, -1)
        get = lambda d: float(ft.loc[M.iso(d), "n_triples"]) if M.iso(d) in ft.index else 0.0
        d = A.focus_decomp(df, get(b), get(c))
        tag = f"{kind} {M.label(b)}→{M.label(c)}"
        checks.append(A.check(f"холдинг: разложение сходится ({tag})", d["residual"]))
        sr = res.get("stop_raw", {}).get((kind, m))
        if sr is not None and not sr.empty:
            checks.append(A.check(f"холдинг: B2C + B2B = перестали ({tag})",
                                  float(A.stop_buckets(sr, focus_only=True)["n_tr"].sum()) - d["stopped"]))
        res["focus"]["comp"][(kind, m)] = {"b": M.iso(b), "c": M.iso(c), "d": d,
                                           "rows": A.focus_rows(d, b, c)}
    for m in report:
        mi = M.iso(m)
        fc = res["focus"]["comp"]
        if ("cur", mi) not in fc or ("prev", mi) not in fc:
            continue
        dc, dp = fc[("cur", mi)]["d"], fc[("prev", mi)]["d"]
        t = A.focus_did(dc, dp)
        checks.append(A.check(f"холдинг: разница разниц = разница изменений ({M.label(m)})",
                              float(t["diff"].sum()) - (dc["delta"] - dp["delta"])))
        res["focus"]["did"][mi] = t
        sr = res.get("stop_raw", {})
        if sr.get(("cur", mi)) is not None and not sr[("cur", mi)].empty:
            res["focus"]["stop"][mi] = {"did": A.stop_did(sr[("cur", mi)], sr.get(("prev", mi)), True),
                                        "yoy": A.stop_buckets(sr.get(("yoy", mi)), True)}


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
        # Латинское имя колонки номера организации в выгрузку не идёт — «org_id».
        out = o["list"].rename(columns=lambda c: c.replace("".join(map(chr, (105, 110, 110))), "org_id"))
        out.to_csv(p, index=False, encoding="utf-8-sig", sep=";")
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
