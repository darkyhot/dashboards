"""Оркестратор: разведка → выгрузка → расчёты → тексты → HTML.

Все параметры приходят из тетрадки и мержатся поверх DEFAULTS. Ни одного
значения, которое надо править в .py, чтобы поменять модель, месяц или горизонт.

Отчётный месяц — ЯВНЫЙ параметр. Автовыбор «последний месяц витрины» тихо
сдвигает отчёт: запуск третьего числа возьмёт уже начавшийся месяц вместо
предыдущего, и заметить это по готовому файлу невозможно. Пустое значение
допускается, но сопровождается предупреждением в прогрессе.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from uzp_dash import config, db, llm, progress
from uzp_dash.anonymize import Aliases

from . import agency as AG
from . import analyze as A
from . import fetch, forecast as F, narrative as N, probe, prompts as P
from . import queries as LQ
from . import view as V

DEFAULTS = dict(
    # Отчётный месяц, 'ГГГГ-ММ'. Пусто — последний месяц витрины (с предупреждением).
    report_month="",
    # До какого месяца строится прогноз, 'ГГГГ-ММ'.
    horizon_to="2026-12",
    # Сколько месяцев истории тянуть в разрезы оттока.
    history_months=12,
    # Сколько месяцев истории тянуть в панель численности (нужна прогнозу и причинам).
    panel_months=24,
    # Окно разбора причин, мес.
    cause_window=A.CAUSE_WINDOW_M,
    # Окно, по которому берутся слагаемые прогноза, мес.
    forecast_window=F.WINDOW_M,
    # Потолок вызовов LLM НА ВЕСЬ ОТЧЁТ: единица разбора здесь одна — отчёт целиком.
    llm_max_calls=6,
    # Сколько строк показывать в территориальных таблицах.
    top_n=12,
)


def _month_end(value: str) -> pd.Timestamp:
    return pd.Timestamp(value).to_period("M").to_timestamp("M")


def _resolve_month(engine, report_month: str) -> pd.Timestamp:
    """Отчётный месяц: из параметра либо последний месяц витрины оттока."""
    if report_month:
        return _month_end(report_month)
    df = db.read_sql(engine,
                     "SELECT max(report_dt) AS d FROM {schema}.uzp_dwh_fact_outflow "
                     "WHERE segment_name = :seg", {"seg": LQ.SEG_SHORT})
    d = df["d"].iloc[0] if not df.empty else None
    if d is None:
        raise RuntimeError("витрина оттока пуста — отчётный месяц определить не из чего")
    m = _month_end(str(d))
    progress.warn(f"report_month не задан — взят последний месяц витрины {m:%m.%Y}. "
                  f"Если прогон идёт в начале месяца, это может быть НЕЗАКРЫТЫЙ "
                  f"месяц: задайте месяц явно параметром report_month")
    return m


def run(conn: str | None = None, out_dir: str | Path | None = None,
        contour: str | None = None, verbose: bool = True, show_sql: bool = False,
        show_llm: bool = False, llm_opts: dict | None = None, **params) -> dict:
    """Полный прогон. Возвращает словарь результатов, HTML кладёт в out_dir."""
    t_start = time.time()
    opts = {**DEFAULTS, **params}

    progress.enable(verbose=verbose, show_sql=show_sql, show_llm=show_llm)
    config.set_contour(contour)
    lo = llm.configure(**(llm_opts or {}))
    model = llm._model_for(config.CONTOUR, None)   # noqa: SLF001 — только для печати
    progress.done(f"LLM: модель {model} · max_tokens={lo['max_tokens']} · "
                  f"timeout={lo['timeout']} · бюджет вызовов {opts['llm_max_calls']} · "
                  f"логи → {progress.LOG_DIR}")

    out_dir = Path(out_dir) if out_dir else (config.OUTPUT_DIR / "rgs_outflow")
    out_dir.mkdir(parents=True, exist_ok=True)

    progress.step(f"Отток бюджетной сферы · контур {config.CONTOUR} · "
                  f"схема {config.SCHEMA}")
    engine = db.get_engine(config.db_url(conn))
    db.ping(engine)

    # --- опорные даты ---
    month = _resolve_month(engine, str(opts["report_month"]).strip())
    d_to = month.date().isoformat()
    d_from = (month - pd.DateOffset(months=int(opts["history_months"]) - 1))
    d_from = _month_end(d_from).date().isoformat()
    p_from = _month_end(month - pd.DateOffset(months=int(opts["panel_months"]) - 1))
    p_from = p_from.date().isoformat()
    horizon_to = _month_end(str(opts["horizon_to"]))
    progress.done(f"отчётный месяц {month:%m.%Y}; история оттока с {d_from}; "
                  f"панель с {p_from}; горизонт прогноза до {horizon_to:%m.%Y}")

    # --- разведка ---
    pr = probe.run(engine, d_from, d_to, out_dir)

    # --- выгрузка ---
    fact = fetch.rgs_fact(engine, d_from, d_to)
    names = fetch.rgs_names(engine, d_from, d_to)
    comps = fetch.rgs_competitors(engine, d_from, d_to)
    gosb = fetch.gosb_dim(engine)
    panel = fetch.rgs_panel(engine, d_from, d_to, p_from)
    metrics = fetch.seg_metrics(engine, p_from, d_to)

    # --- разметка и расчёты ---
    progress.step("Расчёты")
    df, meta = A.enrich(fact, names, gosb, comps)
    if pd.Timestamp(A.report_month(df)) != month:
        progress.warn(f"в выборке нет месяца {month:%m.%Y} — разрезы строятся "
                      f"на {A.report_month(df):%m.%Y}")
        month = pd.Timestamp(A.report_month(df))

    org_meta = df.drop_duplicates("inn")[
        [c for c in ("inn", "agency", "region_name", "tb_short_name", "company_name")
         if c in df]]

    ag, chk_ag = A.by_agency(df, month)
    ag_shown, ag_cut = A.materialize(ag, "agency", "out_kept", keep_last=AG.UNKNOWN)
    tr = A.trend(df)
    ag_tr = A.agency_trends(df)
    tb = A.by_territory(df, month, "tb_short_name").head(int(opts["top_n"]))
    reg = A.by_territory(df, month, "region_name").head(int(opts["top_n"]))
    subj = A.by_territory(df, month, "subject_code").head(int(opts["top_n"]))

    org_causes, causes_ag, chk_cause = A.causes(
        panel, org_meta, month, int(opts["cause_window"]))
    causes_reg = A.causes_by_region(org_causes)
    macro = A.macro(panel, org_meta, metrics, gosb, month)
    cp = A.competitors(df, month, pr["key_client"]["coverage_of_segment"])

    fc, end, fdiag = F.run(panel, df, org_meta, month, horizon_to,
                           int(opts["forecast_window"]))

    m = df[pd.to_datetime(df["report_dt"]) == month]
    totals = {
        "base_fl": float(m["calc_fl"].sum()), "out_qty": float(m["out_qty"].sum()),
        "ret_qty": float(m["ret_qty"].sum()), "out_kept": float(m["out_kept"].sum()),
        "n_org": int(m["inn"].nunique()),
    }

    # --- тексты ---
    progress.step("Текстовые выводы")
    texts = _narrate(df, month, totals, tr, ag_shown, tb, reg, causes_ag, causes_reg,
                     org_causes, cp, end, fdiag, opts, gosb)

    # --- сборка ---
    progress.step("Сборка HTML")
    checks = [chk_ag, chk_cause] + list(fdiag.get("checks", []))
    warnings = list(pr["warnings"]) + list(fdiag.get("warnings", []))
    oktmo_note = ""
    if pr["oktmo"]["n_full"] < pr["fact"]["n_rows"]:
        share = pr["oktmo"]["n_full"] / max(pr["fact"]["n_rows"], 1)
        oktmo_note = (
            f"Субъект РФ известен по {share:.0%} строк витрины: код берётся из "
            f"полного ОКТМО. Колонка oktmo_subject_code для этого непригодна — "
            f"в ней встречаются значения длиной ноль-два символа и нечисловой "
            f"мусор. Организации без ОКТМО в этот разрез не попали.")

    drop = float(org_causes["drop"].sum()) if not org_causes.empty else 0.0
    staff = float(org_causes["staff_cut"].sum()) if not org_causes.empty else 0.0
    comp_v = float(org_causes["competitor"].sum()) if not org_causes.empty else 0.0
    used_window = int(opts["cause_window"])
    if not org_causes.empty:
        used_window = len(pd.period_range(
            pd.Timestamp(org_causes["base_month"].iloc[0]),
            pd.Timestamp(org_causes["report_month"].iloc[0]), freq="M")) - 1

    blocks = [
        V.head_kpi(month, totals["base_fl"], totals["out_qty"], totals["ret_qty"],
                   totals["out_kept"], totals["n_org"]),
        V.agencies_block(ag_shown, ag_cut, texts["overview"][0], texts["overview"][1]),
        V.trend_block(tr, ag_tr, pr["trend_source"]),
        V.territory_block(tb, reg, subj, texts["territory"][0], texts["territory"][1],
                          oktmo_note),
        V.causes_block(drop, staff, comp_v, used_window, causes_ag, causes_reg,
                       texts["causes"][0], texts["causes"][1]),
        V.macro_block(macro),
        V.competitors_block(cp, texts["competitors"][0], texts["competitors"][1]),
        V.outlook_block(fc, end, fdiag, month, totals["base_fl"],
                        texts["outlook"][0], texts["outlook"][1]),
        V.limits_block(warnings, checks, meta),
    ]
    footer = (f"Сформировано {datetime.now():%d.%m.%Y %H:%M} · контур "
              f"{config.CONTOUR} · модель {model} · "
              f"источники: витрина оттока, возвраты, витрина организаций, "
              f"справочник компаний, ключевые клиенты, справочник ГОСБ")
    html = V.page(f"Отток бюджетной сферы · {month:%m.%Y}",
                  "Ведомства, территории, причины и прогноз численности",
                  blocks, footer)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"rgs_outflow_{month:%Y%m}_{ts}.html"
    path.write_text(html, encoding="utf-8")
    (out_dir / "run_config.json").write_text(
        json.dumps({**opts, "report_month": f"{month:%Y-%m}", "contour": config.CONTOUR,
                    "model": model, "d_from": d_from, "d_to": d_to, "p_from": p_from},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    size_mb = path.stat().st_size / 1e6
    progress.done(f"Готово за {time.time() - t_start:.0f} с: {path} ({size_mb:.2f} МБ)")
    for w in warnings:
        progress.warn(w)
    return {"path": str(path), "probe": pr, "agencies": ag, "trend": tr,
            "causes": org_causes, "causes_by_agency": causes_ag, "macro": macro,
            "competitors": cp, "forecast": fc, "scenarios": end, "checks": checks,
            "warnings": warnings, "meta": meta, "totals": totals}


def _narrate(df, month, totals, tr, ag, tb, reg, causes_ag, causes_reg, org_causes,
             cp, end, fdiag, opts, gosb) -> dict:
    """Тексты разделов. Названия подразделений и регионов уходят в модель токенами.

    Псевдонимы заводятся на КАЖДЫЙ раздел заново: словарь живёт ровно один вызов,
    и токен из одного промпта не должен «протекать» в другой.
    """
    nar = N.Narrator(max_calls=int(opts["llm_max_calls"]))
    ms = f"{month:%m.%Y}"
    out: dict = {}

    out["overview"] = nar.section(
        "обзор",
        P.overview(ms, totals["out_qty"], totals["out_kept"], totals["ret_qty"],
                   totals["base_fl"], tr["direction"], ag),
        N.fb_overview(ms, totals["out_kept"], totals["ret_qty"], totals["base_fl"],
                      ag, tr["direction"]))

    al = Aliases()
    tb_m = N.mask_frame(tb, "tb_short_name", "ТБ", al)
    reg_m = N.mask_frame(reg, "region_name", "Регион", al)
    prompt = P.territory(ms, tb_m, reg_m)
    # Проверка обезличивания: ни одно настоящее название не должно найтись
    forbidden = list(tb.get("tb_short_name", [])) + list(reg.get("region_name", []))
    leaks = N.check_masked(prompt, forbidden)
    if leaks:
        progress.warn(f"обезличивание: в промпт «территории» попали настоящие "
                      f"названия ({', '.join(leaks[:5])}) — раздел уходит в фолбэк")
        out["territory"] = (N.fb_territory(tb, reg), True)
    else:
        out["territory"] = nar.section("территории", prompt,
                                       N.fb_territory(tb, reg), al)

    al2 = Aliases()
    creg_m = N.mask_frame(causes_reg.head(12), "region_name", "Регион", al2)
    drop = float(org_causes["drop"].sum()) if not org_causes.empty else 0.0
    staff = float(org_causes["staff_cut"].sum()) if not org_causes.empty else 0.0
    comp_v = float(org_causes["competitor"].sum()) if not org_causes.empty else 0.0
    out["causes"] = nar.section(
        "причины",
        P.causes(ms, int(opts["cause_window"]), drop, staff, comp_v,
                 causes_ag, creg_m),
        N.fb_causes(int(opts["cause_window"]), drop, staff, comp_v, causes_reg), al2)

    out["competitors"] = nar.section(
        "конкуренты",
        P.competitors(ms, cp.get("coverage", 0.0), cp.get("banks", pd.DataFrame())),
        N.fb_competitors(cp.get("coverage", 0.0), cp.get("banks", pd.DataFrame())))

    horizon = fdiag.get("horizon") or []
    hz = pd.Timestamp(horizon[-1]).strftime("%m.%Y") if horizon else "—"
    out["outlook"] = nar.section(
        "прогноз",
        P.outlook(ms, hz, len(horizon), totals["base_fl"],
                  end if end is not None else pd.DataFrame(),
                  bool(fdiag.get("season_measurable"))),
        N.fb_outlook(hz, len(horizon), totals["base_fl"], end,
                     bool(fdiag.get("season_measurable"))))

    progress.done(f"тексты: вызовов LLM {nar.calls}/{nar.max_calls}, "
                  f"на фолбэке разделов {len(nar.used_fallback)}"
                  + (f" ({', '.join(nar.used_fallback)})" if nar.used_fallback else ""))
    return out
