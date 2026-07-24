"""Бизнес-логика дэша tb_health.

Грейн работы с клиентом — (ГОСБ, ИНН): одна организация может обслуживаться в
нескольких ГОСБ, и в каждом своя история отработки. Все агрегаты воронки считаются
по ВСЕМ активностям за 3 месяца (не по последней задаче).

Рекомендации разрешаются в порядке: чек-лист (причина оттока) -> ключевые слова ->
LLM (только там, где есть содержательный текст и правила не сработали) -> правило.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ... import progress
from ...db import read_sql
from . import prompts, queries as Q, segments, text_rules

RUB_TO_MLN = 1e6
LLM_TOP_N_DEFAULT = 20      # потолок: сколько организаций разбирать LLM в одном ГОСБ
LLM_BATCH_DEFAULT = 30      # организаций в одном запросе к LLM
PLAN_OVERSHOOT = 1.5        # разбираем с запасом на перевыполнение плана до +50%
MAX_WAVES = 3               # сколько раз добираем, если запаса не хватило
PLAN_TARGETS = (1.0, 1.2, 1.5)   # цели в дэше: выполнить план / +20% / +50%
# Сегмент западает, если план не выполнен (exec < 1) — тот же признак, что даёт
# красную ячейку в тепловой карте (render.components.heat_bg). Порог в одного
# получателя отсекает только шум округления.
MIN_SEG_GAP = 1.0


def _failing_seg(nedobor, execution_percent=None) -> bool:
    return float(nedobor or 0) >= MIN_SEG_GAP


@dataclass
class Analysis:
    tb_short: str
    tb_id: int
    tb_full: str
    ref_date: str
    verdict: dict
    gap_rcp: float
    gap_fot: float
    gap_fot_mln: float
    matrix: pd.DataFrame
    gosb_gap: pd.DataFrame
    top_cells: pd.DataFrame
    attract: pd.DataFrame
    retention: pd.DataFrame
    activity: dict
    to_work: pd.DataFrame
    no_point: pd.DataFrame
    sim: dict
    gosb_plan: dict = field(default_factory=dict)   # new_gosb_id -> потребность под план
    insights: dict = field(default_factory=dict)   # (gosb_id, inn) -> reason/action/…
    themes: str = "—"
    llm_stats: dict = field(default_factory=dict)


def run(ctx, tb_short: str) -> Analysis:
    e = ctx.engine
    top_n = int(ctx.params.get("llm_top_n", LLM_TOP_N_DEFAULT))
    batch = int(ctx.params.get("llm_batch", LLM_BATCH_DEFAULT))

    progress.step(f"Резолв ТБ «{tb_short}»")
    tb = read_sql(e, Q.TB_RESOLVE, {"tb": tb_short})
    if tb.empty:
        raise ValueError(f"ТБ '{tb_short}' не найден в справочнике")
    tb_id = int(tb.tb_id.iloc[0]); tb_full = str(tb.tb_full_name.iloc[0])

    # --- Опорный месяц (метрики/витрина) и месяц задач (ref + 1) ---
    date_param = ctx.params.get("date")
    if date_param:
        ref = (pd.to_datetime(date_param) + pd.offsets.MonthEnd(0)).date()
        progress.done(f"Опорный месяц (задан): {ref}")
    else:
        ref = pd.to_datetime(read_sql(e, Q.REF_DATE).iloc[0, 0]).date()
        progress.done(f"Опорный месяц (авто, макс. report_dt): {ref}")
    # Задачи по метрикам идут месяцем позже -> воронка за месяц T = ref + 1.
    # Окно — 3 календарных месяца: с первого дня месяца T-2 по конец месяца T.
    ref_funnel = (pd.Timestamp(ref) + pd.offsets.MonthEnd(1)).date()
    funnel_from = (pd.Timestamp(ref_funnel).to_period("M") - 2).to_timestamp().date()
    months = ", ".join(
        (pd.Timestamp(ref_funnel).to_period("M") - k).strftime("%m.%Y") for k in (2, 1, 0))
    progress.done(f"Окно задач воронки: {funnel_from} … {ref_funnel} ({months})")
    # Сделки судим по дате СОЗДАНИЯ СДЕЛКИ: заведённые в двух последних месяцах окна
    # ещё не могли дать зачисления, по ним недоработку не считаем.
    fresh_from = (pd.Timestamp(ref_funnel).to_period("M") - 1).to_timestamp().date()
    progress.done(f"Сделки: созданные с {fresh_from} — свежие (организация уже в работе); "
                  f"недоработку считаем только по сделкам, созданным раньше")
    p = {"m_fot": Q.METRIC_FOT, "m_rcp": Q.METRIC_RECIPIENTS, "ref": ref}
    pf = {"tb_id": tb_id, "ref_funnel": ref_funnel, "funnel_from": funnel_from,
          "fresh_from": fresh_from}

    # --- Вердикт + ранги ---
    progress.step("Вердикт по ТБ + ранги (ФОТ, получатели)")
    v = read_sql(e, Q.TB_VERDICT, p)
    verdict, ref_date = _verdict(v, tb_id)
    gap_rcp = max(0.0, verdict["rcp"]["plan"] - verdict["rcp"]["fact"])
    gap_fot = max(0.0, verdict["fot"]["plan"] - verdict["fot"]["fact"])

    # --- ГОСБ×сегмент ---
    progress.step("Матрица ГОСБ × сегмент + разрыв по ГОСБ")
    matrix = read_sql(e, Q.GOSB_SEG, {**p, "tb_id": tb_id})
    matrix["seg_name"] = matrix["seg_id"].map(segments.short)
    gosb_gap = read_sql(e, Q.GOSB_TOTALS, {**p, "tb_id": tb_id})
    top_cells = (matrix[matrix.nedobor > 0]
                 .sort_values("nedobor", ascending=False)
                 .assign(share=lambda d: d.nedobor / max(gap_rcp, 1))
                 .head(8))

    # --- Организации ---
    progress.step("Витрина организаций (потенциал/отток)")
    orgs = read_sql(e, Q.ORGS, {"tb_id": tb_id, "ref": ref})

    # --- Активности: агрегат по (ГОСБ, ИНН) по ВСЕМ задачам за 3 мес ---
    progress.step("Активности воронки за 3 мес: агрегат по (ГОСБ, ИНН)")
    fagg = read_sql(e, Q.FUNNEL_AGG, pf)
    activity = _activity(read_sql(e, Q.ACTIVITY_TOTALS, pf),
                         read_sql(e, Q.ACTIVITY_BREAKDOWN, pf))
    progress.done(f"задач {activity.get('n', 0)} → пар (ГОСБ,ИНН) {len(fagg)}"
                  f" · из них с текстом {int(fagg['any_text'].sum()) if len(fagg) else 0}")

    orgs = _merge_funnel(orgs, fagg)
    orgs["fot_potential_mln"] = orgs["fot_potential_amt"] / RUB_TO_MLN
    orgs["fot_outflow_mln"] = orgs["fot_outflow_amt"] / RUB_TO_MLN
    orgs["seg_name"] = orgs["segment_big"].map(segments.short_of_big).fillna("—")
    orgs["company_name"] = orgs["company_name"].fillna("")

    attract = (orgs[orgs.emp_potential_qty >= 1]
               .sort_values("emp_potential_qty", ascending=False).head(15).copy())
    retention = (orgs[orgs.fl_outflow_qty >= 1]
                 .sort_values("fl_outflow_qty", ascending=False).head(15).copy())

    # --- Классификация по агрегатам (все активности) ---
    progress.step("Классификация (ГОСБ,ИНН): работать / нет смысла")
    cand = orgs[(orgs.emp_potential_qty >= 1) | (orgs.fl_outflow_qty >= 1)].copy()
    cand["impact_fl"] = cand[["emp_potential_qty", "fl_outflow_qty"]].max(axis=1)
    cand["lever"] = ["Привлечь" if a >= b else "Вернуть"
                     for a, b in zip(cand.emp_potential_qty, cand.fl_outflow_qty)]
    cand["impact_fot_mln"] = [
        (pot if lev == "Привлечь" else out) / RUB_TO_MLN
        for lev, pot, out in zip(cand.lever, cand.fot_potential_amt, cand.fot_outflow_amt)
    ]
    to_work, no_point = _classify(cand)

    # --- Разрывы на грейне (ГОСБ, сегмент): работаем именно с западающими ---
    matrix["is_failing"] = [_failing_seg(r.nedobor, r.execution_percent)
                            for r in matrix.itertuples()]
    seg_gaps = {(int(r.new_gosb_id), r.seg_name): float(r.nedobor)
                for r in matrix.itertuples() if r.is_failing}
    n_gosb_seg = len({nid for nid, _ in seg_gaps})
    progress.done(f"Западающих (ГОСБ, сегмент): {len(seg_gaps)} в {n_gosb_seg} ГОСБ")

    # --- Рекомендации: чек-лист -> ключевые слова -> LLM (кто нужен под план) ---
    insights, to_work, no_point, themes, llm_stats = _resolve(
        ctx, e, pf, to_work, no_point, top_n, batch, seg_gaps)

    # --- Отбор «ровно под план»: закрываем разрыв КАЖДОГО западающего сегмента ---
    progress.step("Отбор организаций под план по (ГОСБ, сегмент)")
    to_work, gosb_plan = _select(to_work, seg_gaps)
    sim = _plan_summary(to_work, gosb_plan, gap_rcp,
                        verdict["rcp"]["fact"], verdict["rcp"]["plan"])
    progress.done(f"Под план нужно {sim['k']} организаций (+{sim['closable']:.0f} чел); "
                  f"потенциал западающих сегментов покрывает разрыв на "
                  f"{sim['coverage']*100:.0f}% · добор из других сегментов: {sim['filler_n']}")
    progress.step("Разрез по проблемным ГОСБ")
    gosb_cards = _gosb_cards(gosb_gap, matrix, to_work, fagg, gosb_plan)

    a = Analysis(
        tb_short=tb_short, tb_id=tb_id, tb_full=tb_full, ref_date=ref_date,
        verdict=verdict, gap_rcp=gap_rcp, gap_fot=gap_fot, gap_fot_mln=gap_fot / RUB_TO_MLN,
        matrix=matrix, gosb_gap=gosb_gap, top_cells=top_cells,
        attract=attract, retention=retention, activity=activity,
        to_work=to_work, no_point=no_point, sim=sim, gosb_plan=gosb_plan,
        insights=insights, themes=themes, llm_stats=llm_stats,
    )
    a.gosb_cards = gosb_cards
    return a


# --------------------------------------------------------------------------- #
def _merge_funnel(orgs: pd.DataFrame, fagg: pd.DataFrame) -> pd.DataFrame:
    """Приклеить агрегат воронки на грейне (ГОСБ, ИНН)."""
    num_cols = ["n_tasks", "n_calls", "n_meetings", "n_success", "n_overdue", "n_outflow",
                "plan_deal", "fact_deal", "plan_deal_old", "fact_deal_old", "unrealized"]
    bool_cols = ["any_success", "any_text", "has_fresh_deal"]
    if fagg.empty:
        for c in num_cols:
            orgs[c] = 0
        for c in bool_cols:
            orgs[c] = False
        orgs["fresh_deal_dt"] = pd.NaT
        orgs["worked"] = False
        return orgs
    fagg = fagg.copy()
    fagg["new_gosb_id"] = fagg["new_gosb_id"].astype("Int64")
    orgs["new_gosb_id"] = orgs["new_gosb_id"].astype("Int64")
    orgs = orgs.merge(fagg, on=["new_gosb_id", "inn"], how="left")
    orgs["worked"] = orgs["n_tasks"].notna() & (orgs["n_tasks"].fillna(0) > 0)
    for c in num_cols:
        orgs[c] = orgs[c].fillna(0).astype(int)
    for c in bool_cols:
        orgs[c] = orgs[c].fillna(False).astype(bool)
    return orgs


def _activity(totals: pd.DataFrame, breakdown: pd.DataFrame) -> dict:
    if totals.empty or not int(totals.n.iloc[0] or 0):
        return {"n": 0}
    t = totals.iloc[0]
    def _dim(name):
        d = breakdown[breakdown.dim == name]
        return {str(r.k): int(r.n) for r in d.itertuples()}
    return {
        "n": int(t.n), "orgs": int(t.orgs),
        "calls": int(t.calls or 0), "meetings": int(t.meetings or 0),
        "success_rate": float(t.success_rate or 0), "overdue": int(t.overdue or 0),
        "plan_deal": int(t.plan_deal or 0), "fact_deal": int(t.fact_deal or 0),
        "unrealized": int(t.unrealized or 0),
        "by_role": _dim("role"), "by_type": _dim("type"), "by_status": _dim("status"),
    }


def _verdict(v: pd.DataFrame, tb_id: int) -> tuple[dict, str]:
    out = {}; ref = ""
    for key, mid in (("rcp", Q.METRIC_RECIPIENTS), ("fot", Q.METRIC_FOT)):
        row = v[(v.metric_id == mid) & (v.tb_id == tb_id)]
        if row.empty:
            out[key] = {"plan": 0, "fact": 0, "exec": None, "rank": None, "n_tb": None}
            continue
        r = row.iloc[0]; ref = str(r.end_dt)
        out[key] = {"plan": float(r.plan_amt), "fact": float(r.fact_amt),
                    "exec": float(r.execution_percent), "rank": int(r.rnk),
                    "n_tb": int(r.n_tb)}
    return out, ref


def _classify(cand: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """ОДИН вывод «работать / не работать» по ВСЕМ активностям за 3 мес.

    Сделки оцениваются по дате создания сделки: свежая сделка означает, что
    организация уже в работе и зачисления просто не успели пройти — такие в список
    к отработке не берём. Недоработку считаем только по «старым» сделкам.
    """
    work_rows, skip_rows = [], []
    for _, o in cand.iterrows():
        fresh_dt = o.get("fresh_deal_dt")
        when = f" с {pd.Timestamp(fresh_dt):%m.%Y}" if pd.notna(fresh_dt) else ""
        if bool(o.get("has_fresh_deal", False)):
            skip_rows.append((o, f"Сделка в работе{when} — ждём зачислений"))
        elif not bool(o.get("worked", False)):
            work_rows.append((o, "Не работали за 3 мес — начать отработку"))
        elif int(o.get("fact_deal_old", 0)) < int(o.get("plan_deal_old", 0)):
            work_rows.append((o, f"Недоработка по сделке: {int(o['fact_deal_old'])} "
                                 f"из {int(o['plan_deal_old'])} получателей"))
        elif int(o.get("n_overdue", 0)) > 0:
            work_rows.append((o, f"Просроченных задач: {int(o['n_overdue'])} — вернуть в работу"))
        elif bool(o.get("any_success", False)):
            skip_rows.append((o, "Отработана успешно — потенциал реализуется"))
        else:
            work_rows.append((o, "Отработана без результата — повторная активность"))

    def _mk(rows):
        if not rows:
            return pd.DataFrame(columns=list(cand.columns) + ["reason"])
        recs = []
        for o, reason in rows:
            d = o.to_dict(); d["reason"] = reason; recs.append(d)
        return pd.DataFrame(recs).sort_values("impact_fl", ascending=False)
    return _mk(work_rows), _mk(skip_rows)


# --------------------------------------------------------------------------- #
def _resolve(ctx, engine, pf: dict, to_work: pd.DataFrame, no_point: pd.DataFrame,
             top_n: int, batch: int, seg_gaps: dict):
    """Рекомендации по (ГОСБ, ИНН): детерминированно, затем LLM для остатка.

    Разбираем не «фиксированный топ», а тех, кто реально нужен под план западающих
    сегментов (тот же `_mark_needed` с запасом ×PLAN_OVERSHOOT). Строки, уже
    получившие вердикт «не работать», из расчёта выбывают — их место занимают
    следующие, поэтому нужна следующая волна. top_n — потолок стоимости на ГОСБ.
    """
    stats = {"top_n": top_n, "batch": batch, "cand": 0, "checklist": 0,
             "keyword": 0, "llm": 0, "no_text": 0, "fallback": 0, "batches": 0}
    if to_work.empty:
        return {}, to_work, no_point, "—", stats

    insights: dict = {}
    resolved_keys: set = set()
    all_texts: list[str] = []
    processed_keys: set = set()

    for wave in range(MAX_WAVES):
        pool = _pool_needed(to_work, seg_gaps, insights, top_n, processed_keys)
        if pool.empty:
            break
        processed_keys |= {(int(r.new_gosb_id), int(r.inn)) for r in pool.itertuples()}
        stats["cand"] += len(pool)
        progress.step(f"Рекомендации, волна {wave + 1}: кандидатов {len(pool)}"
                      f" (нужны под план западающих сегментов ×{PLAN_OVERSHOOT},"
                      f" потолок {top_n} на ГОСБ)")

        # текст только по кандидатам этой волны
        inns = sorted({int(x) for x in pool["inn"]})
        text_df = read_sql(engine, Q.FUNNEL_TEXT, {**pf, "inns": inns})
        notes = _collect_notes(text_df, pool, all_texts)

        need_llm = []
        for r in pool.itertuples():
            key = (int(r.new_gosb_id), int(r.inn))
            item_notes = notes.get(key, [])
            det = _deterministic(item_notes)
            if det:
                insights[key] = det
                stats["checklist" if det["source"].startswith("чек-лист") else "keyword"] += 1
            elif item_notes:
                # названия ГОСБ и компаний в LLM не передаём — только id и сегмент
                need_llm.append({
                    "gosb_id": key[0], "inn": key[1],
                    "segment": str(getattr(r, "seg_name", "") or ""),
                    "notes": item_notes,
                })
            else:
                # нет содержательного текста -> остаётся причина из правил (_classify)
                insights[key] = {"reason": "", "action": "", "verdict": "work",
                                 "source": "правило"}
                stats["no_text"] += 1

        if need_llm:
            got, nb = prompts.text_insights(ctx, need_llm, batch=batch)
            stats["batches"] += nb
            for it in need_llm:
                key = (it["gosb_id"], it["inn"])
                if key in got:
                    insights[key] = got[key]; stats["llm"] += 1
                else:   # LLM не вернул -> детерминированный фолбэк по тексту
                    insights[key] = _fallback_insight(it["notes"])
                    stats["fallback"] += 1

        resolved_keys |= processed_keys

    to_work, no_point = _reclassify(to_work, no_point, insights)
    return insights, to_work, no_point, text_rules.themes(all_texts), stats


def _pool_needed(to_work: pd.DataFrame, seg_gaps: dict, insights: dict,
                 top_n: int, processed: set) -> pd.DataFrame:
    """Кандидаты волны: кто нужен под план западающих сегментов и ещё не разобран.

    Строки, уже признанные «не работать», из расчёта исключаем — тогда следующая
    волна автоматически подтягивает тех, кто занял их место.
    """
    alive = to_work[[
        (insights.get((int(r.new_gosb_id), int(r.inn))) or {}).get("verdict", "work") != "no_point"
        for r in to_work.itertuples()]]
    if alive.empty:
        return to_work.iloc[:0]
    chosen, _, _ = _mark_needed(alive, seg_gaps, PLAN_OVERSHOOT)
    if not chosen:
        return to_work.iloc[:0]
    pool = alive.loc[sorted(chosen)].sort_values("impact_fl", ascending=False)
    pool = pool.groupby("new_gosb_id", group_keys=False).head(top_n)   # потолок стоимости
    pool = pool[[(int(r.new_gosb_id), int(r.inn)) not in processed for r in pool.itertuples()]]
    return pool


def _deterministic(notes: list[dict]) -> dict | None:
    """Детерминированное разрешение. None — нужен LLM.

    Порядок: 1) причина оттока из ЧЕК-ЛИСТА (структурный ответ);
             2) ключевые слова в КОММЕНТАРИИ;
             3) ключевые слова в ОТВЕТАХ чек-листа.
    Важно: по сырому тексту анкеты не матчим — там формулировки ВОПРОСОВ
    (напр. «Получено согласие» с ответом «Нет») дают ложные срабатывания.
    """
    for n in notes:
        det = text_rules.outflow_reason(n.get("questionnaire"))
        if det:
            return det
    for n in notes:
        det = text_rules.match_keyword(n.get("comment"))
        if det:
            return det
    for n in notes:
        answers = " ".join(text_rules.parse_questionnaire(n.get("questionnaire")).values())
        det = text_rules.match_keyword(answers)
        if det:
            return det
    return None


def _fallback_insight(notes: list[dict]) -> dict:
    """Если LLM не ответил — разрешаем теми же детерминированными правилами.

    Порядок тот же, что и до LLM (чек-лист -> ключевые слова), плюс последняя
    попытка по склейке всех заметок. notes — список СЛОВАРЕЙ, поэтому текст
    собираем через prompts.notes_text (иначе " ".join падает на dict).
    """
    det = _deterministic(notes)
    if det:
        return det
    fb = text_rules.match_keyword(prompts.notes_text(notes))
    return fb or {"reason": "", "action": "", "verdict": "work", "source": "правило"}


def _collect_notes(text_df: pd.DataFrame, pool: pd.DataFrame, sink: list) -> dict:
    """Содержательные заметки по каждой паре (ГОСБ, ИНН): ВСЕ активности за 3 мес."""
    out: dict = {}
    if text_df.empty:
        return out
    keys = {(int(r.new_gosb_id), int(r.inn)) for r in pool.itertuples()}
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
        if not (ok_c or ok_q):
            why = why_c if comment else why_q
            skipped[why] = skipped.get(why, 0) + 1
            continue
        text = f"[{r.task_type}/{r.task_text_status}] "
        if ok_c:
            text += comment
        if ok_q:
            text += " | анкета: " + quest.replace("\n", "; ")
        out.setdefault(key, []).append({"text": text[:400], "comment": comment,
                                        "questionnaire": quest})
        sink.append(comment or text)
    if skipped and progress.SHOW_LLM:
        for why, n in sorted(skipped.items(), key=lambda x: -x[1])[:6]:
            progress.done(f"отсеяно без LLM ×{n}: {why}")
    # ограничим объём промпта: до 8 записей на организацию (самые свежие — сверху)
    return {k: [x for x in v][:8] for k, v in out.items()}


def _reclassify(to_work: pd.DataFrame, no_point: pd.DataFrame, insights: dict):
    """Вердикт no_point (напр. ликвидация) переносит строку в «нет смысла»."""
    if to_work.empty:
        return to_work, no_point
    move_mask = []
    for r in to_work.itertuples():
        v = insights.get((int(r.new_gosb_id), int(r.inn)))
        move_mask.append(bool(v and v.get("verdict") == "no_point"))
    move_mask = pd.Series(move_mask, index=to_work.index)
    moved = to_work[move_mask].copy()
    if not moved.empty:
        moved["reason"] = [insights[(int(r.new_gosb_id), int(r.inn))]["reason"]
                           for r in moved.itertuples()]
        no_point = pd.concat([no_point, moved], ignore_index=True)
        to_work = to_work[~move_mask].copy()
        progress.done(f"перенесено в «нет смысла» по тексту: {len(moved)}")
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


def _gosb_cards(gosb_gap: pd.DataFrame, matrix: pd.DataFrame, to_work: pd.DataFrame,
                fagg: pd.DataFrame, gosb_plan: dict) -> list:
    """По каждому проблемному ГОСБ — что конкретно сделать, чтобы закрыть разрыв."""
    fg = {}
    if not fagg.empty:
        for nid, g in fagg.dropna(subset=["new_gosb_id"]).groupby("new_gosb_id"):
            n_tasks = int(g.n_tasks.sum())
            fg[int(nid)] = {"act_n": n_tasks,
                            "success": float(g.n_success.sum() / n_tasks) if n_tasks else 0.0,
                            "worked_orgs": int(g.inn.nunique())}
    # Карточку строим и для ГОСБ, ВЫПОЛНЯЮЩЕГО общий план: если внутри есть
    # западающий сегмент, это тоже проблема — просто её вытягивают другие сегменты.
    failing = matrix[matrix["is_failing"]] if "is_failing" in matrix else \
        matrix[matrix.nedobor > 0]
    seg_bad_ids = set(failing["new_gosb_id"].astype(int))
    prob = gosb_gap[(gosb_gap.nedobor > 0)
                    | (gosb_gap.new_gosb_id.astype(int).isin(seg_bad_ids))]
    prob = prob.sort_values("nedobor", ascending=False)

    cards = []
    for r in prob.itertuples():
        nid = int(r.new_gosb_id); name = r.gosb_name
        sub = to_work[to_work.new_gosb_id == nid] if not to_work.empty else to_work
        sel = sub[(sub.need_k > 0) & (sub.need_k <= 1.0)] if not sub.empty else sub
        seg_bad = (failing[failing.new_gosb_id == nid]
                   .sort_values("nedobor", ascending=False))
        p = gosb_plan.get(nid, {})
        plan_by_seg = {s["seg"]: s for s in p.get("segs", [])}
        segs = []
        for s in seg_bad.itertuples():
            ps = plan_by_seg.get(s.seg_name, {})
            segs.append({
                "seg": s.seg_name, "exec": float(s.execution_percent),
                "nedobor": float(s.nedobor),
                "n_need": int(ps.get("n_need", 0)), "fl_need": float(ps.get("fl_need", 0.0)),
                "coverage": ps.get("coverage"), "n_avail": int(ps.get("n_avail", 0)),
            })
        cards.append({
            "gosb_name": name, "exec": float(r.execution_percent), "gap": float(r.nedobor),
            "seg_only": float(r.nedobor) <= 0,     # план в целом выполняется
            "segs": segs, "gap_seg": float(p.get("gap_seg", sum(s["nedobor"] for s in segs))),
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
