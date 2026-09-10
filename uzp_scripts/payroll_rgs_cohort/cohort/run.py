"""Оркестратор: разведка → рабочий набор → расчёты → тексты → HTML и документ.

Все параметры приходят из тетрадки и мержатся поверх DEFAULTS. Ни одного
значения, которое надо править в .py, чтобы поменять месяц, порог или модель.

Отчётный месяц — ЯВНЫЙ параметр. Автовыбор «последний месяц витрины» тихо
сдвигает разбор: запуск третьего числа возьмёт уже начавшийся месяц вместо
предыдущего, и заметить это по готовому файлу невозможно.

Лестница считается ДВАЖДЫ: отчётный месяц к тому же месяцу год назад и он же к
предыдущему месяцу. Второе сравнение обязательно потому, что отчётный месяц
может быть сезонной ямой: тогда годовое падение целиком повторяет обычный
месячный провал, и по одному сравнению эти два случая неразличимы.

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
    # Второе сравнение: отчётный месяц к месяцу на столько назад. 1 — к
    # предыдущему. Он же используется для проверки сезонности: она сравнивает
    # пару (предыдущий, отчётный) в текущем году с той же парой год назад.
    # Ноль отключает и второе сравнение, и проверку сезонности.
    prev_offset_months=1,
    # Глубина помесячного ряда, мес. Ряд обязан покрывать оба опорных месяца, а
    # для отделения сезонности — хотя бы 15 месяцев.
    history_months=25,
    # Порог: 'inn' — сумма за месяц по организации (так задано постановкой),
    # 'inn_gosb' — сумма по подразделению. Разведка печатает оба числа.
    amt_scope=LQ.AMT_SCOPE_INN,
    # Стаж ушедших — самый дорогой запрос отчёта (скан всего ряда). Выключается
    # первым, если прогон на проме окажется долгим.
    with_tenure=True,
    # Сколько строк показывать в разрезах и в списке организаций.
    top_n=12,
    top_orgs=15,
    # Минимальное число людей, переехавших из одной организации в другую, чтобы
    # пара «откуда→куда» вообще попала в разбор миграции. Меньше — это шум.
    migration_min_movers=5,
    # Минимальная ДОЛЯ потерь организации, ушедшая в одного приёмника, чтобы
    # назвать это переоформлением. Числом людей это не измерить: двадцать из
    # двадцати и двадцать из двух тысяч — разные события.
    migration_min_share=0.30,
    # Потолок вызовов LLM на весь разбор: разделов пять.
    llm_max_calls=6,
)


def _month_end(value) -> pd.Timestamp:
    return pd.Timestamp(value).to_period("M").to_timestamp("M")


def _shift(month: pd.Timestamp, back: int) -> pd.Timestamp:
    return _month_end(month - pd.DateOffset(months=int(back)))


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
    base = _shift(cur, opts["base_offset_months"])
    prev = _shift(cur, opts["prev_offset_months"]) if int(opts["prev_offset_months"]) else None
    first = _shift(cur, int(opts["history_months"]) - 1)
    if first > base:
        # Ряд обязан покрывать базовый месяц: иначе дожитие когорты начиналось бы
        # не с той точки, а «когда» отвечалось бы по куску периода.
        progress.warn(f"история {opts['history_months']} мес. не покрывает базовый "
                      f"месяц {base:%m.%Y} — ряд расширен до него")
        first = base

    # Опорные месяцы: отчётный, предыдущий, тот же месяц год назад и предыдущий
    # год назад. Последний нужен ТОЛЬКО для проверки сезонности: она требует
    # сравнить поведение человека в двух парах месяцев, и по трём месяцам этого
    # не сделать. Каждый месяц — отдельный скан партиции, поэтому лишних нет.
    base_prev = _shift(base, opts["prev_offset_months"]) if prev is not None else None
    anchors = [base, cur] + [m for m in (prev, base_prev) if m is not None]
    months = sorted(set(anchors))
    d = {m: m.date().isoformat() for m in months}
    d_cur, d_base, d_from = d[cur], d[base], first.date().isoformat()

    progress.step(f"Численность {LQ.SEG_BIG}: {base:%m.%Y} → {cur:%m.%Y}"
                  + (f" (и {prev:%m.%Y} → {cur:%m.%Y})" if prev is not None else "")
                  + f" · контур {config.CONTOUR} · схема {config.SCHEMA}")
    engine = db.get_engine(config.db_url(conn))
    db.ping(engine)

    month_strs = [d[m] for m in months]
    with db.session(engine) as conn_live:
        pr = probe.run(engine, conn_live, month_strs, d_base, d_cur, d_from, out_dir)

        ws = fetch.Workspace(
            engine, conn_live, pr["code_column"],
            {"seg": LQ.SEG_BIG, "codes": list(LQ.CODES), "amt_min": LQ.AMT_MIN,
             "months": month_strs,
             "seen_months": month_strs,
             "d_base": d_base, "d_cur": d_cur,
             "d_prev": d[prev] if prev is not None else d_cur,
             "d_base_prev": d[base_prev] if base_prev is not None else d_base},
            use_temp=bool(pr["temp_tables"]),
            amt_scope=str(opts["amt_scope"]))
        try:
            ws.build()

            progress.step("Выгрузка разбора")
            mt = fetch.month_totals(ws)

            # --- основное сравнение: год к году ---
            lost = fetch.lost(ws, d_base)
            gained = fetch.gained(ws, d_base)
            lost_e = fetch.lost_epk(ws, d_base)
            gained_e = fetch.gained_epk(ws, d_base)
            lost_inn = fetch.lost_by_inn(ws, d_base)
            gained_inn = fetch.gained_by_inn(ws, d_base)

            # --- второе сравнение: к предыдущему месяцу ---
            prev_pack = None
            if prev is not None:
                progress.step(f"Сравнение с предыдущим месяцем {prev:%m.%Y}")
                prev_pack = {
                    "lost": fetch.lost(ws, d[prev], "_prev"),
                    "gained": fetch.gained(ws, d[prev], "_prev"),
                    "lost_epk": fetch.lost_epk(ws, d[prev], "_prev"),
                    "gained_epk": fetch.gained_epk(ws, d[prev], "_prev"),
                }

            attrs = fetch.seg_attrs(ws)
            monthly = fetch.monthly(ws, d_from, d_cur)
            monthly_all = fetch.monthly_all(ws, d_from, d_cur)
            surv = fetch.survival(ws, d_base)
            thr_raw = fetch.threshold_sens(ws)
            seas_raw = fetch.seasonal(ws) if prev is not None else pd.DataFrame()
            codes_m_raw = fetch.code_months(ws)
            seg_raw = fetch.left_segment(ws, d_base)
            codes_raw = fetch.left_codes(ws, d_base)
            mig_raw = fetch.inn_migration(ws, d_base, int(opts["migration_min_movers"]))
            tb = fetch.tb_dim(ws)
            gosb = fetch.gosb_dim(ws)
            gosb_key = probe.pick_gosb_key(fetch.gosb_match(ws), pr)
            ten_raw = (fetch.tenure(ws, d_base, d_from)
                       if opts["with_tenure"] else pd.DataFrame())
            shown = dict(ws.shown)
        finally:
            # Уборка обязательна и при падении: соединение возвращается в пул, и
            # оставленная временная таблица досталась бы следующему прогону — с
            # ЧУЖИМИ датами внутри и правдоподобными числами.
            ws.drop()

    # --- расчёты ---
    progress.step("Расчёты")
    t = A.totals(mt, lost, gained, lost_e, gained_e, base, cur)
    checks = A.check_additive(t, lost, gained, lost_e, gained_e)
    causes = A.ladder(lost, A.CAUSES, "n_triples")
    gains = A.ladder(gained, A.GAINS, "n_triples")
    causes_e = A.ladder(lost_e, A.EPK_CAUSES, "n_epk")
    gains_e = A.ladder(gained_e, A.EPK_GAINS, "n_epk")
    both = A.side_by_side(causes, lost_e)

    t_prev, causes_prev = None, pd.DataFrame()
    if prev_pack is not None:
        t_prev = A.totals(mt, prev_pack["lost"], prev_pack["gained"],
                          prev_pack["lost_epk"], prev_pack["gained_epk"], prev, cur)
        checks += A.check_additive(t_prev, prev_pack["lost"], prev_pack["gained"],
                                   prev_pack["lost_epk"], prev_pack["gained_epk"])
        causes_prev = A.ladder(prev_pack["lost"], A.CAUSES, "n_triples")

    tr = A.trend(monthly)
    st, measured = A.steps(tr)
    cmp_months = A.month_compare(tr, cur, int(opts["prev_offset_months"]) or 1)
    surv_c = A.survival_curve(surv, t["triples_base"])
    load = A.load_health(monthly_all)

    thr = A.threshold(thr_raw, base, cur)
    seas = A.seasonality(seas_raw, cur, prev) if prev is not None else {}
    to_seg = A.left_segment(seg_raw, LQ.SEG_BIG)
    to_codes = A.left_codes(codes_raw)
    codes_m = A.code_months(codes_m_raw, base, prev or base, cur)
    marked, meta = A.enrich(lost_inn, attrs, tb, gosb, gosb_key)
    mig = A.migration(mig_raw, lost_inn, attrs, float(opts["migration_min_share"]))
    ten = A.tenure_table(ten_raw)

    dims = ["holding_name", "agency", "level", "industry_name", "tb_short_name",
            "region_name"]
    cuts = {dim: A.by_dim(marked, dim, int(opts["top_n"])) for dim in dims}
    cuts = {k: v for k, v in cuts.items() if v is not None and not v.empty}
    orgs = A.top_orgs(marked, gained_inn, int(opts["top_orgs"]))

    progress.done(
        f"получателей {t['d_triples']:+,.0f}, людей {t['d_epk']:+,.0f}; "
        f"реально потеряно {t['real_lost']:,.0f}, реально пришло "
        f"{t['real_gained']:,.0f} (чисто {t['net_real']:+,.0f}); "
        f"переходов внутри сегмента {t['inside_lost']:,.0f}")

    # --- тексты ---
    progress.step("Текстовые выводы")
    texts = _narrate(t, causes, gains, causes_e, tr, st, measured, cmp_months,
                     surv_c, seas, codes_m, to_seg, to_codes, mig, cuts, opts)

    # --- сборка ---
    progress.step("Сборка HTML")
    warnings = list(pr["warnings"])
    blocks = [
        V.head_kpi(t),
        V.metric_block(t, thr, shown),
        V.net_block(t, causes, gains, causes_e, gains_e, shown, *texts["net"]),
        V.both_block(both, t, shown),
        V.where_gone_block(to_seg, to_codes, mig, ten, shown, *texts["gone"]),
        V.when_block(tr, st, measured, cmp_months, surv_c, load, seas, codes_m,
                     t_prev, causes_prev, shown, *texts["when"]),
        V.where_block(cuts, orgs, meta, shown, *texts["where"]),
        V.limits_block(warnings, checks, pr, meta),
    ]
    footer = (f"Сформировано {datetime.now():%d.%m.%Y %H:%M} · контур "
              f"{config.CONTOUR} · модель {model} · источники: ведомости, "
              f"справочник ЕПК, справочник ГОСБ")
    html = V.page(
        f"Численность бюджетной сферы · {base:%m.%Y} → {cur:%m.%Y}",
        "Разбор до физического лица: сколько, выросли ли, когда, где и почему",
        blocks, footer)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"payroll_rgs_cohort_{cur:%Y%m}_{ts}.html"
    path.write_text(html, encoding="utf-8")

    # --- обезличенный документ ---
    progress.step("Обезличенный документ")
    doc, leaks = RT.build(
        t=t, t_prev=t_prev, causes=causes, gains=gains, causes_epk=causes_e,
        gains_epk=gains_e, both=both, tr=tr, st=st, measured=measured,
        cmp_months=cmp_months, surv=surv_c, thr=thr, seas=seas,
        codes_m=codes_m, to_seg=to_seg,
        to_codes=to_codes, mig=mig, cuts=cuts, orgs=orgs, tenure=ten,
        checks=checks, warnings=warnings, probe=pr, meta=meta, shown=shown,
        texts={k: v[0] for k, v in texts.items()})
    doc_path = out_dir / f"payroll_rgs_cohort_{cur:%Y%m}_{ts}.md"
    doc_ok = RT.write(doc_path, doc, leaks)

    (out_dir / "run_config.json").write_text(
        json.dumps({**opts, "report_month": f"{cur:%Y-%m}",
                    "base_month": f"{base:%Y-%m}",
                    "prev_month": f"{prev:%Y-%m}" if prev is not None else "",
                    "months": month_strs, "d_from": d_from,
                    "contour": config.CONTOUR, "model": model,
                    "code_column": pr["code_column"],
                    "gosb_key": gosb_key or "",
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
            "doc_leaks": leaks, "probe": pr, "totals": t, "totals_prev": t_prev,
            "causes": causes, "gains": gains, "causes_epk": causes_e,
            "gains_epk": gains_e, "both": both, "trend": tr, "steps": st,
            "measured": measured, "month_compare": cmp_months,
            "survival": surv_c, "load": load, "threshold": thr,
            "seasonality": seas, "code_months": codes_m,
            "left_segment": to_seg, "left_codes": to_codes,
            "migration": mig,
            "cuts": cuts, "orgs": orgs, "tenure": ten, "marked": marked,
            "checks": checks, "warnings": warnings, "meta": meta, "shown": shown}


def _narrate(t, causes, gains, causes_e, tr, st, measured, cmp_months, surv,
             seas, codes_m, to_seg, to_codes, mig, cuts, opts) -> dict:
    """Тексты разделов. Названия организаций и территорий уходят в модель токенами.

    Псевдонимы заводятся на КАЖДЫЙ раздел заново: словарь живёт ровно один вызов,
    и токен из одного промпта не должен «протекать» в другой.
    """
    nar = N.Narrator(max_calls=int(opts["llm_max_calls"]))
    bm, cm = f"{t['base_month']:%m.%Y}", f"{t['report_month']:%m.%Y}"
    out: dict = {}

    # Обзор, «выросли ли» и «когда» названий не содержат вовсе — маскировать нечего.
    out["overview"] = nar.section(
        "обзор", P.overview(bm, cm, t, causes, causes_e),
        N.fb_overview(bm, cm, t, causes, causes_e))
    out["net"] = nar.section(
        "рост", P.net(bm, cm, t, causes, gains), N.fb_net(bm, cm, t, causes))
    out["when"] = nar.section(
        "когда", P.when(cm, tr, st, measured, cmp_months, surv, seas, codes_m),
        N.fb_when(tr, st, measured, cmp_months, surv, seas, codes_m))

    # «Почему» и «где» несут названия — они уходят токенами, и промпт проверяется
    # на утечку. Нашлась хоть одна — раздел уходит в фолбэк целиком: отказ шлюза
    # по blacklist стоит дороже, чем текст, посчитанный правилами.
    al = N.Aliases()
    mig_m = N.mask_frame(N.mask_frame(mig, "name_from", "Орг", al),
                         "name_to", "Орг", al)
    forbidden = ([str(v) for v in mig.get("name_from", [])]
                 + [str(v) for v in mig.get("name_to", [])]) if not mig.empty else []
    # Названия сегментов и видов зачисления — словарные значения, не имена
    # организаций: маскировать их незачем, а без них раздел теряет весь смысл.
    out["gone"] = _guarded(nar, "куда делись", P.gone(to_seg, to_codes, mig_m),
                           N.fb_gone(to_seg, to_codes, mig), forbidden, al)

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
