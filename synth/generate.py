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
from sqlalchemy.engine import Engine

from uzp_dash import config
from uzp_dash.db import read_sql

RNG = np.random.default_rng(42)

METRIC_FOT = 1000164          # Общий ФОТ, млн руб
METRIC_RECIPIENTS = 12400196  # Количество уникальных получателей до ИНН

# Сегменты: extended_dim_1 = extended_dim_id (1 = все сегменты)
SEGMENTS = {
    20: "Крупнейшие",
    21: "Крупные",
    22: "Средние",
    23: "Малые",
    24: "Микро",
    25: "Рег. госсектор",
}
SEG_IDS = list(SEGMENTS)
SEG_WEIGHTS = np.array([0.10, 0.18, 0.22, 0.25, 0.15, 0.10])  # доля получателей по сегментам

PROBLEM_TB_SHORT = "ЮЗБ"        # этот ТБ явно не выполняет план
MONTHS = 24
MAX_MONTH_END = pd.Timestamp("2026-06-30")
GOSB_PER_TB = 8                 # представительная выборка ГОСБ на ТБ
ORGS_TOTAL = 4000


# --------------------------------------------------------------------------- #
def generate_all(engine: Engine) -> dict[str, int]:
    seg_df = _segments()
    gosb = _pick_gosb(engine)
    metrics, latest = _metrics(gosb)
    orgs = _orgs(gosb, latest)
    company = _company_holding(orgs)
    dim_company = orgs[["inn", "extended_dim_id"]].drop_duplicates("inn")
    funnel = _funnel(orgs, gosb)

    counts = {}
    counts["uzp_dim_extended_metrics"] = _bulk(engine, seg_df, "uzp_dim_extended_metrics")
    counts["dim_company"] = _bulk(engine, dim_company, "dim_company")
    counts["uzp_dwh_metrics"] = _bulk(engine, metrics, "uzp_dwh_metrics")
    counts["uzp_dwh_company_holding_metric"] = _bulk(engine, company, "uzp_dwh_company_holding_metric")
    counts["uzp_dwh_sale_funnel_task"] = _bulk(engine, funnel, "uzp_dwh_sale_funnel_task")
    return counts


# --------------------------------------------------------------------------- #
def _segments() -> pd.DataFrame:
    rows = [{"extended_dim_id": 1, "extended_dim_name": "Все сегменты"}]
    rows += [{"extended_dim_id": k, "extended_dim_name": v} for k, v in SEGMENTS.items()]
    return pd.DataFrame(rows)


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


def _target_exec(tb_short: str, gosb_id: int, seg_id: int) -> float:
    """Целевое выполнение плана по (ТБ, ГОСБ, сегмент). <1 — провал."""
    r = RNG.normal(1.03, 0.03)
    if tb_short == PROBLEM_TB_SHORT:
        r = RNG.normal(0.93, 0.03)                       # весь ТБ слабее
        if seg_id in (23, 24):                           # Малые/Микро — хуже всего
            r = RNG.normal(0.82, 0.03)
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
        sal_musd = gr.avg_salary / 1e6            # средняя ЗП, млн руб
        for si, seg_id in enumerate(SEG_IDS):
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
                    "fot_plan": plan_r * sal_musd, "fot_fact": fact_r * sal_musd,
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

    gosb_gap = latest.groupby("gosb_id")["gap"].sum()   # разрыв получателей по ГОСБ

    rows = []
    inn_seq = 1_000_000_000
    for _, gr in gosb.iterrows():
        gid = int(gr.old_gosb_id)
        n = int(org_alloc.get(gid, 5))
        sub = latest[latest.gosb_id == gid]
        seg_fact = sub.set_index("seg_id")["fact_r"]
        p = (seg_fact / seg_fact.sum()).reindex(SEG_IDS).fillna(0).to_numpy()
        if p.sum() == 0:
            p = SEG_WEIGHTS / SEG_WEIGHTS.sum()
        assign = RNG.choice(SEG_IDS, size=n, p=p / p.sum())
        for seg_id in assign:
            inn_seq += int(RNG.integers(1, 900))
            fl = int(max(1, RNG.gamma(2.0, 60)))            # получателей в организации
            sal = float(gr.avg_salary * RNG.uniform(0.85, 1.2))
            rows.append({
                "inn": inn_seq, "gosb_id": gid, "tb_id": int(gr.tb_id),
                "tb_short": gr.tb_short_name, "extended_dim_id": int(seg_id),
                "segment_name": SEGMENTS[int(seg_id)],
                "current_fl_qty": fl, "avg_salary": round(sal, 0),
                "current_fot_amt": round(fl * sal, 2),
                # «сырой» потенциал/возврат — отмасштабируем ниже под разрыв ГОСБ
                "_pull": max(0.0, RNG.normal(0.14, 0.07)) * fl,
                "_back": max(0.0, RNG.normal(0.06, 0.04)) * fl,
            })

    df = pd.DataFrame(rows)
    # Масштабируем потенциал+возврат провальных ГОСБ до ~1.3× разрыва,
    # чтобы список организаций реально закрывал недобор до плана.
    for gid, g in df.groupby("gosb_id"):
        gap = float(gosb_gap.get(gid, 0.0))
        cur = g["_pull"].sum() + g["_back"].sum()
        if gap > 0 and cur > 0:
            factor = (1.8 * gap) / cur   # с запасом: список «работать» закрывает план
            df.loc[g.index, "_pull"] *= factor
            df.loc[g.index, "_back"] *= factor

    df["emp_potential_qty"] = df["_pull"].round(3)
    df["fot_potential_amt"] = (df["_pull"] * df["avg_salary"]).round(2)
    df["fl_outflow_qty"] = df["_back"].round().astype(int)
    df["fot_outflow_amt"] = (df["_back"] * df["avg_salary"]).round(2)
    return df.drop(columns=["_pull", "_back"])


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
    return df


# --- Банки фраз для свободного текста воронки (детерминированно, без API) --- #
BANKS = ["ВТБ", "Альфа-Банк", "Т-Банк", "Газпромбанк", "Райффайзен"]
OUTFLOW_REASONS = [
    "Смена зарплатного банка", "Текучка персонала", "Сокращение штата",
    "Недовольство условиями обслуживания", "Переход сотрудников на самозанятость",
    "Сезонные отпуска", "Ликвидация организации",
]
# причины, при которых работать бессмысленно
DEADEND = {"Ликвидация организации"}

ROLE_BY_SEGMENT = {  # какая роль ведёт сегмент
    "Крупнейшие": "МКК", "Крупные": "МКК", "Средние": "МЗП",
    "Малые": "МЗП", "Микро": "СЗП", "Рег. госсектор": "МЗП",
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
    last3_start = MAX_MONTH_END - pd.Timedelta(days=90)

    for _, o in worked.iterrows():
        # доминирующий рычаг организации
        outflow_heavy = o.fl_outflow_qty >= max(1, 0.6 * o.emp_potential_qty)
        role = ROLE_BY_SEGMENT.get(o.segment_name, "МЗП")
        n_tasks = int(RNG.integers(1, 4))
        for _ in range(n_tasks):
            created = MAX_MONTH_END - pd.Timedelta(days=int(RNG.integers(5, 90)))
            active = created + pd.Timedelta(days=int(RNG.integers(0, 6)),
                                            hours=int(RNG.integers(8, 19)))
            closed = bool(RNG.random() < 0.8)
            success = closed and bool(RNG.random() < 0.5)
            status = ("Закрыта: Своевременно" if success
                      else "Закрыта: С просрочкой" if closed
                      else "Не закрыта: Просрочена")
            is_outflow = outflow_heavy and RNG.random() < 0.8
            if is_outflow:
                tt, text, comment, quest, unreal = _text_outflow(o, success)
            else:
                tt, text, comment, quest, unreal = _text_attract(o, success)
            plan_deal = int(max(0, round(o.emp_potential_qty * RNG.uniform(0.5, 1.2)))) if not is_outflow else 0
            fact_deal = int(round(plan_deal * (RNG.uniform(0.6, 1.0) if success else RNG.uniform(0.0, 0.4))))
            rows.append({
                "report_dt": MAX_MONTH_END.date(),
                "tb_id": int(o.tb_id), "tb_name": tb_full.get(int(o.tb_id)),
                "gosb_id": int(o.gosb_id), "gosb_name": gosb_name.get(int(o.gosb_id)),
                "inn": int(o.inn), "company_name": f"Организация {o.inn}",
                "segment_name": o.segment_name,
                "task_type": tt, "task_subtype": None, "task_category": "Задача",
                "task_code": f"T{int(o.inn)}-{int(RNG.integers(1000,9999))}",
                "task_create_dt": created.date(),
                "is_task_closed": closed,
                "is_task_closed_success": success,
                "is_task_in_progress": not closed,
                "task_text_status": status,
                "role_code": role,
                "last_active_type": str(RNG.choice(["Звонок", "Встреча"], p=[0.65, 0.35])),
                "last_active_status": "Исполнена" if closed else "В работе",
                "last_active_dttm": active,
                "unrealized_deal_potential": unreal,
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
        ]))
        quest = f"1. Получено согласие\nДа\n2. Планируемое привлечение\n{n} чел\n3. Комментарий\n{comment}"
        unreal = int(RNG.integers(0, 3))
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
