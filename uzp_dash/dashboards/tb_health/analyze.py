"""Бизнес-логика дэша tb_health.

Собирает данные запросами, считает разрыв до плана, раскладывает провалы по
ГОСБ×сегмент, классифицирует организации по рычагу (привлечь/вернуть) и по
результату активностей (работать / нет смысла), строит симуляцию закрытия плана
и готовит контекст для LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ...db import read_sql
from . import queries as Q
from . import segments

RUB_TO_MLN = 1e6


@dataclass
class Analysis:
    tb_short: str
    tb_id: int
    tb_full: str
    ref_date: str
    verdict: dict                      # {'rcp': {...}, 'fot': {...}}
    gap_rcp: float                     # недобор получателей (чел)
    gap_fot: float                     # недобор ФОТ (млн ₽)
    matrix: pd.DataFrame               # ГОСБ×сегмент (получатели)
    gosb_gap: pd.DataFrame             # разрыв по ГОСБ (все сегменты)
    top_cells: pd.DataFrame            # топ провальных ячеек
    attract: pd.DataFrame              # организации к привлечению
    retention: pd.DataFrame            # организации к возврату
    activity: dict                     # агрегаты активностей за 3 мес
    to_work: pd.DataFrame              # приоритет «работать»
    no_point: pd.DataFrame             # «нет смысла» + причина
    sim: dict                          # симуляция закрытия плана
    priority_text: list = field(default_factory=list)  # тексты для LLM по орг
    gosb_cards: list = field(default_factory=list)     # разрез по проблемным ГОСБ


def run(ctx, tb_short: str) -> Analysis:
    e = ctx.engine
    p = {"m_fot": Q.METRIC_FOT, "m_rcp": Q.METRIC_RECIPIENTS}

    tb = read_sql(e, Q.TB_RESOLVE, {"tb": tb_short})
    if tb.empty:
        raise ValueError(f"ТБ '{tb_short}' не найден в справочнике")
    tb_id = int(tb.tb_id.iloc[0]); tb_full = str(tb.tb_full_name.iloc[0])

    # --- Вердикт + ранги ---
    v = read_sql(e, Q.TB_VERDICT, p)
    verdict, ref_date = _verdict(v, tb_id)
    gap_rcp = max(0.0, verdict["rcp"]["plan"] - verdict["rcp"]["fact"])
    gap_fot = max(0.0, verdict["fot"]["plan"] - verdict["fot"]["fact"])

    # --- ГОСБ×сегмент (короткие имена сегментов, хардкод) ---
    matrix = read_sql(e, Q.GOSB_SEG, {**p, "tb_id": tb_id})
    matrix["seg_name"] = matrix["seg_id"].map(segments.short)
    gosb_gap = read_sql(e, Q.GOSB_TOTALS, {**p, "tb_id": tb_id})
    top_cells = (matrix[matrix.nedobor > 0]
                 .sort_values("nedobor", ascending=False)
                 .assign(share=lambda d: d.nedobor / max(gap_rcp, 1))
                 .head(8))

    # --- Организации + активности ---
    orgs = read_sql(e, Q.ORGS, {"tb_id": tb_id})
    funnel = read_sql(e, Q.FUNNEL_TB, {"tb_id": tb_id})
    fagg = _funnel_by_inn(funnel)
    activity = _activity_totals(funnel)

    orgs = orgs.merge(fagg, on="inn", how="left")
    orgs["worked"] = orgs["n_tasks"].notna()
    orgs["n_tasks"] = orgs["n_tasks"].fillna(0).astype(int)
    orgs["fot_potential_mln"] = orgs["fot_potential_amt"] / RUB_TO_MLN
    orgs["fot_outflow_mln"] = orgs["fot_outflow_amt"] / RUB_TO_MLN
    # большое имя сегмента организации -> короткое (для отчёта)
    orgs["seg_name"] = orgs["segment_big"].map(segments.short_of_big).fillna("—")
    orgs["company_name"] = orgs["company_name"].fillna("")

    # рычаг: привлечь vs вернуть (по доминирующему потенциалу в людях)
    attract = (orgs[orgs.emp_potential_qty >= 1]
               .sort_values("emp_potential_qty", ascending=False)
               .head(15).copy())
    retention = (orgs[orgs.fl_outflow_qty >= 1]
                 .sort_values("fl_outflow_qty", ascending=False)
                 .head(15).copy())

    # --- Классификация работать / нет смысла (кандидаты с рычагом) ---
    cand = orgs[(orgs.emp_potential_qty >= 1) | (orgs.fl_outflow_qty >= 1)].copy()
    cand["impact_fl"] = cand[["emp_potential_qty", "fl_outflow_qty"]].max(axis=1)
    cand["lever"] = ["Привлечь" if a >= b else "Вернуть"
                     for a, b in zip(cand.emp_potential_qty, cand.fl_outflow_qty)]
    cand["impact_fot_mln"] = [
        (pot if lev == "Привлечь" else out) / RUB_TO_MLN
        for lev, pot, out in zip(cand.lever, cand.fot_potential_amt, cand.fot_outflow_amt)
    ]
    to_work, no_point = _classify(cand)

    # --- Симуляция закрытия плана (получатели — главная) ---
    sim = _simulate(to_work, gap_rcp, verdict["rcp"]["fact"], verdict["rcp"]["plan"])

    # --- Разрез по проблемным ГОСБ (что сделать в каждом) ---
    gosb_cards = _gosb_cards(gosb_gap, matrix, to_work, funnel)

    # --- Тексты для LLM: только по ОТРАБОТАННЫМ организациям (у них есть текст),
    # которые реально показываются в таблицах to_work / no_point ---
    tw_worked = to_work[to_work["worked"] == True] if "worked" in to_work else to_work.iloc[:0]
    priority_inns = (
        list(tw_worked.sort_values("impact_fl", ascending=False).head(8)["inn"])
        + list(no_point.head(4)["inn"] if not no_point.empty else [])
    )
    priority_inns = list(dict.fromkeys(int(x) for x in priority_inns))  # dedup, порядок
    priority_text = _collect_text(funnel, priority_inns)

    return Analysis(
        tb_short=tb_short, tb_id=tb_id, tb_full=tb_full, ref_date=ref_date,
        verdict=verdict, gap_rcp=gap_rcp, gap_fot=gap_fot,
        matrix=matrix, gosb_gap=gosb_gap, top_cells=top_cells,
        attract=attract, retention=retention, activity=activity,
        to_work=to_work, no_point=no_point, sim=sim, priority_text=priority_text,
        gosb_cards=gosb_cards,
    )


def _gosb_cards(gosb_gap: pd.DataFrame, matrix: pd.DataFrame,
                to_work: pd.DataFrame, funnel: pd.DataFrame) -> list:
    """По каждому проблемному ГОСБ — что конкретно сделать."""
    # активности за 3 мес по ГОСБ (грейн — new_gosb_id)
    fg = {}
    if not funnel.empty and "new_gosb_id" in funnel:
        for nid, g in funnel.dropna(subset=["new_gosb_id"]).groupby("new_gosb_id"):
            fg[int(nid)] = {
                "act_n": int(len(g)),
                "success": float(g.is_task_closed_success.mean()),
                "worked_orgs": int(g.inn.nunique()),
            }
    cards = []
    prob = gosb_gap[gosb_gap.nedobor > 0].sort_values("nedobor", ascending=False)
    for r in prob.itertuples():
        nid = int(r.new_gosb_id); name = r.gosb_name
        sub = to_work[to_work.new_gosb_id == nid]
        att = sub[sub.lever == "Привлечь"]; ret = sub[sub.lever == "Вернуть"]
        seg_bad = (matrix[(matrix.new_gosb_id == nid) & (matrix.nedobor > 0)]
                   .sort_values("nedobor", ascending=False))
        # разбивка по сегментам: что западает и сколько организаций к работе
        segs = []
        for s in seg_bad.itertuples():
            ss = sub[sub.seg_name == s.seg_name]
            segs.append({
                "seg": s.seg_name, "exec": float(s.execution_percent),
                "nedobor": float(s.nedobor), "n_work": int(len(ss)),
                "n_attract": int((ss.lever == "Привлечь").sum()),
                "n_return": int((ss.lever == "Вернуть").sum()),
                "pot_fl": float(ss.impact_fl.sum()),
            })
        cards.append({
            "gosb_name": name,
            "exec": float(r.execution_percent), "gap": float(r.nedobor),
            "worst": [(s.seg_name, float(s.execution_percent), float(s.nedobor))
                      for s in seg_bad.head(3).itertuples()],
            "segs": segs,
            "n_work": int(len(sub)), "n_attract": int(len(att)), "n_return": int(len(ret)),
            "pot_fl": float(sub.impact_fl.sum()),
            "pot_fl_att": float(att.impact_fl.sum()), "pot_fl_ret": float(ret.impact_fl.sum()),
            "pot_fot": float(sub.impact_fot_mln.sum()),
            "not_worked": int((~sub.worked).sum()) if "worked" in sub else 0,
            "act": fg.get(nid, {"act_n": 0, "success": 0.0, "worked_orgs": 0}),
            "top": [(int(o.inn), o.lever, float(o.impact_fl)) for o in sub.head(3).itertuples()],
        })
    return cards


# --------------------------------------------------------------------------- #
def _verdict(v: pd.DataFrame, tb_id: int) -> tuple[dict, str]:
    out = {}
    ref = ""
    for key, mid in (("rcp", Q.METRIC_RECIPIENTS), ("fot", Q.METRIC_FOT)):
        row = v[(v.metric_id == mid) & (v.tb_id == tb_id)]
        if row.empty:
            out[key] = {"plan": 0, "fact": 0, "exec": None, "rank": None, "n_tb": None}
            continue
        r = row.iloc[0]
        ref = str(r.end_dt)
        out[key] = {
            "plan": float(r.plan_amt), "fact": float(r.fact_amt),
            "exec": float(r.execution_percent), "rank": int(r.rnk), "n_tb": int(r.n_tb),
        }
    return out, ref


def _funnel_by_inn(f: pd.DataFrame) -> pd.DataFrame:
    if f.empty:
        return pd.DataFrame(columns=["inn"])
    f = f.sort_values("last_active_dttm")
    g = f.groupby("inn")
    agg = g.agg(
        n_tasks=("task_type", "size"),
        n_calls=("last_active_type", lambda s: (s == "Звонок").sum()),
        n_meetings=("last_active_type", lambda s: (s == "Встреча").sum()),
        any_success=("is_task_closed_success", "max"),
        plan_deal=("plan_staff_deal_qty", "sum"),
        fact_deal=("fact_staff_deal_qty", "sum"),
        unrealized=("unrealized_deal_potential", "sum"),
        last_status=("task_text_status", "last"),
        last_type=("task_type", "last"),
        last_comment=("task_comment", "last"),
        last_active=("last_active_dttm", "last"),
    ).reset_index()
    return agg


def _activity_totals(f: pd.DataFrame) -> dict:
    if f.empty:
        return {"n": 0}
    by_role = f.groupby("role_code").size().to_dict()
    by_type = f.groupby("task_type").size().to_dict()
    by_status = f.groupby("task_text_status").size().to_dict()
    return {
        "n": int(len(f)),
        "orgs": int(f.inn.nunique()),
        "calls": int((f.last_active_type == "Звонок").sum()),
        "meetings": int((f.last_active_type == "Встреча").sum()),
        "success_rate": float(f.is_task_closed_success.mean()),
        "overdue": int((f.task_text_status == "Не закрыта: Просрочена").sum()),
        "by_role": by_role, "by_type": by_type, "by_status": by_status,
        "plan_deal": int(f.plan_staff_deal_qty.sum()),
        "fact_deal": int(f.fact_staff_deal_qty.sum()),
        "unrealized": int(f.unrealized_deal_potential.sum()),
    }


def _classify(cand: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Разделить кандидатов на «работать» и «нет смысла» с причиной."""
    work_rows, skip_rows = [], []
    for _, o in cand.iterrows():
        worked = bool(o.get("worked", False))
        status = o.get("last_status")
        comment = str(o.get("last_comment") or "")
        success = bool(o.get("any_success", False))
        plan_deal = o.get("plan_deal") or 0
        fact_deal = o.get("fact_deal") or 0
        deadend = ("ликвидац" in comment.lower())

        if deadend:
            skip_rows.append((o, "Организация ликвидируется — работа нецелесообразна"))
        elif not worked:
            work_rows.append((o, "Не работали за 3 мес — начать отработку"))
        elif status == "Не закрыта: Просрочена":
            work_rows.append((o, "Задача просрочена и не закрыта — вернуть в работу"))
        elif fact_deal < plan_deal:
            work_rows.append((o, f"Недоработка по сделке: привлечено {int(fact_deal)} из {int(plan_deal)}"))
        elif success and status == "Закрыта: Своевременно":
            skip_rows.append((o, "Недавно успешно отработана — потенциал реализуется"))
        else:
            work_rows.append((o, "Отработана без результата — повторная активность"))

    def _mk(rows):
        if not rows:
            return pd.DataFrame(columns=list(cand.columns) + ["reason"])
        recs = []
        for o, reason in rows:
            d = o.to_dict(); d["reason"] = reason; recs.append(d)
        return pd.DataFrame(recs)

    to_work = _mk(work_rows).sort_values("impact_fl", ascending=False) if work_rows else _mk(work_rows)
    no_point = _mk(skip_rows).sort_values("impact_fl", ascending=False) if skip_rows else _mk(skip_rows)
    return to_work, no_point


def _simulate(to_work: pd.DataFrame, gap: float, fact: float, plan: float) -> dict:
    """Сколько верхних организаций «работать» закрывают разрыв (получатели)."""
    if to_work.empty or gap <= 0:
        return {"gap": gap, "fact": fact, "plan": plan, "k": 0, "closable": 0.0,
                "attract": 0.0, "retention": 0.0, "fot_mln": 0.0, "coverage": 0.0,
                "total_potential": 0.0}
    d = to_work.sort_values("impact_fl", ascending=False).copy()
    d["cum"] = d["impact_fl"].cumsum()
    total = float(d["impact_fl"].sum())
    hit = d[d["cum"] >= gap]
    k = int(d.index.get_indexer([hit.index[0]])[0]) + 1 if not hit.empty else len(d)
    sel = d.head(k)
    attract = float(sel[sel.lever == "Привлечь"]["impact_fl"].sum())
    retention = float(sel[sel.lever == "Вернуть"]["impact_fl"].sum())
    return {
        "gap": gap, "fact": fact, "plan": plan, "k": k,
        "closable": min(float(sel["impact_fl"].sum()), gap),
        "attract": attract, "retention": retention,
        "fot_mln": float(sel["impact_fot_mln"].sum()),
        "coverage": total / gap if gap else 0.0,
        "total_potential": total,
    }


def _collect_text(f: pd.DataFrame, inns: list) -> list:
    """Собрать свободный текст задач по приоритетным организациям для LLM."""
    out = []
    for inn in inns:
        sub = f[f.inn == inn]
        if sub.empty:
            continue
        notes = []
        for _, t in sub.iterrows():
            c = (t.task_comment or "").strip()
            q = (t.task_questionnaire or "").replace("\n", "; ").strip()
            notes.append(f"[{t.task_type}/{t.task_text_status}] {c} | анкета: {q}"[:180])
        out.append({
            "inn": int(inn),
            "company": str(sub.company_name.iloc[0]),
            "segment": str(sub.segment_name.iloc[0]),
            "notes": notes[:3],
        })
    return out
