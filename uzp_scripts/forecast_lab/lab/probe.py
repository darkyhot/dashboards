"""Шаг 0: разведка данных. Что вообще есть в витрине и что из этого можно проверить.

Считается ДО перебора и печатается в консоль, а не только в файл: семейство
кандидатов, для которого данных нет, дальше пропускается ЯВНО и с пометкой в
отчёте. Молча посчитать его по нулям нельзя — получится «модель, которая ничего не
предсказывает» с приличной метрикой, и это ввело бы в заблуждение.

Результат — `probe.json` и словарь тех же данных для остальных шагов.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from uzp_dash import db, progress

from . import queries as LQ

# Сколько месяцев истории нужно паре, чтобы по ней можно было что-то оценить.
# Меньше двух не бывает: одна точка не даёт даже дельты.
MIN_HIST_MONTHS = 2


def _df(engine, sql, params=None) -> pd.DataFrame:
    try:
        return db.read_sql(engine, sql, params or {})
    except Exception as ex:                       # таблицы может не быть вовсе
        progress.warn(f"запрос недоступен: {type(ex).__name__}: {str(ex)[:200]}")
        return pd.DataFrame()


def run(engine, out_dir: Path) -> dict:
    """Собрать разведку. Возвращает словарь и пишет probe.json."""
    progress.step("Разведка данных: глубина истории и заполненность признаков")
    res: dict = {"contour_schema": None, "warnings": []}

    from uzp_dash import config
    res["contour_schema"] = config.SCHEMA

    # --- история company_holding_metric ------------------------------------ #
    hist = _df(engine, LQ.PROBE_HISTORY)
    if hist.empty:
        raise RuntimeError("uzp_dwh_company_holding_metric пуста или недоступна — "
                           "лаборатории не на чем работать")
    hist["ym"] = pd.PeriodIndex(pd.to_datetime(hist["ym"]), freq="M")
    hist = hist.sort_values("ym").reset_index(drop=True)
    months = [str(p) for p in hist["ym"]]
    res["history"] = {
        "months": months,
        "n_months": len(months),
        "first": months[0], "last": months[-1],
        "by_month": [
            {"ym": str(r.ym), "rows": int(r.n_rows), "orgs": int(r.n_orgs),
             "gosb": int(r.n_gosb),
             "share_outflow": _share(r.n_out, r.n_rows),
             "share_new_fl": _share(r.n_new_fl, r.n_rows),
             "share_np": _share(r.n_np, r.n_rows),
             "share_emp": _share(r.n_emp, r.n_rows),
             "share_potential": _share(r.n_pot, r.n_rows),
             "sum_fl": float(r.sum_fl or 0), "sum_outflow": float(r.sum_out or 0),
             "sum_new_fl": float(r.sum_new_fl or 0),
             "loaded_at": str(r.loaded_at) if pd.notna(r.loaded_at) else None}
            for r in hist.itertuples()],
    }
    progress.done(f"История витрины: {len(months)} мес ({months[0]} … {months[-1]}), "
                  f"{int(hist['n_orgs'].max()):,} организаций в самом полном месяце")

    sh_out = hist["n_out"].sum() / max(1, hist["n_rows"].sum())
    sh_new = hist["n_new_fl"].sum() / max(1, hist["n_rows"].sum())
    progress.done(f"Заполненность: отток есть у {sh_out * 100:.1f}% строк, "
                  f"новые ФЛ — у {sh_new * 100:.1f}%")
    if sh_out < 0.05:
        res["warnings"].append(
            f"fl_outflow_qty заполнен лишь у {sh_out * 100:.1f}% строк — модель "
            f"оттока текущей формулы почти везде вырождается в ноль")

    # --- лаг загрузки: доступен ли закрытый месяц на 1–5 число -------------- #
    # Отчёт строится 1–5 числа месяца M. Если закрытый месяц M−1 к этому моменту
    # ещё не загружен, базой прогноза служит M−2, и это меняет всю постановку.
    loaded = pd.to_datetime(hist["loaded_at"], errors="coerce")
    lags = []
    for ym, at in zip(hist["ym"], loaded):
        if pd.isna(at):
            continue
        lags.append((pd.Timestamp(at) - ym.to_timestamp("M")).days)
    # Одинаковый modified_dttm у всех месяцев означает, что витрина историю
    # загрузок не хранит (так делает и генератор синтетики). Считать по нему лаг
    # нельзя: получатся сотни дней, которых на самом деле нет.
    constant = loaded.notna().any() and loaded.nunique() <= 1
    res["load_lag_days"] = {
        "values": lags, "measurable": bool(lags) and not constant,
        "median": (float(pd.Series(lags).median())
                   if lags and not constant else None),
        "max": int(max(lags)) if lags and not constant else None,
    }
    if constant:
        progress.warn("modified_dttm одинаков у всех месяцев — витрина не хранит "
                      "историю загрузки, лаг не измерить; база прогноза "
                      "принимается как M−1")
        res["warnings"].append(
            "лаг загрузки закрытого месяца не измерить (modified_dttm одинаков у "
            "всех месяцев) — проверьте вручную, доступен ли месяц M−1 на 1–5 "
            "число месяца M")
    elif lags:
        med = pd.Series(lags).median()
        progress.done(f"Лаг загрузки закрытого месяца: медиана {med:.0f} дн, "
                      f"максимум {max(lags)} дн")
        if med > 5:
            res["warnings"].append(
                f"закрытый месяц появляется в витрине через {med:.0f} дн после его "
                f"конца — на 1–5 число базой прогноза служит M−2, а не M−1")
    else:
        progress.warn("modified_dttm не заполнен — лаг загрузки не измерить, "
                      "база прогноза принимается как M−1")

    # --- сходимость уровней ------------------------------------------------ #
    lv = _df(engine, LQ.PROBE_LEVELS, {"m_rcp": LQ.METRIC_RECIPIENTS})
    if not lv.empty:
        lv["ym"] = pd.PeriodIndex(pd.to_datetime(lv["ym"]), freq="M")
        lv = lv.dropna(subset=["fl_metrics"])
        if not lv.empty:
            g = lv.groupby("ym").agg(orgs=("fl_orgs", "sum"), mtr=("fl_metrics", "sum"))
            g["gap"] = (g["orgs"] - g["mtr"]) / g["mtr"].replace(0, pd.NA)
            res["level_gap"] = {
                "by_month": [{"ym": str(i), "orgs": float(r.orgs),
                              "metrics": float(r.mtr),
                              "gap_pct": (float(r.gap) * 100
                                          if pd.notna(r.gap) else None)}
                             for i, r in g.iterrows()],
                "median_gap_pct": (float(g["gap"].median() * 100)
                                   if g["gap"].notna().any() else None),
            }
            med = res["level_gap"]["median_gap_pct"]
            if med is not None:
                progress.done(f"Расхождение уровней (сумма организаций против строки "
                              f"уровня ТБ): медиана {med:+.2f}%")
                if abs(med) > 1.0:
                    res["warnings"].append(
                        f"сумма организаций отличается от витрины уровня ТБ на "
                        f"{med:+.2f}% — это ПОТОЛОК точности подхода «база единицы + "
                        f"сумма дельт по организациям»")
    else:
        res["level_gap"] = None

    # --- дневная витрина: есть ли внутримесячные срезы ---------------------- #
    day = _df(engine, LQ.PROBE_DAY)
    if not day.empty:
        res["day_outflow"] = {
            "months": int(len(day)),
            "max_act_dt_per_month": int(day["n_act_dt"].max()),
            "median_act_dt_per_month": float(day["n_act_dt"].median()),
            "by_month": [{"report_dt": str(r.report_dt), "n_act_dt": int(r.n_act_dt),
                          "act_min": str(r.act_min), "act_max": str(r.act_max),
                          "rows": int(r.n_rows)} for r in day.itertuples()],
        }
        intramonth = int(day["n_act_dt"].max()) > 1
        progress.done(f"Дневная витрина: {len(day)} мес, срезов act_dt на месяц — "
                      f"максимум {int(day['n_act_dt'].max())}"
                      + ("" if intramonth else " (только конец месяца)"))
        if not intramonth:
            res["warnings"].append(
                "в дневной витрине по одному срезу act_dt на месяц — кривую точности "
                "по дням месяца построить не из чего; на главный горизонт "
                "(1–5 число) это не влияет")
        res["day_outflow"]["intramonth_snapshots"] = intramonth
    else:
        res["day_outflow"] = None

    # --- отдельная витрина месячного оттока --------------------------------- #
    fo = _df(engine, LQ.FACT_OUTFLOW_PROBE)
    if fo.empty or int(fo["n_rows"].iloc[0] or 0) == 0:
        res["fact_outflow"] = {"available": False}
        progress.warn("uzp_dwh_fact_outflow недоступна или пуста — семейство "
                      "кандидатов по ней будет пропущено")
        res["warnings"].append("uzp_dwh_fact_outflow недоступна — семейство "
                               "кандидатов по месячному факту оттока пропущено")
    else:
        res["fact_outflow"] = {
            "available": True, "rows": int(fo["n_rows"].iloc[0]),
            "orgs": int(fo["n_inn"].iloc[0]),
            "first": str(fo["d_min"].iloc[0]), "last": str(fo["d_max"].iloc[0])}
        progress.done(f"uzp_dwh_fact_outflow: {int(fo['n_rows'].iloc[0]):,} строк, "
                      f"{fo['d_min'].iloc[0]} … {fo['d_max'].iloc[0]}")

    # --- метрики: план/факт/собственный прогноз ----------------------------- #
    mt = _df(engine, LQ.PROBE_METRICS, {"m_fot": LQ.METRIC_FOT,
                                        "m_rcp": LQ.METRIC_RECIPIENTS})
    if not mt.empty:
        mt["ym"] = pd.PeriodIndex(pd.to_datetime(mt["ym"]), freq="M")
        agg = mt.groupby(["metric_id", "level_name"]).agg(
            months=("ym", "nunique"), ym_min=("ym", "min"), ym_max=("ym", "max"),
            rows=("n_rows", "sum"), n_plan=("n_plan", "sum"),
            n_fact=("n_fact", "sum"), n_pred=("n_pred", "sum"))
        res["metrics"] = [
            {"metric_id": int(i[0]), "level_name": i[1], "months": int(r.months),
             "first": str(r.ym_min), "last": str(r.ym_max),
             "share_plan": _share(r.n_plan, r.rows),
             "share_fact": _share(r.n_fact, r.rows),
             "share_prediction": _share(r.n_pred, r.rows)}
            for i, r in agg.iterrows()]
        pred_share = float(mt["n_pred"].sum()) / max(1, float(mt["n_rows"].sum()))
        res["prediction_amt_filled"] = pred_share
        progress.done(f"Витрина метрик: собственный прогноз prediction_amt заполнен "
                      f"у {pred_share * 100:.1f}% строк")
        if pred_share < 0.5:
            res["warnings"].append(
                "prediction_amt заполнен меньше чем наполовину — собственный прогноз "
                "витрины в сравнение не попадёт")
    else:
        res["metrics"] = []
        res["prediction_amt_filled"] = 0.0

    # --- какие месяцы вообще можно оценивать -------------------------------- #
    # Оцениваемый месяц M требует истории < M и факта за сам M. Плюс минимум
    # признаковой глубины: без неё лаги пусты и все варианты выродятся в базу.
    evaluable = months[MIN_HIST_MONTHS:]
    res["evaluable_months"] = evaluable
    progress.done(f"Оцениваемых месяцев: {len(evaluable)} "
                  + (f"({evaluable[0]} … {evaluable[-1]})" if evaluable else "— нет"))
    if len(evaluable) < 4:
        res["warnings"].append(
            f"оцениваемых месяцев всего {len(evaluable)} — выборка слишком мала, "
            f"победителя таблицы лидеров нельзя считать надёжным")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "probe.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    progress.done(f"Разведка сохранена: {path}")
    for w in res["warnings"]:
        progress.warn(w)
    return res


def _share(part, whole) -> float:
    part, whole = float(part or 0), float(whole or 0)
    return round(part / whole, 4) if whole else 0.0
