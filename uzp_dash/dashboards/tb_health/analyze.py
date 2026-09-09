"""Бизнес-логика дэша tb_health.

Дэш строится на ТЕКУЩИЙ (незакрытый) месяц по ПРОГНОЗУ: окончательная ЗП-ведомость
есть только за закрытый месяц, а управляющему нужно понимать, выполняется ли план
СЕЙЧАС, пока на него ещё можно повлиять.

    прогноз = факт закрытого месяца − ожидаемый отток + приход из пайплайна

Математика прогноза вынесена в forecast.py; здесь она сшивается с планом текущего
месяца, разрывом по (ГОСБ, сегмент) и списком организаций к работе.

Грейн работы с клиентом — (ГОСБ, ИНН): одна организация может обслуживаться в
нескольких ГОСБ, и в каждом своя история отработки. Все агрегаты воронки считаются
по ВСЕМ активностям за 3 месяца (не по последней задаче).

Рекомендации разрешаются в порядке: чек-лист (причина оттока) -> ключевые слова ->
LLM (только там, где есть содержательный текст и правила не сработали) -> правило.

В БД этот модуль НЕ ходит: все данные читает `bank.load` одним проходом по банку,
а уровни отчёта (СБ и каждый ТБ) собираются здесь из готовых кадров.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ... import progress
from . import forecast, prompts, queries as Q, segments, text_rules
from .bank import Bank, month_index, next_month

RUB_TO_MLN = 1e6
LLM_BATCH_DEFAULT = 12      # пар (ГОСБ, ИНН) в одном запросе к LLM (один глубокий проход)
LLM_MIN_IMPACT_DEFAULT = 1  # ниже этого эффекта (чел) в LLM не отправляем — только правила
LLM_MAX_CALLS_DEFAULT = 80  # жёсткий потолок вызовов на весь ТБ; хвост уходит на правила
PLAN_TARGETS = (1.0, 1.2, 1.5)   # цели в дэше: выполнить план / +20% / +50%
# Порог эффекта для СПИСКА В ФАЙЛЕ: на проме кандидатов под 70 тыс., и хвост
# организаций по одному-два человека раздувал HTML до 20 МБ, не давая ничего для
# работы. Отсекаются ВСЕ строки мельче порога, включая нужные под план.
#
# Порог — ТОЛЬКО про видимость: на отбор он не влияет и влиять не должен. Разрыв
# сегмента считается по прогнозу, а не по составу кандидатов, поэтому убери мелкие
# организации из отбора — сегменту искусственно «не хватит» своих, и включится добор
# из другого сегмента (`_mark_needed`, фаза 2) там, где своих хватало.
EXPLORER_MIN_FL_DEFAULT = 5
# Сегмент западает, если план не выполнен (exec < 1) — тот же признак, что даёт
# красную ячейку в тепловой карте (render.components.heat_bg). Порог в одного
# получателя отсекает только шум округления.
MIN_SEG_GAP = 1.0


def _failing_seg(nedobor) -> bool:
    return float(nedobor or 0) >= MIN_SEG_GAP


@dataclass
class Analysis:
    tb_short: str
    tb_id: int
    tb_full: str
    ref_date: str            # прогнозный (текущий) месяц — им подписан весь дэш
    verdict: dict            # план текущего месяца против ПРОГНОЗА
    gap_rcp: float
    gap_fot: float
    gap_fot_mln: float
    matrix: pd.DataFrame
    gosb_gap: pd.DataFrame
    top_cells: pd.DataFrame
    activity: dict
    to_work: pd.DataFrame
    no_point: pd.DataFrame
    sim: dict
    gosb_plan: dict = field(default_factory=dict)   # new_gosb_id -> потребность под план
    insights: dict = field(default_factory=dict)   # (gosb_id, inn) -> reason/action/…
    themes: str = "—"
    llm_stats: dict = field(default_factory=dict)
    dates: dict = field(default_factory=dict)      # ref_cur / ref_closed / out_months / …
    pf: dict = field(default_factory=dict)         # блок «Управление портфелем»
    closed: dict = field(default_factory=dict)     # вердикт ЗАКРЫТОГО месяца + ранг
    yoy: dict = field(default_factory=dict)        # прирост закрытого месяца год к году
    fc_stats: dict = field(default_factory=dict)   # диагностика прогноза
    gosb_detail: dict = field(default_factory=dict)  # new_gosb_id -> разбор прогноза
    explorer_min_fl: float = EXPLORER_MIN_FL_DEFAULT  # порог эффекта для списка в файле
    # Уровень отчёта. "tb" — область один ТБ, единица разбора ГОСБ; "sb" — область весь
    # банк, единица ТБ. Всё остальное устройство отчёта от уровня не зависит.
    level: str = "tb"
    unit_label: str = "ГОСБ"
    unit_src: str = "new_gosb_id"     # колонка грейна организаций = единица разбора
    gosb_cards: list = field(default_factory=list)   # карточки единиц уровня
    # Рабочие кадры уровня. Живут в объекте, потому что уровень собирается в два
    # шага: `prepare` (без БД и LLM) и `finish` (разбор текста) — между ними одним
    # запросом читаются тексты воронки сразу по всем уровням.
    orgs: pd.DataFrame | None = None      # закреплённые пары — с ними работают
    orgs_fc: pd.DataFrame | None = None   # прогноз на грейне (ГОСБ, ИНН)
    detail: pd.DataFrame | None = None    # справка по организациям единицы уровня
    pool: pd.DataFrame | None = None      # пул аудита отработки
    seg_gaps: dict = field(default_factory=dict)     # (единица, сегмент) -> недобор


def prepare(b: Bank, tb_id: int, tb_short: str, tb_full: str) -> Analysis:
    """Уровень ТБ — всё, что считается БЕЗ обращения к БД и без LLM.

    Данные уже прочитаны одним проходом по банку (`bank.load`), здесь остаются срезы
    и арифметика. Разбор текста вынесен в `finish` намеренно: пул аудита каждого ТБ
    известен только после классификации, а тексты воронки читаются ОДНИМ запросом на
    весь отчёт — сначала считаются все уровни, потом один запрос, потом LLM.
    """
    d = b.dates
    closed_verdict, _ = _verdict(b.verdict, "tb", d["ref_closed"], tb_id)
    plan_cur, ref_date = _verdict(b.verdict, "tb", d["ref_cur"], tb_id)
    prev_yoy, _ = _verdict(b.verdict, "tb", d["ref_yoy"], tb_id)
    progress.done(f"{d['ref_closed']} закрыт: получатели {closed_verdict['rcp']['fact']:.0f} "
                  f"из {closed_verdict['rcp']['plan']:.0f} "
                  f"({(closed_verdict['rcp']['exec'] or 0) * 100:.0f}%) · "
                  f"план на {d['ref_cur']}: {plan_cur['rcp']['plan']:.0f}")
    yoy = _yoy(prev_yoy, closed_verdict, d)

    # Рабочий набор — только закреплённые в эталонной базе: работать можно с ними.
    # Полный срез (`detail`) нужен детализации прогноза: в ожидаемый отток входят и
    # организации вне базы, и без них блок не сойдётся с итогом единицы.
    detail = b.orgs[b.orgs["tb_id"] == tb_id]
    orgs = detail[detail["in_ref"]].reset_index(drop=True)
    orgs_fc = b.orgs_fc[b.orgs_fc["tb_id"] == tb_id]
    fc_stats = dict(b.fc_stats)
    fc_stats.update({"conv_tb": b.conv["by_tb"].get(tb_id, b.conv["sb"]),
                     "conv": b.conv["diag_tb"].get(tb_id, b.conv["diag_sb"]),
                     "conv_by_gosb": b.conv["by_gosb"]})

    base_seg = _units(b.unit_seg, d["ref_closed"], b.apparat, tb_id, seg=True)
    plan_seg = _units(b.unit_seg, d["ref_cur"], b.apparat, tb_id, seg=True)
    base_tot = _units(b.unit_tot, d["ref_closed"], b.apparat, tb_id)
    plan_tot = _units(b.unit_tot, d["ref_cur"], b.apparat, tb_id)
    a = _assemble(b, tb_short, tb_id, tb_full, ref_date, closed_verdict, plan_cur, yoy,
                  orgs, orgs_fc, detail, base_seg, plan_seg, base_tot, plan_tot,
                  fc_stats, _activity(b.act_tot[b.act_tot.tb_id == tb_id],
                                      b.act_brk[b.act_brk.tb_id == tb_id]))

    # --- Классификация по агрегатам (все активности) ---
    progress.step(f"{tb_short}: классификация (ГОСБ,ИНН) — работать / нет смысла")
    cand = _candidates(orgs, d["days_left"], b.apparat)
    a.to_work, a.no_point = _classify(cand)
    n_gosb_seg = len({nid for nid, _ in a.seg_gaps})
    progress.done(f"Западающих (ГОСБ, сегмент): {len(a.seg_gaps)} в {n_gosb_seg} ГОСБ")
    a.pool = _audit_pool(a.to_work, a.seg_gaps,
                         float(b.params.get("llm_min_impact", LLM_MIN_IMPACT_DEFAULT)))
    return a


def build_sb(b: Bank, per_tb: list) -> Analysis:
    """Уровень СБ: тот же отчёт, где единица разбора — ТБ, а область — весь банк.

    План, факт и портфель-база берутся ОТДЕЛЬНОЙ строкой витрины (`level_name='sb'`),
    а не складываются из ТБ: уровни витрины считаются независимо, и сумма ТБ ей
    не равна — то же расхождение, что между ГОСБ и ТБ.

    Прогнозные слагаемые (отток, приток, пайплайн) — наоборот, свод грейна
    (ГОСБ, ИНН) целиком: он посчитан один раз на весь банк, и дедуплицировать его не
    нужно — каждый ГОСБ принадлежит ровно одному ТБ (см. `bank._prepare_orgs`).
    """
    d = b.dates
    progress.step(f"Уровень СБ: вердикт банка за закрытый месяц {d['ref_closed']}")
    closed_verdict, _ = _verdict(b.verdict, "sb", d["ref_closed"])
    plan_cur, ref_date = _verdict(b.verdict, "sb", d["ref_cur"])
    prev_yoy, _ = _verdict(b.verdict, "sb", d["ref_yoy"])
    progress.done(f"{d['ref_closed']} закрыт: получатели {closed_verdict['rcp']['fact']:.0f} "
                  f"из {closed_verdict['rcp']['plan']:.0f} "
                  f"({(closed_verdict['rcp']['exec'] or 0) * 100:.0f}%) · "
                  f"план на {d['ref_cur']}: {plan_cur['rcp']['plan']:.0f}")
    yoy = _yoy(prev_yoy, closed_verdict, d, area="Банк")

    # Единицы уровня — ТБ, и берутся они строками level_name='tb' КАК ЕСТЬ, вместе
    # с аппаратом: см. `_units_sb`.
    base_seg = _units_sb(b.unit_seg, d["ref_closed"], seg=True)
    plan_seg = _units_sb(b.unit_seg, d["ref_cur"], seg=True)
    base_tot = _units_sb(b.unit_tot, d["ref_closed"])
    plan_tot = _units_sb(b.unit_tot, d["ref_cur"])

    fc_stats = dict(b.fc_stats)
    fc_stats.update({"conv_tb": b.conv["sb"], "conv": b.conv["diag_sb"],
                     "conv_by_gosb": b.conv["by_tb"]})
    a = _assemble(b, "СБ", 0, "Сбербанк — все территориальные банки", ref_date,
                  closed_verdict, plan_cur, yoy,
                  pd.DataFrame(), b.orgs_fc, b.orgs_tb,
                  base_seg, plan_seg, base_tot, plan_tot, fc_stats,
                  _activity(b.act_tot, b.act_brk),
                  unit_src="tb_id", unit_label="ТБ", level="sb")

    # Список к работе и причины НЕ пересчитываются: отбор организаций — решение
    # уровня ТБ, и карточка банка показывает уже принятое решение, а не своё.
    a.to_work = _cat([x.to_work for x in per_tb])
    a.no_point = _cat([x.no_point for x in per_tb])
    a.gosb_plan = {x.tb_id: _tb_plan_row(x) for x in per_tb}
    a.sim = _sum_sim(per_tb, a.gap_rcp, a.verdict["rcp"]["fact"], a.verdict["rcp"]["plan"])
    # причины с уровня ТБ ключуются по (ГОСБ, ИНН), а строки этого уровня — по
    # (ТБ, ИНН): перекладываем, иначе блок годового тренда банка остался бы без
    # единой зафиксированной причины
    a.insights = _insights_by_tb(per_tb, b.tb_of)

    progress.step("Уровень СБ: карточки ТБ + детализация прогноза")
    a.gosb_cards = _unit_cards(a.gosb_gap, a.matrix, pd.DataFrame(), pd.DataFrame(),
                               a.gosb_plan, unit_src="tb_id")
    act = {int(r.tb_id): {"act_n": int(r.n or 0),
                          "success": (float(r.n_success or 0) / int(r.n)) if int(r.n or 0) else 0.0,
                          "worked_orgs": int(r.orgs or 0)}
           for r in b.act_tot.itertuples()}
    for c in a.gosb_cards:
        c["act"] = act.get(c["gosb_id"], {"act_n": 0, "success": 0.0, "worked_orgs": 0})
    a.gosb_detail = _unit_detail(a.orgs_fc, a.detail, a.insights, a.to_work, a.no_point,
                                 a.gosb_gap, fc_stats["conv_tb"], fc_stats["conv"],
                                 fc_stats["conv_by_gosb"], d,
                                 unit_src="tb_id", unit_label="ТБ",
                                 funnel_months=b.fmonths)
    return a


def audit_inns(preps: list) -> list:
    """ИНН всех аудиторских пулов сразу — под ОДИН запрос текстов воронки.

    Раньше тексты читались по одному запросу на ТБ: двенадцать проходов по самой
    большой таблице ради выборок, которые отлично объединяются в одну.
    """
    inns: set = set()
    for a in preps:
        if a.pool is not None and not a.pool.empty:
            inns.update(int(x) for x in a.pool["inn"])
    return sorted(inns)


def audit_enabled(ctx) -> bool:
    """Нужен ли аудит отработки моделью. Бюджет 0 — выключен целиком.

    Спрашивается ДО чтения текстов активностей: этот запрос нужен только аудиту и на
    проме стоит десятки секунд, а при выключенном аудите не нужен вовсе.
    """
    return int(ctx.params.get("llm_max_calls", LLM_MAX_CALLS_DEFAULT)) > 0


def finish(ctx, b: Bank, a: Analysis, text_df) -> Analysis:
    """Разбор текста моделью, отбор под план и детализация — по одному уровню ТБ."""
    d = b.dates
    batch = int(ctx.params.get("llm_batch", LLM_BATCH_DEFAULT))
    max_calls = int(ctx.params.get("llm_max_calls", LLM_MAX_CALLS_DEFAULT))
    # Опорный месяц задач = конец окна воронки: относительно него решаем, назван ли
    # в тексте срок В БУДУЩЕМ (тогда спрашивать результат ещё рано).
    ref_ym = (pd.Timestamp(d["ref_funnel"]).year, pd.Timestamp(d["ref_funnel"]).month)
    insights, to_work, no_point, themes, llm_stats = _resolve(
        ctx, a.pool, a.to_work, a.no_point, text_df, batch, max_calls, ref_ym,
        min_impact=float(ctx.params.get("llm_min_impact", LLM_MIN_IMPACT_DEFAULT)))

    progress.step(f"{a.tb_short}: отбор организаций под план по (ГОСБ, сегмент)")
    to_work, gosb_plan = _select(to_work, a.seg_gaps)
    sim = _plan_summary(to_work, gosb_plan, a.gap_rcp,
                        a.verdict["rcp"]["fact"], a.verdict["rcp"]["plan"])
    progress.done(f"Под план нужно {sim['k']} организаций (+{sim['closable']:.0f} чел); "
                  f"потенциал западающих сегментов покрывает разрыв на "
                  f"{sim['coverage']*100:.0f}% · добор из других сегментов: {sim['filler_n']}")

    gosb_ids = set(a.gosb_gap["unit_id"].astype("int64")) if not a.gosb_gap.empty else set()
    fagg = b.fagg[b.fagg["new_gosb_id"].isin(gosb_ids)] if not b.fagg.empty else b.fagg
    fmonths = (b.fmonths[b.fmonths["tb_id"] == a.tb_id]
               if "tb_id" in b.fmonths else b.fmonths)
    a.insights, a.to_work, a.no_point = insights, to_work, no_point
    a.themes, a.llm_stats, a.sim, a.gosb_plan = themes, llm_stats, sim, gosb_plan
    a.gosb_cards = _unit_cards(a.gosb_gap, a.matrix, to_work, fagg, gosb_plan)
    a.gosb_detail = _unit_detail(a.orgs_fc, a.detail, insights, to_work, no_point,
                                 a.gosb_gap, a.fc_stats.get("conv_tb", 1.0),
                                 a.fc_stats.get("conv", {}),
                                 a.fc_stats.get("conv_by_gosb", {}), d,
                                 funnel_months=fmonths)
    n_named = sum(sum(len(g["rows"]) for g in v["out_groups"]) + len(v["top_pipe"])
                  for v in a.gosb_detail.values())
    n_grp = sum(len(v["out_groups"]) for v in a.gosb_detail.values())
    progress.done(f"Карточек ГОСБ: {len(a.gosb_cards)} (все, включая выполняющие план) · "
                  f"детализация по {len(a.gosb_detail)} ГОСБ, названо {n_named} организаций "
                  f"(до {DETAIL_COVER*100:.0f}% блока, максимум {DETAIL_MAX_ROWS} строк) · "
                  f"отток разложен на {n_grp} групп по причине — они покрывают его целиком")
    return a


# --------------------------------------------------------------------------- #
def _assemble(b: Bank, tb_short: str, tb_id: int, tb_full: str, ref_date: str,
              closed_verdict: dict, plan_cur: dict, yoy: dict,
              orgs: pd.DataFrame, orgs_fc: pd.DataFrame, detail: pd.DataFrame,
              base_seg: pd.DataFrame, plan_seg: pd.DataFrame,
              base_tot: pd.DataFrame, plan_tot: pd.DataFrame,
              fc_stats: dict, activity: dict,
              unit_src: str = "new_gosb_id", unit_label: str = "ГОСБ",
              level: str = "tb") -> Analysis:
    """Общая часть любого уровня: матрица, разрыв по единицам и портфель.

    Уровни отличаются только тем, ЧТО является единицей разбора (ГОСБ или ТБ) и
    откуда взята строка вердикта; вся арифметика между ними одинакова, поэтому она
    живёт здесь, а не в двух почти одинаковых функциях.
    """
    d = b.dates
    progress.step(f"{tb_short}: матрица {unit_label} × сегмент по ПРОГНОЗУ + разрыв")
    matrix, mstats = forecast.build_matrix(base_seg, plan_seg, unit_src=unit_src)
    matrix = matrix.merge(
        base_seg[["unit_id", "seg_name", "unit_name"]].drop_duplicates(),
        on=["unit_id", "seg_name"], how="left")
    gosb_gap = forecast.build_totals(base_tot, plan_tot, unit_src=unit_src)
    fc_stats.update(mstats)
    if mstats.get("no_pred"):
        progress.warn(f"Ячеек с планом, но без прогноза витрины: {mstats['no_pred']} "
                      f"из {mstats.get('cells', 0)} — по ним недобор равен всему плану")

    # Прогноз и план области — строкой витрины своего уровня, как факт и план.
    # Собственного расчёта больше нет: prediction_amt считает витрина.
    pf = _portfolio(closed_verdict, plan_cur, orgs_fc, d)
    verdict = {
        "rcp": {"plan": pf["plan"], "fact": pf["forecast"], "exec": pf["exec"],
                "rank": closed_verdict["rcp"]["rank"], "n_tb": closed_verdict["rcp"]["n_tb"]},
        "fot": {"plan": pf["plan_fot"], "fact": pf["forecast_fot"], "exec": pf["exec_fot"],
                "rank": closed_verdict["fot"]["rank"], "n_tb": closed_verdict["fot"]["n_tb"]},
    }
    gap_rcp = max(0.0, verdict["rcp"]["plan"] - verdict["rcp"]["fact"])
    gap_fot = max(0.0, verdict["fot"]["plan"] - verdict["fot"]["fact"])
    progress.done(
        f"Прогноз витрины на {d['ref_cur']}: {pf['forecast']:.0f} из плана "
        f"{pf['plan']:.0f} ({(pf['exec'] or 0) * 100:.1f}%) · портфель "
        f"{pf['base']:.0f} · отток за {d['out_label']} не вернулся "
        f"{pf['out_kept']:.0f} · пайплайн {pf['pipe']:.0f} фл")

    matrix["is_failing"] = [_failing_seg(r.nedobor) for r in matrix.itertuples()]
    top_cells = (matrix[matrix.nedobor > 0]
                 .sort_values("nedobor", ascending=False)
                 .assign(share=lambda x: x.nedobor / max(gap_rcp, 1))
                 .head(8))
    seg_gaps = {(int(r.unit_id), r.seg_name): float(r.nedobor)
                for r in matrix.itertuples() if r.is_failing}

    a = Analysis(
        tb_short=tb_short, tb_id=tb_id, tb_full=tb_full, ref_date=ref_date,
        verdict=verdict, gap_rcp=gap_rcp, gap_fot=gap_fot, gap_fot_mln=gap_fot / RUB_TO_MLN,
        matrix=matrix, gosb_gap=gosb_gap, top_cells=top_cells, activity=activity,
        to_work=pd.DataFrame(), no_point=pd.DataFrame(), sim={},
        dates=d, pf=pf, closed=closed_verdict, yoy=yoy, fc_stats=fc_stats,
        explorer_min_fl=float(b.params.get("explorer_min_fl", EXPLORER_MIN_FL_DEFAULT)),
        level=level, unit_label=unit_label, unit_src=unit_src,
    )
    a.orgs, a.orgs_fc, a.detail, a.seg_gaps = orgs, orgs_fc, detail, seg_gaps
    return a


def _units(df: pd.DataFrame, end_dt, apparat: set, tb_id: int,
           seg: bool = False) -> pd.DataFrame:
    """Срез метрик единиц отчёта по ТБ: строки уровня ГОСБ, свой ТБ, нужный месяц.

    Аппараты не продают — в разборе по подразделениям им делать нечего, карточки для
    них не строятся. Вердикт ТБ при этом остаётся из строки level_name='tb', как есть:
    сумма карточек ему не равна, и это нормально — уровни витрины считаются
    независимо (раздел 11).
    """
    f = df[(df["level_name"] == "gosb") & (df["tb_id"] == tb_id)
           & (df["end_dt"] == end_dt)]
    f = _drop_apparat(f, apparat).copy()
    if seg and not f.empty:
        f["seg_name"] = f["seg_id"].map(segments.short)
    return f


def _units_sb(df: pd.DataFrame, end_dt, seg: bool = False) -> pd.DataFrame:
    """Срез метрик единиц отчёта по банку: строки уровня ТБ — КАК ЕСТЬ.

    Складывать ГОСБ ради ТБ нельзя, и аппарат из ТБ не вычитается. План сегмента
    нередко стоит именно на аппарате ТБ (у СЗБ в сегменте СКМ это ~94% плана), и
    свёртка ГОСБ-строк без аппарата теряла его целиком: план падал с 125 383 до
    7 859, а выполнение показывало 1455%. Строка ТБ в витрине уже есть — её и берём.

    Аппараты не показываются только уровнем ниже, когда единица разбора — ГОСБ.
    """
    f = df[(df["level_name"] == "tb") & (df["end_dt"] == end_dt)].copy()
    if seg and not f.empty:
        f["seg_name"] = f["seg_id"].map(segments.short)
    return f


def _insights_by_tb(per_tb: list, tb_of: dict) -> dict:
    """Причины уровня ТБ, переложенные на ключ (ТБ, ИНН).

    Одна организация может обслуживаться в нескольких ГОСБ, и причина у каждой пары
    своя; для уровня банка берём первую зафиксированную — блок годового тренда там
    отвечает на вопрос «известна ли причина вообще», а не «какая именно в каком ГОСБ».
    """
    out: dict = {}
    for a in per_tb:
        for (gid, inn), v in (a.insights or {}).items():
            key = (tb_of.get(int(gid), a.tb_id), int(inn))
            # пустая причина не должна вытеснять уже найденную содержательную
            if key not in out or (v.get("reason") and not out[key].get("reason")):
                out[key] = v
    return out


def _verdict(v: pd.DataFrame, level: str, end_dt, level_id: int | None = None
             ) -> tuple[dict, str]:
    """План и факт уровня за месяц — строкой витрины, а не суммой нижнего уровня.

    Уровни витрины считаются независимо: сумма ТБ не равна строке банка ровно так же,
    как сумма ГОСБ не равна строке ТБ (раздел 11 методологии). Ранг есть только у ТБ —
    банк сравнивать не с кем.
    """
    out: dict = {}
    ref = ""
    sub = v[(v["level_name"] == level) & (v["end_dt"] == end_dt)] if not v.empty else v
    if level_id is not None and not sub.empty:
        sub = sub[sub["level_id"] == level_id]
    for key, mid in (("rcp", Q.METRIC_RECIPIENTS), ("fot", Q.METRIC_FOT)):
        row = sub[sub["metric_id"] == mid] if not sub.empty else sub
        if row.empty:
            out[key] = {"plan": 0, "fact": 0, "exec": None, "rank": None, "n_tb": None,
                        "pred": 0.0, "pred_exec": None}
            continue
        # если под уровнем оказалось несколько level_id (у банка в профиле прома
        # встречаются level_value 0/1/99), берём строку с наибольшим фактом —
        # про саму неоднозначность уже сказано вслух в bank.load
        r = row.sort_values("fact_amt", ascending=False).iloc[0]
        ref = str(r.end_dt)
        ex = r.execution_percent
        plan = float(r.plan_amt) if pd.notna(r.plan_amt) else 0.0
        # ПРОГНОЗ витрины. Процент считаем сами от плана, а не берём
        # prediction_percent: витрина хранит его в своём масштабе (на проме
        # execution_percent бывает в процентах, а не долей), и смешивать нельзя.
        pred = float(r.pred_amt) if pd.notna(getattr(r, "pred_amt", None)) else 0.0
        out[key] = {"plan": plan, "fact": float(r.fact_amt),
                    "exec": (float(ex) if pd.notna(ex) else None),
                    "pred": pred,
                    "pred_exec": (pred / plan) if plan else None,
                    "rank": (int(r.rnk) if level == "tb" else None),
                    "n_tb": (int(r.n_tb) if level == "tb" else None)}
    return out, ref


def _yoy(prev: dict, closed: dict, d: dict, area: str = "Год к году") -> dict:
    """Прирост год к году по ЗАКРЫТОМУ месяцу: два факта, а не факт против прогноза.

    Если строки за месяц годом ранее в витрине нет (на проме витрина метрик может не
    уходить так глубоко), возвращаем пустой результат — в карточке будет «—». Показать
    вместо этого ноль нельзя: его не отличить от настоящего нулевого прироста.
    """
    out: dict = {}
    for key, label, scale in (("rcp", "получатели", 1.0),
                              ("fot", "ФОТ млн ₽", RUB_TO_MLN)):
        was = float(prev.get(key, {}).get("fact") or 0)
        now = float(closed.get(key, {}).get("fact") or 0)
        if was <= 0:
            out[key] = None
            continue
        out[key] = {"fact": was, "delta": now - was, "pct": now / was - 1}
        progress.done(f"{area} ({label}): {d['closed_label']} {now / scale:,.0f} против "
                      f"{was / scale:,.0f} год назад → {(now - was) / scale:+,.0f} "
                      f"({(now / was - 1) * 100:+.1f}%)".replace(",", " "))
    if all(x is None for x in out.values()):
        progress.done(f"{area} НЕ рассчитан: в витрине метрик нет месяца "
                      f"{d['ref_yoy']} — в карточках будет «—»")
    return out


def _activity(totals: pd.DataFrame, breakdown: pd.DataFrame) -> dict:
    """Активности области: сумма строк ТБ (у банка — всех, у ТБ — своей одной).

    `orgs` на уровне банка складывается по ТБ и потому слегка завышен: организация,
    обслуживаемая в двух ТБ, посчитана дважды. Точная величина потребовала бы
    отдельного прохода по воронке, а строка отвечает на вопрос «сколько организаций
    в работе», где эта разница несущественна.
    """
    if totals is None or totals.empty:
        return {"n": 0}
    n = int(pd.to_numeric(totals["n"], errors="coerce").fillna(0).sum())
    if not n:
        return {"n": 0}

    def s(col):
        return int(pd.to_numeric(totals[col], errors="coerce").fillna(0).sum())

    def _dim(name):
        if breakdown is None or breakdown.empty:
            return {}
        d = breakdown[breakdown.dim == name]
        return {str(k): int(g["n"].sum()) for k, g in d.groupby("k")}

    return {
        "n": n, "orgs": s("orgs"),
        "calls": s("calls"), "meetings": s("meetings"),
        "success_rate": s("n_success") / n, "overdue": s("overdue"),
        "plan_deal": s("plan_deal"), "fact_deal": s("fact_deal"),
        "unrealized": s("unrealized"),
        "by_role": _dim("role"), "by_type": _dim("type"), "by_status": _dim("status"),
    }


def _cat(frames: list) -> pd.DataFrame:
    """Склеить непустые кадры; пустой результат — пустой DataFrame, а не ошибка."""
    fs = [f for f in frames if f is not None and not f.empty]
    return pd.concat(fs, ignore_index=True) if fs else pd.DataFrame()


def _tb_plan_row(a: Analysis) -> dict:
    """Потребность ТБ под план — сумма его собственных ГОСБ.

    Это ровно те числа, что стоят в отчёте этого ТБ: карточка ТБ на уровне СБ не
    пересчитывает отбор организаций, а показывает уже принятое решение уровнем ниже.
    """
    segs: dict = {}
    for p in (a.gosb_plan or {}).values():
        for s in p.get("segs", []):
            cur = segs.setdefault(s["seg"], {"seg": s["seg"], "gap": 0.0, "n_need": 0,
                                             "fl_need": 0.0, "coverage": None,
                                             "lack": 0.0, "n_avail": 0})
            cur["gap"] += s["gap"]; cur["n_need"] += s["n_need"]
            cur["fl_need"] += s["fl_need"]; cur["lack"] += s.get("lack", 0.0) or 0.0
            cur["n_avail"] += s.get("n_avail", 0)
    for s in segs.values():
        s["coverage"] = (s["fl_need"] / s["gap"]) if s["gap"] > 0 else None
    keys = ("gap_seg", "n_need", "fl_need", "fot_need", "n_attract", "fl_attract",
            "n_return", "fl_return", "filler_n", "filler_fl", "n_total", "fl_total")
    out = {k: sum(float(p.get(k, 0) or 0) for p in (a.gosb_plan or {}).values())
           for k in keys}
    for k in ("n_need", "n_attract", "n_return", "filler_n", "n_total"):
        out[k] = int(out[k])
    out["segs"] = sorted(segs.values(), key=lambda s: -s["gap"])
    return out


def _sum_sim(per_tb: list, gap: float, fact: float, plan: float) -> dict:
    """Итог «что даст работа по всему банку» — сумма планов работы по ТБ.

    Разрыв берётся СБ-шный (из вердикта банка), а закрываемая часть — сумма по ТБ:
    это честно отвечает на вопрос «хватит ли того, что уже отобрано ниже».
    """
    def s(key, cast=float):
        return cast(sum(float(a.sim.get(key, 0) or 0) for a in per_tb))

    gap_seg = s("gap_seg")
    return {
        "gap": gap, "gap_seg": gap_seg, "fact": fact, "plan": plan,
        "k": s("k", int), "closable": s("closable"),
        "attract": s("attract"), "retention": s("retention"),
        "fot_mln": s("fot_mln"), "filler_n": s("filler_n", int),
        # покрытие считается от суммы разрывов сегментов ТБ, как и на уровне ТБ:
        # там оно тоже про сегментные разрывы, а не про общий разрыв области
        "coverage": (s("closable") / gap_seg) if gap_seg > 0 else 0.0,
        "total_potential": s("total_potential"),
    }


# --------------------------------------------------------------------------- #
def _portfolio(closed: dict, plan_cur: dict, orgs_fc: pd.DataFrame, d: dict) -> dict:
    """Числа блока «Управление портфелем».

    ЭТО НЕ ФОРМУЛА. Раньше здесь был водопад, где база минус отток плюс пайплайн
    обязаны были дать прогноз, и сходимость проверялась в ноль. Теперь прогноз
    приходит из витрины и с этими слагаемыми арифметически не связан: перед
    управляющим три независимых факта — что есть, что потеряли и что ждём.

    Складывать их обратно в прогноз нельзя: витрина считает его своей моделью, и
    подогнанное равенство было бы выдумкой.
    """
    z = {c: 0.0 for c in ("out_kept", "out_fot", "pipe_np", "pipe_np_raw", "pipe_fot")}
    if orgs_fc is not None and not orgs_fc.empty:
        for c in z:
            if c in orgs_fc:
                z[c] = float(pd.to_numeric(orgs_fc[c], errors="coerce").fillna(0).sum())
    return {
        "base": closed["rcp"]["fact"], "base_fot": closed["fot"]["fact"],
        "out_kept": z["out_kept"], "out_fot": z["out_fot"],
        "pipe": z["pipe_np"], "pipe_raw": z["pipe_np_raw"], "pipe_fot": z["pipe_fot"],
        "pipe_upside": max(0.0, z["pipe_np_raw"] - z["pipe_np"]),
        "plan": plan_cur["rcp"]["plan"], "plan_fot": plan_cur["fot"]["plan"],
        "forecast": plan_cur["rcp"]["pred"], "forecast_fot": plan_cur["fot"]["pred"],
        "exec": plan_cur["rcp"]["pred_exec"], "exec_fot": plan_cur["fot"]["pred_exec"],
        "out_label": d.get("out_label", ""),
    }


def _candidates(orgs: pd.DataFrame, days_left: int = 0,
                apparat: set | None = None) -> pd.DataFrame:
    """Кандидаты к работе и рычаг: Привлечь / Вернуть.

    Организации аппаратов в кандидаты не идут: аппарат не продающее подразделение,
    работать по нему некому и карточки у него нет.

    Рычага два. Раньше был третий — «Удержать», он опирался на ОЖИДАЕМЫЙ отток
    текущего месяца, который считала наша модель. Модели больше нет, а витрина
    прогнозирует получателей, а не отток по организациям, — предсказывать, кто
    уйдёт, стало нечем, и рычаг убран целиком.

    «Вернуть» теперь считается по ФАКТИЧЕСКОМУ оттоку за три закрытых месяца,
    который НЕ ВЕРНУЛСЯ (`out_kept`), а не по `fl_outflow_qty` витрины организаций:
    первое — это люди, которых действительно потеряли, второе включало и тех, кто
    успел вернуться сам.

    Защита от двойного счёта: приход из пайплайна УЖЕ учтён в прогнозе витрины,
    поэтому эффект привлечения уменьшается на него — иначе одну и ту же сделку
    посчитали бы дважды.
    """
    o = orgs.copy()
    if apparat and not o.empty:
        n0 = len(o)
        o = o[~o["new_gosb_id"].isin(apparat)]
        if n0 != len(o):
            progress.done(f"Из кандидатов исключены организации аппаратов: "
                          f"{n0 - len(o)} пар (ГОСБ, ИНН) — работать по ним некому")
    o["impact_attract"] = (o["emp_potential_qty"] - o["pipe_np"]).clip(lower=0)
    o["impact_return"] = forecast.num(o, "out_kept").clip(lower=0)
    cand = o[(o.impact_attract >= 1) | (o.impact_return >= 1)].copy()
    if cand.empty:
        cand["impact_fl"] = []
        cand["lever"] = []
        cand["impact_fot_mln"] = []
        return cand
    two = cand[["impact_attract", "impact_return"]]
    cand["impact_fl"] = two.max(axis=1)
    cand["lever"] = two.idxmax(axis=1).map(
        {"impact_attract": "Привлечь", "impact_return": "Вернуть"})
    fot = []
    for r in cand.itertuples():
        if r.lever == "Привлечь":
            # ФОТ привлечения пропорционально уменьшен на долю, уже стоящую в пайплайне
            k = (r.impact_attract / r.emp_potential_qty) if r.emp_potential_qty else 0.0
            fot.append(float(r.fot_potential_amt) * k)
        else:
            # ФОТ возврата — по ушедшим и не вернувшимся, по средней ЗП организации
            fot.append(float(r.impact_return) * float(r.avg_salary or 0))
    cand["impact_fot_mln"] = [x / RUB_TO_MLN for x in fot]
    return cand


# --------------------------------------------------------------------------- #
def _drop_apparat(df: pd.DataFrame, apparat: set) -> pd.DataFrame:
    """Убрать аппараты из выборки метрик по подразделениям."""
    if df is None or df.empty or not apparat or "unit_id" not in df:
        return df
    return df[~df["unit_id"].isin(apparat)].reset_index(drop=True)


def _classify(cand: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """ОДИН вывод «работать / не работать» по ВСЕМ активностям за 3 мес.

    Сделки оцениваются по дате создания сделки: свежая сделка означает, что
    организация уже в работе и зачисления просто не успели пройти — такие в список
    к отработке не берём. Недоработку считаем только по «старым» сделкам, и только
    если сделка по задачам вообще ожидалась (deal_expected).

    Отдельная ветка — задачи, которые ЕЩЁ В РАБОТЕ и не просрочены: ни одна не
    закрыта, спрашивать результат рано, это не недоработка.

    ПЕРВОЙ идёт проверка отработки ОТТОКА — но только у организаций, чей рычаг
    «Вернуть». Для них вопрос «отработали ли отток в месяц ухода» и есть главный, а
    ветки трёхмесячного агрегата ниже отвечают на другой вопрос — про сделки и
    активности вообще. Организация с потенциалом привлечения сюда не попадёт: у неё
    рычаг «Привлечь», и её судьбу решают обычные ветки.
    """
    work_rows, skip_rows = [], []
    for _, o in cand.iterrows():
        fresh_dt = o.get("fresh_deal_dt")
        when = f" с {pd.Timestamp(fresh_dt):%m.%Y}" if pd.notna(fresh_dt) else ""
        n_ip = int(o.get("n_in_progress", 0))
        if o.get("lever") == "Вернуть":
            # вывод делается ПО МЕСЯЦАМ УХОДА (T и T+1), а не по трёхмесячному
            # агрегату воронки: ветки ниже отвечают на другой вопрос
            months = ", ".join(list(o.get("out_months") or [])[:3]) or "месяц ухода"
            n_t = int(o.get("out_tasks", 0) or 0)
            n_o = int(o.get("out_tasks_outflow", 0) or 0)
            if bool(o.get("out_worked", False)):
                skip_rows.append((o, f"Люди ушли в {months}, с клиентом тогда "
                                     f"работали и вопрос закрыли — вернуть их уже "
                                     f"не получится"))
            elif n_t == 0:
                work_rows.append((o, f"Люди ушли в {months}, и ни одной встречи "
                                     f"или звонка тогда не было — сходить к клиенту"))
            elif n_o == 0:
                work_rows.append((o, f"Люди ушли в {months}. Встречи были ({n_t}), "
                                     f"но про уход не говорили — выяснить причину"))
            else:
                work_rows.append((o, f"Люди ушли в {months}. Про уход говорили "
                                     f"{n_o} раз, но никого не вернули — пробовать "
                                     f"ещё"))
        elif bool(o.get("has_fresh_deal", False)):
            skip_rows.append((o, f"Сделка в работе{when} — ждём первых зачислений"))
        elif not bool(o.get("worked", False)):
            work_rows.append((o, "За три месяца ни одной встречи и ни одного "
                                 "звонка — начать работать"))
        elif n_ip > 0 and int(o.get("n_closed", 0)) == 0 and int(o.get("n_overdue", 0)) == 0:
            skip_rows.append((o, f"В работе {n_ip} задач, срок ещё не вышел — "
                                 f"ждём результата"))
        elif (float(o.get("plan_np_due", 0)) >= 1
                and float(o.get("fact_np_due", 0)) < float(o.get("plan_np_due", 0))):
            # сравниваем с АГРЕГАТОМ планов закрытых месяцев: сотрудник мог завести
            # несколько сделок на один месяц, и пришедшие люди относятся к их сумме
            work_rows.append((o, f"По сделкам обещали "
                                 f"{float(o['plan_np_due']):.0f} человек за "
                                 f"{int(o.get('due_months', 0))} мес, пришло "
                                 f"{float(o['fact_np_due']):.0f} — вернуться к клиенту"))
        elif int(o.get("n_overdue", 0)) > 0:
            work_rows.append((o, f"Просрочено задач: {int(o['n_overdue'])} — "
                                 f"довести до конца"))
        elif bool(o.get("any_success", False)):
            skip_rows.append((o, "Работа с клиентом дала результат — люди придут"))
        else:
            work_rows.append((o, "Работали, но результата нет — нужен повторный "
                                 "контакт"))

    def _mk(rows):
        if not rows:
            return pd.DataFrame(columns=list(cand.columns) + ["reason"])
        recs = []
        for o, reason in rows:
            d = o.to_dict(); d["reason"] = reason; recs.append(d)
        return pd.DataFrame(recs).sort_values("impact_fl", ascending=False)
    return _mk(work_rows), _mk(skip_rows)


# Правила, заявляющие УСПЕХ: при факте по старой сделке 0 их вердикт подозрителен —
# такой кейс проверяет модель на «формальное закрытие» (см. suspect_formal в _resolve).
_POSITIVE_REASONS = {"Клиент согласился", "Клиент набирает людей"}


# --------------------------------------------------------------------------- #
def _resolve(ctx, pool: pd.DataFrame, to_work: pd.DataFrame, no_point: pd.DataFrame,
             text_df: pd.DataFrame, batch: int, max_calls: int,
             ref_ym: tuple[int, int], min_impact: float = 0.0):
    """Аудит отработки по (ГОСБ, ИНН): один проход по всему пулу западающих сегментов.

    Пул считается заранее (`_audit_pool` в `prepare`): ВСЕ организации западающих
    сегментов их ГОСБ + доборные из других сегментов ГОСБ (не «топ» и не «минимум под
    план»). Тексты воронки тоже прочитаны заранее — одним запросом на весь отчёт;
    `_collect_notes` берёт из них только строки своего пула.

    Разрешение: чек-лист -> названный в тексте будущий срок -> ключевые слова (кроме
    needs_llm и пар с ≥2 авторами) -> LLM с фактами и хронологией -> фолбэк на правила
    для того, что не влезло в бюджет вызовов.
    """
    stats = {"batch": batch, "min_impact": min_impact, "max_calls": max_calls,
             "pool": 0, "checklist": 0, "keyword": 0, "llm": 0, "no_text": 0,
             "fallback": 0, "batches": 0, "capped": 0, "deadline": 0, "no_influence": 0}
    if to_work.empty or pool is None or pool.empty:
        return {}, to_work, no_point, "—", stats
    # Бюджет 0 — аудит выключен сознательно. Раньше пул всё равно резался на батчи и
    # собирались промпты, а в лог падала «ОШИБКА: исчерпан бюджет»: выключенная
    # функция не должна выглядеть сбоем и тратить время.
    if max_calls <= 0:
        progress.done(f"{'Аудит отработки отключён (llm_max_calls=0)'}: пул "
                      f"{len(pool)} пар не разбирается, причины берутся из правил")
        return {}, to_work, no_point, "—", stats
    stats["pool"] = len(pool)
    progress.step(f"Аудит отработки: пул {len(pool)} пар (ГОСБ,ИНН) — все организации "
                  f"западающих сегментов + добор, эффект ≥ {min_impact:g} чел")

    insights: dict = {}
    all_texts: list[str] = []
    notes = _collect_notes(text_df, pool, all_texts)

    need_llm = []
    for r in pool.itertuples():
        key = (int(r.new_gosb_id), int(r.inn))
        item_notes = notes.get(key, [])
        facts = _facts(r)
        # назван ли в тексте срок ПОЗЖЕ опорного месяца («зачисления пройдут 08.2026»)
        facts["deadline"] = text_rules.deadline(
            " ".join(n.get("comment") or "" for n in item_notes), ref_ym)
        multi_author = len({n["author"] for n in item_notes}) >= 2
        det = _deterministic(item_notes, multi_author, facts)
        # «лишь бы закрыть»: правило заявляет успех (согласие/расширение), но по СТАРОЙ
        # сделке план>0, факт=0 — не закрываем правилом, отдаём модели на проверку
        # формальности (это и есть вопрос «качественно или просто закрыли»).
        suspect_formal = (facts["plan_np_due"] > 0 and facts["fact_np_due"] == 0)
        if det and not (suspect_formal and det.get("reason") in _POSITIVE_REASONS):
            insights[key] = det
            stats["checklist" if det["source"].startswith("чек-лист") else "keyword"] += 1
        elif any(n.get("for_llm") for n in item_notes):
            # в LLM: id + сегмент + рычаг + ФАКТЫ (числа витрины) + хронология заметок
            llm_notes = [n for n in item_notes if n.get("for_llm")]
            llm_notes.sort(key=lambda n: n.get("_sort") or pd.Timestamp.min)
            need_llm.append({
                "gosb_id": key[0], "inn": key[1],
                "segment": str(getattr(r, "seg_name", "") or ""),
                "lever": str(getattr(r, "lever", "") or ""),
                "facts": facts,
                "notes": llm_notes,
            })
        else:
            # нет содержательного текста -> остаётся причина из правил (_classify)
            insights[key] = {"reason": "", "action": "", "verdict": "work",
                             "source": "правило"}
            stats["no_text"] += 1

    if need_llm:
        # пул отсортирован по убыванию эффекта -> бюджет тратится на крупные первыми
        ref_label = f"{ref_ym[1]:02d}.{ref_ym[0]}"
        got, nb = prompts.text_insights(ctx, need_llm, batch=batch, max_calls=max_calls,
                                       ref_label=ref_label)
        stats["batches"] += nb
        for it in need_llm:
            key = (it["gosb_id"], it["inn"])
            if key in got:
                insights[key] = _finalize(got[key], it["facts"]); stats["llm"] += 1
            else:   # не влез в бюджет вызовов / LLM не вернул -> фолбэк на правила
                insights[key] = _fallback_insight(it["notes"], it["facts"])
                stats["fallback"] += 1
        stats["capped"] = sum(1 for it in need_llm if (it["gosb_id"], it["inn"]) not in got)

    stats["deadline"] = sum(1 for v in insights.values()
                            if v.get("source") == "срок в тексте")
    stats["no_influence"] = sum(1 for v in insights.values()
                                if v.get("can_influence") == "нет"
                                or v.get("action") == text_rules.NO_INFLUENCE_ACTION)
    to_work, no_point = _reclassify(to_work, no_point, insights)
    return insights, to_work, no_point, text_rules.themes(all_texts), stats


def _audit_pool(to_work: pd.DataFrame, seg_gaps: dict, min_impact: float) -> pd.DataFrame:
    """Пул на аудит: ВСЕ организации западающих сегментов + доборные из других
    сегментов ГОСБ (когда своих не хватает). Порог по эффекту отсекает мелочь;
    сортировка по убыванию эффекта — чтобы бюджет вызовов шёл на крупные первыми."""
    if to_work.empty:
        return to_work.iloc[:0]
    own_mask = [(int(r.new_gosb_id), r.seg_name) in seg_gaps for r in to_work.itertuples()]
    own = to_work[own_mask]
    # доборные под цель +50%: организации других сегментов ГОСБ, если своих мало
    _, filler_idx, _ = _mark_needed(to_work, seg_gaps, max(PLAN_TARGETS))
    filler = to_work.loc[sorted(filler_idx)] if filler_idx else to_work.iloc[:0]
    pool = pd.concat([own, filler]).drop_duplicates(subset=["new_gosb_id", "inn"])
    pool = pool[pool["impact_fl"] >= float(min_impact)]
    return pool.sort_values("impact_fl", ascending=False)


def _facts(r) -> dict:
    """Числовые ФАКТЫ витрины для LLM. Сделки — только «старый» месяц (*_old).

    deal_expected: ожидается ли по задачам сделка вообще (план>0 либо заведён deal_code).
    Если нет — отсутствие сделки НЕ дефект, и спрашивать по ней факт зачислений нельзя.
    n_in_progress / n_closed: сколько задач ещё в работе (не просрочены) и сколько
    закрыто — без этого открытая задача выглядит как «не отработана».

    Отток здесь ФАКТИЧЕСКИЙ, за три закрытых месяца: `out_gone` — сколько ушло,
    `out_returned` — сколько вернулось, `out_kept` — сколько потеряли насовсем.
    Разделение существенно: организация, откуда ушли и вернулись, отработана, а
    ровно та же цифра ухода без возврата означает потерю.
    `pipe_np_cur` снимает ложное «нужна активность»: организация с планом на текущий
    месяц уже в работе.
    """
    return {
        "potential": int(getattr(r, "emp_potential_qty", 0) or 0),
        "outflow_fl": int(getattr(r, "fl_outflow_qty", 0) or 0),
        "outflow_fot_mln": float(getattr(r, "fot_outflow_mln", 0) or 0),
        "avg_salary": float(getattr(r, "avg_salary", 0) or 0),
        "out_gone": float(getattr(r, "out_qty", 0) or 0),
        "out_returned": float(getattr(r, "ret_qty", 0) or 0),
        "out_kept": float(getattr(r, "out_kept", 0) or 0),
        # отрабатывали ли ОТТОК в месяц ухода и следующий — без этого модель
        # советует «начать отработку» там, где её уже вели
        "out_tasks": int(getattr(r, "out_tasks", 0) or 0),
        "out_tasks_outflow": int(getattr(r, "out_tasks_outflow", 0) or 0),
        "out_worked": bool(getattr(r, "out_worked", False)),
        "pipe_np_cur": float(getattr(r, "pipe_np_raw", 0) or 0),
        "lever": str(getattr(r, "lever", "") or ""),
        "deal_expected": bool(getattr(r, "deal_expected", False)),
        "plan_deal_old": int(getattr(r, "plan_deal_old", 0) or 0),
        "fact_deal_old": int(getattr(r, "fact_deal_old", 0) or 0),
        # план/факт по сделкам за ЗАКРЫТЫЕ месяцы — агрегат по всем сделкам организации
        "plan_np_due": float(getattr(r, "plan_np_due", 0) or 0),
        "fact_np_due": float(getattr(r, "fact_np_due", 0) or 0),
        "due_months": int(getattr(r, "due_months", 0) or 0),
        "pipe_fact_mtd": float(getattr(r, "pipe_fact_mtd", 0) or 0),
        "has_fresh_deal": bool(getattr(r, "has_fresh_deal", False)),
        "n_overdue": int(getattr(r, "n_overdue", 0) or 0),
        "n_in_progress": int(getattr(r, "n_in_progress", 0) or 0),
        "n_closed": int(getattr(r, "n_closed", 0) or 0),
        "any_success": bool(getattr(r, "any_success", False)),
    }


def _finalize(ins: dict, facts: dict) -> dict:
    """Привести ответ модели в вид, который ложится в поле «Причина / действие».

    Метки качества дописываются ПРЕФИКСОМ в reason (без правок вёрстки, индексируются
    поиском таблицы). Каждая метка гасится там, где она заведомо не имеет смысла:
      * «формально закрыто» / «не отработана» — только если задачи вообще ЗАКРЫТЫ:
        по открытой и не просроченной задаче спрашивать результат рано;
      * «отток не отработан» — только при реальном оттоке И когда банк мог на него
        повлиять (объективный отток вроде отпусков отрабатывать нечем);
      * «влиять нечем» переводит вердикт в no_point — такая пара уходит из списка,
        а действие заменяется на мониторинг, чтобы не требовать выдуманных шагов.
    """
    has_closed = facts.get("n_closed", 0) > 0 or facts.get("n_overdue", 0) > 0
    has_outflow = int(facts.get("outflow_fl", 0)) > 0
    no_influence = ins.get("can_influence") == "нет"
    prefixes = []
    if no_influence:
        prefixes.append("влиять нечем")
        ins["verdict"] = "no_point"
        ins["action"] = text_rules.NO_INFLUENCE_ACTION
    if has_closed and not no_influence:
        if ins.get("quality") == "формально":
            prefixes.append("формально закрыто")
        elif ins.get("quality") == "не отработана":
            prefixes.append("не отработана")
    if ins.get("contradiction") == "да":
        prefixes.append("противоречие в комментариях")
    if ins.get("outflow_worked") == "нет" and has_outflow and not no_influence:
        prefixes.append("отток не отработан")
    if prefixes:
        base = ins.get("reason", "")
        ins["reason"] = " · ".join(prefixes) + (f" · {base}" if base else "")
    return ins


def _deterministic(notes: list[dict], multi_author: bool = False,
                   facts: dict | None = None) -> dict | None:
    """Детерминированное разрешение. None — нужен LLM.

    Порядок: 1) причина оттока из ЧЕК-ЛИСТА (структурный ответ);
             2) названный в тексте СРОК позже опорного месяца;
             3) ключевые слова в КОММЕНТАРИИ;
             4) ключевые слова в ОТВЕТАХ чек-листа.
    Ужесточения:
      * структурный no_point из чек-листа (ликвидация, отпуска/сезонность, сокращение
        штата) короткозамыкает всегда — это поле формы, а не догадка по тексту;
      * названный будущий срок при отсутствии просрочки закрывает кейс как in_progress:
        сотрудник назвал дату, она не наступила — требовать результата сейчас не за что;
      * при ≥2 авторах правила НЕ закрывают кейс (кроме того no_point) — противоречия
        между сотрудниками может оценить только LLM;
      * правила с флагом needs_llm (ликвидация по ключевым словам, «влиять нечем» по
        свободному тексту, незнакомая причина оттока) — лишь подсказка, вердикт
        подтверждает модель, поэтому здесь их не применяем.
    По сырому тексту анкеты не матчим — там формулировки ВОПРОСОВ дают ложные
    срабатывания (матчим отдельно по РАЗОБРАННЫМ ответам).
    """
    facts = facts or {}
    for n in notes:
        det = text_rules.outflow_reason(n.get("questionnaire"))
        if det and det.get("verdict") == "no_point":
            return det                    # структурный ответ чек-листа — доверяем всегда
    # срок назван и ещё не наступил, просрочки нет -> работа идёт, ждём
    due = facts.get("deadline")
    if due and not facts.get("n_overdue"):
        return {"reason": f"назван срок {due} — ещё не наступил",
                "action": f"Проконтролировать в {due}",
                "verdict": "in_progress", "source": "срок в тексте", "needs_llm": False}
    if multi_author:
        return None                       # ≥2 авторов -> в LLM (кроме no_point выше)
    for n in notes:
        det = text_rules.outflow_reason(n.get("questionnaire"))
        if det and not det.get("needs_llm"):
            return det
    for n in notes:
        det = text_rules.match_keyword(n.get("comment"))
        if det and not det.get("needs_llm"):
            return det
    for n in notes:
        answers = " ".join(text_rules.parse_questionnaire(n.get("questionnaire")).values())
        det = text_rules.match_keyword(answers)
        if det and not det.get("needs_llm"):
            return det
    return None


def _fallback_insight(notes: list[dict], facts: dict | None = None) -> dict:
    """Если LLM не ответил / не влез в бюджет — разрешаем детерминированными правилами.

    Текст для повторного матчинга собираем из КОММЕНТАРИЯ и РАЗОБРАННЫХ ОТВЕТОВ анкеты,
    а НЕ из сырого n['text'] (там формулировки вопросов чек-листа дают ложные
    срабатывания).

    В отличие от `_deterministic`, здесь needs_llm-правила ПРИМЕНЯЮТСЯ как есть:
    подтвердить их некому, а догадка по ключевым словам («сезонный фактор»,
    «признаки ликвидации») честнее слепого «работать». Ложный no_point по ликвидации
    так не возникает: у этого правила вердикт и так work, модель лишь могла его усилить.
    """
    det = _deterministic(notes, facts=facts)
    if det:
        return det
    safe_parts = []
    for n in notes:
        if n.get("comment"):
            safe_parts.append(n["comment"])
        safe_parts.extend(text_rules.parse_questionnaire(n.get("questionnaire")).values())
    fb = text_rules.match_keyword(" ".join(safe_parts))
    if fb:
        return fb
    return {"reason": "", "action": "", "verdict": "work", "source": "правило"}


def _collect_notes(text_df: pd.DataFrame, pool: pd.DataFrame, sink: list) -> dict:
    """Заметки по каждой паре (ГОСБ, ИНН): ВСЕ активности за 3 мес, с автором и датой.

    Автор обезличивается ПО ПАРЕ (`Сотрудник-1..N` по табельному isu_struct_saphr_id) —
    ФИО не тянем; этого достаточно, чтобы модель различала сотрудников для поиска
    противоречий. Неинформативные заметки НЕ выбрасываем (чтобы короткие «ушли в ВТБ»
    видели правила), а помечаем for_llm=False — в LLM уходит только содержательный текст.
    """
    out: dict = {}
    if text_df.empty:
        return out
    keys = {(int(r.new_gosb_id), int(r.inn)) for r in pool.itertuples()}
    authors: dict = {}     # key -> {author_id: "Сотрудник-NN"}
    skipped: dict[str, int] = {}
    for r in text_df.itertuples():
        if pd.isna(r.new_gosb_id):
            continue
        key = (int(r.new_gosb_id), int(r.inn))
        if key not in keys:
            continue
        comment = (r.task_comment or "").strip()
        quest = (r.task_questionnaire or "").strip()
        ok_c, why_c = text_rules.is_meaningful(comment)
        ok_q, why_q = text_rules.is_meaningful(quest)
        for_llm = ok_c or ok_q
        if not for_llm:
            why = why_c if comment else why_q
            skipped[why] = skipped.get(why, 0) + 1
        # содержательный текст для промпта (без [тип/статус] — они идут отдельным мета)
        parts = []
        if ok_c:
            parts.append(comment)
        if ok_q:
            parts.append("анкета: " + quest.replace("\n", "; "))
        text = " | ".join(parts)[:400]
        # автор -> обезличенный токен в рамках этой пары
        amap = authors.setdefault(key, {})
        aid = getattr(r, "author_id", None)
        aid = int(aid) if pd.notna(aid) else -1
        if aid not in amap:
            amap[aid] = f"Сотрудник-{len(amap) + 1:02d}"
        created = getattr(r, "task_create_dt", None)
        closed = getattr(r, "fact_close_task_dttm", None)
        closed_same_day = bool(pd.notna(created) and pd.notna(closed)
                               and pd.Timestamp(created).date() == pd.Timestamp(closed).date())
        out.setdefault(key, []).append({
            "text": text, "comment": comment, "questionnaire": quest, "for_llm": for_llm,
            "date": (pd.Timestamp(created).strftime("%d.%m") if pd.notna(created) else ""),
            "_sort": (pd.Timestamp(created) if pd.notna(created) else None),
            "author": amap[aid], "role": str(getattr(r, "role_code", "") or ""),
            "type": " / ".join(x for x in (str(getattr(r, "task_type", "") or ""),
                                           str(getattr(r, "task_subtype", "") or "")) if x),
            "status": str(getattr(r, "task_text_status", "") or ""),
            "closed_same_day": closed_same_day,
            # чтобы модель не спрашивала результат с ещё открытой задачи
            "in_progress": bool(getattr(r, "is_in_progress", False)),
            "overdue": bool(getattr(r, "is_overdue", False)),
        })
        if comment:
            sink.append(comment)
    if skipped and progress.SHOW_LLM:
        for why, n in sorted(skipped.items(), key=lambda x: -x[1])[:6]:
            progress.done(f"неинформативных заметок ×{n}: {why}")
    # до 8 записей на организацию (самые свежие — сверху, text_df уже DESC)
    return {k: v[:8] for k, v in out.items()}


# Вердикты, при которых организации в списке «к работе» делать нечего ПРЯМО СЕЙЧАС:
#   no_point    — влиять нечем (ликвидация, отпуска/сезонность, сокращение штата);
#   in_progress — работа идёт: задача не закрыта и не просрочена, либо назван срок,
#                 который ещё не наступил, либо только что заведена сделка.
# И то и другое уходит из списка: список должен отвечать на вопрос «что мы РЕАЛЬНО
# можем сделать сейчас», а не перечислять всё, к чему можно придраться.
_MOVE_VERDICTS = ("no_point", "in_progress")


def _reclassify(to_work: pd.DataFrame, no_point: pd.DataFrame, insights: dict):
    """Перенести из «к работе» строки, где действовать сейчас не за что."""
    if to_work.empty:
        return to_work, no_point
    move_mask = []
    for r in to_work.itertuples():
        v = insights.get((int(r.new_gosb_id), int(r.inn)))
        move_mask.append(bool(v and v.get("verdict") in _MOVE_VERDICTS))
    move_mask = pd.Series(move_mask, index=to_work.index)
    moved = to_work[move_mask].copy()
    if not moved.empty:
        moved["reason"] = [insights[(int(r.new_gosb_id), int(r.inn))]["reason"]
                           for r in moved.itertuples()]
        n_wait = sum(1 for r in moved.itertuples()
                     if insights[(int(r.new_gosb_id), int(r.inn))]["verdict"] == "in_progress")
        no_point = pd.concat([no_point, moved], ignore_index=True)
        to_work = to_work[~move_mask].copy()
        progress.done(f"убрано из списка по тексту: {len(moved)} "
                      f"(влиять нечем {len(moved) - n_wait}, работа идёт {n_wait})")
    return to_work, no_point


# --------------------------------------------------------------------------- #
def _mark_needed(rows: pd.DataFrame, seg_gaps: dict, k: float) -> tuple[set, set, dict]:
    """Кто нужен, чтобы закрыть разрыв каждого ЗАПАДАЮЩЕГО сегмента при цели k.

    Фаза 1 — внутри сегмента: организации ЭТОГО сегмента в ЭТОМ ГОСБ по убыванию
    эффекта, пока не набрано gap × k. Фаза 2 — добор: если своих не хватило,
    остаток закрываем организациями других сегментов того же ГОСБ (в т.ч. без
    сегмента в справочнике), помечая строки как доборные.

    Возврат: (индексы отобранных, индексы доборных, инфо по (ГОСБ, сегмент)).
    """
    chosen: set = set()
    filler: set = set()
    info: dict = {}
    if rows.empty:
        return chosen, filler, info

    by_gosb = {}
    for (nid, seg), gap in seg_gaps.items():
        by_gosb.setdefault(nid, []).append((seg, gap))

    for nid, g in rows.groupby("new_gosb_id"):
        bad_segs = by_gosb.get(int(nid), [])
        if not bad_segs:
            continue
        shortfall = 0.0
        for seg, gap in sorted(bad_segs, key=lambda x: -x[1]):
            target = gap * k
            sub = g[g.seg_name == seg].sort_values("impact_fl", ascending=False)
            acc = 0.0; n_own = 0
            for idx, fl in zip(sub.index, sub["impact_fl"]):
                if acc >= target:
                    break
                chosen.add(idx); acc += float(fl); n_own += 1
            lack = max(0.0, target - acc)
            shortfall += lack
            info[(int(nid), seg)] = {
                "gap": gap, "own_n": n_own, "own_fl": acc, "lack": lack,
                "coverage": (acc / target) if target > 0 else None,
                "n_avail": int(len(sub)),
            }
        if shortfall <= 0:
            continue
        # добор из других сегментов ГОСБ — только на недостающий объём
        rest = (g[~g.index.isin(chosen)].sort_values("impact_fl", ascending=False))
        acc = 0.0
        for idx, fl in zip(rest.index, rest["impact_fl"]):
            if acc >= shortfall:
                break
            chosen.add(idx); filler.add(idx); acc += float(fl)
    return chosen, filler, info


def _select(to_work: pd.DataFrame, seg_gaps: dict) -> tuple[pd.DataFrame, dict]:
    """Пометить строки минимальной целью, при которой они нужны (need_k).

    need_k = 1.0 / 1.2 / 1.5 — цель «выполнить план / +20% / +50%»; 0 — организация
    не нужна ни при какой цели (здоровый сегмент или хвост списка), её видно только
    при выборе «Все организации».
    """
    out = to_work.copy().reset_index(drop=True)
    if out.empty:
        out["need_k"] = 0.0
        out["filler"] = False
        return out, {}

    out["need_k"] = 0.0
    out["filler"] = False
    seg_info: dict = {}
    # от большей цели к меньшей: меньшая перезаписывает — остаётся минимальная
    for k in sorted(PLAN_TARGETS, reverse=True):
        chosen, filler, info = _mark_needed(out, seg_gaps, k)
        if chosen:
            idx = sorted(chosen)
            out.loc[idx, "need_k"] = k
            out.loc[idx, "filler"] = [i in filler for i in idx]
        if k == 1.0:
            seg_info = info

    # сначала нужные под план (need_k>0), внутри — по убыванию эффекта
    out["_ord"] = out["need_k"].replace(0.0, 99.0)
    out = out.sort_values(["_ord", "impact_fl"], ascending=[True, False],
                          ignore_index=True).drop(columns="_ord")

    plan: dict = {}
    for nid, g in out.groupby("new_gosb_id"):
        sel = g[(g.need_k > 0) & (g.need_k <= 1.0)]
        att = sel[sel.lever == "Привлечь"]; ret = sel[sel.lever == "Вернуть"]
        fill = sel[sel.filler]
        segs = []
        for (gid, seg), d in seg_info.items():
            if gid != int(nid):
                continue
            s_sel = sel[sel.seg_name == seg]
            segs.append({"seg": seg, "gap": d["gap"], "n_need": int(len(s_sel)),
                         "fl_need": float(s_sel["impact_fl"].sum()),
                         "coverage": d["coverage"], "lack": d["lack"],
                         "n_avail": d["n_avail"]})
        segs.sort(key=lambda s: -s["gap"])
        plan[int(nid)] = {
            "segs": segs,
            "gap_seg": sum(s["gap"] for s in segs),
            "n_need": int(len(sel)), "fl_need": float(sel["impact_fl"].sum()),
            "fot_need": float(sel["impact_fot_mln"].sum()),
            "n_attract": int(len(att)), "fl_attract": float(att["impact_fl"].sum()),
            "n_return": int(len(ret)), "fl_return": float(ret["impact_fl"].sum()),
            "filler_n": int(len(fill)), "filler_fl": float(fill["impact_fl"].sum()),
            "n_total": int(len(g)), "fl_total": float(g["impact_fl"].sum()),
            "not_worked": int((~sel["worked"]).sum()) if "worked" in sel else 0,
        }
    return out, plan


def _plan_summary(to_work: pd.DataFrame, gosb_plan: dict, gap: float,
                  fact: float, plan_amt: float) -> dict:
    """Итог по ТБ: сколько организаций нужно суммарно и что это даёт."""
    gap_sum = sum(p["gap_seg"] for p in gosb_plan.values()) or gap
    sel = to_work[(to_work.need_k > 0) & (to_work.need_k <= 1.0)] if not to_work.empty \
        else to_work
    # потенциал, который реально считается «в тему»: западающие сегменты
    own = 0.0
    for p in gosb_plan.values():
        own += sum(s["fl_need"] for s in p["segs"])
    return {
        "gap": gap, "gap_seg": gap_sum, "fact": fact, "plan": plan_amt,
        "k": int(len(sel)),
        "closable": float(sel["impact_fl"].sum()) if len(sel) else 0.0,
        "attract": float(sel[sel.lever == "Привлечь"]["impact_fl"].sum()) if len(sel) else 0.0,
        "retention": float(sel[sel.lever == "Вернуть"]["impact_fl"].sum()) if len(sel) else 0.0,
        "fot_mln": float(sel["impact_fot_mln"].sum()) if len(sel) else 0.0,
        "filler_n": int(sel["filler"].sum()) if len(sel) else 0,
        "coverage": (own / gap_sum) if gap_sum else 0.0,
        "total_potential": float(to_work["impact_fl"].sum()) if not to_work.empty else 0.0,
    }


# Остаток меньше этой доли плана ГОСБ в таблицу не выносим — это шум округления.
REST_MIN_SHARE = 0.005

# Пороги именной детализации. Именами число объяснить нельзя: на тестовом ГОСБ
# ожидаемый отток 552 чел размазан по 67 организациям, топ-5 дают лишь 32%, топ-20 —
# 80%. Поэтому называем только тех, кто реально двигает цифру, а хвост честно
# сворачиваем в одну строку; структуру объясняют разборы по причине и зоне влияния.
DETAIL_COVER = 0.60       # набираем строки, пока не покрыто столько блока
DETAIL_MIN_FL = 3         # но не показываем строки мельче стольких человек
DETAIL_MAX_ROWS = 8       # потолок строк в блоке


def _material(rows: list, key: str, cap: int = DETAIL_MAX_ROWS,
              min_fl: float = DETAIL_MIN_FL) -> tuple:
    """Материальные строки блока + честный хвост (сколько организаций и человек).

    Отбор идёт по НАКОПЛЕННОМУ ПОКРЫТИЮ, а не по доле каждой строки. Прежний порог
    «≥5% блока» не масштабируется: на проме в блоке бывает 1 000–5 000 организаций,
    и 5% не набирает никто — детализация вырождалась в «нет организаций с заметным
    вкладом» почти везде. Покрытие устойчиво к размеру блока: сколько бы строк ни
    было, крупнейшие набираются, пока не объяснят DETAIL_COVER блока.

    Абсолютный пол `min_fl` остаётся: в мелком ГОСБ строки по одному человеку
    покрытие бы набрали, но смысла в них нет. Внутри группы (см. `_out_groups`) пол
    снижается до 1: там группа уже названа и посчитана, и пустое раскрытие выглядит
    поломкой, а не экономией внимания.

    Возвращает (строки, число орг в хвосте, человек в хвосте, покрытие показанных).
    """
    total = float(sum(abs(r[key]) for r in rows))
    if total <= 0:
        return [], 0, 0.0, 0.0
    ordered = sorted(rows, key=lambda r: -abs(r[key]))
    acc, n = 0.0, 0
    for r in ordered:
        if n >= cap or acc >= DETAIL_COVER * total or abs(r[key]) < min_fl:
            break
        acc += abs(r[key])
        n += 1
    tail = ordered[n:]
    return ordered[:n], len(tail), float(sum(abs(r[key]) for r in tail)), acc / total


def _portfolio_unit(t, g: pd.DataFrame) -> dict:
    """Числа блока «Управление портфелем» для ОДНОЙ единицы разбора.

    Портфель, план и прогноз — из строки витрины по этой единице (`t`), отток и
    пайплайн — свёртка её организаций (`g`). Формулы, связывающей их, нет: см.
    `_portfolio`.
    """
    num = forecast.num
    pipe = float(num(g, "pipe_np").sum())
    pipe_raw = float(num(g, "pipe_np_raw").sum())
    plan = float(getattr(t, "plan_amt", 0) or 0)
    fc = float(getattr(t, "fact_amt", 0) or 0)      # fact_amt единицы = прогноз витрины
    return {
        "base": float(getattr(t, "base_amt", 0) or 0),
        "out_kept": float(num(g, "out_kept").sum()),
        "out_fot": float(num(g, "out_fot").sum()),
        "pipe": pipe, "pipe_raw": pipe_raw,
        "pipe_upside": max(0.0, pipe_raw - pipe),
        "pipe_fot": float(num(g, "pipe_fot").sum()),
        "plan": plan, "forecast": fc,
        "exec": (fc / plan) if plan else None,
    }


def _out_groups(rows: list, closed_label: str, out_label: str = "") -> list:
    """Организации в оттоке, разложенные по ВОЗВРАТУ и МЕСЯЦУ ухода.

    Плоский список сортировался по вкладу, и рядом оказывались организации с
    противоположным выводом. Раньше осью были классы модели (устойчивый / сезонный /
    разовый) — модели больше нет, и ось теперь фактическая, из витрины возвратов:

      * никто не вернулся — чистая потеря, разбирать в первую очередь;
      * вернулись частично — работа шла и что-то дала, стоит довести;
      * ушли в последнем закрытом месяце — свежая потеря, причина ещё по горячим следам.

    Первые две группы про ГЛУБИНУ потери, третья про СВЕЖЕСТЬ, поэтому организация
    может попасть и в «свежие», и в одну из первых двух. Это не двойной счёт: доли
    считаются от общего оттока блока и внутри каждой группы независимо, а вопрос
    «за что браться» у групп разный.
    """
    total = float(sum(r["out"] for r in rows)) or 1.0
    fresh_m = str(closed_label or "").strip()

    def _no_return(r):
        return float(r.get("ret", 0) or 0) <= 0

    def _partial(r):
        return float(r.get("ret", 0) or 0) > 0

    def _fresh(r):
        return bool(fresh_m) and fresh_m in (r.get("months") or [])

    kinds = [
        ("kept", _no_return, "Ушли и не вернулись",
         f"за {out_label} не вернулся никто — это чистая потеря" if out_label
         else "не вернулся никто — это чистая потеря"),
        ("partial", _partial, "Часть людей вернулась",
         "кто-то уже вернулся — значит работа идёт, её стоит довести"),
        ("fresh", _fresh, "Ушли в последнем закрытом месяце",
         f"ушли в {fresh_m} — по свежим следам ещё можно выяснить причину"
         if fresh_m else "самые свежие уходы"),
    ]
    groups = []
    for key, match, title, sub in kinds:
        part = [r for r in rows if match(r)]
        if not part:
            continue
        fl = float(sum(r["out"] for r in part))
        work = [r for r in part if r["zone"] == "можно работать"]
        # внутри группы пол в 3 человека отсекал бы целиком мелкие группы
        top, tail_n, tail_fl, _ = _material(part, "out", min_fl=1)
        groups.append({
            "key": key, "title": title, "sub": sub,
            "n": len(part), "fl": fl, "share": fl / total,
            "work_n": len(work), "work_fl": float(sum(r["out"] for r in work)),
            "rows": top, "tail_n": tail_n, "tail_fl": tail_fl,
        })
    return sorted(groups, key=lambda g: -g["fl"])



def _yoy_rows(nid: int, inns, rows: list, names: dict, yoy: dict, cur: dict, ref: dict,
              insights: dict, work: set, nopt: set, emp_of: dict | None = None) -> list:
    """Просевшие за год организации единицы — ПО СТРОКАМ ВИТРИНЫ, а не по прогнозу.

    Источник принципиален. Блок отвечает на вопрос «на сколько просел портфель за год»,
    и его итог обязан сходиться с витриной. Если собирать блок из строк прогноза
    (грейн (ГОСБ, ИНН)), в него не попадают организации, которых в прогнозе нет вовсе —
    например, обслуживаемые только аппаратом, исключённым из разбора, — и сумма блока
    оказывалась меньше витрины.

    Всё, что известно про отток и отработку, подмешивается из прогноза, если строка
    там есть; если нет — месяцев оттока мы не знаем, и организация уходит в группу
    «отработка неизвестна», а не в «таяли постепенно».

    `emp_of` — ФИО закреплённого сотрудника по паре; заполнен только на уровне, где
    единица разбора это ГОСБ (см. `_unit_detail`). Пустая строка означает «показывать
    нечего», и подпись под названием организации просто не рисуется.
    """
    known = {r["inn"]: r for r in rows}
    out = []
    for inn in inns:
        k = (nid, int(inn))
        delta = yoy.get(k, 0.0)
        if delta > -1:
            continue
        base = known.get(int(inn), {})
        ins = insights.get(k, {})
        out.append({
            "inn": int(inn), "name": names.get(k, f"Орг. {int(inn)}"),
            "emp": (emp_of or {}).get(k, ""),
            "yoy": delta, "cur": cur.get(k, 0.0),
            "out": base.get("out", 0.0), "pipe": base.get("pipe", 0.0),
            "out_months": list(base.get("out_months") or []),
            "reason": ins.get("reason", ""), "action": ins.get("action", ""),
            "in_ref": ref.get(k, False),
            "zone": ("можно работать" if k in work else
                     "повлиять уже нельзя" if k in nopt
                     else "клиент не закреплён за банком"),
        })
    return out



def _unit_grain(orgs_fc: pd.DataFrame, unit_src: str):
    """Прогноз, сведённый к грейну ЕДИНИЦЫ уровня: (единица, организация).

    На уровне ТБ единица — ГОСБ, и свёртка не нужна: грейн прогноза уже такой. На
    уровне СБ единица — ТБ, а грейн прогноза остаётся (ГОСБ, ИНН), и организация,
    обслуживаемая в трёх ГОСБ одного ТБ, давала в блоках ТРИ строки. В блоке
    «Портфель год к году» каждой из них приписывалась ПОЛНАЯ годовая дельта
    организации по ТБ (она берётся по ключу (ТБ, ИНН)) — одна и та же цифра
    повторялась, а итог блока оказывался кратно больше витрины.

    Отдаёт пары (единица, кадр), готовые к groupby-обходу.
    """
    f = orgs_fc.dropna(subset=[unit_src]).copy() if orgs_fc is not None else None
    if f is None or f.empty:
        return []
    f[unit_src] = f[unit_src].astype("int64")
    f["inn"] = f["inn"].astype("int64")
    if unit_src == "new_gosb_id":
        return list(f.groupby(unit_src))
    num = ["out_qty", "ret_qty", "out_kept", "pipe_np", "pipe_np_raw", "pipe_fact_mtd"]
    agg = f.groupby([unit_src, "inn"], as_index=False).agg(
        **{c: (c, "sum") for c in num if c in f})
    months = (f.groupby([unit_src, "inn"])["out_months"]
               .apply(lambda s: sorted({m for v in s for m in (v or [])}, reverse=True))
               .rename("out_months").reset_index())
    agg = agg.merge(months, on=[unit_src, "inn"], how="left")
    return list(agg.groupby(unit_src))



def _yoy_groups(rows: list, nid: int, fmonths: dict, fwindow: set) -> list:
    """Просевшие за год, разложенные по ОБЪЯСНЁННОСТИ падения.

    Плоский список «просели за год» показывал дельту и больше ничего: по нему нельзя
    было понять, потеряли людей разовым оттоком или штат таял месяцами, и работали ли
    с организацией, когда это происходило. Ось группировки выбрана так, чтобы у каждой
    группы было своё действие.

    `fmonths` — {(ГОСБ, ИНН, «MM.YYYY»): агрегат задач за месяц}; `fwindow` — множество
    месяцев, попавших в выборку воронки. Разница между «активностей не было» и
    «неизвестно» держится именно на `fwindow`: если месяц оттока в окно не попал,
    сказать про отработку нечего, и это пишется прямо. Молчаливое «не отрабатывали»
    было бы утверждением о факте, которого у нас нет.
    """
    kinds = [
        ("worked", "Люди ушли — с клиентом работали",
         "в месяцы ухода и сразу после были встречи и звонки: вопрос не в том, "
         "дошли ли до клиента, а в том, что ему предложили"),
        ("missed", "Люди ушли — с клиентом не работали",
         "ни в месяц ухода, ни в следующий не было ни одной встречи и ни одного "
         "звонка: клиента просто пропустили"),
        ("unknown", "Люди ушли давно — что делали тогда, неизвестно",
         "эти месяцы старше, чем данные о встречах и звонках, — сказать про работу "
         "с клиентом нечего"),
        ("melt", "Уходили по одному, без крупных увольнений",
         "заметных уходов не было: сотрудники уходили поодиночке месяц за месяцем"),
    ]
    total = float(sum(abs(r["yoy"]) for r in rows)) or 1.0
    for r in rows:
        months = r.get("out_months") or []
        # Задачу на отток ставят и ВНУТРИ месяца, и следующим отчётным месяцем:
        # витрина оттока закрывается позже, чем он случился. Поэтому по каждому месяцу
        # оттока M смотрим задачи и в M, и в M+1 — иначе нормально отработанный отток
        # выглядел бы пропущенным.
        look = {m: [m, next_month(m)] for m in months}
        known = [m for m, ms in look.items() if any(x in fwindow for x in ms)]
        tasks = sum(int(fmonths.get((nid, r["inn"], x), {}).get("n_tasks", 0))
                    for m in known for x in look[m] if x in fwindow)
        r["yoy_tasks"] = tasks
        r["yoy_months_known"] = known
        if not months:
            # месяцев с фактическим оттоком нет: штат таял ровно, без заметных уходов
            r["yoy_key"] = "melt"
        elif not known:
            r["yoy_key"] = "unknown"
        elif tasks > 0:
            r["yoy_key"] = "worked"
        else:
            r["yoy_key"] = "missed"

    groups = []
    for key, title, sub in kinds:
        part = [r for r in rows if r["yoy_key"] == key]
        if not part:
            continue
        fl = float(sum(abs(r["yoy"]) for r in part))
        # внутри группы пол снижен до 1: группа уже названа, и пустое раскрытие
        # выглядело бы поломкой (та же логика, что в _out_groups)
        top, tail_n, tail_fl, _ = _material(part, "yoy", min_fl=1)
        with_reason = sum(1 for r in part if r.get("reason"))
        groups.append({
            "key": key, "title": title, "sub": sub,
            "n": len(part), "fl": fl, "share": fl / total,
            "work_n": with_reason,
            "work_fl": float(sum(abs(r["yoy"]) for r in part if r.get("reason"))),
            "rows": top, "tail_n": tail_n, "tail_fl": tail_fl,
        })
    return sorted(groups, key=lambda g: -g["fl"])


def _rest_row(gosb_row, segs: list, n_need_total: int) -> dict | None:
    """Строка «прочие» для таблицы карточки: ГОСБ минус показанные сегменты.

    В карточке перечисляются ТОЛЬКО западающие сегменты, поэтому их сумма не обязана
    равняться итогу ГОСБ, а таблица, которая не сходится, выглядит сломанной. Остаток
    закрывает разницу; он же поглощает известное расхождение уровней `tb`/`gosb`
    в витрине метрик (строка «все сегменты» — не сумма строк по сегментам).
    """
    plan = float(gosb_row.plan_amt) - sum(s["plan"] for s in segs)
    forecast = float(gosb_row.fact_amt) - sum(s["forecast"] for s in segs)
    n_need = max(0, n_need_total - sum(s["n_need"] for s in segs))
    if abs(plan) < REST_MIN_SHARE * max(float(gosb_row.plan_amt), 1) and abs(forecast) < 1:
        return None
    return {"plan": plan, "forecast": forecast, "nedobor": plan - forecast,
            "n_need": n_need}


def _unit_detail(orgs_fc: pd.DataFrame, detail: pd.DataFrame, insights: dict,
                 to_work: pd.DataFrame, no_point: pd.DataFrame,
                 gosb_gap: pd.DataFrame, conv_tb: float,
                 conv_diag: dict | None = None,
                 conv_by_gosb: dict | None = None, dates: dict | None = None,
                 unit_src: str = "new_gosb_id", unit_label: str = "ГОСБ",
                 funnel_months: pd.DataFrame | None = None) -> dict:
    """Разбор прогноза по каждой единице — то, что открывается по клику на карточку.

    Единица — ГОСБ в отчёте ТБ и ТБ в отчёте СБ; `unit_src` называет колонку грейна
    организаций, по которой они группируются.

    Отвечает на вопрос «что с портфелем»: числа этой единицы, затем отток по
    причинам (см. `_out_groups`), крупнейшие организации в пайплайне и тренд портфеля
    год к году. Отток покрыт группами целиком; в пайплайне именами объясняется только
    материальная часть (см. `_material`), поэтому там показывается покрытие.
    У строки остаётся признак `zone` (можно работать / влиять
    нечем / вне эталонной базы) — он помечает организации, которые в список к работе не
    попадут, чтобы их не пытались распределять.
    """
    out: dict = {}
    if orgs_fc is None or orgs_fc.empty:
        return out
    names, yoy, ref, cur = {}, {}, {}, {}
    yoy_tot: dict = {}
    det_by_unit: dict = {}
    emp_of: dict = {}
    # ФИО закреплённого сотрудника показываем ТОЛЬКО там, где единица разбора — ГОСБ.
    # Закрепление живёт на грейне (ГОСБ, организация): на уровне ТБ у организации
    # закреплённых может быть несколько, и выбрать «того самого» нечем. Условие
    # явное, а не «в кадре нет колонки»: кадр уровня СБ приходит другой (строки
    # витрины уровня tb), и молчаливое совпадение легко потерять при правке.
    with_emp = unit_src == "new_gosb_id" and detail is not None and "emp_fio" in detail
    # Справочник приходит на грейне ЕДИНИЦЫ УРОВНЯ: у ТБ это строки витрины уровня
    # gosb, у банка — строки уровня tb. Ключ по ГОСБ на уровне СБ не нашёлся бы
    # никогда, и блок годового тренда молча оставался бы пустым.
    dsrc = unit_src if (detail is not None and unit_src in detail) else "new_gosb_id"
    if detail is not None and not detail.empty:
        for r in detail.dropna(subset=[dsrc]).itertuples():
            k = (int(getattr(r, dsrc)), int(r.inn))
            names[k] = str(r.company_name or "").strip() or f"Орг. {int(r.inn)}"
            yoy[k] = yoy.get(k, 0.0) + float(r.fl_yoy or 0)
            cur[k] = cur.get(k, 0.0) + float(r.current_fl_qty or 0)
            ref[k] = ref.get(k, False) or bool(r.in_ref)
            yoy_tot[k[0]] = yoy_tot.get(k[0], 0.0) + float(r.fl_yoy or 0)
            # dict как упорядоченное множество: если справочник вдруг придёт с
            # повторами пары, организация не должна попасть в блок дважды
            det_by_unit.setdefault(k[0], {})[k[1]] = None
            if with_emp:
                emp_of[k] = str(getattr(r, "emp_fio", "") or "").strip()

    def _keys(df):
        src = unit_src if (df is not None and unit_src in df) else "new_gosb_id"
        return ({(int(getattr(r, src)), int(r.inn)) for r in df.dropna(subset=[src]).itertuples()}
                if df is not None and not df.empty else set())
    work, nopt = _keys(to_work), _keys(no_point)

    def _reasons(df):
        """Причина классификации по паре — фолбэк там, где LLM не сработала.

        Без неё строка «влиять нечем» стоит в блоке оттока вообще без объяснения:
        `insights` заполняет только аудит, а он идёт после и не по всем парам.
        """
        src = unit_src if (df is not None and unit_src in df) else "new_gosb_id"
        if df is None or df.empty or "reason" not in df:
            return {}
        return {(int(getattr(r, src)), int(r.inn)): str(r.reason or "")
                for r in df.dropna(subset=[src]).itertuples()}
    why_of = {**_reasons(to_work), **_reasons(no_point)}
    totals = {int(r.unit_id): r for r in gosb_gap.itertuples()}
    fmonths, fwindow = month_index(funnel_months, unit_src)
    # строки блоков собираются на грейне ЕДИНИЦЫ, а суммы портфеля — по сырым
    # строкам оттока и пайплайна
    by_unit = {int(k): v for k, v in _unit_grain(orgs_fc, unit_src)}

    skipped_no_base, group_mismatch = [], []
    for nid, g in orgs_fc.dropna(subset=[unit_src]).groupby(unit_src):
        nid = int(nid)
        t = totals.get(nid)
        if t is None:
            # единицы нет в витрине метрик — ни плана, ни базы. Такое бывает у
            # old_gosb_id, числящихся сразу под двумя tb_id (см. _GMAP): организации на
            # него мапятся, а метрики уходят в другой ТБ. Водопад без базы построить
            # нельзя, карточка для него всё равно не строится — пропускаем, но считаем.
            skipped_no_base.append((nid, float(forecast.num(g, "out_kept").sum())))
            continue
        pf = _portfolio_unit(t, g)
        rows = []
        for r in by_unit.get(nid, g.iloc[:0]).itertuples():
            k = (nid, int(r.inn))
            ins = insights.get(k, {})
            rows.append({
                "inn": int(r.inn), "name": names.get(k, f"Орг. {int(r.inn)}"),
                # ФИО закреплённого сотрудника: тем же ключом, что и у годового
                # тренда. Показывать его или нет, решает уже view — этими строками
                # живут ДВА блока (отток и пайплайн), а подпись нужна одному
                "emp": emp_of.get(k, ""),
                "out": float(r.out_kept), "ret": float(r.ret_qty),
                "gone": float(r.out_qty),
                "pipe": float(r.pipe_np_raw), "pipe_adj": float(r.pipe_np),
                "action": ins.get("action", ""),
                "yoy": yoy.get(k, 0.0), "cur": cur.get(k, 0.0),
                "months": list(getattr(r, "out_months", None) or []),
                "out_months": list(getattr(r, "out_months", None) or []),
                # вывод аудита, если он был; иначе — причина классификации, чтобы
                # строка «влиять нечем» не стояла в блоке вообще без объяснения
                "reason": ins.get("reason") or why_of.get(k, ""),
                "in_ref": ref.get(k, False),
                "zone": ("можно работать" if k in work else
                         "повлиять уже нельзя" if k in nopt
                         else "клиент не закреплён за банком"),
            })
        # порог > 0, а не >= 1: организации с долей человека тоже должны попасть
        # в хвост, иначе «названные + хвост» не сойдутся с итогом блока
        out_rows = [r for r in rows if r["out"] > 0]
        pipe_rows = [r for r in rows if r["pipe"] > 0]
        out_groups = _out_groups(out_rows, (dates or {}).get("closed_label", ""),
                                 (dates or {}).get("out_label", ""))
        # Группы «не вернулись» и «вернулись частично» ВМЕСТЕ обязаны покрывать блок
        # целиком: возврат либо был, либо нет, третьего не дано. Группа «свежие» с
        # ними пересекается намеренно и в проверку не входит.
        cover = [g for g in out_groups if g["key"] in ("kept", "partial")]
        g_fl = sum(g["fl"] for g in cover)
        g_n = sum(g["n"] for g in cover)
        blk_fl = sum(r["out"] for r in out_rows)
        if abs(g_fl - blk_fl) > 0.5 or g_n != len(out_rows):
            group_mismatch.append((nid, g_n, len(out_rows), g_fl, blk_fl))
        top_pipe, pipe_n, pipe_fl, pipe_cov = _material(pipe_rows, "pipe")
        # выросшие за год не показываем: блок отвечает на «почему просели»
        yoy_rows = _yoy_rows(nid, det_by_unit.get(nid, ()), rows, names, yoy, cur, ref,
                             insights, work, nopt, emp_of)
        yoy_groups = _yoy_groups(yoy_rows, nid, fmonths, fwindow)
        out[nid] = {
            # коэффициент ИМЕННО ЭТОГО ГОСБ; если своей истории мало, он ушёл на
            # коэффициент ТБ — тогда это подписывается в карточке явно
            "pf": pf, "conv": (conv_by_gosb or {}).get(nid, conv_tb),
            "conv_is_tb": nid not in (conv_by_gosb or {}),
            "conv_diag": conv_diag or {},
            "out_tot": float(sum(r["out"] for r in rows)),
            "out_n_all": len(out_rows), "out_groups": out_groups,
            "top_pipe": top_pipe, "pipe_tail_n": pipe_n, "pipe_tail_fl": pipe_fl,
            "pipe_cov": pipe_cov, "pipe_n_all": len(pipe_rows),
            "yoy_total": float(yoy_tot.get(nid, 0.0)), "yoy_groups": yoy_groups,
            "yoy_n_all": len(yoy_rows),
            "yoy_down_tot": float(sum(r["yoy"] for r in yoy_rows)),
        }
    if skipped_no_base:
        fl = sum(x[1] for x in skipped_no_base)
        progress.done(f"Пропущено ГОСБ без базы в витрине метрик: {len(skipped_no_base)} "
                      f"(отток {fl:.0f} чел) — их old_gosb_id числится под другим ТБ, "
                      f"карточки для них и так не строятся")
    if group_mismatch:
        nid, g_n, n_all, g_fl, blk_fl = group_mismatch[0]
        progress.done(f"ВНИМАНИЕ: группы оттока не покрывают блок у "
                      f"{len(group_mismatch)} ГОСБ (первый {nid}: {g_n} из {n_all} орг, "
                      f"{g_fl:.0f} из {blk_fl:.0f} чел) — часть оттока показана "
                      f"без причины")
    return out


def _unit_cards(gosb_gap: pd.DataFrame, matrix: pd.DataFrame, to_work: pd.DataFrame,
                fagg: pd.DataFrame, gosb_plan: dict,
                unit_src: str = "new_gosb_id") -> list:
    """По каждой единице (ГОСБ или ТБ) — что конкретно сделать, чтобы закрыть разрыв."""
    fg = {}
    if not fagg.empty and unit_src in fagg:
        for nid, g in fagg.dropna(subset=[unit_src]).groupby(unit_src):
            n_tasks = int(g.n_tasks.sum())
            fg[int(nid)] = {"act_n": n_tasks,
                            "success": float(g.n_success.sum() / n_tasks) if n_tasks else 0.0,
                            "worked_orgs": int(g.inn.nunique())}
    # Карточки строим по ВСЕМ единицам, включая выполняющие план: управляющему нужно
    # видеть и за счёт чего план вытягивается, а не только где провал. Сортировка по
    # недобору оставляет проблемные сверху.
    is_fail = (matrix["is_failing"] if "is_failing" in matrix
               else matrix["nedobor"] > 0)
    order = gosb_gap.sort_values("nedobor", ascending=False)

    cards = []
    for r in order.itertuples():
        nid = int(r.unit_id); name = r.unit_name
        sub = (to_work[to_work[unit_src] == nid]
               if not to_work.empty and unit_src in to_work else to_work)
        sel = sub[(sub.need_k > 0) & (sub.need_k <= 1.0)] if not sub.empty else sub
        # ВСЕ сегменты единицы: западающие первыми (по недобору), затем выполняющие
        # без ведущего подчёркивания: itertuples переименовывает такие колонки
        g_seg = matrix[matrix.unit_id == nid].copy()
        g_seg["fails"] = is_fail.reindex(g_seg.index).fillna(False)
        g_seg = g_seg.sort_values(["fails", "nedobor"], ascending=[False, False])
        p = gosb_plan.get(nid, {})
        plan_by_seg = {s["seg"]: s for s in p.get("segs", [])}
        segs = []
        for s in g_seg.itertuples():
            ps = plan_by_seg.get(s.seg_name, {})
            segs.append({
                "seg": s.seg_name, "exec": float(s.execution_percent or 0),
                "nedobor": float(s.nedobor), "failing": bool(s.fails),
                "plan": float(s.plan_amt), "forecast": float(s.fact_amt),
                "n_need": int(ps.get("n_need", 0)), "fl_need": float(ps.get("fl_need", 0.0)),
                "coverage": ps.get("coverage"), "n_avail": int(ps.get("n_avail", 0)),
            })
        bad = [s for s in segs if s["failing"]]
        cards.append({
            # имена ключей исторические (карточка родилась ГОСБ-центричной), но
            # содержат единицу текущего уровня — ГОСБ или ТБ
            "gosb_id": nid,
            "gosb_name": name, "exec": float(r.execution_percent), "gap": float(r.nedobor),
            "plan": float(r.plan_amt), "forecast": float(r.fact_amt),
            "rest": _rest_row(r, segs, int(p.get("n_need", 0))),
            # ГОСБ здоров, если и общий план выполняется, и западающих сегментов нет
            "healthy": float(r.nedobor) <= 0 and not bad,
            "seg_only": float(r.nedobor) <= 0 and bool(bad),   # план вытянут другими
            "segs": segs, "segs_bad": bad,
            "gap_seg": float(p.get("gap_seg", sum(s["nedobor"] for s in bad))),
            "n_need": int(p.get("n_need", 0)), "fl_need": float(p.get("fl_need", 0.0)),
            "fot_need": float(p.get("fot_need", 0.0)),
            "n_attract": int(p.get("n_attract", 0)), "fl_attract": float(p.get("fl_attract", 0.0)),
            "n_return": int(p.get("n_return", 0)), "fl_return": float(p.get("fl_return", 0.0)),
            "filler_n": int(p.get("filler_n", 0)), "filler_fl": float(p.get("filler_fl", 0.0)),
            "n_total": int(p.get("n_total", 0)), "fl_total": float(p.get("fl_total", 0.0)),
            "not_worked": int((~sel.worked).sum()) if not sel.empty else 0,
            "act": fg.get(nid, {"act_n": 0, "success": 0.0, "worked_orgs": 0}),
        })
    return cards
