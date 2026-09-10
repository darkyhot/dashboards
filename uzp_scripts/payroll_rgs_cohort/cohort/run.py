"""Оркестратор: разведка → рабочий набор → расчёты → тексты → HTML и документ.

Все параметры приходят из тетрадки и мержатся поверх DEFAULTS. Ни одного
значения, которое надо править в .py, чтобы поменять месяц, порог или модель.

Отчётный месяц — ЯВНЫЙ параметр. Автовыбор «последний месяц витрины» тихо
сдвигает разбор: запуск третьего числа возьмёт уже начавшийся месяц вместо
предыдущего, и заметить это по готовому файлу невозможно.

Соединение держится ОДНО на весь прогон (`db.session`): на нём живёт рабочий
набор. Без этого каждая выборка открывала бы своё соединение, временные таблицы
исчезали бы между запросами, и разбор либо падал бы, либо — хуже — находил
чужие таблицы, оставшиеся в пуле от прошлого прогона.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from uzp_dash import config, db, llm, progress

from . import analyze as A
from . import fetch, narrative as N, probe, prompts as P
from . import queries as LQ
from . import report_text as RT
from . import view as V

DEFAULTS = dict(
    # Отчётный месяц, 'ГГГГ-ММ'. Сравнивается с тем же месяцем годом ранее.
    report_month="2026-08",
    # На сколько месяцев назад брать базу сравнения. 12 — год к году.
    base_offset_months=12,
    # Глубина помесячного ряда, мес. Ряд обязан покрывать оба опорных месяца.
    history_months=25,
    # Сколько строк показывать в разрезах.
    top_n=12,
    # Сколько организаций показывать в списке крупнейших потерь.
    top_orgs=15,
    # Минимальное число людей, переехавших из одного номера в другой, чтобы пара
    # «откуда→куда» вообще попала в разбор миграции. Меньше — это шум.
    migration_min_movers=5,
    # Минимальная ДОЛЯ потерь организации, ушедшая в один приёмник, чтобы назвать
    # это реорганизацией. Числом людей это не измерить: двадцать из двадцати и
    # двадцать из двух тысяч — разные события.
    migration_min_share=0.30,
    # Потолок вызовов LLM на весь разбор: разделов четыре.
    llm_max_calls=5,
)


def _month_end(value: str) -> pd.Timestamp:
    return pd.Timestamp(value).to_period("M").to_timestamp("M")


def run(conn: str | None = None, out_dir: str | Path | None = None,
        contour: str | None = None, verbose: bool = True, show_sql: bool = False,
        show_llm: bool = False, llm_opts: dict | None = None, **params) -> dict:
    """Полный прогон. Возвращает словарь результатов, файлы кладёт в out_dir."""
    t_start = time.time()
    opts = {**DEFAULTS, **params}

    progress.enable(verbose=verbose, show_sql=show_sql, show_llm=show_llm)
    config.set_contour(contour)
    lo = llm.configure(**(llm_opts or {}))
    model = llm._model_for(config.CONTOUR, None)   # noqa: SLF001 — только для печати
    progress.done(f"LLM: модель {model} · max_tokens={lo['max_tokens']} · "
                  f"timeout={lo['timeout']} · бюджет вызовов {opts['llm_max_calls']} · "
                  f"логи → {progress.LOG_DIR}")

    out_dir = Path(out_dir) if out_dir else (config.OUTPUT_DIR / "payroll_rgs_cohort")
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- опорные даты ---
    cur = _month_end(str(opts["report_month"]))
    base = _month_end(cur - pd.DateOffset(months=int(opts["base_offset_months"])))
    first = _month_end(cur - pd.DateOffset(months=int(opts["history_months"]) - 1))
    if first > base:
        # Ряд обязан покрывать базовый месяц: иначе дожитие когорты начиналось бы
        # не с той точки, а «когда» отвечалось бы по куску периода.
        progress.warn(f"история {opts['history_months']} мес. не покрывает базовый "
                      f"месяц {base:%m.%Y} — ряд расширен до него")
        first = base
    d_cur, d_base, d_from = (cur.date().isoformat(), base.date().isoformat(),
                             first.date().isoformat())

    progress.step(f"Численность {LQ.SEG_BIG}: {base:%m.%Y} → {cur:%m.%Y} · "
                  f"контур {config.CONTOUR} · схема {config.SCHEMA}")
    engine = db.get_engine(config.db_url(conn))
    db.ping(engine)

    with db.session(engine) as conn_live:
        pr = probe.run(engine, conn_live, d_base, d_cur, d_from, out_dir)

        ws = fetch.Workspace(
            engine, conn_live, pr["code_column"],
            {"seg": LQ.SEG_BIG, "codes": list(LQ.CODES), "amt_min": LQ.AMT_MIN,
             "d_base": d_base, "d_cur": d_cur},
            use_temp=bool(pr["temp_tables"]))
        try:
            ws.build()

            progress.step("Выгрузка разбора")
            mt = fetch.month_totals(ws)
            lost = fetch.lost(ws)
            gained = fetch.gained(ws)
            lost_inn = fetch.lost_by_inn(ws)
            attrs = fetch.seg_attrs(ws)
            monthly = fetch.monthly(ws, d_from, d_cur)
            monthly_all = fetch.monthly_all(ws, d_from, d_cur)
            surv = fetch.survival(ws)
            thr_raw = fetch.threshold_sens(ws)
            codes_raw = fetch.code_mix(ws)
            mig_raw = fetch.inn_migration(ws, int(opts["migration_min_movers"]))
            gosb = fetch.gosb_dim(ws)
            gmap = fetch.gosb_map(ws)
        finally:
            # Уборка обязательна и в случае падения: соединение возвращается в
            # пул, и оставленная временная таблица досталась бы следующему
            # прогону — с ЧУЖИМИ датами внутри и правдоподобными числами.
            ws.drop()

    # --- расчёты ---
    progress.step("Расчёты")
    t = A.totals(mt, lost, gained)
    checks = A.check_additive(t, lost, gained)
    causes = A.causes_table(lost, t["lost"])
    gains = A.gains_table(gained)

    tr = A.trend(monthly)
    st = A.steps(tr)
    surv_c = A.survival_curve(surv, t["pairs_base"])
    load = A.load_health(monthly_all)

    thr = A.threshold(thr_raw)
    codes = A.code_shift(codes_raw, t["base_month"], t["report_month"], LQ.CODES)
    marked, meta = A.enrich(lost_inn, attrs, gosb, gmap)
    mig = A.migration(mig_raw, lost_inn, attrs, float(opts["migration_min_share"]))

    dims = ["holding_name", "agency", "level", "industry_name", "tb_short_name",
            "region_name"]
    cuts = {d: A.by_dim(marked, d, int(opts["top_n"])) for d in dims}
    cuts = {k: v for k, v in cuts.items() if v is not None and not v.empty}
    orgs = A.top_orgs(marked, int(opts["top_orgs"]))

    progress.done(f"падение {t['d_pairs']:,.0f} пар: людей "
                  f"{t['d_by_people']:,.0f}, совместительство "
                  f"{t['d_by_multi']:,.0f}; из потерь не отток "
                  f"{t['lost_not_real']:,.0f} из {t['lost']:,.0f}")

    # --- тексты ---
    progress.step("Текстовые выводы")
    texts = _narrate(t, causes, tr, st, surv_c, thr, codes, mig, cuts, opts)

    # --- сборка ---
    progress.step("Сборка HTML")
    warnings = list(pr["warnings"])
    blocks = [
        V.head_kpi(t),
        V.metric_block(t),
        V.causes_block(causes, gains, t, *texts["overview"]),
        V.when_block(tr, st, surv_c, load, *texts["when"]),
        V.why_block(thr, codes, mig, *texts["why"]),
        V.where_block(cuts, orgs, meta, *texts["where"]),
        V.limits_block(warnings, checks, pr),
    ]
    footer = (f"Сформировано {datetime.now():%d.%m.%Y %H:%M} · контур "
              f"{config.CONTOUR} · модель {model} · источники: ведомости, "
              f"справочник ЕПК, справочник ГОСБ")
    html = V.page(
        f"Численность бюджетной сферы · {base:%m.%Y} → {cur:%m.%Y}",
        "Разбор падения до физического лица: сколько, когда, где и почему",
        blocks, footer)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"payroll_rgs_cohort_{cur:%Y%m}_{ts}.html"
    path.write_text(html, encoding="utf-8")

    # --- обезличенный документ ---
    progress.step("Обезличенный документ")
    doc, leaks = RT.build(t, causes, gains, tr, st, surv_c, thr, codes, mig, cuts,
                          orgs, checks, warnings, pr,
                          {k: v[0] for k, v in texts.items()})
    doc_path = out_dir / f"payroll_rgs_cohort_{cur:%Y%m}_{ts}.md"
    doc_ok = RT.write(doc_path, doc, leaks)

    (out_dir / "run_config.json").write_text(
        json.dumps({**opts, "report_month": f"{cur:%Y-%m}",
                    "base_month": f"{base:%Y-%m}", "d_from": d_from,
                    "contour": config.CONTOUR, "model": model,
                    "code_column": pr["code_column"],
                    "temp_tables": pr["temp_tables"]},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    size_mb = path.stat().st_size / 1e6
    progress.done(f"Готово за {time.time() - t_start:.0f} с: {path} ({size_mb:.2f} МБ)")
    for w in warnings:
        progress.warn(w)
    for c in checks:
        if not c["ok"]:
            progress.warn(f"проверка не сошлась: {c['name']}")

    return {"path": str(path), "doc": str(doc_path) if doc_ok else "",
            "doc_leaks": leaks, "probe": pr, "totals": t, "causes": causes,
            "gains": gains, "trend": tr, "steps": st, "survival": surv_c,
            "load": load, "threshold": thr, "codes": codes, "migration": mig,
            "cuts": cuts, "orgs": orgs, "marked": marked, "checks": checks,
            "warnings": warnings, "meta": meta}


def _narrate(t, causes, tr, st, surv, thr, codes, mig, cuts, opts) -> dict:
    """Тексты разделов. Названия организаций и территорий уходят в модель токенами.

    Псевдонимы заводятся на КАЖДЫЙ раздел заново: словарь живёт ровно один вызов,
    и токен из одного промпта не должен «протекать» в другой.
    """
    nar = N.Narrator(max_calls=int(opts["llm_max_calls"]))
    bm, cm = f"{t['base_month']:%m.%Y}", f"{t['report_month']:%m.%Y}"
    out: dict = {}

    # Обзор и «когда» названий не содержат вовсе — маскировать нечего.
    out["overview"] = nar.section(
        "обзор", P.overview(bm, cm, t, causes),
        N.fb_overview(bm, cm, t, causes))
    out["when"] = nar.section(
        "когда", P.when(cm, tr, st, surv), N.fb_when(tr, st, surv))

    # «Почему» и «где» несут названия — они уходят токенами, и промпт проверяется
    # на утечку. Нашлась хоть одна — раздел уходит в фолбэк целиком: отказ шлюза
    # по blacklist стоит дороже, чем текст, посчитанный правилами.
    al = N.Aliases()
    mig_m = N.mask_frame(N.mask_frame(mig, "name_from", "Орг", al),
                         "name_to", "Орг", al)
    prompt = P.why(thr, codes, mig_m)
    forbidden = ([str(v) for v in mig.get("name_from", [])]
                 + [str(v) for v in mig.get("name_to", [])]) if not mig.empty else []
    out["why"] = _guarded(nar, "почему", prompt, N.fb_why(thr, codes, mig),
                          forbidden, al)

    al2 = N.Aliases()
    cuts_m, forbidden2 = {}, []
    kinds = {"holding_name": "Холдинг", "region_name": "Регион",
             "tb_short_name": "ТБ"}
    for dim, df in cuts.items():
        if dim in kinds:
            forbidden2 += [str(v) for v in df[dim]]
            cuts_m[dim] = N.mask_frame(df, dim, kinds[dim], al2)
        else:
            # Ведомство, уровень и отрасль — словарные значения, не названия:
            # маскировать их незачем, а без них раздел теряет весь смысл.
            cuts_m[dim] = df
    out["where"] = _guarded(nar, "где", P.where(cuts_m), N.fb_where(cuts),
                            forbidden2, al2)

    progress.done(f"тексты: вызовов LLM {nar.calls}/{nar.max_calls}, "
                  f"на фолбэке разделов {len(nar.used_fallback)}"
                  + (f" ({', '.join(nar.used_fallback)})" if nar.used_fallback else ""))
    return out


def _guarded(nar, label: str, prompt: str, fallback: str,
             forbidden: list[str], al) -> tuple[str, bool]:
    """Раздел с проверкой обезличивания промпта перед отправкой."""
    leaks = N.check_masked(prompt, forbidden)
    if leaks:
        progress.warn(f"обезличивание: в промпт «{label}» попали настоящие "
                      f"названия ({', '.join(leaks[:5])}) — раздел уходит в фолбэк")
        return fallback, True
    return nar.section(label, prompt, fallback, al)
