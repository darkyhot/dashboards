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

    # --- Рекомендации: чек-лист -> ключевые слова -> LLM (кто нужен под план) ---
    gaps = {int(r.new_gosb_id): max(float(r.nedobor), 0.0) for r in gosb_gap.itertuples()}
    insights, to_work, no_point, themes, llm_stats = _resolve(
        ctx, e, pf, to_work, no_point, top_n, batch, gaps)

    # --- Отбор «ровно под план»: сколько организаций закрывает разрыв ГОСБ ---
    progress.step("Отбор организаций под план по каждому ГОСБ")
    to_work, gosb_plan = _select(to_work, gaps)
    sim = _plan_summary(to_work, gosb_plan, gap_rcp,
                        verdict["rcp"]["fact"], verdict["rcp"]["plan"])
    progress.done(f"Под план нужно {sim['k']} организаций (+{sim['closable']:.0f} чел); "
                  f"потенциал покрывает разрыв на {sim['coverage']*100:.0f}%")
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
             top_n: int, batch: int, gaps: dict):
    """Рекомендации по (ГОСБ, ИНН): детерминированно, затем LLM для остатка.

    Разбираем не «фиксированный топ», а тех, кто реально нужен под план: в каждом
    ГОСБ идём по убыванию эффекта, пока не наберём разрыв × PLAN_OVERSHOOT
    (запас на перевыполнение до +50%). top_n — потолок стоимости на ГОСБ.
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
        pool = _pool_per_gosb(to_work, top_n, processed_keys, gaps)
        if pool.empty:
            break
        processed_keys |= {(int(r.new_gosb_id), int(r.inn)) for r in pool.itertuples()}
        stats["cand"] += len(pool)
        progress.step(f"Рекомендации, волна {wave + 1}: кандидатов {len(pool)}"
                      f" (нужны под план ×{PLAN_OVERSHOOT}, потолок {top_n} на ГОСБ)")

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
                need_llm.append({
                    "gosb_id": key[0], "inn": key[1],
                    "gosb": str(getattr(r, "gosb_name", "") or ""),
                    "company": str(getattr(r, "company_name", "") or ""),
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
        if _enough(to_work, insights, top_n, gaps):
            break

    to_work, no_point = _reclassify(to_work, no_point, insights)
    return insights, to_work, no_point, text_rules.themes(all_texts), stats


def _rows_for_target(g: pd.DataFrame, target: float) -> int:
    """Сколько верхних строк (по убыванию эффекта) закрывают target получателей."""
    if target <= 0:
        return 0
    cum = g["impact_fl"].cumsum().to_numpy()
    for i, c in enumerate(cum, start=1):
        if c >= target:
            return i
    return len(g)


def _pool_per_gosb(to_work: pd.DataFrame, top_n: int, processed: set,
                   gaps: dict) -> pd.DataFrame:
    """Кандидаты волны: сверху вниз по эффекту, пока не наберётся план ×1.5."""
    parts = []
    for nid, g in to_work.groupby("new_gosb_id"):
        g = g.sort_values("impact_fl", ascending=False)
        target = gaps.get(int(nid), 0.0) * PLAN_OVERSHOOT
        need = min(max(_rows_for_target(g, target), 1), top_n)
        done_here = sum(1 for r in g.itertuples()
                        if (int(r.new_gosb_id), int(r.inn)) in processed)
        take = need - done_here
        if take <= 0:
            continue
        rest = g[[(int(r.new_gosb_id), int(r.inn)) not in processed for r in g.itertuples()]]
        if not rest.empty:
            parts.append(rest.head(take))
    return pd.concat(parts, ignore_index=True) if parts else to_work.iloc[:0]


def _enough(to_work: pd.DataFrame, insights: dict, top_n: int, gaps: dict) -> bool:
    """Хватает ли подтверждённых «работать», чтобы закрыть план ×1.5 в каждом ГОСБ."""
    for nid, g in to_work.groupby("new_gosb_id"):
        target = gaps.get(int(nid), 0.0) * PLAN_OVERSHOOT
        acc = 0.0; n = 0
        for r in g.sort_values("impact_fl", ascending=False).itertuples():
            if acc >= target or n >= top_n:
                break
            v = insights.get((int(r.new_gosb_id), int(r.inn)))
            if v is None:
                return False               # ещё не разобрали — нужна следующая волна
            n += 1
            if v.get("verdict", "work") == "work":
                acc += float(r.impact_fl)
    return True


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
def _select(to_work: pd.DataFrame, gaps: dict) -> tuple[pd.DataFrame, dict]:
    """Отбор «ровно под план» внутри каждого ГОСБ.

    Рычаг не важен: привлечение и возврат в одном списке, сортировка по эффекту в
    получателях. Идём сверху вниз и набираем, пока разрыв ГОСБ не закрыт. Строка
    попадает в цель k, если накопленное ДО неё меньше разрыва × k — так последняя
    организация закрывает остаток, а перебор не превышает одну организацию.
    """
    if to_work.empty:
        for c in ["cum", "cum_prev", "gosb_gap", "rank_in_gosb"]:
            to_work[c] = 0.0
        to_work["need_100"] = False
        return to_work, {}

    parts = []
    for nid, g in to_work.groupby("new_gosb_id"):
        g = g.sort_values("impact_fl", ascending=False).copy()
        g["cum"] = g["impact_fl"].cumsum()
        g["cum_prev"] = g["cum"] - g["impact_fl"]
        g["gosb_gap"] = gaps.get(int(nid), 0.0)
        g["rank_in_gosb"] = range(1, len(g) + 1)
        parts.append(g)
    out = pd.concat(parts, ignore_index=True)
    out["need_100"] = out["cum_prev"] < out["gosb_gap"]
    out = out.sort_values(["gosb_gap", "rank_in_gosb"], ascending=[False, True],
                          ignore_index=True)

    plan: dict = {}
    for nid, g in out.groupby("new_gosb_id"):
        gap = float(g["gosb_gap"].iloc[0])
        sel = g[g.need_100]
        att = sel[sel.lever == "Привлечь"]; ret = sel[sel.lever == "Вернуть"]
        total = float(g["impact_fl"].sum())
        plan[int(nid)] = {
            "gap": gap, "n_need": int(len(sel)),
            "fl_need": float(sel["impact_fl"].sum()),
            "fot_need": float(sel["impact_fot_mln"].sum()),
            "n_attract": int(len(att)), "fl_attract": float(att["impact_fl"].sum()),
            "n_return": int(len(ret)), "fl_return": float(ret["impact_fl"].sum()),
            "n_total": int(len(g)), "fl_total": total,
            "coverage": (total / gap) if gap > 0 else None,
            "not_worked": int((~g["worked"]).sum()) if "worked" in g else 0,
        }
    return out, plan


def _plan_summary(to_work: pd.DataFrame, gosb_plan: dict, gap: float,
                  fact: float, plan_amt: float) -> dict:
    """Итог по ТБ: сколько организаций нужно суммарно и что это даёт."""
    gap_sum = sum(p["gap"] for p in gosb_plan.values()) or gap
    total = float(to_work["impact_fl"].sum()) if not to_work.empty else 0.0
    sel = to_work[to_work.need_100] if not to_work.empty else to_work
    return {
        "gap": gap, "gap_gosb": gap_sum, "fact": fact, "plan": plan_amt,
        "k": int(len(sel)),
        "closable": float(sel["impact_fl"].sum()) if len(sel) else 0.0,
        "attract": float(sel[sel.lever == "Привлечь"]["impact_fl"].sum()) if len(sel) else 0.0,
        "retention": float(sel[sel.lever == "Вернуть"]["impact_fl"].sum()) if len(sel) else 0.0,
        "fot_mln": float(sel["impact_fot_mln"].sum()) if len(sel) else 0.0,
        "coverage": (total / gap_sum) if gap_sum else 0.0, "total_potential": total,
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
    cards = []
    prob = gosb_gap[gosb_gap.nedobor > 0].sort_values("nedobor", ascending=False)
    for r in prob.itertuples():
        nid = int(r.new_gosb_id); name = r.gosb_name
        sub = to_work[to_work.new_gosb_id == nid] if not to_work.empty else to_work
        sel = sub[sub.need_100] if not sub.empty else sub
        seg_bad = (matrix[(matrix.new_gosb_id == nid) & (matrix.nedobor > 0)]
                   .sort_values("nedobor", ascending=False))
        segs = [{"seg": s.seg_name, "exec": float(s.execution_percent),
                 "nedobor": float(s.nedobor),
                 "n_work": int(len(sel[sel.seg_name == s.seg_name])) if not sel.empty else 0}
                for s in seg_bad.itertuples()]
        p = gosb_plan.get(nid, {})
        cards.append({
            "gosb_name": name, "exec": float(r.execution_percent), "gap": float(r.nedobor),
            "segs": segs,
            "n_need": int(p.get("n_need", 0)), "fl_need": float(p.get("fl_need", 0.0)),
            "fot_need": float(p.get("fot_need", 0.0)),
            "n_attract": int(p.get("n_attract", 0)), "fl_attract": float(p.get("fl_attract", 0.0)),
            "n_return": int(p.get("n_return", 0)), "fl_return": float(p.get("fl_return", 0.0)),
            "n_total": int(p.get("n_total", 0)), "fl_total": float(p.get("fl_total", 0.0)),
            "coverage": p.get("coverage"),
            "not_worked": int((~sel.worked).sum()) if not sel.empty else 0,
            "act": fg.get(nid, {"act_n": 0, "success": 0.0, "worked_orgs": 0}),
        })
    return cards
