"""Управление портфелем: фактический отток и приход из пайплайна.

ПРОГНОЗ ЗДЕСЬ НЕ СЧИТАЕТСЯ. Он приходит готовым из витрины метрик
(`uzp_dwh_metrics.prediction_amt`) на грейне «единица × сегмент» — `build_matrix`
и `build_totals` только раскладывают его против плана.

Раньше в этом файле жила модель оттока: сезонность, устойчивость, разовость и
стыковка с ежедневной витриной. Она удалена целиком вместе с ежедневной витриной —
отчёт больше ничего не предсказывает, он показывает случившееся:

  * `org_outflow`       — фактический отток за три закрытых месяца за вычетом
    вернувшихся, свёрнутый до пары (ГОСБ, ИНН);
  * `pipeline_current`  — плановый приход НП, с поправкой на историческую
    реализуемость сделок;
  * `build_matrix` / `build_totals` — план против прогноза витрины.

Все функции — чистые преобразования DataFrame, без обращений к БД.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RUB_TO_MLN = 1e6

# Коэффициент реализуемости пайплайна держим в разумных пределах: 0 занулил бы
# весь пайплайн, >1 означал бы перевыполнение планов сделок.
CONV_MIN, CONV_MAX = 0.2, 1.0
# Свой коэффициент ГОСБ считается, только если за закрытые месяцы он планировал не
# меньше этого. На меньших объёмах отношение факт/план — шум: одна сделка даёт 0.0 или
# 1.0. Такие ГОСБ берут коэффициент ТБ, и в карточке это подписано явно.
CONV_MIN_PLAN = 30.0



def num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    """Числовая колонка, которой может не быть вовсе.

    `pd.to_numeric(df.get(col))` для отсутствующей колонки возвращает СКАЛЯР (nan),
    и следующий `.fillna()` падает с AttributeError. Это выстреливает на пустых
    срезах — например, когда у ТБ нет ни одной сделки в пайплайне.
    """
    if col in df:
        return pd.to_numeric(df[col], errors="coerce").fillna(default)
    return pd.Series(default, index=df.index, dtype="float64")


def _as_period(df: pd.DataFrame, col: str = "plan_month") -> pd.DataFrame:
    """Привести месяц к Period и ключи к Int64 — иначе merge молча не сойдётся."""
    out = df.copy()
    out["per"] = pd.PeriodIndex(pd.to_datetime(out[col]), freq="M")
    for c in ("new_gosb_id", "inn", "saphr_id"):
        if c in out:
            out[c] = pd.to_numeric(out[c], errors="coerce").astype("Int64")
    return out


def _conv_k(plan: float, fact: float) -> tuple[float, float | None, bool]:
    """Коэффициент реализуемости по паре (план, факт): значение, сырое, упёрся ли."""
    raw = (fact / plan) if plan > 0 else None
    k = float(np.clip(raw if raw is not None else 1.0, CONV_MIN, CONV_MAX))
    return k, raw, raw is not None and not (CONV_MIN <= raw <= CONV_MAX)


def conversion_by_month(plan_m: pd.DataFrame, fact_m: pd.DataFrame, ref_cur,
                        tb_of: dict | None = None) -> dict:
    """Реализуемость пайплайна: ФАКТ продаж против плана по ЗАКРЫТЫМ месяцам.

    Грейн сравнения — (месяц, ГОСБ, ИНН, сотрудник). Это принципиально: сотрудник мог
    завести две сделки по одной организации и обе запланировать на один месяц (10 и 15),
    и пришедшие люди относятся к их СУММЕ (25), а не к каждой сделке по отдельности.
    Группировку даёт запрос, здесь остаётся свернуть по ГОСБ.

    Считается сразу на ТРЁХ уровнях, потому что отчёт строится по всему банку и у
    каждого уровня свой коэффициент: у ГОСБ — свой, у ТБ — свой (он же фолбэк для
    ГОСБ с малым объёмом), у банка — свой. Раньше уровень СБ получал жёсткую
    единицу, и в блоке по банку всегда стояло «коэф. 1.00».

    Текущий месяц исключён: он не отработан, его неполный факт занизил бы коэффициент.

    Диагностика возвращает СЫРОЕ значение до клипа: если реальная конверсия ниже
    CONV_MIN, клип поднимает её до пола и тем самым ЗАВЫШАЕТ вклад пайплайна. Молчать
    об этом нельзя — иначе в логе видно ровно «0.20» и непонятно, это настоящая
    конверсия или сработавшая граница.

    `tb_of` — соответствие ГОСБ → ТБ (единственный источник — queries.GOSB_FLAGS).
    """
    empty_diag = {"months": 0, "plan": 0.0, "fact": 0.0, "tb_raw": None,
                  "tb_clipped": False, "n_gosb": 0, "n_gosb_clipped": 0,
                  "n_gosb_fallback": 0}
    out = {"by_gosb": {}, "by_tb": {}, "of_gosb": {}, "sb": 1.0,
           "diag_tb": {}, "diag_sb": dict(empty_diag)}
    if plan_m is None or plan_m.empty:
        return out
    tb_of = tb_of or {}
    cur = pd.Period(pd.Timestamp(ref_cur), freq="M")
    p = _as_period(plan_m)
    all_gosb = {int(g) for g in p["new_gosb_id"].dropna()}
    p = p[p["per"] < cur]                       # только закрытые месяцы
    if p.empty:
        return out

    keys = ["new_gosb_id", "inn", "saphr_id", "per"]
    if fact_m is not None and not fact_m.empty:
        f = _as_period(fact_m)
        p = p.merge(f[keys + ["fact_np"]], on=keys, how="left")
    p["fact_np"] = num(p, "fact_np")
    p["plan_np"] = pd.to_numeric(p["plan_np"], errors="coerce").fillna(0.0)
    p = p.dropna(subset=["new_gosb_id"]).copy()
    p["tb_id"] = [tb_of.get(int(g)) for g in p["new_gosb_id"]]

    # --- ГОСБ: свой коэффициент только при достаточном объёме плана --------- #
    by_gosb, gosb_clipped, gosb_fallback = {}, {}, {}
    for gid, g in p.groupby("new_gosb_id"):
        tb = tb_of.get(int(gid))
        pl = float(g["plan_np"].sum())
        if pl < CONV_MIN_PLAN:
            # объёма мало — свой коэффициент был бы шумом, ГОСБ уйдёт на коэффициент ТБ
            gosb_fallback[tb] = gosb_fallback.get(tb, 0) + 1
            continue
        k, raw, clipped = _conv_k(pl, float(g["fact_np"].sum()))
        by_gosb[int(gid)] = k
        if clipped:
            gosb_clipped[tb] = gosb_clipped.get(tb, 0) + 1

    # --- ТБ: коэффициент всего ТБ, он же фолбэк его ГОСБ -------------------- #
    n_own = {}
    for gid in by_gosb:
        tb = tb_of.get(int(gid))
        n_own[tb] = n_own.get(tb, 0) + 1
    for tb, g in p.dropna(subset=["tb_id"]).groupby("tb_id"):
        tb = int(tb)
        pl, fc = float(g["plan_np"].sum()), float(g["fact_np"].sum())
        k, raw, clipped = _conv_k(pl, fc)
        out["by_tb"][tb] = k
        out["diag_tb"][tb] = {
            "months": int(g["per"].nunique()), "plan": pl, "fact": fc,
            "tb_raw": raw, "tb_clipped": clipped,
            "n_gosb": n_own.get(tb, 0), "n_gosb_clipped": gosb_clipped.get(tb, 0),
            "n_gosb_fallback": gosb_fallback.get(tb, 0)}

    # --- Банк: тот же расчёт по всем строкам сразу -------------------------- #
    pl, fc = float(p["plan_np"].sum()), float(p["fact_np"].sum())
    k_sb, raw_sb, clipped_sb = _conv_k(pl, fc)
    n_tb_clipped = sum(1 for d in out["diag_tb"].values() if d["tb_clipped"])
    out["sb"] = k_sb
    out["diag_sb"] = {
        "months": int(p["per"].nunique()), "plan": pl, "fact": fc,
        "tb_raw": raw_sb, "tb_clipped": clipped_sb,
        # единица уровня СБ — ТБ, поэтому и «сколько единиц упёрлось» считается по ТБ
        "n_gosb": len(out["by_tb"]), "n_gosb_clipped": n_tb_clipped,
        "n_gosb_fallback": sum(gosb_fallback.values())}

    # Готовый коэффициент КАЖДОГО ГОСБ: свой, иначе своего ТБ, иначе банковский.
    # Собирается здесь, чтобы прогноз пайплайна не знал про иерархию вовсе.
    out["by_gosb"] = by_gosb
    out["of_gosb"] = {gid: by_gosb.get(gid, out["by_tb"].get(tb_of.get(gid), k_sb))
                      for gid in all_gosb}
    return out


def pipeline_current(plan_m: pd.DataFrame, fact_m: pd.DataFrame, ref_cur,
                     conv_of: dict, default_k: float = 1.0,
                     time_left: float = 1.0) -> pd.DataFrame:
    """Пайплайн ТЕКУЩЕГО месяца по (ГОСБ, ИНН): план, уже пришедший факт, прогноз.

    В витрине премирования есть факт и за текущий месяц, поэтому известно, сколько НП
    уже привлечено на отчётную дату. Пришедшее — уже в кармане, под риском остаётся
    только невыполненная часть плана:

        rest    = max(0, план − факт)
        pipe_np = факт + rest × реализуемость(ГОСБ) × time_left

    `time_left` — доля КАЛЕНДАРНОГО месяца, которая ещё впереди, считается от РЕАЛЬНОЙ
    текущей даты (см. `analyze._dates`). Это не то же самое, что доля отыгранных выплат
    в модели оттока: та меряется по `act_dt` витрины и отвечает на вопрос «сколько мы
    уже увидели», а здесь вопрос другой — «сколько времени осталось, чтобы привлечения
    успели дойти». Витрина оттока про будущие дни ничего не знает.

    Множитель обязателен: без него формула не знает про календарь и в последний день
    месяца всё равно прибавляла бы к факту людей, которые уже физически не придут.

    Клип `max(0, …)` нужен: у организации факт может превысить план, и остаток тогда
    нулевой, а не отрицательный (иначе прогноз оказался бы НИЖЕ уже случившегося факта).

    Сотрудники сворачиваются: в прогнозе организация фигурирует целиком.
    """
    cols = ["new_gosb_id", "inn", "seg_funnel", "pipe_np_raw", "pipe_np",
            "pipe_fact_mtd", "pipe_rest", "pipe_expect",
            "pipe_fot_raw", "pipe_fot", "conv", "n_deals"]
    if plan_m is None or plan_m.empty:
        return pd.DataFrame(columns=cols)
    cur = pd.Period(pd.Timestamp(ref_cur), freq="M")
    p = _as_period(plan_m)
    p = p[(p["per"] == cur) & p["new_gosb_id"].notna()]
    if p.empty:
        return pd.DataFrame(columns=cols)

    agg = p.groupby(["new_gosb_id", "inn"], as_index=False).agg(
        seg_funnel=("seg_funnel", "min"), pipe_np_raw=("plan_np", "sum"),
        pipe_fot_raw=("plan_fot", "sum"), n_deals=("n_deals", "sum"))
    if fact_m is not None and not fact_m.empty:
        f = _as_period(fact_m)
        f = f[f["per"] == cur]
        if not f.empty:
            fa = f.groupby(["new_gosb_id", "inn"], as_index=False).agg(
                pipe_fact_mtd=("fact_np", "sum"))
            agg = agg.merge(fa, on=["new_gosb_id", "inn"], how="left")
    agg["pipe_fact_mtd"] = num(agg, "pipe_fact_mtd")
    for c in ("pipe_np_raw", "pipe_fot_raw"):
        agg[c] = pd.to_numeric(agg[c], errors="coerce").fillna(0.0)
    # `conv_of` уже разрешён по иерархии (свой ГОСБ → его ТБ → банк) в
    # conversion_by_month: здесь про уровни знать незачем
    agg["conv"] = [conv_of.get(int(g), default_k) for g in agg["new_gosb_id"]]
    left = float(np.clip(time_left, 0.0, 1.0))
    agg["pipe_rest"] = (agg["pipe_np_raw"] - agg["pipe_fact_mtd"]).clip(lower=0)
    agg["pipe_expect"] = agg["pipe_rest"] * agg["conv"] * left
    agg["pipe_np"] = agg["pipe_fact_mtd"] + agg["pipe_expect"]
    # ФОТ — тем же множителем, иначе разъедется с получателями
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where(agg["pipe_np_raw"] > 0, agg["pipe_np"] / agg["pipe_np_raw"], 0.0)
    agg["pipe_fot"] = agg["pipe_fot_raw"] * share
    return agg[cols]


def deal_due(plan_m: pd.DataFrame, fact_m: pd.DataFrame, ref_cur) -> pd.DataFrame:
    """План и факт по сделкам за ЗАКРЫТЫЕ месяцы, свёрнутые до (ГОСБ, ИНН).

    Это то, с чем аудит сравнивает результат: срок по этим месяцам уже прошёл, значит
    спрашивать за них правомерно. Сумма по сотрудникам и сделкам — по той же причине,
    что и в conversion_by_month: две сделки одного месяца дают один общий план.
    """
    cols = ["new_gosb_id", "inn", "plan_np_due", "fact_np_due", "due_months"]
    if plan_m is None or plan_m.empty:
        return pd.DataFrame(columns=cols)
    cur = pd.Period(pd.Timestamp(ref_cur), freq="M")
    p = _as_period(plan_m)
    p = p[(p["per"] < cur) & p["new_gosb_id"].notna()]
    if p.empty:
        return pd.DataFrame(columns=cols)
    keys = ["new_gosb_id", "inn", "saphr_id", "per"]
    if fact_m is not None and not fact_m.empty:
        f = _as_period(fact_m)
        p = p.merge(f[keys + ["fact_np"]], on=keys, how="left")
    p["fact_np"] = num(p, "fact_np")
    out = p.groupby(["new_gosb_id", "inn"], as_index=False).agg(
        plan_np_due=("plan_np", "sum"), fact_np_due=("fact_np", "sum"),
        due_months=("per", "nunique"))
    return out[cols]


# --------------------------------------------------------------------------- #
def org_outflow(out: pd.DataFrame, pipe: pd.DataFrame, seg_of: dict) -> pd.DataFrame:
    """Свести фактический отток и пайплайн в одну строку на (ГОСБ, ИНН).

    Отток приходит помесячно (строка на месяц) — здесь он сворачивается до пары
    (ГОСБ, ИНН), а сами месяцы сохраняются списком `out_months`: по ним блок
    «крупнейшие оттоки» группирует организации и отвечает на вопрос, отрабатывали ли
    отток тогда, когда он случился.

    `seg_of` — фолбэк-сегмент по ИНН из uzp_dim_company (короткое имя): нужен там,
    где организации нет в витрине оттока и сегмент взять больше неоткуда.
    """
    cols = ["new_gosb_id", "inn", "seg_name", "out_qty", "ret_qty", "out_kept",
            "out_months", "pipe_np", "pipe_np_raw", "pipe_fact_mtd", "pipe_rest",
            "pipe_expect", "pipe_fot", "pipe_fot_raw", "n_deals", "avg_salary_m"]
    p = pipe if pipe is not None and not pipe.empty else pd.DataFrame(
        columns=["new_gosb_id", "inn", "pipe_np", "pipe_np_raw", "seg_funnel"])
    if (out is None or out.empty) and p.empty:
        return pd.DataFrame(columns=cols)

    if out is not None and not out.empty:
        o = out.copy()
        o["ym"] = pd.to_datetime(o["report_dt"]).dt.strftime("%m.%Y")
        base = o.groupby(["new_gosb_id", "inn"], as_index=False).agg(
            out_qty=("out_qty", "sum"), ret_qty=("ret_qty", "sum"),
            out_kept=("out_kept", "sum"), avg_salary_m=("avg_salary_m", "max"),
            seg_fact=("seg_fact", "max"),
            # месяцы, в которых отток БЫЛ и не вернулся целиком: только они означают
            # потерю, по остальным разбирать нечего
            out_months=("ym", list))
        keep_months = (o[o["out_kept"] > 0]
                       .groupby(["new_gosb_id", "inn"])["ym"].apply(list).to_dict())
        base["out_months"] = [keep_months.get((g, i), [])
                              for g, i in zip(base["new_gosb_id"], base["inn"])]
    else:
        base = pd.DataFrame(columns=["new_gosb_id", "inn"])

    m = base.merge(p, on=["new_gosb_id", "inn"], how="outer")
    for c in ("out_qty", "ret_qty", "out_kept", "pipe_np", "pipe_np_raw",
              "pipe_fact_mtd", "pipe_rest", "pipe_expect", "pipe_fot",
              "pipe_fot_raw", "n_deals", "avg_salary_m"):
        m[c] = num(m, c)
    # колонка-список переживает outer-merge только с явной нормализацией: NaN здесь
    # истинно и молча просочился бы в детализацию
    m["out_months"] = ([v if isinstance(v, list) else [] for v in m["out_months"]]
                       if "out_months" in m else [[] for _ in range(len(m))])

    seg_fact = m.get("seg_fact")
    seg_fun = m.get("seg_funnel")
    m["seg_name"] = [
        _first_seg(a, b, seg_of.get(int(i)) if pd.notna(i) else None)
        for a, b, i in zip(
            seg_fact if seg_fact is not None else [None] * len(m),
            seg_fun if seg_fun is not None else [None] * len(m),
            m["inn"])
    ]
    return m[cols]


def _first_seg(*candidates):
    for c in candidates:
        if c is not None and pd.notna(c) and str(c).strip():
            return str(c).strip()
    return "—"


# --------------------------------------------------------------------------- #
def build_matrix(base_seg: pd.DataFrame, plan_seg: pd.DataFrame,
                 unit_src: str = "new_gosb_id") -> tuple[pd.DataFrame, dict]:
    """Матрица «единица × сегмент»: план текущего месяца против ПРОГНОЗА ВИТРИНЫ.

    Единица разбора — ГОСБ в отчёте ТБ и ТБ в отчёте СБ; здесь она везде называется
    `unit_id`. Сама логика от уровня не зависит.

    Прогноз берётся готовой колонкой `pred_amt` (uzp_dwh_metrics.prediction_amt) на
    том же грейне, что план. Раньше он собирался из дельт по организациям и требовал
    разноса неатрибутированного остатка по сегментам — теперь разносить нечего:
    витрина считает прогноз на каждой ячейке сама.
    """
    cols = ["unit_id", "seg_name", "fact_amt"]
    base = (base_seg[cols].copy() if not base_seg.empty
            else pd.DataFrame(columns=cols))
    pcols = ["unit_id", "seg_name", "plan_amt", "pred_amt"]
    plan = (plan_seg[pcols].copy() if not plan_seg.empty
            else pd.DataFrame(columns=pcols))
    for f in (base, plan):
        if not f.empty:
            f["unit_id"] = f["unit_id"].astype("int64")
    m = plan.merge(base, on=["unit_id", "seg_name"], how="outer")
    for c in ("plan_amt", "fact_amt", "pred_amt"):
        m[c] = pd.to_numeric(m[c], errors="coerce").fillna(0.0)

    # base_amt — факт закрытого месяца (портфель-база), fact_amt — прогноз витрины.
    # Имя fact_amt историческое: так эту колонку читают матрица, карточки и разрыв.
    m["base_amt"] = m["fact_amt"]
    m["fact_amt"] = m["pred_amt"].clip(lower=0)
    m["execution_percent"] = m["fact_amt"] / m["plan_amt"].replace(0, np.nan)
    m["nedobor"] = m["plan_amt"] - m["fact_amt"]
    # ячейки с планом, но без прогноза витрины — про них надо сказать вслух,
    # иначе недобор будет равен всему плану и это спишут на провал
    no_pred = int(((m["plan_amt"] > 0) & (m["pred_amt"] <= 0)).sum())
    return m, {"cells": len(m), "no_pred": no_pred}



def build_totals(base_tot: pd.DataFrame, plan_tot: pd.DataFrame,
                 unit_src: str = "new_gosb_id") -> pd.DataFrame:
    """То же, что build_matrix, но на грейне единицы (без сегмента).

    Отдельная функция, а не свёртка матрицы: и план, и прогноз единицы берутся из
    строки «все сегменты» витрины, а она не обязана в точности равняться сумме
    сегментов — уровни и разрезы витрина считает независимо.
    """
    base = (base_tot[["unit_id", "unit_name", "fact_amt"]].copy()
            if base_tot is not None and not base_tot.empty
            else pd.DataFrame(columns=["unit_id", "unit_name", "fact_amt"]))
    pcols = ["unit_id", "unit_name", "plan_amt", "pred_amt"]
    plan = (plan_tot[pcols].copy()
            if plan_tot is not None and not plan_tot.empty
            else pd.DataFrame(columns=pcols))
    m = plan.merge(base.drop(columns=["unit_name"]), on="unit_id", how="outer")
    if m.empty:
        return pd.DataFrame(columns=["unit_id", "unit_name", "plan_amt", "base_amt",
                                     "fact_amt", "execution_percent", "nedobor"])
    for c in ("plan_amt", "fact_amt", "pred_amt"):
        m[c] = pd.to_numeric(m[c], errors="coerce").fillna(0.0)
    m["base_amt"] = m["fact_amt"]
    m["fact_amt"] = m["pred_amt"].clip(lower=0)
    m["execution_percent"] = m["fact_amt"] / m["plan_amt"].replace(0, np.nan)
    m["nedobor"] = m["plan_amt"] - m["fact_amt"]
    return m.sort_values("nedobor", ascending=False).reset_index(drop=True)
