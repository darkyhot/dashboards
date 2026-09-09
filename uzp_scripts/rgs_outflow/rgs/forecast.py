"""Шаг 3: прогноз численности бюджетной сферы до конца года.

Каркас тот же, что уже отобран в forecast_lab, — АДДИТИВНЫЙ, а не подобранная
модель:

    численность(t+1) = численность(t) − ожидаемый отток(t+1) + ожидаемый приход(t+1)

Так сделано намеренно. Модель, подогнанная под ряд из десяти точек, покажет
красивую метрику и ничего не будет значить; аддитивная раскладка держит каждый
месяц проверяемым: видно, из чего он сложился, и любую часть можно сверить
с витриной.

Три сценария — не «оптимистичный/реалистичный/пессимистичный» на глазок, а три
разных способа взять слагаемые из ОДНОЙ И ТОЙ ЖЕ истории:

* **сохранение** — отток и приход по среднему за последние k месяцев;
* **восстановление** — отток по среднему, приход по лучшему месяцу окна;
* **риск** — отток по худшему месяцу окна, приход по среднему.

Сезонность применяется, только если история это позволяет: чтобы отличить
сентябрьский набор в образовании от тренда, нужно два полных года. Меньше — и
множитель считался бы по одному наблюдению на месяц, то есть был бы просто
шумом, выданным за закономерность. Тогда сезонность отключается, и в отчёт идёт
честная оговорка.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from uzp_dash import progress

# Окно, по которому берутся слагаемые. Меньше трёх месяцев — среднее считать не по чему.
WINDOW_M = 6
MIN_WINDOW_M = 3
# Сезонность оценивается только при полных двух годах истории.
SEASON_MIN_MONTHS = 24

SC_HOLD = "Сохранение"
SC_RECOVER = "Восстановление"
SC_RISK = "Риск"
SCENARIOS = (SC_HOLD, SC_RECOVER, SC_RISK)


def _month_ends(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """Концы месяцев строго ПОСЛЕ start и по end включительно."""
    if end <= start:
        return []
    return list(pd.date_range(start=start + pd.offsets.MonthEnd(1), end=end, freq="ME"))


def _series(panel: pd.DataFrame, fact: pd.DataFrame, org_meta: pd.DataFrame
            ) -> pd.DataFrame:
    """Помесячный ряд по ведомствам: численность, отток, приход.

    Численность и приход берутся из витрины организаций, невозвращённый отток —
    из витрины оттока: только там есть возвраты. Соединяются по (ведомство, месяц).
    """
    p = panel.copy()
    p["ym"] = pd.to_datetime(p["ym"]).dt.to_period("M").dt.to_timestamp("M")
    if not org_meta.empty:
        p = p.merge(org_meta[["inn", "agency"]].drop_duplicates("inn"),
                    on="inn", how="left")
    p["agency"] = p.get("agency", pd.Series("", index=p.index)).fillna("—")
    base = p.groupby(["agency", "ym"], as_index=False).agg(
        fl=("fl", "sum"), new_fl=("new_fl", "sum"), np_cnt=("np", "sum"))

    f = fact.copy()
    f["ym"] = pd.to_datetime(f["report_dt"]).dt.to_period("M").dt.to_timestamp("M")
    out = f.groupby(["agency", "ym"], as_index=False).agg(
        out_kept=("out_kept", "sum"), out_qty=("out_qty", "sum"),
        ret_qty=("ret_qty", "sum"))

    s = base.merge(out, on=["agency", "ym"], how="left")
    for c in ("out_kept", "out_qty", "ret_qty"):
        s[c] = pd.to_numeric(s[c], errors="coerce").fillna(0.0)
    s = s.sort_values(["agency", "ym"]).reset_index(drop=True)

    # ПРИХОД ВЫВОДИТСЯ ИЗ ИСТОРИИ, а не берётся колонкой витрины.
    #
    # Соблазн взять `new_fl_cnt + np_cnt` велик, и это ошибка: обе колонки живут
    # в своей мере (новые ФЛ и новые получатели — разные сущности, и складывать
    # их значит считать одних и тех же людей дважды), а с наблюдаемой
    # численностью они не сходятся вовсе. При первом прогоне такой «приход»
    # оказался втрое больше оттока, и прогноз показал рост бюджетной сферы
    # на 57% за полгода — при том, что фактическая численность в ряду падала.
    #
    # Поэтому приход считается из тождества, которое обязано выполняться:
    #     численность(t) − численность(t−1) = приход(t) − отток(t)
    # откуда   приход(t) = Δчисленности(t) + отток(t).
    # Тогда модель, применённая к истории, воспроизводит её ТОЧНО, а не примерно.
    #
    # Величина может выйти отрицательной — это не сбой. Так проявляется витрина
    # оттока, заполненная не полностью: численность упала сильнее, чем объясняет
    # учтённый отток. Обнулять такое нельзя — прогноз systematically поехал бы
    # вверх ровно там, где данных не хватает. Поэтому знак сохраняется, а колонка
    # называется «приход (расчётный)», а не «привлечение».
    s["d_fl"] = s.groupby("agency")["fl"].diff()
    s["in_impl"] = s["d_fl"] + s["out_kept"]
    s["in_mart"] = s["new_fl"] + s["np_cnt"]      # витринный приход — только для сверки
    return s


def _seasonality(s: pd.DataFrame) -> tuple[dict, bool]:
    """Множители сезонности оттока по месяцу года. Второе значение — измерима ли.

    Оценивается ПО ВСЕМУ сегменту, а не по каждому ведомству: на ведомство
    приходится в разы меньше наблюдений, и множитель получился бы шумом.
    """
    tot = s.groupby("ym", as_index=False)["out_kept"].sum().sort_values("ym")
    if len(tot) < SEASON_MIN_MONTHS:
        return {}, False
    tot["m"] = tot["ym"].dt.month
    overall = tot["out_kept"].mean()
    if overall <= 0:
        return {}, False
    idx = (tot.groupby("m")["out_kept"].mean() / overall).to_dict()
    # Множитель ограничиваем: один аномальный месяц не должен утроить прогноз
    return {int(k): float(np.clip(v, 0.5, 2.0)) for k, v in idx.items()}, True


def _terms(g: pd.DataFrame, window: int) -> dict:
    """Слагаемые сценариев по истории одного ведомства.

    Первый месяц ряда участвовать не может: у него нет предыдущего, а значит нет
    и расчётного прихода. Молча подставить ноль нельзя — это занизило бы средний
    приход ровно на одно наблюдение из окна.
    """
    tail = g.dropna(subset=["in_impl"]).tail(window)
    out = tail["out_kept"].to_numpy(dtype=float)
    inn = tail["in_impl"].to_numpy(dtype=float)
    if not len(out):
        return {"out_mean": 0.0, "out_worst": 0.0, "in_mean": 0.0, "in_best": 0.0,
                "n_months": 0}
    return {
        "out_mean": float(out.mean()),
        "out_worst": float(out.max()),
        "in_mean": float(inn.mean()),
        "in_best": float(inn.max()),
        "n_months": int(len(tail)),
    }


def run(panel: pd.DataFrame, fact: pd.DataFrame, org_meta: pd.DataFrame,
        month, horizon_to, window: int = WINDOW_M
        ) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Прогноз по ведомствам и по сегменту в целом.

    Возвращает (помесячный прогноз, свод по сценариям, диагностика).
    """
    diag: dict = {"warnings": []}
    if panel.empty or fact.empty:
        diag["warnings"].append("нет истории — прогноз не строится")
        return pd.DataFrame(), pd.DataFrame(), diag

    s = _series(panel, fact, org_meta)
    now = pd.Timestamp(month).to_period("M").to_timestamp("M")
    s = s[s["ym"] <= now]
    n_months = s["ym"].nunique()
    if n_months < MIN_WINDOW_M:
        diag["warnings"].append(
            f"истории всего {n_months} мес. — для прогноза нужно минимум "
            f"{MIN_WINDOW_M}; раздел не строится")
        return pd.DataFrame(), pd.DataFrame(), diag

    win = min(window, n_months)
    if win < window:
        diag["warnings"].append(
            f"окно прогноза сжато до {win} мес. вместо {window} — столько истории есть")

    # Витринный приход против расчётного: расхождение в разы — сигнал о том, что
    # колонки витрины меряют не то, что кажется. В прогнозе они не участвуют,
    # но знать об этом надо: на проме это первый признак, что что-то не так.
    obs_in = s["in_impl"].dropna()
    mart_in = s.loc[obs_in.index, "in_mart"]
    if len(obs_in) and obs_in.abs().sum() > 0:
        ratio = float(mart_in.sum() / obs_in.sum()) if obs_in.sum() else float("inf")
        diag["inflow_mart_ratio"] = ratio
        if not (0.5 <= ratio <= 2.0):
            diag["warnings"].append(
                f"витринный приход (new_fl_cnt + np_cnt) отличается от расчётного "
                f"в {ratio:.1f} раза — в прогнозе он не используется; "
                f"приход выведен из динамики численности")

    season, season_ok = _seasonality(s)
    diag["season_measurable"] = season_ok
    if not season_ok:
        diag["warnings"].append(
            f"сезонность не оценивается: нужно {SEASON_MIN_MONTHS} мес. истории, "
            f"есть {n_months}. Прогноз построен без сезонного множителя — "
            f"сентябрьский набор в образовании в нём не учтён")

    horizon = _month_ends(now, pd.Timestamp(horizon_to))
    if not horizon:
        diag["warnings"].append(
            f"горизонт прогноза пуст: отчётный месяц {now:%m.%Y} не раньше "
            f"конца горизонта {pd.Timestamp(horizon_to):%m.%Y}")
        return pd.DataFrame(), pd.DataFrame(), diag
    diag["horizon"] = [str(h.date()) for h in horizon]
    diag["window"] = win

    rows = []
    for agency, g in s.groupby("agency"):
        g = g.sort_values("ym")
        t = _terms(g, win)
        base_fl = float(g["fl"].iloc[-1])
        for sc in SCENARIOS:
            if sc == SC_HOLD:
                out_m, in_m = t["out_mean"], t["in_mean"]
            elif sc == SC_RECOVER:
                out_m, in_m = t["out_mean"], t["in_best"]
            else:
                out_m, in_m = t["out_worst"], t["in_mean"]
            fl = base_fl
            for h in horizon:
                k = season.get(h.month, 1.0) if season_ok else 1.0
                out_h = out_m * k
                # Уйти может не больше, чем есть: без этого длинный горизонт
                # уводит численность в минус, и итог выглядит абсурдно.
                out_h = min(out_h, fl)
                nxt = fl - out_h + in_m
                rows.append({"agency": agency, "scenario": sc, "ym": h,
                             "fl_start": fl, "out": out_h, "inflow": in_m,
                             "fl_end": nxt, "season_k": k})
                fl = nxt

    fc = pd.DataFrame(rows)
    if fc.empty:
        return fc, pd.DataFrame(), diag

    # Свод: чем закончится год по каждому сценарию
    last = horizon[-1]
    base_total = float(s[s["ym"] == now]["fl"].sum())
    end = (fc[fc["ym"] == last].groupby("scenario", as_index=False)
           .agg(fl_end=("fl_end", "sum")))
    end["fl_base"] = base_total
    end["delta"] = end["fl_end"] - base_total
    end["delta_perc"] = end["delta"] / max(base_total, 1)
    end["months"] = len(horizon)
    order = {sc: i for i, sc in enumerate(SCENARIOS)}
    end = end.sort_values("scenario", key=lambda c: c.map(order)).reset_index(drop=True)

    # Сходимость: помесячный водопад каждого сценария обязан сойтись с итогом.
    # Если не сходится — часть эффекта потеряна по дороге, и это видно в прогрессе.
    checks = []
    for sc in SCENARIOS:
        w = fc[fc["scenario"] == sc]
        left = base_total - w["out"].sum() + w["inflow"].sum()
        right = float(end.loc[end["scenario"] == sc, "fl_end"].iloc[0])
        residual = left - right
        if abs(residual) > 0.5:
            progress.warn(f"водопад «{sc}» не сходится: невязка {residual:+.2f}")
        checks.append({"name": f"водопад «{sc}»", "left": left, "right": right,
                       "residual": residual, "ok": abs(residual) <= 0.5})
    diag["checks"] = checks

    hold = end[end["scenario"] == SC_HOLD].iloc[0]
    progress.done(
        f"прогноз: {now:%m.%Y} → {last:%m.%Y} ({len(horizon)} мес.), окно {win} мес."
        f"{'' if season_ok else ', без сезонности'}; сценарий «{SC_HOLD}»: "
        f"{base_total:,.0f} → {hold['fl_end']:,.0f} ФЛ "
        f"({hold['delta_perc']:+.1%})")
    for w in diag["warnings"]:
        progress.warn(w)
    return fc, end, diag
