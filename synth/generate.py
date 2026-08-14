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
MAX_MONTH_END = pd.Timestamp("2026-06-30")            # последний ЗАКРЫТЫЙ месяц
# Задачи по метрикам идут месяцем позже метрик -> воронка в следующем месяце
FUNNEL_END = MAX_MONTH_END + pd.offsets.MonthEnd(1)   # 2026-07-31

# --- Текущий (незакрытый) месяц: на него дэш строит ПРОГНОЗ ---
CUR_MONTH_END = FUNNEL_END                            # 2026-07-31
# Дата актуальности ежедневной витрины: по какое число есть факт зачислений
ACT_DT = CUR_MONTH_END - pd.Timedelta(days=5)         # 2026-07-26
# Доля месяца, которая уже прошла — столько же риска оттока уже отыграно
MONTH_ELAPSED = ACT_DT.day / CUR_MONTH_END.day
# Сезонное окно организаций-«сезонников» начинается С ПРОГНОЗНОГО месяца: иначе
# база (июнь) и прогноз (июль) лежат в одной фазе сезона и сезонной дельты нет.
SEASON_MONTHS = (7, 8, 9)
SEASON_UP, SEASON_DOWN = 1.40, 0.60
# Ежедневная витрина оттока хранит ВСЕ месяцы, а не только текущий: без этого не
# проверить пересборку отчёта за прошлый месяц. Закрытые месяцы отдаются целиком.
DAY_OUTFLOW_MONTHS = 4
# Частичная ЗП-ведомость текущего месяца: этот факт в дэше НЕ используется
# (в этом и смысл прогноза), но в витрине он есть — как на проме.
PARTIAL_FACT_SHARE = 0.62
# Идентификатор уровня «весь банк» (level_name='sb') в витринах метрик и
# организаций. Отдельного sb_id нет ни в одном справочнике — уровень живёт только
# внутри витрин, поэтому здесь это просто константа. На проме встречаются
# level_value 0/1/99, и дэш проверяет, что строка на метрику одна.
SB_LEVEL_ID = 1
# Раскладка плана сделки по трём месяцам её жизни (пайплайн)
PIPELINE_SPLIT = (0.2, 0.5, 0.3)
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
    metrics, latest = _metrics(gosb)              # закрытые месяцы
    orgs = _orgs(gosb, latest)
    company, profiles = _company_holding(orgs)    # 24 месяца истории
    dim_company = _dim_company(orgs)
    funnel = _funnel(orgs, gosb)
    pipeline = _pipeline(funnel, orgs)
    motivation = _motivation(funnel, pipeline, orgs)
    day_outflow = _day_outflow(orgs, profiles)
    # План текущего месяца выводится ИЗ прогноза, поэтому считается последним
    metrics_cur = _metrics_current(orgs, latest, company, funnel, pipeline,
                                   day_outflow, motivation)
    metrics = pd.concat([metrics, metrics_cur], ignore_index=True)
    ref_base = _reference_base(orgs, gosb)

    counts = {}
    counts["uzp_dim_company"] = _bulk(engine, dim_company, "uzp_dim_company")
    counts["uzp_dim_mzp_reference_base"] = _bulk(engine, ref_base, "uzp_dim_mzp_reference_base")
    counts["uzp_dwh_metrics"] = _bulk(engine, metrics, "uzp_dwh_metrics")
    counts["uzp_dwh_company_holding_metric"] = _bulk(engine, company, "uzp_dwh_company_holding_metric")
    counts["uzp_dwh_fact_outflow"] = _bulk(engine, _fact_outflow(company, orgs),
                                           "uzp_dwh_fact_outflow")
    counts["uzp_dwh_sale_funnel_task"] = _bulk(engine, funnel, "uzp_dwh_sale_funnel_task")
    counts["uzp_dwh_day_outflow"] = _bulk(engine, day_outflow, "uzp_dwh_day_outflow")
    counts["yva_pl_task_deal_code"] = _bulk(engine, pipeline, "yva_pl_task_deal_code",
                                            schema=config.SCHEMA_T)
    counts["uzp_data_mzp_motivation_detail_corr"] = _bulk(
        engine, motivation, "uzp_data_mzp_motivation_detail_corr")
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
    """uzp_dwh_metrics за ЗАКРЫТЫЕ месяцы: обе метрики, уровни gosb и tb,
    по сегментам и all(=1).

    Возвращает (rows_df, latest_df). latest_df — план/факт получателей за
    последний закрытый месяц по (gosb, seg) для увязки с витриной организаций;
    там же лежит целевое выполнение texec — план текущего месяца строится по
    ТОМУ ЖЕ сценарию (см. _metrics_current), иначе картина ГОСБ поплывёт.
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
                                   float(gr.avg_salary), plan_r, fact_r, texec))

    tidy = pd.DataFrame(tidy)
    latest_df = pd.DataFrame(latest, columns=[
        "gosb_id", "tb_id", "tb_short", "seg_id", "avg_salary", "plan_r", "fact_r",
        "texec"])

    rows = pd.concat(_level_frames(tidy), ignore_index=True)[OUT_COLS]
    return rows, latest_df


def _level_frames(tidy: pd.DataFrame) -> list:
    """Свёртки одного и того же tidy на все три уровня витрины метрик.

    Уровень sb получается агрегатом по всем ТБ, потому что отдельного sb_id нет ни
    в одной таблице — в витрине метрик это просто строка с постоянным level_id.
    """
    t = tidy.copy()
    t["sb_id"] = SB_LEVEL_ID
    return [
        _agg(t, "gosb_id", "gosb", by_segment=True),
        _agg(t, "gosb_id", "gosb", by_segment=False),
        _agg(t, "tb_id", "tb", by_segment=True),
        _agg(t, "tb_id", "tb", by_segment=False),
        _agg(t, "sb_id", "sb", by_segment=True),
        _agg(t, "sb_id", "sb", by_segment=False),
    ]


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


# Профили истории организации. Раздаются так, чтобы в синтетике встретился
# КАЖДЫЙ класс модели прогноза (forecast.outflow_model):
#   persistent — отток два закрытых месяца подряд;
#   one_off    — отток только в последнем закрытом месяце;
#   season_out — сезонный спад с прогнозного месяца, с восстановлением год назад;
#   season_in  — сезонный бизнес: приход именно в прогнозном месяце;
#   flat       — ровный, без сигнала.
def _archetypes(orgs: pd.DataFrame) -> np.ndarray:
    """Профиль истории по организации. Отточные профили достаются только тем,
    у кого отток есть в последнем закрытом месяце, — иначе класс не сложится."""
    has_out = orgs["fl_outflow_qty"].to_numpy() >= 1
    r = RNG.random(len(orgs))
    kind = np.where(
        has_out,
        np.where(r < 0.35, "persistent", np.where(r < 0.70, "one_off", "season_out")),
        np.where(r < 0.75, "flat", "season_in"),
    )
    return kind.astype(object)


def _fact_outflow(company: pd.DataFrame, orgs: pd.DataFrame) -> pd.DataFrame:
    """Месячный факт оттока — ОТДЕЛЬНАЯ витрина, дэшем не используемая.

    Нужна forecast_lab: он перебирает и модель оттока, построенную по ней.

    Воспроизводится главное свойство прома: здесь отток заполнен ГУЩЕ, чем
    `fl_outflow_qty` в company_holding_metric (там он есть менее чем у процента
    пар, и модель истории из-за этого почти не работает). Поэтому к строкам с
    нулевым оттоком добавляется небольшая фоновая убыль — и лаборатория получает
    ветку, где эта витрина реально сильнее.
    """
    c = company[(company["level_name"] == "gosb")
                & (company["org_type"] == "inn")].copy()
    seg_of = dict(zip(orgs["inn"].astype("int64"), orgs["segment_name"]))
    tb_of = dict(zip(orgs["gosb_id"].astype(int), orgs["tb_id"].astype(int)))
    fl = c["current_fl_qty"].to_numpy(dtype=float)
    out = c["fl_outflow_qty"].to_numpy(dtype=float)
    # фоновая убыль там, где основная витрина показывает ноль
    extra = np.where(out > 0, 0.0,
                     np.rint(fl * RNG.uniform(0, 0.02, len(c))))
    out_full = np.minimum(out + extra, fl)
    prev = np.maximum(fl + out_full, fl)
    return pd.DataFrame({
        "report_dt": c["report_dt"].to_numpy(),
        "tb_id": [tb_of.get(int(g), 0) for g in c["level_id"]],
        "gosb_id": c["level_id"].astype(int).to_numpy(),
        "inn": c["org_id"].astype("int64").to_numpy(),
        "segment_name": [seg_of.get(int(i), "Микро") for i in c["org_id"]],
        "is_force": False,
        "mzp_fio": None,
        "saphr_id": None,
        "calc_fl_qty": fl.astype(int),
        "prev_m_overflow_qty": 0,
        "plan_payee_qty": prev.astype(int),
        "fact_payee_qty": (prev - out_full).astype(int),
        "outflow_qty": out_full.astype(int),
        "outflow_perc": (out_full / np.maximum(prev, 1)).round(4),
        "other_inn_emp_perc": 0.0,
        "m_avg_salary_amt": (c["current_fot_amt"].to_numpy(dtype=float)
                             / np.maximum(fl, 1)).round(2),
        "prev_m_avg_salary_amt": None,
        "next_m_avg_salary_amt": None,
        "prev_m_fl_val": prev.astype(int),
        "next_m_fl_val": None,
        "is_task": out_full > 0,
        "inserted_dttm": pd.Timestamp.now(),
    })


def _company_holding(orgs: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Витрина по организациям за MONTHS месяцев (нужна модели прогноза оттока).

    История рисуется НАЗАД от текущих значений: последний месяц (MAX_MONTH_END) в
    точности равен orgs, поэтому закрытый месяц и весь существующий дэш не
    меняются ни на цифру.

    Возвращает (строки витрины, профили организаций). Профиль нужен ежедневной
    витрине, чтобы намеренно воспроизвести оба кейса стыковки прогноза и факта.
    """
    month_ends = pd.date_range(end=MAX_MONTH_END, periods=MONTHS, freq="ME")
    cal = np.array([m.month for m in month_ends])
    is_season = np.isin(cal, SEASON_MONTHS)
    o = orgs.reset_index(drop=True)
    n, M = len(o), len(month_ends)
    kind = _archetypes(o)
    base_fl = o["current_fl_qty"].to_numpy(dtype=float)
    target_out = o["fl_outflow_qty"].to_numpy(dtype=float)
    sal = o["avg_salary"].to_numpy(dtype=float)

    # --- форма численности получателей (нормируется так, что последний мес = 1) ---
    w = np.tile(0.88 + 0.12 * np.arange(M) / (M - 1), (n, 1))
    w *= RNG.normal(1.0, 0.03, (n, M))
    w[kind == "season_in"] *= np.where(is_season, SEASON_UP, 1.0)
    w[kind == "season_out"] *= np.where(is_season, SEASON_DOWN, 1.0)
    decline = np.ones(M)
    decline[-3:] = (0.97, 0.93, 0.90)          # устойчивый отток «съедает» базу
    w[kind == "persistent"] *= decline
    w /= w[:, -1:]
    fl = np.rint(w * base_fl[:, None]).clip(min=0)

    # --- отток по месяцам ---
    out = (RNG.random((n, M)) < 0.08).astype(float)          # фон: изредка 1 человек
    pers = kind == "persistent"
    out[pers, -2] = np.maximum(1.0, np.rint(target_out[pers] * 0.7))
    out[pers, -3] = np.maximum(1.0, np.rint(target_out[pers] * 0.4))
    season_cols = np.where(is_season)[0]
    so = kind == "season_out"
    if len(season_cols) and so.any():
        out[np.ix_(so, season_cols)] = np.rint(base_fl[so, None] * 0.30)
    oo = kind == "one_off"
    out[oo, -3:-1] = 0.0                       # апрель и май чисто — отток разовый
    np.minimum(out, fl, out=out)
    # последний закрытый месяц — РОВНО значения orgs (регресс закрытого месяца)
    fl[:, -1] = base_fl
    out[:, -1] = target_out

    idx = np.repeat(np.arange(n), M)
    fl_flat, out_flat = fl.reshape(-1), out.reshape(-1)
    sal_flat = sal[idx]
    # год к году: разница с тем же месяцем прошлого года (первые 12 мес — 0)
    d_fl = np.zeros_like(fl)
    d_fl[:, 12:] = fl[:, 12:] - fl[:, :-12]

    df = pd.DataFrame({
        "report_dt": np.tile([m.date() for m in month_ends], n),
        "level_name": "gosb",
        "level_id": o["gosb_id"].to_numpy()[idx].astype(int),
        "org_type": "inn",
        "org_id": o["inn"].to_numpy()[idx].astype("int64"),
        "ul_outflow_qty": 0,
        "fl_outflow_qty": out_flat.astype(int),
        "fot_outflow_amt": (out_flat * sal_flat).round(2),
        "current_fot_amt": (fl_flat * sal_flat).round(2),
        "fot_y_1_diff_amt": (d_fl.reshape(-1) * sal_flat).round(2),
        "current_fl_qty": fl_flat.astype(int),
        "fl_y_1_diff_qty": d_fl.reshape(-1).astype(int),
        "emp_potential_qty": o["emp_potential_qty"].to_numpy()[idx],
        "fot_potential_amt": o["fot_potential_amt"].to_numpy()[idx],
        "modified_dttm": pd.Timestamp.now(),
    })
    # ФОТ/отток закрытого месяца — ровно из orgs (там неокруглённая база)
    last = np.arange(n) * M + (M - 1)
    df.loc[last, "fot_outflow_amt"] = o["fot_outflow_amt"].to_numpy()
    df.loc[last, "current_fot_amt"] = o["current_fot_amt"].to_numpy()

    # штат ≥ получателей; проникновение = получатели/штат (низкое = резерв привлечения)
    emp_k = RNG.uniform(1.05, 2.5, n)[idx]
    total_emp = np.maximum(np.rint(fl_flat * emp_k), fl_flat)
    df["total_emp_qty"] = total_emp
    df["zp_fl_perc"] = (fl_flat / np.maximum(total_emp, 1)).round(4)
    df["new_fl_cnt"] = RNG.integers(0, 20, len(df))
    df["np_cnt"] = RNG.integers(0, 10, len(df))

    # --- Строки уровней ТБ и СБ: та же организация, свёрнутая уровнем выше ---
    # На проме витрина хранит все три уровня в ОДНОЙ таблице (level_name =
    # 'gosb' / 'tb' / 'sb', level_id — номер соответствующей единицы, у банка 1).
    # Воспроизводим это здесь, потому что номера ТБ пересекаются со значениями
    # old_gosb_id: без фильтра по level_name join по old_gosb_id = level_id
    # затягивает агрегатные строки уровня ТБ в разбор обычного ГОСБ. Ошибка
    # воспроизводимая, и синтетика обязана её показывать.
    df["tb_id"] = o["tb_id"].to_numpy()[idx].astype(int)
    sum_cols = ["ul_outflow_qty", "fl_outflow_qty", "fot_outflow_amt", "current_fot_amt",
                "fot_y_1_diff_amt", "current_fl_qty", "fl_y_1_diff_qty",
                "emp_potential_qty", "fot_potential_amt", "total_emp_qty",
                "new_fl_cnt", "np_cnt"]
    upper = []
    for level_name, level_key in (("tb", "tb_id"), ("sb", None)):
        keys = ["report_dt", "org_id"] + ([level_key] if level_key else [])
        a = df.groupby(keys, as_index=False)[sum_cols].sum()
        a["level_name"] = level_name
        a["level_id"] = a.pop(level_key) if level_key else SB_LEVEL_ID
        a["org_type"] = "inn"
        a["zp_fl_perc"] = (a["current_fl_qty"] /
                           a["total_emp_qty"].clip(lower=1)).round(4)
        a["modified_dttm"] = pd.Timestamp.now()
        upper.append(a)
    df = df.drop(columns=["tb_id"])

    # Строки уровня holding / head_holding (в отчёте фильтруются: org_type='inn').
    # org_id у них — id холдинга, а не ИНН; значения крупнее (агрегаты).
    # Достаточно последнего месяца: история по холдингам нигде не читается.
    lastm = df[df["report_dt"] == MAX_MONTH_END.date()]
    n_h = max(50, len(lastm) // 12)
    samp = lastm.sample(n=n_h, random_state=7).copy()
    samp["org_type"] = RNG.choice(["holding", "head_holding"], size=n_h, p=[0.7, 0.3])
    samp["org_id"] = (9_000_000_000_000 + np.arange(n_h)).astype("int64")
    mult = RNG.integers(2, 6, n_h)
    for c in ("current_fl_qty", "fl_outflow_qty"):
        samp[c] = (samp[c].to_numpy() * mult)
    samp["current_fot_amt"] = samp["current_fot_amt"].to_numpy() * mult
    samp["fot_potential_amt"] = samp["fot_potential_amt"].to_numpy() * mult

    profiles = pd.DataFrame({
        "inn": o["inn"].astype("int64"), "gosb_id": o["gosb_id"].astype(int),
        "kind": kind, "base_fl": base_fl.astype(int),
    })
    return pd.concat([df, samp] + upper, ignore_index=True), profiles


def _day_outflow(orgs: pd.DataFrame, profiles: pd.DataFrame) -> pd.DataFrame:
    """Ежедневная витрина оттока за DAY_OUTFLOW_MONTHS месяцев.

    Витрина хранит ВСЕ месяцы, а не только текущий, — иначе не проверить сценарий
    «пересобрать отчёт за прошлый месяц». Закрытые месяцы отдаются целиком
    (act_dt = конец месяца), текущий — частично (act_dt = ACT_DT).

    Строка на каждую ПРОШЕДШУЮ выплатную дату месяца (аванс + основная). У аванса
    неоплаченных больше, поэтому min(outflow_unpaid_m_qty) по двум датам — это и
    есть «отток по итогу обеих дат зачисления», как считает бизнес.

    Намеренно воспроизводим оба кейса стыковки прогноза и факта:
      * у части организаций с сильным прогнозным оттоком факт ≈ 0 — прогноз не
        оправдался, почти все уже зачислились;
      * у части ровных, наоборот, крупный наблюдаемый отток, которого модель не ждала.
    У ~30% организаций прошёл только аванс — на них проверяется остаточный риск.
    """
    o = orgs.merge(profiles, on=["inn", "gosb_id"], how="left")
    rows = []
    months = [(CUR_MONTH_END.to_period("M") - k).to_timestamp("M")
              for k in range(DAY_OUTFLOW_MONTHS - 1, -1, -1)]
    for month_end in months:
        is_cur = month_end.to_period("M") == CUR_MONTH_END.to_period("M")
        act = ACT_DT if is_cur else month_end
        for r in o.itertuples():
            fl_prev = int(r.current_fl_qty)
            kind = r.kind if isinstance(r.kind, str) else "flat"
            u = float(RNG.random())
            if kind in ("persistent", "season_out"):
                observed = (int(RNG.integers(0, 4)) if u < 0.45
                            else int(round(fl_prev * RNG.uniform(0.05, 0.20))))
            elif kind == "flat" and u > 0.85:
                observed = int(round(fl_prev * RNG.uniform(0.15, 0.30)))
            else:
                observed = int(RNG.integers(0, 3))
            observed = min(observed, fl_prev)
            two_pay = bool(RNG.random() < 0.70)
            paid = fl_prev - observed
            if not two_pay:                      # прошёл только аванс — часть ещё в пути
                paid = int(round(paid * RNG.uniform(0.80, 0.92)))
            sal = float(r.avg_salary)
            expect = max(1, fl_prev)
            for order in ((1, 2) if two_pay else (1,)):
                # у аванса «неоплаченных» больше: часть получит только основную выплату
                extra = int(RNG.integers(0, 6)) if (two_pay and order == 1) else 0
                unpaid_m = min(expect, observed + extra)
                day_out = min(expect, unpaid_m + int(RNG.integers(0, 3)))
                pay_dt = month_end.replace(day=10 if order == 1 else 25)
                rows.append({
                    "row_code": (f"{month_end:%Y%m%d}_{act:%Y%m%d}_"
                                 f"{int(r.gosb_id)}_{int(r.inn)}_{order}"),
                    "report_dt": month_end.date(),
                    "act_dt": act.date(),
                    "tb_id": int(r.tb_id),
                    "gosb_id": int(r.gosb_id),
                    "org_inn": int(r.inn),
                    "segment_name": SEG_SHORT[int(r.seg_code)],
                    "company_name": None,
                    "holding_name": None,
                    "is_security_force": False,
                    "saphr_id": (None if RNG.random() < 0.15
                                 else int(1_000_000 + int(r.inn) % 900_000)),
                    "salary_payment_dt": pay_dt.date(),
                    "payment_order_num": order,
                    "expect_fl_qty": expect,
                    "overflow_qty": 0,
                    "plan_fl_qty": expect,
                    "fl_day_qty": max(0, expect - day_out),
                    "fl_2_d_qty": max(0, expect - unpaid_m),
                    "fact_fl_qty": paid,
                    "outflow_unpaid_report_qty": day_out,
                    "outflow_unpaid_2_d_qty": unpaid_m,
                    "outflow_unpaid_m_qty": unpaid_m,
                    "outflow_day_perc": round(day_out / expect, 4),
                    "outflow_2_d_perc": round(unpaid_m / expect, 4),
                    "outflow_unpaid_m_perc": round(unpaid_m / expect, 4),
                    "overflow_other_inn_perc": 0.0,
                    "m_avg_salary_amt": round(sal, 2),
                    "prev_m_avg_salary_amt": round(sal * float(RNG.uniform(0.95, 1.05)), 2),
                    "next_m_avg_salary_amt": None,
                    "fl_crnt_m_qty": paid,
                    "fl_prev_m_qty": fl_prev,
                    "fl_next_m_qty": 0,
                    "is_d_outflow_task": bool(unpaid_m > 0 and RNG.random() < 0.3),
                    "client_communication_infopovod": None,
                    "is_oktmo": None,
                    "oktmo_subject_code": None,
                    "oktmo_subject_district_code": None,
                    "oktmo_subject_district_city_code": None,
                    "oktmo_code": None,
                    "inserted_dttm": pd.Timestamp.now(),
                    "author_login": "synth",
                })
    return pd.DataFrame(rows)


def _pipeline(funnel: pd.DataFrame, orgs: pd.DataFrame) -> pd.DataFrame:
    """Помесячная раскладка плана привлечения (инструмент «Пайплайн»).

    В воронке plan_staff_deal_qty — план на все ТРИ месяца жизни сделки; здесь он
    раскладывается по месяцам, начиная с месяца создания. Ключ —
    coalesce(deal_code, task_code), поэтому в пайплайн попадают и офферы без
    заведённой сделки: на них проверяется ветка COALESCE в запросе дэша.
    """
    sal_by_inn = orgs.groupby("inn")["avg_salary"].mean().to_dict()
    rows, seen = [], set()
    for r in funnel.itertuples():
        deal = None if pd.isna(r.deal_code) else r.deal_code
        code = deal or r.task_code
        if not code or code in seen:
            continue
        plan = int(r.plan_staff_deal_qty or 0)
        if deal is None:
            # часть офферов без сделки тоже стоит в пайплайне — план по потенциалу
            unreal = int(r.unrealized_deal_potential or 0)
            if unreal <= 0 or RNG.random() > 0.15:
                continue
            plan = int(max(1, round(unreal * RNG.uniform(0.2, 0.6))))
        if plan <= 0:
            continue
        seen.add(code)
        start = (pd.Timestamp(r.deal_create_dttm) if not pd.isna(r.deal_create_dttm)
                 else pd.Timestamp(r.task_create_dt))
        parts = [int(round(plan * s)) for s in PIPELINE_SPLIT[:-1]]
        parts.append(plan - sum(parts))
        sal = float(sal_by_inn.get(int(r.inn), 60_000))
        for k, part in enumerate(parts):
            part = max(0, part)
            rows.append({
                "pl_task_deal_code": code,
                "pl_month_num": int((start.to_period("M") + k).month),
                "pl_plan_np_amt": part,
                "pl_plan_fot_amt": int(round(part * sal)),
            })
    if not rows:
        return pd.DataFrame(columns=["pl_task_deal_code", "pl_month_num",
                                     "pl_plan_np_amt", "pl_plan_fot_amt"])
    return (pd.DataFrame(rows)
            .drop_duplicates(subset=["pl_task_deal_code", "pl_month_num"])
            .reset_index(drop=True))


def _deal_codes(funnel: pd.DataFrame) -> pd.DataFrame:
    """Справочник кодов сделок: код → (ГОСБ, ИНН, сотрудник, месяц создания).

    Месяц создания нужен, чтобы восстановить ГОД для `pl_month_num` (в пайплайне лежит
    только номер месяца): сделка живёт 3 месяца, значит планировать может лишь
    m0, m0+1, m0+2.
    """
    f = funnel.copy()
    f["code"] = f["deal_code"].where(f["deal_code"].notna(), f["task_code"])
    f["m0"] = pd.to_datetime(
        f["deal_create_dttm"].where(f["deal_create_dttm"].notna(),
                                    pd.to_datetime(f["task_create_dt"]))
    ).dt.to_period("M")
    return (f.dropna(subset=["code"])
            .groupby("code", as_index=False)
            .agg(tb_id=("tb_id", "min"), gosb_id=("gosb_id", "min"), inn=("inn", "min"),
                 saphr_id=("isu_struct_saphr_id", "min"), m0=("m0", "min"),
                 segment_name=("segment_name", "min"), company_name=("company_name", "min"),
                 deal_code=("deal_code", "min"), task_code=("task_code", "min")))


def _plan_months(funnel: pd.DataFrame, pipeline: pd.DataFrame) -> pd.DataFrame:
    """План пайплайна с ВОССТАНОВЛЕННЫМ месяцем (год берётся из даты создания сделки)."""
    codes = _deal_codes(funnel)
    pj = pipeline.merge(codes, left_on="pl_task_deal_code", right_on="code", how="inner")
    off = (pj["pl_month_num"] - pj["m0"].apply(lambda p: p.month) + 12) % 12
    pj = pj[off <= 2].copy()
    pj["plan_month"] = [m0 + int(o) for m0, o in zip(pj["m0"], off[off <= 2])]
    return pj


# Распределение product_cmnt как на проме: учтено / не в учёте / для информации
_CMNT = ("учтено", "не соответствует критериям учета",
         "для информации, не участвует в расчете kpi")
_CMNT_P = (0.823, 0.123, 0.054)


def _motivation(funnel: pd.DataFrame, pipeline: pd.DataFrame,
                orgs: pd.DataFrame) -> pd.DataFrame:
    """Факт привлечения по сделкам, помесячно (витрина премирования МЗП).

    По каждой паре (код сделки, месяц плана) генерируется факт: за ЗАКРЫТЫЕ месяцы —
    план × реализуемость ГОСБ с шумом, за текущий — частично (месяц ещё идёт), за
    будущие — ноль. Часть строк помечается как не «учтено» (фрод и «для информации»),
    чтобы фильтр было на чём проверить; в них факт тоже есть — иначе фильтр ничего
    бы не менял. Плюс строки чужой метрики (1003007), которые дэш обязан отбросить.
    """
    pj = _plan_months(funnel, pipeline)
    if pj.empty:
        return pd.DataFrame()
    # реализуемость своя у каждого ГОСБ — иначе коэффициент везде одинаковый
    real_by_gosb = {int(g): float(RNG.uniform(0.35, 0.85))
                    for g in pj["gosb_id"].dropna().unique()}
    seg_of_inn = {int(i): SEG_SHORT[int(s)]
                  for i, s in zip(orgs["inn"], orgs["seg_code"])}
    cur = CUR_MONTH_END.to_period("M")
    rows = []
    for r in pj.itertuples():
        plan = int(r.pl_plan_np_amt or 0)
        if plan <= 0:
            continue
        k = real_by_gosb.get(int(r.gosb_id), 0.6) * float(RNG.uniform(0.7, 1.3))
        if r.plan_month < cur:
            fact = int(round(plan * k))                    # месяц закрыт — факт полный
        elif r.plan_month == cur:
            fact = int(round(plan * k * MONTH_ELAPSED))    # месяц идёт — факт частичный
        else:
            fact = 0                                       # будущее — фактa ещё нет
        cmnt = str(RNG.choice(_CMNT, p=_CMNT_P))
        bad = cmnt != "учтено"
        month_end = r.plan_month.to_timestamp("M").date()
        base = {
            "report_dt": month_end, "tb_id": int(r.tb_id), "gosb_id": int(r.gosb_id),
            "inn": int(r.inn), "company_name": r.company_name,
            "segment_name": seg_of_inn.get(int(r.inn), "ММБ"),
            "agrmnt_num": int(RNG.integers(30_000_000, 90_000_000)),
            "saphr_id": int(r.saphr_id) if pd.notna(r.saphr_id) else None,
            "position_name": "Менеджер по продаже зарплатных проектов",
            "metric_id": 1000636,
            "product_group_name": "Новые получатели", "product_id": 2,
            "product_name": "Новые получатели b2b",
            "sales_amt": float(fact),
            "up_weight": 1.0,
            "sales_prediction_percent": round(float(k) * 100, 2),
            "up_sales_amt": float(fact),
            "consultation_start_dt": r.m0.to_timestamp().date(),
            "consultation_success_dt": r.m0.to_timestamp("M").date(),
            "product_cmnt": cmnt, "is_motiv": not bad, "is_fraud": bad,
            "metric_name": "Новые получатели b2b",
            "sale_plan_amt": None, "sale_prediction_amt": float(plan),
            "up_sale_prediction_amt": None, "kpp": None,
            "deal_code": r.deal_code if pd.notna(r.deal_code) else None,
            "offer_code": None,
            "task_code": r.task_code if pd.notna(r.task_code) else None,
            "ul_epk_id": int(1_200_000_000_000_000_000 + int(r.inn)),
            "fl_epk_id": None, "sale_approve_dt": None,
            "calc_dttm": pd.Timestamp.now(), "inserted_dttm": pd.Timestamp.now(),
            "author_login": "synth",
        }
        rows.append(base)
        # строка ЧУЖОЙ метрики: дэш обязан отбросить её по metric_id
        if RNG.random() < 0.25:
            other = dict(base)
            other.update({"metric_id": 1003007, "metric_name": "Новые физические лица b2b",
                          "product_name": "Новые физические лица b2b", "product_id": 1,
                          "product_group_name": "Новые физические лица",
                          "sales_amt": float(fact * 3)})
            rows.append(other)
    return pd.DataFrame(rows)


def _forecast_delta(orgs: pd.DataFrame, company: pd.DataFrame, funnel: pd.DataFrame,
                    pipeline: pd.DataFrame, day_outflow: pd.DataFrame,
                    motivation: pd.DataFrame) -> dict:
    """Дельта прогноза по (ГОСБ, сегмент): −отток +приток +пайплайн.

    Считается ТЕМИ ЖЕ функциями forecast.py, что и в дэше, и из тех же таблиц,
    поэтому план текущего месяца не разъезжается с прогнозом, который дэш покажет.
    """
    from uzp_dash.dashboards.tb_health import forecast

    # level_name='gosb' обязателен: в витрине лежат ещё строки уровней ТБ и СБ, и без
    # фильтра агрегаты уровня ТБ вошли бы в историю как обычный ГОСБ (номера ТБ
    # встречаются среди old_gosb_id) — ровно та ошибка, от которой защищается дэш
    hist = (company[(company["org_type"] == "inn") & (company["level_name"] == "gosb")]
            .rename(columns={"level_id": "new_gosb_id", "org_id": "inn"})
            [["new_gosb_id", "inn", "report_dt", "fl_outflow_qty", "current_fl_qty"]])

    day = (day_outflow.groupby(["gosb_id", "org_inn"], as_index=False)
           .agg(seg_day=("segment_name", "max"),
                out_observed=("outflow_unpaid_m_qty", "min"),
                paid_mtd=("fact_fl_qty", "max"),
                fl_prev_m=("fl_prev_m_qty", "max"),
                avg_salary_m=("m_avg_salary_amt", "max"))
           .rename(columns={"gosb_id": "new_gosb_id", "org_inn": "inn"}))

    # план и факт пайплайна помесячно — ровно те же функции, что и в дэше
    plan_m = (_plan_months(funnel, pipeline)
              .rename(columns={"gosb_id": "new_gosb_id"})
              .groupby(["new_gosb_id", "inn", "saphr_id", "plan_month"], as_index=False)
              .agg(seg_funnel=("segment_name", "min"), plan_np=("pl_plan_np_amt", "sum"),
                   plan_fot=("pl_plan_fot_amt", "sum"), n_deals=("code", "nunique")))
    plan_m["plan_month"] = [p.to_timestamp("M").date() for p in plan_m["plan_month"]]
    mv = motivation[(motivation["metric_id"] == 1000636)
                    & (motivation["product_cmnt"] == "учтено")]
    fact_m = (mv.rename(columns={"gosb_id": "new_gosb_id"})
              .groupby(["new_gosb_id", "inn", "saphr_id", "report_dt"], as_index=False)
              .agg(fact_np=("sales_amt", "sum"))
              .rename(columns={"report_dt": "plan_month"}))

    tb_of = {int(r.gosb_id): int(r.tb_id) for r in orgs.itertuples()}
    conv = forecast.conversion_by_month(plan_m, fact_m, CUR_MONTH_END, tb_of)
    # дэшу историю сворачивает БД; здесь она уже в памяти — сворачиваем той же
    # функцией, которая описывает, что именно должен посчитать SQL
    pred = forecast.outflow_model(
        forecast.aggregate_history(hist, CUR_MONTH_END), CUR_MONTH_END)
    rec = forecast.reconcile(day, pred, MONTH_ELAPSED)
    pipe_fc = forecast.pipeline_current(plan_m, fact_m, CUR_MONTH_END,
                                        conv["of_gosb"], conv["sb"])
    ofc = forecast.org_forecast(rec, pipe_fc, seg_of={})

    code_of = {v: k for k, v in SEG_SHORT.items()}
    delta: dict = {}
    for r in ofc.itertuples():
        seg = code_of.get(r.seg_name)
        if seg is None or pd.isna(r.new_gosb_id):
            continue
        d_fl = float(r.delta_fl)
        cur = delta.setdefault((int(r.new_gosb_id), int(seg)), [0.0, 0.0])
        cur[0] += d_fl
        cur[1] += d_fl * float(r.avg_salary_m or 0.0)
    return {k: (v[0], v[1]) for k, v in delta.items()}


def _metrics_current(orgs: pd.DataFrame, latest: pd.DataFrame, company: pd.DataFrame,
                     funnel: pd.DataFrame, pipeline: pd.DataFrame,
                     day_outflow: pd.DataFrame, motivation: pd.DataFrame) -> pd.DataFrame:
    """Строки uzp_dwh_metrics за ТЕКУЩИЙ (незакрытый) месяц.

    План выводится ИЗ прогноза: plan = прогноз / целевое выполнение. Так сценарий
    («ЮЗБ проваливает план, остальные в норме») воспроизводится точно, без подгонки,
    и при этом план текущего месяца не равен факту закрытого.

    fact_amt — ЧАСТИЧНАЯ ведомость. Дэш её не использует (в этом и смысл прогноза),
    но она должна быть, как на проме: если кто-то возьмёт её по ошибке, это сразу
    видно по заниженным цифрам.
    """
    delta = _forecast_delta(orgs, company, funnel, pipeline, day_outflow, motivation)
    start = CUR_MONTH_END.replace(day=1)
    tidy = []
    for r in latest.itertuples():
        d_fl, d_fot = delta.get((int(r.gosb_id), int(r.seg_id)), (0.0, 0.0))
        sal = float(r.avg_salary)
        fc_r = max(0.0, float(r.fact_r) + d_fl)
        fc_fot = max(0.0, float(r.fact_r) * sal + d_fot)
        plan_r = fc_r / float(r.texec) if float(r.texec) else fc_r
        tidy.append({
            "tb_id": int(r.tb_id), "gosb_id": int(r.gosb_id), "seg_id": int(r.seg_id),
            "start_dt": start.date(), "end_dt": CUR_MONTH_END.date(),
            "plan_r": plan_r, "fact_r": fc_r * PARTIAL_FACT_SHARE,
            "fot_plan": plan_r * sal, "fot_fact": fc_fot * PARTIAL_FACT_SHARE,
        })
    tidy = pd.DataFrame(tidy)
    return pd.concat(_level_frames(tidy), ignore_index=True)[OUT_COLS]


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
def _bulk(engine: Engine, df: pd.DataFrame, table: str, schema: str | None = None) -> int:
    df = df.where(pd.notnull(df), None)
    df.to_sql(table, engine, schema=schema or config.SCHEMA, if_exists="append",
              index=False, method="multi", chunksize=1000)
    return len(df)
