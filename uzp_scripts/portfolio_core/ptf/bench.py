"""Стенд сторожа: фон, закладка событий и артефактов, метрики, простой соперник.

Сторож принимает панель (`inputs.Panel`) и номер месяца, возвращает карточки. Стенд
закладывает в панель события с известным ответом и смотрит, что сторож нашёл.

* Фон открытого контура — `simulate()`: атомы и крупные организации с сезонностью
  (декабрь/январь, август/сентябрь), разной текучестью организаций и «комками».
* Полусинтетика на проме — та же закладка в панель реального поколения
  (`inputs.load`): фон настоящий, ответ известен.

Закладка меняет меры согласованно с разметкой подложки: тождество «портфель − база =
пришли − ушли» в каждом ряду, база следующего месяца = портфель этого, год к году —
окно 12 месяцев. Событие в организации ложится и в её главный атом.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import months as M
from .inputs import IN_M, MEAS, OUT_M, Panel

# --------------------------------------------------------------------------- #
# Карточка и событие
# --------------------------------------------------------------------------- #
LEVELS = ["org", "atom", "gosb", "tb", "seg", "bank"]


@dataclass
class Card:
    level: str                 # org / atom / gosb / tb / seg / bank
    key: object                # номер ряда (org, atom), id ГОСБ / ТБ, сегмент, 'bank'
    measure: str               # net — изменение портфеля; иначе мера движения
    delta: float               # факт − ожидание, люди (со знаком)
    label: str | None = None   # объяснение сторожа: calendar / underload / recode / ...
    score: float = 0.0         # нижняя граница лишних людей (сторож) или z (соперник)
    obs: float = float("nan")  # факт
    exp: float = float("nan")  # ожидание
    info: dict = field(default_factory=dict)


@dataclass
class Event:
    kind: str                  # leave / stop / dip / hire / reorg / conc / codes
    t: int                     # месяц (индекс панели)
    n: int                     # людей
    row: int = -1              # ряд (организация или атом)
    row2: int = -1             # куда (reorg)
    tb: int = -1               # underload
    q: float = 0.0             # calendar: доля базы
    sign: int = -1             # знак влияния на портфель ряда row
    extra: dict = field(default_factory=dict)


ARTIFACTS = {"calendar", "underload", "recode"}
EVENT_KINDS = ["leave", "stop", "dip", "hire", "reorg", "conc", "codes"]

# --------------------------------------------------------------------------- #
# Фон открытого контура
# --------------------------------------------------------------------------- #
SEG_LIST = ["Микро", "Малые", "Средние", "Крупные", "КСБ", "Рег. госсектор",
            "Фед. госсектор", "CIB"]
TB_LIST = [13, 16, 18, 38, 40, 42, 44, 52, 54, 55, 70, 99, 77]
# сезонность (месяц → множитель): декабрь — разовые и премии, январь — их уход;
# август — сезонные, сентябрь — приход (учебный год)
SEAS_OUT = {1: 2.2, 2: 1.0, 3: 0.95, 4: 0.95, 5: 1.0, 6: 1.0, 7: 1.05, 8: 1.15, 9: 1.0,
            10: 0.95, 11: 0.95, 12: 0.9}
SEAS_IN = {1: 0.8, 2: 1.0, 3: 1.0, 4: 1.0, 5: 0.95, 6: 1.0, 7: 1.0, 8: 1.0, 9: 1.25,
           10: 1.05, 11: 1.0, 12: 2.0}
SEAS_LB = {8: 0.08, 9: 0.06}           # доля сезонных среди ушедших


def sim_months(first: str = "2024-01", last: str = "2026-09") -> list[str]:
    a, b = M.parse(first), M.parse(last)
    out = []
    while a <= b:
        out.append(M.iso(a))
        a = M.shift(a, 1)
    return out


def simulate(seed: int = 1, n_orgs: int = 3000, gosb_per_tb: int = 6,
             tail_mean: float = 22_000, months: list[str] | None = None) -> Panel:
    """Чистый фон: никаких событий, только сезонность, текучесть и «комки»."""
    rng = np.random.default_rng(seed)
    months = months or sim_months()
    T = len(months)
    mon = np.array([int(m[5:7]) for m in months])

    # атомы: сегмент × ГОСБ (не все сочетания есть)
    rows = []
    for tb in TB_LIST:
        for j in range(gosb_per_tb):
            g = tb * 100 + j
            for s in SEG_LIST:
                if rng.random() < 0.8:
                    rows.append({"seg_rep": s, "sys_tb_id": tb, "sys_gosb_id": g})
    akeys = pd.DataFrame(rows)
    na = len(akeys)
    tail0 = rng.lognormal(np.log(tail_mean), 0.8, na)

    # крупные организации: Парето от 1 000, главная ячейка — атом подходящего сегмента
    size0 = np.minimum(1000 * (rng.pareto(1.2, n_orgs) + 1), 150_000)
    seg_of = np.where(size0 > 20_000, "Крупные",
                      np.where(size0 > 5_000, rng.choice(["Крупные", "Рег. госсектор", "КСБ"], n_orgs),
                               rng.choice(["Средние", "Рег. госсектор", "Фед. госсектор", "КСБ"], n_orgs)))
    org_atom = np.array([rng.choice(np.flatnonzero(akeys["seg_rep"].to_numpy() == s))
                         for s in seg_of])

    # ряды моделирования: хвост каждого атома + организации
    n = na + n_orgs
    s0 = np.concatenate([tail0, size0]).round()
    k = np.concatenate([np.full(na, 30.0), np.full(n_orgs, 5.0)])
    r_out = 0.05 * rng.gamma(k, 1 / k)
    r_in = r_out * (1 + rng.normal(0.01, 0.01, n))
    sig = np.concatenate([np.full(na, 0.06), np.full(n_orgs, 0.3)])

    x = {m: np.full((n, T), np.nan) for m in MEAS}
    base = s0.copy()
    temp_prev = np.zeros(n)
    fl_prev = base - 0.5 * np.round(base * 0.12)

    def shock():
        return np.exp(rng.normal(0, sig) - sig ** 2 / 2)

    for t in range(T):
        mo = mon[t]
        out_left = rng.poisson(base * r_out * SEAS_OUT[mo] * shock())
        out_oth = rng.poisson(base * 0.012 * shock())
        out_codes = rng.poisson(base * 0.007)
        tot = out_left + out_oth + out_codes
        over = tot > 0.95 * base
        if over.any():
            f = np.where(over, 0.95 * base / np.maximum(tot, 1), 1.0)
            out_left, out_oth, out_codes = (np.floor(v * f) for v in (out_left, out_oth, out_codes))
        temp = rng.binomial(out_left.astype(int), 0.2)
        back = rng.poisson(base * 0.012)              # вернулись после долгого перерыва
        in_ret = temp_prev + back
        in_new = np.maximum(rng.poisson(base * r_in * SEAS_IN[mo] * shock()) - in_ret, 0)
        in_out = in_new + in_ret
        in_oth = rng.poisson(base * 0.012 * shock())
        in_codes = rng.poisson(base * 0.007)
        pf = base + in_out + in_oth + in_codes - out_left - out_oth - out_codes

        seas = rng.binomial(out_left.astype(int), SEAS_LB.get(mo, 0.01))
        one = rng.binomial((out_left - seas).astype(int), 0.12)
        multi = rng.binomial(pf.astype(int), 0.12)
        pf_fl = pf - 0.5 * multi
        fl_in, fl_out = 0.95 * in_out, 0.95 * out_left
        vals = {
            "pf_recip": pf, "pf_fl": pf_fl, "any_recip": pf + rng.binomial(pf.astype(int), 0.06),
            "raw_rows": pf + rng.binomial(pf.astype(int), 0.45), "fot_amt": pf * 55_000.0,
            "single_pay_recip": rng.binomial(pf.astype(int), 0.2), "multi_recip": multi,
            "one_time_pf": rng.binomial(pf.astype(int), 0.01),
            "m_base_pf": base, "m_in_from_outside": in_out, "m_in_returned": in_ret,
            "m_in_from_other_org": in_oth, "m_in_codes": in_codes,
            "m_out_left_bank": out_left, "m_lb_seasonal": seas, "m_lb_org_stopped": 0 * pf,
            "m_lb_one_time": one, "m_lb_unknown": out_left - seas - one,
            "m_out_to_other_org": out_oth, "m_out_codes": out_codes,
            "m_out_temp": temp if t < T - 1 else np.full(n, np.nan),
            "m_fl_in": fl_in, "m_fl_out": fl_out, "m_fl_conc": (pf_fl - fl_prev) - fl_in + fl_out,
        }
        for m, v in vals.items():
            x[m][:, t] = v
        base, temp_prev, fl_prev = pf, temp, pf_fl

    _fill_yoy(x, T)

    # панель: атом = хвост + организации его ячейки; организация — свой ряд
    agg = {m: x[m][:na].copy() for m in MEAS}
    for m in MEAS:
        np.add.at(agg[m], org_atom, np.nan_to_num(x[m][na:]))
        agg[m][np.isnan(x[m][:na])] = np.nan
    meta = pd.concat([
        akeys.assign(lvl="atom", org_id=-1, atom=np.arange(na)),
        akeys.iloc[org_atom].reset_index(drop=True).assign(
            lvl="org", org_id=np.arange(1, n_orgs + 1) + 10 ** 9, atom=org_atom),
    ], ignore_index=True)[["lvl", "org_id", "seg_rep", "sys_tb_id", "sys_gosb_id", "atom"]]
    xs = {m: np.vstack([agg[m], x[m][na:]]) for m in MEAS}
    scopes = {}
    for level, col in (("atom", "atom"), ("gosb", "sys_gosb_id"), ("tb", "sys_tb_id")):
        a = np.full((n, T), -1, dtype=np.int64)
        a[na:] = meta.loc[na:, col].to_numpy(dtype=np.int64)[:, None]
        scopes[level] = a
    flows = pd.DataFrame({
        "row": np.repeat(np.arange(na, n), T), "t": np.tile(np.arange(T), n_orgs),
        "atom": np.repeat(org_atom, T),
        "in_other": xs["m_in_from_other_org"][na:].ravel(),
        "out_other": xs["m_out_to_other_org"][na:].ravel(),
    }).set_index(["row", "t", "atom"]).sort_index()
    return Panel(months, meta, xs, scopes, flows)


def _fill_yoy(x: dict, T: int) -> None:
    """Год к году из помесячных рядов: база — портфель 12 мес. назад, потоки — доля
    суммы помесячных за окно, «пришли извне» — остаток тождества."""
    for t in range(12, T):
        w = slice(t - 11, t + 1)
        yb = x["pf_recip"][:, t - 12]
        yo = np.round(0.55 * x["m_out_left_bank"][:, w].sum(1))
        yoo = np.round(0.6 * x["m_out_to_other_org"][:, w].sum(1))
        yoc = np.round(0.5 * x["m_out_codes"][:, w].sum(1))
        yio = np.round(0.6 * x["m_in_from_other_org"][:, w].sum(1))
        yic = np.round(0.5 * x["m_in_codes"][:, w].sum(1))
        yin = x["pf_recip"][:, t] - yb - yio - yic + yo + yoo + yoc
        neg = np.minimum(yin, 0)
        yo, yin = yo - neg, yin - neg
        x["y_base_pf"][:, t] = yb
        x["y_in_from_outside"][:, t] = yin
        x["y_in_from_other_org"][:, t] = yio
        x["y_in_codes"][:, t] = yic
        x["y_out_left_bank"][:, t] = yo
        x["y_out_to_other_org"][:, t] = yoo
        x["y_out_codes"][:, t] = yoc
        x["y_fl_conc"][:, t] = (x["pf_fl"][:, t] - x["pf_fl"][:, t - 12]) - 0.95 * yin + 0.95 * yo


# --------------------------------------------------------------------------- #
# Закладка
# --------------------------------------------------------------------------- #
def _rows(p: Panel, r: int) -> list[int]:
    """Ряд события и его атом (для организации)."""
    a = int(p.meta.at[r, "atom"])
    return [r, a] if p.meta.at[r, "lvl"] == "org" and a >= 0 else [r]


def _add(p: Panel, rows, m: str, t0: int, t1: int, d: float) -> None:
    t1 = min(t1, len(p.months) - 1)
    if t0 > t1:
        return
    v = p.x[m][rows, t0:t1 + 1]
    p.x[m][rows, t0:t1 + 1] = np.where(np.isnan(v), v, v + d)


def _stock(p: Panel, rows, t: int, d: float, fl: float | None = None) -> None:
    """Портфель меняется на d с месяца t и дальше; база — со следующего; год к году —
    через 12 мес. Сопутствующие остатки — пропорционально."""
    T = len(p.months) - 1
    fl = d if fl is None else fl
    _add(p, rows, "pf_recip", t, T, d)
    _add(p, rows, "pf_fl", t, T, fl)
    _add(p, rows, "any_recip", t, T, d)
    _add(p, rows, "raw_rows", t, T, round(1.45 * d))
    _add(p, rows, "fot_amt", t, T, 55_000.0 * d)
    _add(p, rows, "single_pay_recip", t, T, round(0.2 * d))
    _add(p, rows, "m_base_pf", t + 1, T, d)
    _add(p, rows, "y_base_pf", t + 12, T, d)


def inject(p: Panel, ev: Event) -> Panel:
    """Закладывает событие или артефакт в копию панели."""
    p = p.copy()
    t, n = ev.t, ev.n
    k = ev.kind
    # Закладка использует главную ячейку. Если в реальном месяце организация
    # расположена иначе, после закладки нельзя утверждать полное вложение в неё.
    for r in (ev.row, ev.row2) if k == "reorg" else (ev.row,):
        if r < 0 or p.meta.at[r, "lvl"] != "org":
            continue
        atom = int(p.meta.at[r, "atom"])
        if atom < 0:
            continue
        for level, col in (("atom", "atom"), ("gosb", "sys_gosb_id"), ("tb", "sys_tb_id")):
            if level not in p.org_scopes:
                continue
            scope = p.org_scopes[level][r, t:]
            scope[scope != p.meta.at[atom, col]] = -1
            if k == "recode":
                dst = p.meta.at[ev.row2, col]
                if dst != p.meta.at[atom, col]:
                    scope[:] = -1
    if k in ("leave", "stop", "codes"):
        rows = _rows(p, ev.row)
        flow = "m_out_codes" if k == "codes" else "m_out_left_bank"
        _add(p, rows, flow, t, t, n)
        if k != "codes":
            _add(p, rows, "m_lb_org_stopped" if k == "stop" else "m_lb_unknown", t, t, n)
        _add(p, rows, "m_fl_out", t, t, n)
        _stock(p, rows, t, -n)
        _add(p, rows, "y_out_codes" if k == "codes" else "y_out_left_bank", t, t + 11, n)
    elif k == "hire":
        rows = _rows(p, ev.row)
        _add(p, rows, "m_in_from_outside", t, t, n)
        _add(p, rows, "m_fl_in", t, t, n)
        _stock(p, rows, t, n)
        _add(p, rows, "y_in_from_outside", t, t + 11, n)
    elif k == "dip":
        rows = _rows(p, ev.row)
        for m in ("m_out_left_bank", "m_lb_unknown", "m_out_temp", "m_fl_out"):
            _add(p, rows, m, t, t, n)
        for m in ("pf_recip", "pf_fl", "any_recip"):
            _add(p, rows, m, t, t, -n)
        _add(p, rows, "m_base_pf", t + 1, t + 1, -n)
        for m in ("m_in_from_outside", "m_in_returned", "m_fl_in"):
            _add(p, rows, m, t + 1, t + 1, n)
        _add(p, rows, "y_out_left_bank", t, t, n)
    elif k == "reorg":
        a, b = _rows(p, ev.row), _rows(p, ev.row2)
        _move(p, a, b, t, n)
    elif k == "recode":
        # организация сменила ГОСБ: атомы меняются, итог организации — нет (только
        # переход внутри неё: ушёл и пришёл одновременно)
        _move(p, [int(p.meta.at[ev.row, "atom"])], [ev.row2], t, n)
        for m in ("m_out_to_other_org", "m_in_from_other_org"):
            _add(p, [ev.row], m, t, t, n)
    elif k == "conc":
        rows = _rows(p, ev.row)
        h = n / 2
        _add(p, rows, "m_out_to_other_org", t, t, n)
        _add(p, rows, "m_fl_conc", t, t, -h)
        _add(p, rows, "multi_recip", t, len(p.months) - 1, -n)
        _stock(p, rows, t, -n, fl=-h)
        _add(p, rows, "y_out_to_other_org", t, t + 11, n)
        _add(p, rows, "y_fl_conc", t, t + 11, -h)
        # вторая работа у них в другом атоме: там доля человека растёт с 1/2 до 1
        o = int(ev.extra.get("other_atom", 0))
        for m in ("m_fl_conc", "pf_fl"):
            _add(p, [o], m, t, t if m == "m_fl_conc" else len(p.months) - 1, h)
        _add(p, [o], "multi_recip", t, len(p.months) - 1, -n)
    elif k == "calendar":
        # выплата за 1-е ушла в прошлый месяц: доля людей «пропала» на месяц и вернулась
        base = np.nan_to_num(p.x["m_base_pf"][:, t])
        nn = np.round(ev.q * base)
        for r in np.flatnonzero(nn > 0):
            sub = Event("dip", t, int(nn[r]), row=r)
            p = _inject_rows(p, sub, [r])
        _add_vec(p, "single_pay_recip", t, np.round(3 * ev.q * base))
    elif k == "underload":
        rows = np.flatnonzero((p.meta["sys_tb_id"].to_numpy() == ev.tb))
        for r in rows:
            f = ev.q * np.nan_to_num(p.x["pf_recip"][r, t])
            d = round(f)
            if d <= 0:
                continue
            for m in ("m_out_left_bank", "m_lb_unknown", "m_fl_out"):
                _add(p, [r], m, t, t, d)
            for m in ("pf_recip", "pf_fl", "any_recip"):
                _add(p, [r], m, t, t, -d)
            _add(p, [r], "raw_rows", t, t, -round(1.45 * d))
    else:
        raise ValueError(f"неизвестное событие {k}")
    _inject_cell_flows(p, ev)
    return p


def _inject_cell_flows(p: Panel, ev: Event) -> None:
    """Та же закладка в детализации потоков организации; исходная панель уже скопирована."""
    if p.org_flows.empty:
        return
    changes = []
    if ev.kind in ("recode", "reorg", "conc"):
        changes.append((ev.row, int(p.meta.at[ev.row, "atom"]), "out_other"))
    if ev.kind == "reorg":
        changes.append((ev.row2, int(p.meta.at[ev.row2, "atom"]), "in_other"))
    elif ev.kind == "recode":
        changes.append((ev.row, ev.row2, "in_other"))
    for r, a, f in changes:
        if p.meta.at[r, "lvl"] != "org" or a < 0:
            continue
        key = (r, ev.t, a)
        if key not in p.org_flows.index:
            p.org_flows.loc[key, :] = 0.0
        p.org_flows.at[key, f] += ev.n
    if changes:
        p.org_flows.sort_index(inplace=True)


def _move(p: Panel, a: list[int], b: list[int], t: int, n: int) -> None:
    """n человек перешли из рядов a в ряды b (другая организация или ГОСБ)."""
    _add(p, a, "m_out_to_other_org", t, t, n)
    _add(p, b, "m_in_from_other_org", t, t, n)
    _add(p, a, "m_fl_conc", t, t, -n)
    _add(p, b, "m_fl_conc", t, t, n)
    _stock(p, a, t, -n)
    _stock(p, b, t, n)
    _add(p, a, "y_out_to_other_org", t, t + 11, n)
    _add(p, b, "y_in_from_other_org", t, t + 11, n)


def _inject_rows(p: Panel, ev: Event, rows: list[int]) -> Panel:
    """Провал на месяц ровно в указанных рядах (без атома организации)."""
    t, n = ev.t, ev.n
    for m in ("m_out_left_bank", "m_lb_unknown", "m_out_temp", "m_fl_out"):
        _add(p, rows, m, t, t, n)
    for m in ("pf_recip", "pf_fl", "any_recip"):
        _add(p, rows, m, t, t, -n)
    _add(p, rows, "m_base_pf", t + 1, t + 1, -n)
    for m in ("m_in_from_outside", "m_in_returned", "m_fl_in"):
        _add(p, rows, m, t + 1, t + 1, n)
    _add(p, rows, "y_out_left_bank", t, t, n)
    return p


def _add_vec(p: Panel, m: str, t: int, d: np.ndarray) -> None:
    v = p.x[m][:, t]
    p.x[m][:, t] = np.where(np.isnan(v), v, v + d)


# --------------------------------------------------------------------------- #
# Шум ряда и выбор целей
# --------------------------------------------------------------------------- #
def net(p: Panel, t: int) -> np.ndarray:
    return p.x["pf_recip"][:, t] - p.x["m_base_pf"][:, t]


def row_noise(p: Panel, t: int, rows: np.ndarray, hist: int = 12) -> np.ndarray:
    """Шум изменения портфеля ряда: 1,4826 × MAD за прошлые месяцы (минимум 1)."""
    lo = max(0, t - hist)
    h = np.stack([net(p, s)[rows] for s in range(lo, t)], axis=1)
    med = np.nanmedian(h, axis=1, keepdims=True)
    return np.maximum(1.4826 * np.nanmedian(np.abs(h - med), axis=1), 1.0)


def pick_events(p: Panel, rng, kind: str, k: float, t: int, n_ev: int,
                min_people: int = 100) -> list[Event]:
    """n_ev событий типа kind размером k шумов в месяце t, в разных рядах."""
    org_kinds = {"stop", "reorg", "conc", "codes"}
    pool = p.orgs if kind in org_kinds or rng.random() < 0.5 else p.atoms
    base = np.nan_to_num(p.x["pf_recip"][pool, t - 1])
    nz = row_noise(p, t, pool)
    size = np.maximum(min_people, np.round(k * nz))
    ok = (base >= 2 * size) if kind != "stop" else (base >= min_people)
    cand = pool[ok]
    if len(cand) == 0:
        return []
    chosen = rng.choice(cand, size=min(n_ev, len(cand)), replace=False)
    out = []
    for r in chosen:
        i = int(np.flatnonzero(pool == r)[0])
        n = int(base[i]) if kind == "stop" else int(size[i])
        ev = Event(kind, t, n, row=int(r), sign=(1 if kind == "hire" else -1),
                   extra={"k": k, "noise": float(nz[i])})
        if kind == "reorg":
            others = cand[cand != r]
            ev.row2 = int(rng.choice(others)) if len(others) else int(r)
        if kind == "conc":
            ev.extra["other_atom"] = int(rng.choice(p.atoms))
        out.append(ev)
    return out


# --------------------------------------------------------------------------- #
# Простой соперник: «где изменение дальше всего от своей медианы»
# --------------------------------------------------------------------------- #
def baseline(p: Panel, t: int, z_min: float = 4.0, min_people: int = 100,
             top: int = 25) -> list[Card]:
    rows = np.arange(len(p.meta))
    best: dict[int, Card] = {}
    series = {"net": lambda s: net(p, s)}
    for m in IN_M + OUT_M:
        series[m] = (lambda mm: (lambda s: p.x[mm][:, s]))(m)
    for name, f in series.items():
        cur = f(t)
        h = np.stack([f(s) for s in range(max(0, t - 12), t)], axis=1)
        med = np.nanmedian(h, axis=1)
        mad = 1.4826 * np.nanmedian(np.abs(h - med[:, None]), axis=1) + 1
        dev = cur - med
        z = np.abs(dev) / mad
        for r in rows[(z >= z_min) & (np.abs(dev) >= min_people)]:
            if r not in best or z[r] > best[r].score:
                lvl = p.meta.at[r, "lvl"]
                best[r] = Card(lvl, int(r), name, float(dev[r]), score=float(z[r]))
    return sorted(best.values(), key=lambda c: -abs(c.delta))[:top]


# --------------------------------------------------------------------------- #
# Метрики
# --------------------------------------------------------------------------- #
def _where(p: Panel, r: int) -> dict:
    a = int(p.meta.at[r, "atom"])
    m = p.meta.iloc[a]
    return {"org": r if p.meta.at[r, "lvl"] == "org" else None, "atom": a,
            "gosb": int(m["sys_gosb_id"]), "tb": int(m["sys_tb_id"]), "seg": m["seg_rep"]}


def match(p: Panel, cards: list[Card], r: int, sign: int) -> str | None:
    """Самый точный уровень карточки, указавшей на ряд r с нужным знаком."""
    w = _where(p, r)
    got = []
    for c in cards:
        if c.label in ARTIFACTS or np.sign(c.delta) != sign:
            continue
        if c.level == "org" and c.key == w["org"]:
            got.append("org")
        elif c.level == "atom" and c.key == w["atom"]:
            got.append("atom")
        elif c.level in ("gosb", "tb", "seg") and c.key == w[c.level]:
            got.append(c.level)
        elif c.level == "bank":
            got.append("bank")
    return min(got, key=LEVELS.index) if got else None


def evaluate(detect, p: Panel, seed: int = 7, months_back: int = 6,
             sizes=(3.0, 4.0, 6.0), per_cell: int = 20, kinds=EVENT_KINDS) -> dict:
    """Экзамен: полнота по типу × размеру, точность места, ложные на чистых месяцах,
    эхо (провал прошлого месяца), утечка артефактов."""
    rng = np.random.default_rng(seed)
    T = len(p.months)
    ts = list(range(max(13, T - months_back), T))
    rec = []
    for kind in kinds:
        for k in sizes if kind != "stop" else (0.0,):
            for _ in range(per_cell):
                t = int(rng.choice(ts))
                evs = pick_events(p, rng, kind, k, t, 1)
                if not evs:
                    continue
                ev = evs[0]
                q = inject(p, ev)
                cards = detect(q, t)
                targets = [(ev.row, ev.sign)] + ([(ev.row2, 1)] if kind == "reorg" else [])
                for r, sgn in targets:
                    lv = match(q, cards, r, sgn)
                    rec.append({"kind": kind, "k": k, "n": ev.n, "lvl": p.meta.at[r, "lvl"],
                                "found": lv is not None and LEVELS.index(lv) <= LEVELS.index("tb"),
                                "exact": lv in ("org", "atom"), "level": lv})
    recall = pd.DataFrame(rec)

    clean = [len(detect(p, t)) for t in ts]

    echo = []
    for _ in range(per_cell):
        t = int(rng.choice(ts[1:])) if len(ts) > 1 else ts[0]
        evs = pick_events(p, rng, "dip", 4.0, t - 1, 1)
        if evs:
            q = inject(p, evs[0])
            echo.append(match(q, detect(q, t), evs[0].row, 1) is not None)

    leak = {}
    t = ts[-1]
    base_cards = detect(p, t)
    q = inject(p, Event("calendar", t - 1, 0, q=0.005))
    leak["calendar"] = (_extra_cards(detect(q, t - 1), detect(p, t - 1))
                        + _extra_cards(detect(q, t), base_cards))
    tb = int(rng.choice(p.meta.loc[p.atoms, "sys_tb_id"].unique()))
    q = inject(p, Event("underload", t, 0, tb=tb, q=0.08))
    leak["underload"] = _extra_cards(detect(q, t), base_cards)
    o = p.orgs
    if len(o) >= 2:
        r = int(o[np.nanargmax(np.nan_to_num(p.x["pf_recip"][o, t - 1]))])
        same = p.atoms[(p.meta.loc[p.atoms, "sys_tb_id"] == p.meta.at[r, "sys_tb_id"]).to_numpy()]
        dst = int(rng.choice(same[same != p.meta.at[r, "atom"]])) if len(same) > 1 else r
        n = int(0.5 * np.nan_to_num(p.x["pf_recip"][r, t - 1]))
        q = inject(p, Event("recode", t, n, row=r, row2=dst))
        leak["recode"] = _extra_cards(detect(q, t), base_cards)
    return {"recall": recall, "clean_cards": clean, "echo": float(np.mean(echo)) if echo else None,
            "leak": leak}


def _extra_cards(cards: list[Card], clean: list[Card]) -> int:
    """Карточки, которых не было в чистом месяце и которые не помечены как артефакт."""
    was = {(c.level, c.key) for c in clean}
    return sum(1 for c in cards if c.label not in ARTIFACTS and (c.level, c.key) not in was)


ACCEPT = {
    "recall_4": 0.9,     # ≥ 4 шумов и ≥ 2 000 чел. — найдено
    "recall_3": 0.5,     # 3 шума
    "clean_max": 5,      # карточек в спокойный месяц, в среднем
    "echo_max": 0.05,
    "leak_max": 0,       # лишних карточек от артефакта
}


def summary(res: dict) -> pd.DataFrame:
    r = res["recall"]
    if r.empty:
        return pd.DataFrame(columns=["kind", "k", "событий", "найдено", "точно", "людей_медиана"])
    t = (r.groupby(["kind", "k"])
         .agg(событий=("found", "size"), найдено=("found", "mean"), точно=("exact", "mean"),
              людей_медиана=("n", "median"))
         .round(2).reset_index())
    return t


def by_size(res: dict) -> pd.DataFrame:
    """Полнота по размеру события в шумах × в людях."""
    r = res["recall"]
    if r.empty:
        return pd.DataFrame()
    r = r.assign(люди=pd.cut(r["n"], [0, 300, 1000, 2000, 10 ** 9],
                             labels=["<300", "300–1 000", "1 000–2 000", "2 000+"]))
    return r.pivot_table(index="k", columns="люди", values="found", aggfunc="mean",
                         observed=False).round(2)


def verdict(res: dict) -> list[tuple[str, bool, str]]:
    """Пороги приёмки. Карточка требует ≥ 100 лишних людей с запасом на шум, поэтому
    события меньше 300 человек упираются в этот пол по построению: порог «3 шума»
    засчитывается от 300 человек, полнота по всем размерам показывается рядом."""
    r = res["recall"]
    if r.empty:
        return [("события заложить не удалось: ряды меньше 100 человек", False, "")]
    big = r[(r["k"] >= 4) & (r["n"] >= 2000)]
    mid = r[(r["k"] == 3) & (r["n"] >= 300)]
    out = [
        ("полнота: ≥ 4 шумов и ≥ 2 тыс. чел.", len(big) > 0 and big["found"].mean() >= ACCEPT["recall_4"],
         f"{big['found'].mean():.2f} из {len(big)}" if len(big) else "нет событий"),
        ("полнота: 3 шума, от 300 чел.", len(mid) > 0 and mid["found"].mean() >= ACCEPT["recall_3"],
         f"{mid['found'].mean():.2f} из {len(mid)} (все размеры: {r[r['k'] == 3]['found'].mean():.2f})"),
        ("карточек в спокойный месяц", np.mean(res["clean_cards"]) <= ACCEPT["clean_max"],
         f"{np.mean(res['clean_cards']):.1f}"),
        ("эхо провала", res["echo"] is not None and res["echo"] <= ACCEPT["echo_max"], f"{res['echo']}"),
    ]
    for a, v in res["leak"].items():
        out.append((f"артефакт «{a}» не даёт карточек", v <= ACCEPT["leak_max"], f"лишних {v}"))
    return out
