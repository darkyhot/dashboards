"""Генерация синтетики для открытого контура.

Представительный объём, внутренне согласованный так, чтобы дэш tb_health
был осмысленным:
- есть провальный ТБ, внутри него — провальные ГОСБ и сегменты;
- по провальным ГОСБ сумма потенциала/оттока организаций сопоставима с
  разрывом до плана (список организаций «закрывает недобор»);
- воронка задач позволяет делить организации на «работать / нет смысла».
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from faker import Faker
from sqlalchemy.engine import Engine

from uzp_dash import config
from uzp_dash.db import read_sql

RNG = np.random.default_rng(42)
FAKE = Faker("ru_RU")
Faker.seed(42)

METRIC_FOT = 1000164          # Общий ФОТ, млн руб
METRIC_RECIPIENTS = 12400196  # Количество уникальных получателей до ИНН

# Сегменты УЗП: extended_dim_1 (короткие коды). 1 = Все. Некоторые коды
# объединяют несколько «больших» сегментов (см. BIG_BY_CODE).
SEG_CODES = [21, 22, 23, 24, 25, 1092]
SEG_SHORT = {21: "КСБ", 22: "РГС", 23: "СКМ", 24: "КФИ", 25: "БМО", 1092: "ММБ"}
# большие сегменты (uzp_dim_company.segment_name) по коду
BIG_BY_CODE = {
    21: ["Средние", "Крупные", "Крупнейшие"],   # КСБ
    22: ["Рег. госсектор"],                       # РГС
    23: ["Клиенты машиностроения"],               # СКМ
    24: ["Фин.институты"],                         # КФИ
    25: ["SBI"],                                   # БМО
    1092: ["Микро", "Малые"],                     # ММБ
}
SEG_WEIGHTS = np.array([0.22, 0.10, 0.08, 0.05, 0.03, 0.52])  # КСБ,РГС,СКМ,КФИ,БМО,ММБ
PROBLEM_CODES = (1092,)         # ММБ (Микро+Малые) западает сильнее всего

PROBLEM_TB_SHORT = "ЮЗБ"        # этот ТБ явно не выполняет план
MONTHS = 24
MAX_MONTH_END = pd.Timestamp("2026-06-30")
# Задачи по метрикам идут месяцем позже метрик -> воронка в следующем месяце
FUNNEL_END = MAX_MONTH_END + pd.offsets.MonthEnd(1)   # 2026-07-31
# Месяц ПОСЛЕ опорного — для комментариев с ещё не наступившим сроком
# («зачисления пройдут 08.2026»): такой срок не является недоработкой.
_NEXT_MONTH = FUNNEL_END + pd.offsets.MonthEnd(1)
FUTURE_MONTH = _NEXT_MONTH.strftime("%m.%Y")
FUTURE_MONTH_NAME = ("январе феврале марте апреле мае июне июле августе сентябре "
                     "октябре ноябре декабре").split()[_NEXT_MONTH.month - 1]
ORGS_TOTAL = 4000


# --------------------------------------------------------------------------- #
def generate_all(engine: Engine) -> dict[str, int]:
    gosb = _pick_gosb(engine)
    metrics, latest = _metrics(gosb)
    orgs = _orgs(gosb, latest)
    company = _company_holding(orgs)
    dim_company = _dim_company(orgs)
    funnel = _funnel(orgs, gosb)
    ref_base = _reference_base(orgs, gosb)

    counts = {}
    counts["uzp_dim_company"] = _bulk(engine, dim_company, "uzp_dim_company")
    counts["uzp_dim_mzp_reference_base"] = _bulk(engine, ref_base, "uzp_dim_mzp_reference_base")
    counts["uzp_dwh_metrics"] = _bulk(engine, metrics, "uzp_dwh_metrics")
    counts["uzp_dwh_company_holding_metric"] = _bulk(engine, company, "uzp_dwh_company_holding_metric")
    counts["uzp_dwh_sale_funnel_task"] = _bulk(engine, funnel, "uzp_dwh_sale_funnel_task")
    return counts


def _pick_gosb(engine: Engine) -> pd.DataFrame:
    """Реальные ГОСБ каждого ТБ = distinct (tb_id, new_gosb_id) из справочника.
    Метрики лежат на уровне old_gosb_id; берём один представительный old_gosb_id
    на каждый реальный ГОСБ. Количество ГОСБ — сколько даёт справочник (без кэпа)."""
    g = read_sql(engine, """
        SELECT tb_id, tb_short_name, tb_full_name, old_gosb_id, new_gosb_id, new_gosb_name
        FROM {schema}.uzp_dim_gosb
        WHERE old_gosb_id > 0 AND new_gosb_id IS NOT NULL AND tb_id IS NOT NULL
    """)
    g = g.dropna(subset=["old_gosb_id", "new_gosb_id"]).drop_duplicates("old_gosb_id")
    picked = (
        g.sort_values("old_gosb_id")
         .drop_duplicates(["tb_id", "new_gosb_id"])   # один old_gosb_id на реальный ГОСБ
         .reset_index(drop=True)
    )
    picked["old_gosb_id"] = picked["old_gosb_id"].astype(int)
    picked["new_gosb_id"] = picked["new_gosb_id"].astype(int)
    # средняя ЗП по ГОСБ, руб/мес (для пересчёта потенциала в ФОТ)
    picked["avg_salary"] = RNG.uniform(45_000, 85_000, len(picked)).round(0)
    # базовая численность получателей ЗП по ГОСБ (последний месяц)
    picked["base_recipients"] = RNG.integers(4_000, 40_000, len(picked))
    return picked


# Профили ГОСБ проблемного ТБ: дэш должен показывать все три ситуации —
# план вытянут за счёт сильных сегментов, но один сегмент провален; провалены
# два сегмента; провал по всему ГОСБ.
GOSB_PROFILES = ("one_bad_seg", "two_bad_segs", "all_bad")


def _gosb_profile(gosb_id: int) -> tuple[str, tuple[int, ...]]:
    """Детерминированный профиль ГОСБ и его западающие сегменты."""
    prof = GOSB_PROFILES[gosb_id % len(GOSB_PROFILES)]
    if prof == "one_bad_seg":
        bad = (SEG_CODES[(gosb_id // 3) % len(SEG_CODES)],)
    elif prof == "two_bad_segs":
        i = (gosb_id // 3) % len(SEG_CODES)
        bad = (SEG_CODES[i], SEG_CODES[(i + 3) % len(SEG_CODES)])
    else:
        bad = tuple(SEG_CODES)
    return prof, bad


def _target_exec(tb_short: str, gosb_id: int, seg_id: int) -> float:
    """Целевое выполнение плана по (ТБ, ГОСБ, сегмент). <1 — провал."""
    if tb_short != PROBLEM_TB_SHORT:
        return float(np.clip(RNG.normal(1.03, 0.03), 0.6, 1.25))
    prof, bad = _gosb_profile(gosb_id)
    if seg_id in bad:
        # ММБ (самый крупный сегмент) проваливается сильнее прочих
        r = RNG.normal(0.80 if seg_id in PROBLEM_CODES else 0.85, 0.03)
    elif prof == "one_bad_seg":
        r = RNG.normal(1.08, 0.02)      # сильные сегменты вытягивают ГОСБ выше 100%
    elif prof == "two_bad_segs":
        r = RNG.normal(1.03, 0.02)      # вытягивают частично
    else:
        r = RNG.normal(0.93, 0.03)
    return float(np.clip(r, 0.6, 1.25))


OUT_COLS = [
    "metric_id", "start_dt", "end_dt", "level_name", "level_value", "level_id",
    "period_type", "plan_amt", "fact_amt", "execution_percent",
    "prediction_amt", "prediction_percent", "modified_dttm",
    *[f"extended_dim_{i}" for i in range(1, 11)],
]


def _metrics(gosb: pd.DataFrame):
    """uzp_dwh_metrics: обе метрики, уровни gosb и tb, по сегментам и all(=1).

    Возвращает (rows_df, latest_df). latest_df — план/факт получателей за
    последний месяц по (gosb, seg) для увязки с витриной организаций.
    """
    month_ends = pd.date_range(end=MAX_MONTH_END, periods=MONTHS, freq="ME")
    tidy = []   # длинная таблица: строка на (gosb, seg, month)
    latest = []

    for _, gr in gosb.iterrows():
        gid = int(gr.old_gosb_id)
        seg_recips = gr.base_recipients * SEG_WEIGHTS
        salary = gr.avg_salary                    # средняя ЗП, РУБЛИ (ФОТ-метрика в рублях)
        for si, seg_id in enumerate(SEG_CODES):
            texec = _target_exec(gr.tb_short_name, gid, seg_id)
            base = seg_recips[si]
            for mi, mend in enumerate(month_ends):
                trend = 0.82 + 0.18 * (mi / (MONTHS - 1))
                fact_r = max(0.0, base * trend * RNG.normal(1.0, 0.03))
                plan_r = fact_r / texec
                tidy.append({
                    "tb_id": int(gr.tb_id), "gosb_id": gid, "seg_id": int(seg_id),
                    "start_dt": mend.replace(day=1).date(), "end_dt": mend.date(),
                    "plan_r": plan_r, "fact_r": fact_r,
                    "fot_plan": plan_r * salary, "fot_fact": fact_r * salary,
                })
                if mi == MONTHS - 1:
                    latest.append((gid, int(gr.tb_id), gr.tb_short_name, int(seg_id),
                                   float(gr.avg_salary), plan_r, fact_r))

    tidy = pd.DataFrame(tidy)
    latest_df = pd.DataFrame(latest, columns=[
        "gosb_id", "tb_id", "tb_short", "seg_id", "avg_salary", "plan_r", "fact_r"])

    frames = [
        _agg(tidy, "gosb_id", "gosb", by_segment=True),
        _agg(tidy, "gosb_id", "gosb", by_segment=False),
        _agg(tidy, "tb_id", "tb", by_segment=True),
        _agg(tidy, "tb_id", "tb", by_segment=False),
    ]
    rows = pd.concat(frames, ignore_index=True)[OUT_COLS]
    return rows, latest_df


def _agg(tidy: pd.DataFrame, level_field: str, level_name: str, by_segment: bool) -> pd.DataFrame:
    """Свернуть tidy до уровня (gosb|tb) × (сегмент|all) и развернуть в 2 метрики."""
    keys = [level_field, "start_dt", "end_dt"] + (["seg_id"] if by_segment else [])
    a = tidy.groupby(keys, as_index=False).agg(
        plan_r=("plan_r", "sum"), fact_r=("fact_r", "sum"),
        fot_plan=("fot_plan", "sum"), fot_fact=("fot_fact", "sum"))
    a["extended_dim_1"] = a["seg_id"] if by_segment else 1

    rec = _rows(a, METRIC_RECIPIENTS, level_name, level_field, "plan_r", "fact_r")
    fot = _rows(a, METRIC_FOT, level_name, level_field, "fot_plan", "fot_fact")
    return pd.concat([rec, fot], ignore_index=True)


def _rows(a: pd.DataFrame, metric_id: int, level_name: str,
          level_field: str, plan_col: str, fact_col: str) -> pd.DataFrame:
    df = pd.DataFrame({
        "metric_id": metric_id,
        "start_dt": a["start_dt"], "end_dt": a["end_dt"],
        "level_name": level_name,
        "level_value": a[level_field].astype(int).astype(str),
        "level_id": a[level_field].astype(int),
        "period_type": "m",
        "plan_amt": a[plan_col].round(3), "fact_amt": a[fact_col].round(3),
        "execution_percent": (a[fact_col] / a[plan_col]).round(6),
        "prediction_amt": None, "prediction_percent": None,
        "modified_dttm": pd.Timestamp.now(),
        "extended_dim_1": a["extended_dim_1"].astype(int),
    })
    for i in range(2, 11):
        df[f"extended_dim_{i}"] = None
    return df


def _orgs(gosb: pd.DataFrame, latest: pd.DataFrame) -> pd.DataFrame:
    """Организации по ГОСБ. Численность ~ доле ГОСБ; потенциал в провальных
    зонах покрывает разрыв до плана."""
    # разрыв получателей по (gosb, seg): plan-fact там, где план не выполнен
    latest = latest.copy()
    latest["gap"] = (latest["plan_r"] - latest["fact_r"]).clip(lower=0)

    total_recip = latest.groupby("gosb_id")["fact_r"].sum()
    org_alloc = (total_recip / total_recip.sum() * ORGS_TOTAL).round().astype(int).clip(lower=3)

    rows = []
    inn_seq = 1_000_000_000
    for _, gr in gosb.iterrows():
        gid = int(gr.old_gosb_id)
        n = int(org_alloc.get(gid, 5))
        sub = latest[latest.gosb_id == gid]
        seg_fact = sub.set_index("seg_id")["fact_r"]
        p = (seg_fact / seg_fact.sum()).reindex(SEG_CODES).fillna(0).to_numpy()
        if p.sum() == 0:
            p = SEG_WEIGHTS / SEG_WEIGHTS.sum()
        assign = RNG.choice(SEG_CODES, size=n, p=p / p.sum())
        for seg_id in assign:
            inn_seq += int(RNG.integers(1, 900))
            fl = int(max(1, RNG.gamma(2.0, 60)))            # получателей в организации
            sal = float(gr.avg_salary * RNG.uniform(0.85, 1.2))
            big = str(RNG.choice(BIG_BY_CODE[int(seg_id)]))  # большое имя сегмента
            rows.append({
                "inn": inn_seq, "gosb_id": gid, "tb_id": int(gr.tb_id),
                "tb_short": gr.tb_short_name, "seg_code": int(seg_id),
                "segment_name": big,
                "current_fl_qty": fl, "avg_salary": round(sal, 0),
                "current_fot_amt": round(fl * sal, 2),
                # «сырой» потенциал/возврат — отмасштабируем ниже под разрыв ГОСБ
                "_pull": max(0.0, RNG.normal(0.14, 0.07)) * fl,
                "_back": max(0.0, RNG.normal(0.06, 0.04)) * fl,
            })

    df = pd.DataFrame(rows)
    df = _spread_multi_gosb(df, gosb)   # часть компаний работает в неск. ГОСБ

    # Масштабируем потенциал+возврат под разрыв КОНКРЕТНОГО (ГОСБ, сегмент): отбор
    # ведётся внутри западающего сегмента, поэтому запас нужен именно там. Запас
    # большой, потому что из отбора выпадает заметная часть организаций: со свежей
    # сделкой (уже в работе) и успешно отработанные.
    seg_gap = latest.set_index(["gosb_id", "seg_id"])["gap"].to_dict()
    scarce = _scarce_segments(seg_gap)      # где намеренно не хватает своих организаций
    for (gid, seg), g in df.groupby(["gosb_id", "seg_code"]):
        gap = float(seg_gap.get((gid, int(seg)), 0.0))
        cur = g["_pull"].sum() + g["_back"].sum()
        if cur <= 0:
            continue
        if gap > 0:
            factor = (3.6 * gap) / cur
            if (gid, int(seg)) in scarce:
                factor *= 0.15              # своих не хватит -> сработает добор
        else:
            factor = 0.6                    # сегмент выполняет план: потенциал скромный
        df.loc[g.index, "_pull"] *= factor
        df.loc[g.index, "_back"] *= factor

    df["emp_potential_qty"] = df["_pull"].round(3)
    df["fot_potential_amt"] = (df["_pull"] * df["avg_salary"]).round(2)
    df["fl_outflow_qty"] = df["_back"].round().astype(int)
    df["fot_outflow_amt"] = (df["_back"] * df["avg_salary"]).round(2)
    return df.drop(columns=["_pull", "_back"])


def _scarce_segments(seg_gap: dict, every: int = 5) -> set:
    """Каждый N-й западающий (ГОСБ, сегмент) делаем дефицитным по потенциалу —
    чтобы в дэше воспроизводился сценарий «своих не хватает, добор из других»."""
    bad = sorted(k for k, v in seg_gap.items() if v > 0)
    return set(bad[::every])


def _spread_multi_gosb(df: pd.DataFrame, gosb: pd.DataFrame, frac: float = 0.12) -> pd.DataFrame:
    """~frac компаний обслуживаются в НЕСКОЛЬКИХ ГОСБ одного ТБ.

    Один и тот же ИНН получает строки в 2–4 ГОСБ со своими показателями — так
    проявляется грейн (ГОСБ, ИНН): в одном городе с клиентом работали, в другом нет.
    Сегмент компании (segment_name) одинаков во всех ГОСБ.
    """
    by_tb = {int(t): g for t, g in gosb.groupby("tb_id")}
    multi = df.sample(frac=frac, random_state=7)
    extra = []
    for _, o in multi.iterrows():
        pool = by_tb.get(int(o.tb_id))
        if pool is None or len(pool) < 2:
            continue
        others = pool[pool.old_gosb_id != int(o.gosb_id)]
        if others.empty:
            continue
        n_more = int(RNG.integers(1, 4))                       # ещё 1–3 ГОСБ
        picks = others.sample(n=min(n_more, len(others)), random_state=int(o.inn) % 10000)
        for _, g2 in picks.iterrows():
            fl = int(max(1, RNG.gamma(2.0, 40)))
            sal = float(g2.avg_salary * RNG.uniform(0.85, 1.2))
            extra.append({
                "inn": int(o.inn), "gosb_id": int(g2.old_gosb_id), "tb_id": int(g2.tb_id),
                "tb_short": g2.tb_short_name, "seg_code": int(o.seg_code),
                "segment_name": o.segment_name,
                "current_fl_qty": fl, "avg_salary": round(sal, 0),
                "current_fot_amt": round(fl * sal, 2),
                "_pull": max(0.0, RNG.normal(0.14, 0.07)) * fl,
                "_back": max(0.0, RNG.normal(0.06, 0.04)) * fl,
            })
    if not extra:
        return df
    return pd.concat([df, pd.DataFrame(extra)], ignore_index=True)


def _company_holding(orgs: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame({
        "report_dt": MAX_MONTH_END.date(),
        "level_name": "gosb",
        "level_id": orgs["gosb_id"].astype(int),
        "org_type": "inn",
        "org_id": orgs["inn"].astype("int64"),
        "ul_outflow_qty": 0,
        "fl_outflow_qty": orgs["fl_outflow_qty"].astype(int),
        "fot_outflow_amt": orgs["fot_outflow_amt"],
        "current_fot_amt": orgs["current_fot_amt"],
        "fot_y_1_diff_amt": (orgs["current_fot_amt"] * RNG.normal(0.05, 0.1, len(orgs))).round(2),
        "current_fl_qty": orgs["current_fl_qty"].astype(int),
        "fl_y_1_diff_qty": (orgs["current_fl_qty"] * RNG.normal(0.03, 0.1, len(orgs))).round().astype(int),
        "emp_potential_qty": orgs["emp_potential_qty"],
        "fot_potential_amt": orgs["fot_potential_amt"],
        "modified_dttm": pd.Timestamp.now(),
    })
    # штат ≥ получателей; проникновение = получатели/штат (низкое = резерв привлечения)
    total_emp = (orgs["current_fl_qty"].to_numpy() * RNG.uniform(1.05, 2.5, len(orgs))).round(0)
    total_emp = pd.Series(total_emp, index=df.index).clip(lower=df["current_fl_qty"])
    df["total_emp_qty"] = total_emp
    df["zp_fl_perc"] = (df["current_fl_qty"] / total_emp).round(4)
    df["new_fl_cnt"] = RNG.integers(0, 20, len(orgs))
    df["np_cnt"] = RNG.integers(0, 10, len(orgs))

    # Строки уровня holding / head_holding (в отчёте фильтруются: org_type='inn').
    # org_id у них — id холдинга, а не ИНН; значения крупнее (агрегаты).
    n_h = max(50, len(df) // 12)
    samp = df.sample(n=n_h, random_state=7).copy()
    samp["org_type"] = RNG.choice(["holding", "head_holding"], size=n_h, p=[0.7, 0.3])
    samp["org_id"] = (9_000_000_000_000 + np.arange(n_h)).astype("int64")
    mult = RNG.integers(2, 6, n_h)
    for c in ("current_fl_qty", "fl_outflow_qty"):
        samp[c] = (samp[c].to_numpy() * mult)
    samp["current_fot_amt"] = samp["current_fot_amt"].to_numpy() * mult
    samp["fot_potential_amt"] = samp["fot_potential_amt"].to_numpy() * mult
    return pd.concat([df, samp], ignore_index=True)


def _dim_company(orgs: pd.DataFrame) -> pd.DataFrame:
    """Справочник компаний: сегмент и наименование по ИНН."""
    o = orgs.drop_duplicates("inn")
    n = len(o)
    df = pd.DataFrame({
        "epk_id": o["inn"].astype("int64").to_numpy(),
        "company_name": [FAKE.company() for _ in range(n)],
        "inn": o["inn"].astype("int64").to_numpy(),
        "kpp": None,
        "segment_name": o["segment_name"].to_numpy(),   # большое имя сегмента
        "holding_name": None,
        "mzp_last_action_dt": None, "km_last_action_dt": None, "crm_client_id": None,
    })
    for flag in ("agrmnt_flag", "rko_flag", "dbo_flag", "credit_flag", "deposit_flag",
                 "corporate_card_flag", "internet_acquiring_flag", "merchant_acquiring_flag"):
        df[flag] = RNG.choice([-1, 0, 1], size=n)
    df["significance_level_id"] = RNG.choice([-1, 1, 2], size=n)
    df["info"] = None
    df["modified_dttm"] = pd.Timestamp.now()
    return df


REF_COVERAGE = 0.85     # доля пар (ГОСБ, ИНН) витрины, закреплённых в эталонной базе
REF_EXTRA = 0.10        # доля «лишних» пар: есть в базе, но нет в витрине


def _reference_base(orgs: pd.DataFrame, gosb: pd.DataFrame) -> pd.DataFrame:
    """Эталонная база закрепления ИУП: с кем вообще можно работать.

    Грейн — (new_gosb_id, ИНН). Закрепляем не все пары витрины: незакреплённые
    дэш обязан отбрасывать, даже если у них есть потенциал или отток.
    """
    new_by_old = gosb.drop_duplicates("old_gosb_id").set_index("old_gosb_id")["new_gosb_id"]
    pairs = (orgs[["gosb_id", "inn"]].copy()
             .assign(gosb_id=lambda d: d["gosb_id"].map(new_by_old))
             .dropna().drop_duplicates())
    pairs["gosb_id"] = pairs["gosb_id"].astype(int)

    keep = pairs.sample(frac=REF_COVERAGE, random_state=11)
    # «лишние» пары: организации, которых нет в витрине этого ГОСБ
    n_extra = int(len(pairs) * REF_EXTRA)
    extra = pd.DataFrame({
        "gosb_id": RNG.choice(pairs["gosb_id"].unique(), size=n_extra),
        "inn": 2_000_000_000 + RNG.integers(1, 900_000, n_extra),
    })
    base = pd.concat([keep, extra], ignore_index=True)

    # несколько срезов актуальности: часть пар присутствует в двух-трёх
    snapshots = [d.date() for d in
                 pd.to_datetime(["2026-04-30", "2026-06-17", FUNNEL_END.date()])]
    rows = []
    for i, dt in enumerate(snapshots):
        part = base if i == len(snapshots) - 1 else base.sample(frac=0.7, random_state=20 + i)
        rows.append(part.assign(actual_dt=dt))
    df = pd.concat(rows, ignore_index=True)
    n = len(df)
    df["main_pos_id"] = 35_000_000 + RNG.integers(0, 900_000, n)
    df["reserve_pos_id"] = 35_000_000 + RNG.integers(0, 900_000, n)
    df["is_q_ref_base"] = False
    df["inserted_dttm"] = pd.Timestamp.now()
    df["author_login"] = "synthetic_loader"
    return df[["gosb_id", "inn", "main_pos_id", "reserve_pos_id", "actual_dt",
               "is_q_ref_base", "inserted_dttm", "author_login"]]


# --- Банки фраз для свободного текста воронки (детерминированно, без API) --- #
BANKS = ["ВТБ", "Альфа-Банк", "Т-Банк", "Газпромбанк", "Райффайзен"]
OUTFLOW_REASONS = [
    "Смена зарплатного банка", "Текучка персонала", "Сокращение штата",
    "Недовольство условиями обслуживания", "Переход сотрудников на самозанятость",
    "Сезонные отпуска", "Отпуска сотрудников", "Ликвидация организации",
]
# причины, при которых работать бессмысленно
DEADEND = {"Ликвидация организации"}
# причины ВНЕ зоны влияния банка: организация не оттекла, а временно просела
# (клиент остался с нами) либо решение принято клиентом. Требовать по ним действий
# нельзя — на этих кейсах проверяем, что аудит не возвращает задачу на доработку.
NO_INFLUENCE = {"Сезонные отпуска", "Отпуска сотрудников", "Сокращение штата"}

# Свободные формулировки без явных ключевых слов — такие строки уходят в LLM
FREEFORM_COMMENTS = [
    "Клиент запросил расчёт экономии по обслуживанию перед принятием решения",
    "Директор в командировке до конца месяца, вернуться в следующем периоде",
    "Ожидаем протокол собрания учредителей по смене банка",
    "Идёт закупочная процедура на банковское обслуживание, участвуем",
    "Головной офис принимает решение централизованно, локально влиять не можем",
    "Просят подготовить презентацию для собственника бизнеса",
    "Часть сотрудников на аутсорсе, схема выплат уточняется",
    "Клиент сравнивает тарифы, обещал дать ответ после квартального отчёта",
]

ROLE_BY_SEGMENT = {  # какая роль ведёт сегмент (по большому имени)
    "Крупнейшие": "МКК", "Крупные": "МКК", "Средние": "МЗП",
    "Малые": "МЗП", "Микро": "СЗП", "Рег. госсектор": "МЗП",
    "Клиенты машиностроения": "МКК", "Фин.институты": "МКК", "SBI": "МКК",
}


def _funnel(orgs: pd.DataFrame, gosb: pd.DataFrame) -> pd.DataFrame:
    """Задачи с активностями за последние 3 месяца и свободным текстом.

    Профиль задачи зависит от организации: высокий отток -> задачи «Отток» с
    причиной ухода; высокий потенциал -> «Привлечение/Расширение» с барьерами.
    Часть организаций остаётся без задач (с ними не работали)."""
    tb_full = gosb.drop_duplicates("tb_id").set_index("tb_id")["tb_full_name"].to_dict()
    gosb_name = gosb.drop_duplicates("old_gosb_id").set_index("old_gosb_id")["new_gosb_name"].to_dict()

    # работаем примерно с 55% организаций (у остальных задач нет = «не работали»)
    worked = orgs.sample(frac=0.55, random_state=1)
    rows = []
    last3_start = FUNNEL_END - pd.Timedelta(days=90)

    for _, o in worked.iterrows():
        # доминирующий рычаг организации
        outflow_heavy = o.fl_outflow_qty >= max(1, 0.6 * o.emp_potential_qty)
        role = ROLE_BY_SEGMENT.get(o.segment_name, "МЗП")
        # пул авторов организации (1–2 сотрудника) — чтобы возникали расхождения между ними
        author_pool = [35_000_000 + (int(o.inn) % 900_000) + k
                       for k in range(int(RNG.integers(1, 3)))]
        n_tasks = int(RNG.integers(1, 4))
        for _ in range(n_tasks):
            author_id = int(RNG.choice(author_pool))
            created = FUNNEL_END - pd.Timedelta(days=int(RNG.integers(5, 90)))
            active = created + pd.Timedelta(days=int(RNG.integers(0, 6)),
                                            hours=int(RNG.integers(8, 19)))
            closed = bool(RNG.random() < 0.8)
            success = closed and bool(RNG.random() < 0.5)
            # Незакрытая задача бывает двух РАЗНЫХ видов, и путать их нельзя:
            #   «Новая»/«В работе» — срок не вышел, спрашивать результат рано;
            #   «Просрочена»       — реальная недоработка.
            overdue = (not closed) and bool(RNG.random() < 0.5)
            status = ("Закрыта: Своевременно" if success
                      else "Закрыта: С просрочкой" if closed
                      else "Не закрыта: Просрочена" if overdue
                      else str(RNG.choice(["Новая", "В работе"])))
            is_outflow = outflow_heavy and RNG.random() < 0.8
            if is_outflow:
                tt, text, comment, quest, unreal = _text_outflow(o, success)
            else:
                tt, text, comment, quest, unreal = _text_attract(o, success)
            # Часть активностей — «Задача» без сделки: сделка по ним не заводится
            # никогда, поэтому её отсутствие не должно считаться недоработкой.
            is_offer = (not is_outflow) and bool(RNG.random() < 0.7)
            plan_deal = (int(max(0, round(o.emp_potential_qty * RNG.uniform(0.5, 1.2))))
                         if is_offer else 0)
            fact_deal = int(round(plan_deal * (RNG.uniform(0.6, 1.0) if success else RNG.uniform(0.0, 0.4))))
            # Сделка заводится через 0–10 дней после задачи; часть сделок оказывается
            # в последних месяцах окна («свежие» — по ним рано судить о зачислениях).
            if plan_deal > 0:
                deal_dt = created + pd.Timedelta(days=int(RNG.integers(0, 11)),
                                                 hours=int(RNG.integers(9, 19)))
                deal_dt = min(deal_dt, FUNNEL_END)
                deal_code = f"D{int(o.inn)}-{int(RNG.integers(1000, 9999))}"
            else:
                deal_dt, deal_code = None, None
            rows.append({
                "report_dt": FUNNEL_END.date(),
                "tb_id": int(o.tb_id), "tb_name": tb_full.get(int(o.tb_id)),
                "gosb_id": int(o.gosb_id), "gosb_name": gosb_name.get(int(o.gosb_id)),
                "inn": int(o.inn), "company_name": f"Организация {o.inn}",
                "segment_name": o.segment_name,
                # task_category различает «Предложение» (по нему бывает сделка) и
                # «Задачу» (сделки не будет) — как в проме
                "task_type": tt, "task_subtype": None,
                "task_category": "Предложение" if is_offer else "Задача",
                "task_code": f"T{int(o.inn)}-{int(RNG.integers(1000,9999))}",
                "task_create_dt": created.date(),
                "fact_close_task_dttm": (active if closed else None),
                "is_task_closed": closed,
                "is_task_closed_success": success,
                "is_task_in_progress": not closed,
                "task_text_status": status,
                "isu_struct_saphr_id": author_id,
                "role_code": role,
                "last_active_type": str(RNG.choice(["Звонок", "Встреча"], p=[0.65, 0.35])),
                "last_active_status": "Исполнена" if closed else "В работе",
                "last_active_dttm": active,
                "unrealized_deal_potential": unreal,
                "deal_code": deal_code,
                "deal_create_dttm": deal_dt,
                "plan_staff_deal_qty": plan_deal,
                "fact_staff_deal_qty": fact_deal,
                "task_text": text, "task_comment": comment, "task_questionnaire": quest,
            })
    df = pd.DataFrame(rows)
    # часть активностей должна быть строго в последних 3 мес (для блока активностей)
    df = df[pd.to_datetime(df["task_create_dt"]) >= last3_start.normalize()].reset_index(drop=True)
    return df


def _text_attract(o, success: bool):
    """Тексты для задач привлечения/расширения."""
    tt = str(RNG.choice(["Привлечение ЗП", "Расширение ЗП"], p=[0.6, 0.4]))
    n = int(max(1, round(o.emp_potential_qty)))
    text = (f"Твоя задача: связаться с ЛПР организации, предложить перевод сотрудников "
            f"на зарплатный проект Сбербанка. Потенциал привлечения — {n} получателей.")
    if success:
        comment = str(RNG.choice([
            f"Клиент согласился на перевод, ожидаем {n} получателей",
            "Оформили согласие на зарплатный проект, готовим реестр",
            f"Расширение подтверждено, {n} новых получателей до конца месяца",
            # срок В БУДУЩЕМ: спрашивать результат в опорном месяце не за что
            f"Договорились о расширении, зачисления пройдут {FUTURE_MONTH}",
            f"Согласовано расширение, первые выплаты ожидаем в {FUTURE_MONTH_NAME}",
        ]))
        quest = f"1. Получено согласие\nДа\n2. Планируемое привлечение\n{n} чел\n3. Комментарий\n{comment}"
        unreal = int(RNG.integers(0, 3))
    elif RNG.random() < 0.35:
        # свободные формулировки, НЕ попадающие под ключевые слова -> уходят в LLM
        comment = str(RNG.choice(FREEFORM_COMMENTS))
        quest = f"1. Получено согласие\nНет\n2. Комментарий\n{comment}\n3. Планируемое привлечение\n{n} чел"
        unreal = n
    else:
        comment = str(RNG.choice([
            "ЛПР не заинтересован, работают с другим банком",
            "Требуется повторная встреча с директором, взяли паузу",
            "Бухгалтер против смены реквизитов, отложили решение",
            "Не дозвонились до контактного лица, перенос активности",
            "Клиент рассматривает предложение, ждём решения",
        ]))
        quest = f"1. Получено согласие\nНет\n2. Причина\n{comment}\n3. Планируемое привлечение\n{n} чел"
        unreal = n
    return tt, text, comment, quest, unreal


def _text_outflow(o, success: bool):
    """Тексты для задач по оттоку."""
    reason = str(RNG.choice(OUTFLOW_REASONS))
    bank = str(RNG.choice(BANKS))
    n = int(max(1, o.fl_outflow_qty))
    text = (f"Твоя задача: отработать риск оттока по организации. Зафиксирован отток "
            f"{n} получателей. Установить причину и удержать клиента.")
    if reason == "Смена зарплатного банка":
        comment = f"Причина оттока: {reason.lower()} на {bank}. {'Удалось удержать часть получателей' if success else 'Клиент подтвердил уход'}"
    elif reason in DEADEND:
        comment = "Организация в процессе ликвидации, работа нецелесообразна"
    elif reason in NO_INFLUENCE:
        # временное снижение: клиент остался с нами, влиять банку нечем.
        # Часть таких комментариев называет будущий срок возврата получателей.
        comment = (f"Причина оттока: {reason.lower()}, сотрудники вернутся, "
                   f"зачисления пройдут {FUTURE_MONTH}" if RNG.random() < 0.5
                   else f"Причина оттока: {reason.lower()}, снижение временное, "
                        f"клиент обслуживание не менял")
    else:
        comment = f"Причина оттока: {reason.lower()}. {'Отток остановлен' if success else 'Отток продолжается'}"
    ret = 0 if reason in DEADEND else int(round(n * (RNG.uniform(0.4, 0.9) if success else RNG.uniform(0, 0.3))))
    quest = (f"1. Причина оттока\n{reason}\n2. Ушли в другой банк\n{'Да' if reason=='Смена зарплатного банка' else 'Нет'}\n"
             f"3. Ожидаемый возврат получателей\n{ret} чел")
    return "Отток", text, comment, quest, int(n)


# --------------------------------------------------------------------------- #
def _bulk(engine: Engine, df: pd.DataFrame, table: str) -> int:
    df = df.where(pd.notnull(df), None)
    df.to_sql(table, engine, schema=config.SCHEMA, if_exists="append",
              index=False, method="multi", chunksize=1000)
    return len(df)
