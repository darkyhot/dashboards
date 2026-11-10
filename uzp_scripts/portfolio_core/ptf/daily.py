"""Календарь выплат по дневной витрине: отличие от прошлого года по «сдвинутому месяцу».

Сезон известен, интересно отличие от того же месяца прошлого года. Часть отличия создаёт
сама граница месяца: выплата 1-го числа, перенесённая на последний рабочий день прошлого
месяца (выходной, праздник, ранняя выплата перед Новым годом), даёт временный отток в M
и лишнего получателя в M−1. Такой перенос проверяется прямо: тот же портфель считается по
месяцу с 16-го числа M−1 по 15-е M. Перенос на несколько дней через 1-е число остаётся
внутри сдвинутого месяца, поэтому

    граница месяца = отличие по календарному месяцу − отличие по сдвинутому месяцу.

Отбор строк и порог — те же, что в сборке по месячной витрине (`sql.T_TR_MONTH`);
календарный месяц из дневной витрины сверяется с месячной сводкой, и при расхождении
вывод о календаре не делается. Пустое значение — «неизвестно», не 0.

Хранится только агрегат `yva_ptf_calendar` (месяц × организация × ГОСБ: счётчики троек);
посчитанный месяц повторно не читается.
"""
from __future__ import annotations

import time
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import db
from . import months as M
from . import sql as Q

SYNC_TOL = 0.005        # дневная и месячная витрины в календарном месяце: расхождение ≤ 0,5%
VANISH = 0.2            # «отличие исчезает»: на сдвинутом месяце осталось меньше 20%
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
SLICE_ROWS = 6


def windows(month) -> dict:
    """Даты календарного и сдвинутого месяца M (строки 'ГГГГ-ММ-ДД')."""
    end = M.parse(month)
    prev = M.shift(end, -1)
    return {"m": end.isoformat(),
            "c_from": date(end.year, end.month, 1).isoformat(), "c_to": end.isoformat(),
            "s_from": date(prev.year, prev.month, 16).isoformat(),
            "s_to": date(end.year, end.month, 15).isoformat()}


def needed(months_of_interest) -> list[str]:
    """Месяцы, нужные для «год к году» месяцев интереса: M, M−1, M−12, M−13."""
    out = {M.iso(M.shift(m, k)) for m in months_of_interest for k in (0, -1, -12, -13)}
    return sorted(out)


def bank_yoy(bank: pd.Series) -> pd.Series:
    """Отличие от прошлого года по ряду итогов банка (индекс — конец месяца, строки)."""
    s = bank.copy()
    s.index = [M.iso(i) for i in s.index]
    out = {}
    for m in s.index:
        keys = [M.iso(M.shift(m, k)) for k in (0, -1, -12, -13)]
        if all(k in s.index for k in keys):
            a, b, c, d = (float(s[k]) for k in keys)
            out[m] = (a - b) - (c - d)
    return pd.Series(out, dtype=float)


def auto_months(connection, top: int = 4) -> list[str]:
    """Месяцы с наибольшим по модулю отличием банка от прошлого года (по месячной сводке)."""
    bank = db.read_sql(connection, Q.CALENDAR_BANK)
    if bank.empty:
        return []
    s = pd.Series(bank["pf"].astype(float).values, index=[M.iso(d) for d in bank["report_dt"]])
    yoy = bank_yoy(s)
    return list(yoy.abs().sort_values(ascending=False).index[:top])


def ensure(connection, months: list[str], amt_min: float = Q.AMT_MIN,
           exc_holding: str = Q.EXC_HOLDING, refresh: bool = False, progress=print) -> dict:
    """Досчитать недостающие месяцы в `yva_ptf_calendar`. Возвращает {месяц: строк}."""
    from .build import Builder

    gp = db.is_greenplum(connection)
    ddl = Q.CREATE_CALENDAR + ("\nDISTRIBUTED BY (org_id)" if gp else "")
    db.execute(connection, ddl)
    params = {"amt_min": amt_min, "def_ver": Q.DEF_VER, "exc_holding": exc_holding}
    done = db.read_sql(connection, Q.CALENDAR_DONE, params)
    have = {M.iso(d) for d, n in zip(done["report_dt"], done["n_cal"]) if n and n > 0}
    todo = [m for m in months if refresh or M.iso(m) not in have]
    out = {}
    if not todo:
        progress(f"Календарь: все {len(months)} мес. уже посчитаны — дневная витрина не читается")
        return out
    b = Builder(connection, gp, params, 1)
    b.create("t_tik", Q.T_TIK)
    b.create("t_exc", Q.T_EXC)
    stamp = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    for m in todo:
        t0 = time.time()
        p = {**params, **windows(m), "created_at": stamp}
        db.execute_tx(connection, [Q.CALENDAR_DELETE], p)
        n = db.execute(connection, Q.CALENDAR_INSERT, p)
        out[M.iso(m)] = n
        msg = f"Календарь {M.label(m)}: {n:,} строк «орг × ГОСБ», {time.time() - t0:.0f} с"
        progress(msg if n else msg + " — в дневной витрине нет данных за месяц")
    return out


def load(connection, months: list[str], amt_min: float = Q.AMT_MIN) -> pd.DataFrame:
    """Сдвинутый и календарный месяц из дневной витрины рядом с месячной сводкой."""
    p = {"amt_min": amt_min, "def_ver": Q.DEF_VER, "d_from": min(months), "d_to": max(months)}
    cal = db.read_sql(connection, Q.CALENDAR_SUMMARY, p)
    mon = db.read_sql(connection, Q.CALENDAR_MONTHLY, p)
    for f in (cal, mon):
        f["report_dt"] = [M.iso(d) for d in f["report_dt"]]
        f["sys_tb_id"] = pd.to_numeric(f["sys_tb_id"], errors="coerce")
    key = ["report_dt", "seg", "sys_tb_id"]
    frame = cal.merge(mon, on=key, how="outer")
    frame = frame[frame["report_dt"].isin([M.iso(m) for m in months])]
    return frame.reset_index(drop=True)


def days(connection, month, refresh: bool = False) -> pd.DataFrame:
    """Строки выплат по дням у границы месяца: с 16-го M−1 по последнее число M.
    Посчитанное окно хранится в `yva_ptf_calendar_days` и витрину повторно не читает."""
    w = windows(month)
    p = {"m": w["m"], "s_from": w["s_from"], "c_to": w["c_to"], "def_ver": Q.DEF_VER}
    db.execute(connection, Q.CREATE_CALENDAR_DAYS + ("\nDISTRIBUTED BY (report_dt)"
                                                     if db.is_greenplum(connection) else ""))
    f = db.read_sql(connection, Q.CALENDAR_DAYS_CACHED, p)
    if refresh or f.empty:
        stamp = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        db.execute_tx(connection, [Q.CALENDAR_DAYS_DELETE, Q.CALENDAR_DAYS_INSERT],
                      {**p, "created_at": stamp})
        f = db.read_sql(connection, Q.CALENDAR_DAYS_CACHED, p)
    f["pay_dt"] = pd.to_datetime(f["pay_dt"]).dt.date
    return f[["pay_dt", "n_rows"]].sort_values("pay_dt").reset_index(drop=True)


def _totals(frame: pd.DataFrame, col: str, by: str | None = None) -> pd.DataFrame | pd.Series:
    """Суммы по месяцу (и срезу); месяц без строк дневной витрины — NaN, не 0."""
    if by is None:
        g = frame.groupby("report_dt")[col].sum(min_count=1)
    else:
        g = frame.groupby(["report_dt", by])[col].sum(min_count=1).unstack(by)
    return g


def _yoy_at(s, m: str) -> tuple[float, float, float]:
    """(изменение этого года, прошлого года, сверх прошлого года) для месяца m."""
    def get(k):
        key = M.iso(M.shift(m, k))
        v = s.get(key, np.nan) if hasattr(s, "get") else np.nan
        return float(v) if v is not None and pd.notna(v) else np.nan
    this = get(0) - get(-1)
    last = get(-12) - get(-13)
    return this, last, this - last


def _sync(frame: pd.DataFrame, m: str) -> float:
    """Наибольшее расхождение календарного месяца дневной витрины с месячной, % (по 4 месяцам)."""
    cal = _totals(frame, "n_cal")
    mon = _totals(frame, "pf")
    worst = np.nan
    for k in (0, -1, -12, -13):
        key = M.iso(M.shift(m, k))
        c, p = cal.get(key, np.nan), mon.get(key, np.nan)
        if pd.isna(c) or pd.isna(p) or not p:
            return np.nan
        d = abs(float(c) - float(p)) / float(p) * 100.
        worst = d if np.isnan(worst) else max(worst, d)
    return worst


def _slice_table(frame: pd.DataFrame, m: str, by: str, name: str) -> pd.DataFrame:
    cal, sh = _totals(frame, "n_cal", by), _totals(frame, "n_sh", by)
    rows = []
    for col in cal.columns:
        c = _yoy_at(cal[col], m)[2]
        s = _yoy_at(sh[col], m)[2] if col in sh.columns else np.nan
        rows.append({name: col, "Сверх прошлого года: календарный месяц": c,
                     "Сверх прошлого года: с 16-го по 15-е": s, "Граница месяца": c - s})
    t = pd.DataFrame(rows)
    if t.empty:
        return t
    if by == "sys_tb_id":
        t[name] = [f"ТБ {int(v)}" if pd.notna(v) else "ТБ не указан" for v in t[name]]
    t = t.reindex(t["Граница месяца"].abs().sort_values(ascending=False, na_position="last").index)
    if len(t) > SLICE_ROWS + 1:
        head, rest = t.iloc[:SLICE_ROWS], t.iloc[SLICE_ROWS:]
        other = {name: f"Остальные ({len(rest)})"}
        for c in t.columns[1:]:
            other[c] = rest[c].sum(min_count=1)
        t = pd.concat([head, pd.DataFrame([other])], ignore_index=True)
    return t.reset_index(drop=True)


def _days_table(this: pd.DataFrame | None, last: pd.DataFrame | None, m: str) -> pd.DataFrame:
    """Последние 5 дней M−1 и первые 5 дней M: этот год рядом с прошлым."""
    def pick(f, month):
        end = M.parse(month)
        prev = M.shift(end, -1)
        dates = [date(prev.year, prev.month, prev.day - k) for k in range(4, -1, -1)]
        dates += [date(end.year, end.month, d) for d in range(1, 6)]
        counts = {} if f is None else dict(zip(f["pay_dt"], f["n_rows"]))
        return dates, [counts.get(d, 0 if f is not None and not f.empty else np.nan) for d in dates]
    d1, n1 = pick(this, m)
    d0, n0 = pick(last, M.shift(m, -12))
    lab = lambda d: f"{d.day:02d}.{d.month:02d} {WEEKDAYS[d.weekday()]}"
    return pd.DataFrame({"Этот год": [lab(d) for d in d1], "Строк выплат": n1,
                         "Прошлый год": [lab(d) for d in d0], "Строк выплат ": n0})


def report(frame: pd.DataFrame, months_of_interest: list[str], day_frames: dict | None = None) -> dict:
    """Сравнение «календарный месяц против сдвинутого» для месяцев интереса."""
    day_frames = day_frames or {}
    cal, sh, mon = _totals(frame, "n_cal"), _totals(frame, "n_sh"), _totals(frame, "pf")
    bank_rows, details, verdicts = [], [], []
    for m in [M.iso(x) for x in months_of_interest]:
        tc, lc, c = _yoy_at(cal, m)
        ts, ls, s = _yoy_at(sh, m)
        p = _yoy_at(mon, m)[2]
        sync = _sync(frame, m)
        effect = c - s
        share = effect / c * 100. if c and np.isfinite(c) and np.isfinite(effect) else np.nan
        gap = p - c          # расхождение источников: месячная сводка − дневная витрина
        bank_rows.append({"Месяц": m[:7], "Месячная сводка": p, "Граница месяца": effect,
                          "Остаток (16-е – 15-е)": s, "Расхождение витрин": gap,
                          "Доля границы, %": effect / p * 100. if p and np.isfinite(p) else np.nan,
                          "Сверка уровней, %": sync})
        if np.isnan(sync):
            text = (f"{M.long(m)}: сверить дневную витрину с месячной нельзя (нет месяца в одной "
                    f"из них) — вывод о календаре не делается.")
        elif sync > SYNC_TOL * 100:
            text = (f"{M.long(m)}: дневная витрина расходится с месячной на {sync:.2f}% — "
                    f"вывод о календаре не делается, сначала сверка источников.")
        elif not np.isfinite(c) or not np.isfinite(s):
            text = f"{M.long(m)}: неизвестно — нет нужных месяцев в дневной витрине."
        else:
            base = p if np.isfinite(p) else c
            text = (f"{M.long(m)}: сверх прошлого года {base:+,.0f} = граница месяца {effect:+,.0f} "
                    f"(перенос выплат через 1-е число) + остаток {s:+,.0f} (есть и при счёте "
                    f"с 16-го по 15-е)")
            text += (f" + расхождение витрин {gap:+,.0f}." if np.isfinite(gap) else ".")
            if abs(s) < VANISH * abs(c):
                text += " Без границы месяца отличие почти исчезает: это календарь, а не потеря людей."
            else:
                text += " Остаток объясняют другие механизмы."
        verdicts.append(text)
        comp = pd.DataFrame([
            {"Счёт": "Месячная сводка", "Этот год": _yoy_at(mon, m)[0], "Прошлый год": _yoy_at(mon, m)[1],
             "Сверх прошлого года": p},
            {"Счёт": "Календарный месяц (дни)", "Этот год": tc, "Прошлый год": lc, "Сверх прошлого года": c},
            {"Счёт": "С 16-го по 15-е", "Этот год": ts, "Прошлый год": ls, "Сверх прошлого года": s},
            {"Счёт": "Граница месяца", "Этот год": tc - ts, "Прошлый год": lc - ls,
             "Сверх прошлого года": effect}])
        d = day_frames.get(m, {})
        details.append({"month": m, "verdict": text, "components": comp,
                        "days": _days_table(d.get("this"), d.get("last"), m) if d else None,
                        "segments": _slice_table(frame, m, "seg", "Сегмент"),
                        "tb": _slice_table(frame, m, "sys_tb_id", "ТБ")})
    return {"bank": pd.DataFrame(bank_rows), "verdicts": verdicts, "details": details,
            "months": [M.iso(x) for x in months_of_interest]}


def run(connection, months: list[str] | None = None, top: int = 4, amt_min: float = Q.AMT_MIN,
        exc_holding: str = Q.EXC_HOLDING, refresh: bool = False, with_days: bool = True,
        progress=print) -> dict:
    """Один вызов из тетрадки: месяцы → досчёт агрегата → сравнение (+ выплаты по дням)."""
    t0 = time.time()
    if months:
        interest = [M.iso(m) for m in months]
    else:
        interest = auto_months(connection, top)
        progress("Календарь: месяцы с наибольшим отличием банка — "
                 + ", ".join(M.label(m) for m in interest))
    if not interest:
        raise ValueError("Календарь: в сводке нет месяцев с прошлым годом для сравнения")
    need = needed(interest)
    ensure(connection, need, amt_min, exc_holding, refresh, progress)
    frame = load(connection, need, amt_min)
    day_frames = {}
    if with_days:
        for m in interest:
            tt = time.time()
            day_frames[m] = {"this": days(connection, m, refresh),
                             "last": days(connection, M.shift(m, -12), refresh)}
            progress(f"Выплаты по дням {M.label(m)} и год назад: {time.time() - tt:.0f} с")
    rep = report(frame, interest, day_frames)
    rep["duration_min"] = (time.time() - t0) / 60.
    return rep
