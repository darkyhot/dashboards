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

import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import months as M
from .inputs import CELL_MEAS, IN_M, MEAS, OUT_M, Panel

def quiet_nan(fn, *args, **kwargs):
    """NaN-свёртка NumPy без предупреждений о пустом срезе.

    Ряд без данных законно даёт NaN («неизвестно»): значение не меняется и нулём
    не подменяется, но тетрадка не засыпается красными RuntimeWarning.
    Глушатся только эти два сообщения; любые другие предупреждения остаются.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", r"All-NaN (slice|axis) encountered", RuntimeWarning)
        warnings.filterwarnings("ignore", r"Mean of empty slice", RuntimeWarning)
        return fn(*args, **kwargs)


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
             tail_mean: float = 22_000, months: list[str] | None = None,
             noise_scale: float = 1.) -> Panel:
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
    sig = noise_scale * np.concatenate([np.full(na, 0.06), np.full(n_orgs, 0.3)])

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
            "m_in_temp_outside": temp_prev,
            "m_in_from_other_org": in_oth, "m_in_codes": in_codes,
            "m_out_left_bank": out_left, "m_lb_seasonal": seas, "m_lb_org_stopped": 0 * pf,
            "m_lb_one_time": one, "m_lb_unknown": out_left - seas - one,
            "m_out_to_other_org": out_oth, "m_out_codes": out_codes,
            "m_out_temp": temp if t < T - 1 else np.full(n, np.nan),
            "m_out_temp_left": temp if t < T - 1 else np.full(n, np.nan),
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
        **{flow: xs[measure][na:].ravel() for flow, measure in CELL_MEAS.items()},
    }).set_index(["row", "t", "atom"]).sort_index()
    stock = pd.DataFrame({"row": np.repeat(np.arange(na, n), T),
                          "t": np.tile(np.arange(T), n_orgs), "atom": np.repeat(org_atom, T),
                          **{m: xs[m][na:].ravel() for m in
                             ("pf_recip", "m_base_pf", "pf_fl", "multi_recip")}})
    stock = stock.set_index(["row", "t", "atom"]).sort_index()
    return Panel(months, meta, xs, scopes, flows, stock)


def vary_departure_reasons(p: Panel, seed: int = 41) -> Panel:
    """Чистый стресс состава уходов: разные доли причин и их плавные колебания.
    Вызывать до распределения организаций по ГОСБ. Меняется только разметка причин,
    общий уход и портфель сохраняются. Хвост атома и организации моделируются отдельно.
    """
    q = p.copy()
    atoms, orgs = p.atoms, p.orgs
    atom = p.meta.loc[orgs, "atom"].to_numpy(dtype=int)
    scope = p.org_scopes.get("atom")
    if scope is None or (atom < 0).any() or not np.all(scope[orgs] == atom[:, None]):
        raise ValueError("Стресс причин нужен до распределения организаций по ГОСБ")
    rows = np.r_[atoms, orgs]
    na, T = len(atoms), len(p.months)
    local_index = {int(r): i for i, r in enumerate(atoms)}
    destination = np.array([local_index[int(a)] for a in atom])
    parts = {}
    for m in ("m_out_left_bank", "m_lb_seasonal", "m_lb_org_stopped"):
        parts[m] = p.x[m][rows].copy()
        np.subtract.at(parts[m], destination, p.x[m][orgs])
        if quiet_nan(np.nanmin, parts[m]) < -1e-6:
            raise ValueError("Организации превышают итог атома")
    rng = np.random.default_rng(seed)
    mean = rng.uniform(.75, .97, len(rows))
    shift = np.zeros(len(rows))
    proportion = np.empty((len(rows), T))
    for t in range(T):
        shift = .7 * shift + rng.normal(0, .02, len(rows))
        proportion[:, t] = np.clip(mean + shift, .5, .995)
    rest = np.maximum(parts["m_out_left_bank"] - parts["m_lb_seasonal"] - parts["m_lb_org_stopped"], 0.)
    unknown = rest * proportion
    for m, values in (("m_lb_unknown", unknown), ("m_lb_one_time", rest - unknown)):
        q.x[m][rows] = values
        np.add.at(q.x[m], atom, values[na:])
    return q


def spread_organizations(p: Panel, seed: int = 31, fraction: float = .4) -> Panel:
    """Независимый от детектора стресс: 40% организаций распределены между двумя ГОСБ.
    Линейное распределение агрегатов сохраняет тождества и общие итоги организации.
    Доли — модельные веса, поэтому внутри стресс-панели допустимы дробные агрегаты."""
    q = p.copy()
    rng = np.random.default_rng(seed)
    new_flows, new_stock = [], []
    for r in rng.permutation(p.orgs)[:int(fraction * len(p.orgs))]:
        a = int(p.meta.at[r, "atom"])
        scope = p.org_scopes.get("atom")
        if a < 0 or scope is None or (scope[r] != a).any():
            continue
        dst = p.atoms[(p.meta.loc[p.atoms, "seg_rep"].to_numpy() == p.meta.at[a, "seg_rep"])
                      & (p.meta.loc[p.atoms, "sys_tb_id"].to_numpy() == p.meta.at[a, "sys_tb_id"])
                      & (p.meta.loc[p.atoms, "sys_gosb_id"].to_numpy() != p.meta.at[a, "sys_gosb_id"])]
        if not len(dst):
            continue
        b = int(rng.choice(dst))
        share = float(rng.uniform(.2, .45))
        for m in MEAS:
            amount = np.nan_to_num(p.x[m][r]) * share
            q.x[m][a] -= amount
            q.x[m][b] += amount
        for frame, extra in ((q.org_flows, new_flows), (q.org_stock, new_stock)):
            if frame.empty:
                continue
            mask = frame.index.get_level_values("row") == r
            portion = frame.loc[mask].copy()
            frame.loc[mask, :] *= 1 - share
            portion *= share
            portion = portion.reset_index()
            portion["atom"] = b
            extra.append(portion.set_index(["row", "t", "atom"]))
        for level in ("atom", "gosb"):
            q.org_scopes[level][r] = -1
    for attr, extra in (("org_flows", new_flows), ("org_stock", new_stock)):
        if extra:
            frame = pd.concat([getattr(q, attr)] + extra)
            setattr(q, attr, frame.groupby(level=["row", "t", "atom"]).sum(min_count=1).sort_index())
    return q


def with_history_events(p: Panel, seed: int = 43) -> Panel:
    """В прошлых месяцах есть реальные отдельные события; последний месяц не меняется
    напрямую. Проверяет, не превращается ли вся история событий в общий порог шума."""
    q = p.copy()
    rng = np.random.default_rng(seed)
    for t in range(max(14, len(p.months) - 18), len(p.months) - 6, 2):
        events = pick_events(q, rng, "hire" if t % 4 else "leave", 6., t, 1, people=3000)
        if events:
            q = inject(q, events[0])
    return q


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
    old_pf = p.x["pf_recip"][rows, t:T + 1].copy()
    # Сопутствующие счётчики — доля виртуальной когорты в исходном остатке,
    # а не фиксированные 1,45 строки или 0,2 одиночной выплаты на человека.
    for m in ("any_recip", "raw_rows", "fot_amt", "single_pay_recip"):
        old = p.x[m][rows, t:T + 1]
        ratio = np.divide(old, old_pf, out=np.zeros_like(old), where=old_pf > 0)
        p.x[m][rows, t:T + 1] = old + d * ratio
    _add(p, rows, "pf_recip", t, T, d)
    _add(p, rows, "pf_fl", t, T, fl)
    _add(p, rows, "m_base_pf", t + 1, T, d)
    _add(p, rows, "y_base_pf", t + 12, T, d)


def _inject_at_main(p: Panel, ev: Event) -> Panel:
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
        fl_n = float(p.x["pf_fl"][ev.row, t]) if k == "stop" else n
        _add(p, rows, "m_fl_out", t, t, fl_n)
        if k == "stop":
            _add(p, rows, "multi_recip", t, t, -float(p.x["multi_recip"][ev.row, t]))
        fl = -fl_n if k == "stop" else None
        _stock(p, rows, t, -n, fl=fl)
        _add(p, rows, "y_out_codes" if k == "codes" else "y_out_left_bank", t, t + 11, n)
    elif k == "hire":
        rows = _rows(p, ev.row)
        _add(p, rows, "m_in_from_outside", t, t, n)
        _add(p, rows, "m_fl_in", t, t, n)
        _stock(p, rows, t, n)
        _add(p, rows, "y_in_from_outside", t, t + 11, n)
    elif k == "dip":
        rows = _rows(p, ev.row)
        for m in ("m_out_left_bank", "m_lb_unknown", "m_out_temp", "m_out_temp_left", "m_fl_out"):
            _add(p, rows, m, t, t, n)
        for m in ("pf_recip", "pf_fl", "any_recip"):
            _add(p, rows, m, t, t, -n)
        _add(p, rows, "m_base_pf", t + 1, t + 1, -n)
        for m in ("m_in_from_outside", "m_in_returned", "m_in_temp_outside", "m_fl_in"):
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
    else:
        raise ValueError(f"неизвестное событие {k}")
    return p


def _atom_stock(p: Panel, atom: int, measure: str) -> np.ndarray:
    """Остаток, доступный событию атома, которое не меняет отслеживаемые организации."""
    values = p.x[measure][atom].copy()
    if p.org_stock.empty or measure not in p.org_stock:
        return values
    local = p.org_stock.loc[p.org_stock.index.get_level_values("atom") == atom, measure]
    grouped = local.groupby(level="t")
    known = grouped.sum(min_count=1)
    known[grouped.count() != grouped.size()] = np.nan
    values[known.index.to_numpy(dtype=int)] -= known.to_numpy()
    return values


def _atom_stocks(p: Panel, measure: str) -> np.ndarray:
    values = p.x[measure][p.atoms].copy()
    if p.org_stock.empty or measure not in p.org_stock:
        return values
    grouped = p.org_stock[measure].groupby(level=["atom", "t"])
    known = grouped.sum(min_count=1)
    known[grouped.count() != grouped.size()] = np.nan
    aa = pd.Index(p.atoms).get_indexer(known.index.get_level_values("atom"))
    tt = known.index.get_level_values("t").to_numpy(dtype=int)
    valid = aa >= 0
    values[aa[valid], tt[valid]] -= known.to_numpy()[valid]
    return values


def cohort_capacity(p: Panel, r: int, t: int, kind: str) -> dict[int, float]:
    """Когорта, которую можно убрать без отрицательных остатков и будущей базы.
    Для организации используются её реальные ячейки, а не главная за всё окно."""
    end = t + 1 if kind == "dip" else len(p.months)
    if kind == "stop" and t != len(p.months) - 1:
        return {}
    if p.meta.at[r, "lvl"] == "org":
        if p.org_stock.empty:
            return {}
        try:
            stock = p.org_stock.xs(r, level="row")
        except KeyError:
            return {}
        ts = stock.index.get_level_values("t")
        stock = stock.loc[(ts >= t) & (ts < end)]
        caps = {}
        for a, g in stock.groupby(level="atom"):
            if a < 0 or len(g) != end - t:
                continue
            cols = ["pf_recip"] + ([] if kind == "stop" else ["pf_fl"])
            if kind == "conc":
                cols.append("multi_recip")
            values = g[cols].to_numpy()
            if not np.isfinite(values).all():
                continue
            caps[int(a)] = max(0., float(values.min()))
            if kind not in ("stop", "conc"):
                singles = (g["pf_recip"] - g["multi_recip"]).to_numpy()
                caps[int(a)] = min(caps[int(a)], max(0., float(np.min(singles))))
    else:
        caps = {r: float("inf")}
    for a in list(caps):
        def stock(measure):
            return (_atom_stock(p, a, measure) if p.meta.at[r, "lvl"] == "atom"
                    else p.x[measure][a])
        cols = ["pf_recip"] + ([] if kind == "stop" else ["pf_fl"])
        if kind == "conc":
            cols.append("multi_recip")
        values = np.concatenate([stock(m)[t:end] for m in cols])
        if kind not in ("stop", "conc"):
            values = np.r_[values, stock("pf_recip")[t:end] - stock("multi_recip")[t:end]]
        if kind != "stop" and t + 1 < len(p.months):
            next_end = t + 2 if kind == "dip" else end
            values = np.r_[values, stock("m_base_pf")[t + 1:next_end]]
        caps[a] = min(caps[a], max(0., float(values.min()))) if np.isfinite(values).all() else 0.
    return caps


def cohort_allocation(p: Panel, r: int, t: int, n: int, kind: str, adding=False,
                      source: int | None = None) -> dict[int, float]:
    if adding:
        if p.meta.at[r, "lvl"] == "org":
            try:
                s = p.org_stock.xs((r, t), level=("row", "t"))["pf_recip"]
            except KeyError:
                raise ValueError("Нет помесячной привязки организации для закладки")
            caps = {int(a): max(0., float(v)) for a, v in s.items() if a >= 0 and np.isfinite(v)}
        else:
            caps = {r: 1.}
        if sum(caps.values()) <= 0:
            raise ValueError("Нет ячейки для прихода когорты")
    else:
        caps = cohort_capacity(p, r, t, kind)
        if source is not None:
            caps = {source: caps.get(source, 0.)}
        if n > sum(caps.values()) + 1e-6:
            raise ValueError(f"Размер когорты {n} превышает доступный остаток {sum(caps.values()):.0f}")
    total = sum(caps.values())
    if n <= 0 or total <= 0:
        raise ValueError("Пустая когорта")
    return {a: n * c / total for a, c in caps.items() if c > 0}


def inject(p: Panel, ev: Event) -> Panel:
    """Согласованная закладка с распределением по реальным ячейкам организации."""
    if ev.kind in ("calendar", "underload"):
        return _inject_artifact(p, ev)
    adding = ev.kind == "hire"
    source = ev.extra.get("source_atom") if ev.kind == "recode" else None
    alloc = cohort_allocation(p, ev.row, ev.t, ev.n, ev.kind, adding, source)
    alloc2 = (cohort_allocation(p, ev.row2, ev.t, ev.n, ev.kind, adding=True)
              if ev.kind == "reorg" else {})
    ev.extra["allocation"] = alloc
    if alloc2:
        ev.extra["allocation2"] = alloc2
    if ev.kind == "recode":
        src = next(iter(alloc)) if len(alloc) == 1 else None
        if (src is None or ev.row2 not in p.atoms
                or p.meta.at[src, "seg_rep"] != p.meta.at[ev.row2, "seg_rep"]
                or p.meta.at[src, "sys_tb_id"] != p.meta.at[ev.row2, "sys_tb_id"]
                or p.meta.at[src, "sys_gosb_id"] == p.meta.at[ev.row2, "sys_gosb_id"]):
            raise ValueError("Смена ГОСБ должна сохранять сегмент и ТБ и менять ГОСБ")
    q = _inject_at_main(p, ev)
    # Сначала берём изменения самой организации; ошибочную привязку расчёта к одному
    # атому полностью заменяем реальным распределением. Факты подложки не меняются.
    participants = [(ev.row, alloc)] + ([(ev.row2, alloc2)] if alloc2 else [])
    if ev.kind == "recode":
        for m in MEAS:
            q.x[m][p.atoms] = p.x[m][p.atoms]
        for a, n in alloc.items():
            _move(q, [a], [ev.row2], ev.t, n)
            for t in range(ev.t, len(p.months)):
                src_key, dst_key = (ev.row, t, a), (ev.row, t, ev.row2)
                if dst_key not in q.org_stock.index:
                    q.org_stock.loc[dst_key, :] = 0.
                for m in ("pf_recip", "pf_fl") + (("m_base_pf",) if t > ev.t else ()):
                    q.org_stock.at[src_key, m] -= n
                    q.org_stock.at[dst_key, m] += n
    else:
        deltas = {r: {m: np.nan_to_num(q.x[m][r] - p.x[m][r], nan=0.) for m in MEAS}
                  for r, _ in participants}
        for m in MEAS:
            q.x[m][p.atoms] = p.x[m][p.atoms]
        for r, portions in participants:
            if p.meta.at[r, "lvl"] == "atom":
                for m in MEAS:
                    q.x[m][r] += deltas[r][m]
                continue
            for a, n in portions.items():
                for m in MEAS:
                    q.x[m][a] += deltas[r][m] * (n / ev.n)
                if ev.kind == "stop":
                    for stock in ("pf_fl", "multi_recip"):
                        old = p.org_stock.at[(r, ev.t, a), stock]
                        q.x[stock][a, ev.t] = p.x[stock][a, ev.t] - old
        if ev.kind == "conc":
            a = int(ev.extra["other_atom"])
            _add(q, [a], "m_fl_conc", ev.t, ev.t, ev.n / 2)
            _add(q, [a], "pf_fl", ev.t, len(p.months) - 1, ev.n / 2)
            _add(q, [a], "multi_recip", ev.t, len(p.months) - 1, -ev.n)
    q.org_flows = p.org_flows.copy()
    for r, portions in participants:
        if p.meta.at[r, "lvl"] != "org":
            continue
        for a, n in portions.items():
            for m in q.org_stock.columns:
                if (r, ev.t, a) not in q.org_stock.index:
                    continue
                times = np.arange(ev.t, len(p.months))
                keys = [(r, int(t), a) for t in times if (r, int(t), a) in q.org_stock.index]
                for key in keys:
                    delta = np.nan_to_num(q.x[m][r, key[1]] - p.x[m][r, key[1]])
                    q.org_stock.at[key, m] += delta * n / ev.n
                    if ev.kind == "stop" and m in ("pf_fl", "multi_recip"):
                        q.org_stock.at[key, m] = 0.
            for f, col in CELL_MEAS.items():
                if f not in q.org_flows:
                    continue
                cell = ev.row2 if ev.kind == "recode" and f == "in_other" else a
                delta = q.x[col][r] - p.x[col][r]
                for h in np.flatnonzero(np.isfinite(delta) & (abs(delta) > 1e-9)):
                    key = (r, int(h), cell)
                    if key not in q.org_flows.index:
                        q.org_flows.loc[key, :] = 0.
                    q.org_flows.at[key, f] += delta[h] * n / ev.n
    if not q.org_flows.empty:
        q.org_flows.sort_index(inplace=True)
    if not q.org_stock.empty:
        q.org_stock.sort_index(inplace=True)
    for m in ("pf_recip", "pf_fl", "multi_recip", "m_base_pf") + tuple(IN_M + OUT_M):
        introduced = (q.x[m] < -1e-6) & (p.x[m] >= -1e-6)
        if introduced.any():
            raise ValueError(f"Закладка создала отрицательное значение {m}")
    for count in ("pf_fl", "multi_recip"):
        invalid = (q.x[count] > q.x["pf_recip"] + 1e-6) & (p.x[count] <= p.x["pf_recip"] + 1e-6)
        if invalid.any():
            raise ValueError(f"Закладка создала {count} больше портфеля")
    gap = q.identity_gap(ev.t) - p.identity_gap(ev.t)
    if quiet_nan(np.nanmax, np.abs(gap)) > 1e-5:
        raise ValueError("Закладка нарушила тождество движений")
    return q


def _cell_changes(frame: pd.DataFrame, changes: list[pd.DataFrame]) -> pd.DataFrame:
    """Пакетное изменение существующих ячеек, без добавления выдуманных нулей."""
    if frame.empty or not changes:
        return frame
    delta = pd.concat(changes).groupby(level=["row", "t", "atom"]).sum(min_count=1)
    if len(delta.index.difference(frame.index)):
        raise ValueError("Для артефакта нет исходной ячейки организации")
    columns = [c for c in delta if c in frame]
    if columns:
        frame.loc[delta.index, columns] = frame.loc[delta.index, columns] + delta[columns]
    return frame.sort_index()


def _inject_artifact(p: Panel, ev: Event) -> Panel:
    """Одна когорта по фактическим ячейкам и остатку, затем суммы по организации.

    Доля совместителей и долевые ФЛ берутся внутри каждой ячейки. Их нельзя
    распределять обратно по долям общего портфеля организации: состав различается.
    """
    if not 0 <= ev.q <= 1:
        raise ValueError("Доля артефакта должна быть от 0 до 1")
    q, t, atoms = p.copy(), ev.t, p.atoms
    calendar = ev.kind == "calendar"
    if calendar and t >= len(p.months) - 1:
        raise ValueError("У календарного сдвига должен наблюдаться месяц возврата")
    stock_measures = ["pf_recip", "m_base_pf", "pf_fl", "multi_recip"]
    if len(p.orgs) and p.org_stock.empty:
        raise ValueError("Для артефакта нужна детализация остатков организаций по ячейкам")
    if p.org_stock.empty:
        current = pd.DataFrame(columns=["row", "atom"] + stock_measures)
    else:
        current = p.org_stock.xs(t, level="t").reset_index()
    # Остаток — отдельная непересекающаяся часть атома; его состав не угадывается.
    tail = pd.DataFrame({"row": atoms, "atom": atoms,
                         **{m: _atom_stocks(p, m)[:, t] for m in stock_measures}})
    components = pd.concat([current, tail], ignore_index=True)
    rr = components["row"].to_numpy(dtype=int)
    aa = components["atom"].to_numpy(dtype=int)
    if (aa < 0).any():
        raise ValueError("Для артефакта неизвестна фактическая ячейка организации")
    known = p.meta.loc[rr, "lvl"].to_numpy() == "org"
    pf = components["pf_recip"].to_numpy(dtype=float)
    valid = np.isfinite(components[stock_measures].to_numpy(dtype=float)).all(axis=1)
    if np.any(valid & (components[stock_measures].to_numpy(dtype=float).min(axis=1) < -1e-6)):
        raise ValueError("В исходном составе артефакта отрицательный остаток")
    fraction = np.zeros(len(p.meta))
    if calendar:
        available = np.minimum(p.x["pf_recip"][atoms, t], p.x["m_base_pf"][atoms, t + 1])
        wanted = np.minimum(np.round(ev.q * p.x["m_base_pf"][atoms, t]), available)
        fraction[atoms] = np.divide(wanted, p.x["pf_recip"][atoms, t],
            out=np.zeros(len(atoms)), where=p.x["pf_recip"][atoms, t] > 0)
        fraction[atoms[~np.isfinite(p.x["m_out_temp"][atoms, t])]] = 0.
    else:
        fraction[atoms[p.meta.loc[atoms, "sys_tb_id"].to_numpy() == ev.tb]] = ev.q
    removed = np.where(valid & (pf > 0), pf * fraction[aa], 0.)
    if calendar:
        next_base = np.zeros(len(components))
        if known.any():
            keys = pd.MultiIndex.from_arrays([rr[known], np.full(known.sum(), t + 1), aa[known]],
                                              names=["row", "t", "atom"])
            next_base[known] = p.org_stock["m_base_pf"].reindex(keys, fill_value=0.).to_numpy()
        next_base[~known] = _atom_stocks(p, "m_base_pf")[:, t + 1]
        # Нельзя вернуть временную когорту в несуществующую базу следующего месяца.
        removed = np.minimum(removed, np.maximum(next_base, 0.))
        removed[~np.isfinite(removed)] = 0.
    take = removed > 0
    components, rr, aa, known, pf, removed = (v[take] if isinstance(v, np.ndarray) else v.loc[take]
                                            for v in (components, rr, aa, known, pf, removed))
    if not len(removed):
        return q
    share = removed / pf
    losses = {m: components[m].to_numpy(dtype=float) * share
              for m in ("pf_recip", "pf_fl", "multi_recip")}

    def aggregate(measure, month, delta):
        # Для остатка row уже равен атому; для организации её локальный вклад
        # добавляется и в строку организации, и в атом.
        np.add.at(q.x[measure][:, month], rr, delta)
        np.add.at(q.x[measure][:, month], aa[known], delta[known])

    for m, loss in losses.items():
        aggregate(m, t, -loss)
    for m in ("m_out_left_bank", "m_lb_unknown"):
        aggregate(m, t, removed)
    aggregate("m_fl_out", t, losses["pf_fl"])
    total_removed = p.x["pf_recip"][:, t] - q.x["pf_recip"][:, t]
    total_share = np.divide(total_removed, p.x["pf_recip"][:, t], out=np.zeros(len(p.meta)),
                            where=p.x["pf_recip"][:, t] > 0)
    for m in ("any_recip", "raw_rows", "fot_amt", "single_pay_recip"):
        q.x[m][:, t] = p.x[m][:, t] * (1 - total_share)
    if calendar:
        for m in ("m_out_temp", "m_out_temp_left", "y_out_left_bank"):
            aggregate(m, t, removed)
        aggregate("m_base_pf", t + 1, -removed)
        for m in ("m_in_from_outside", "m_in_returned", "m_in_temp_outside"):
            aggregate(m, t + 1, removed)
        aggregate("m_fl_in", t + 1, losses["pf_fl"])
        room = np.maximum(q.x["pf_recip"][:, t] - q.x["single_pay_recip"][:, t], 0.)
        q.x["single_pay_recip"][:, t] += np.minimum(3 * total_removed, room)
    if known.any():
        def indexed(values, month):
            return pd.DataFrame(values, index=pd.MultiIndex.from_arrays(
                [rr[known], np.full(known.sum(), month), aa[known]], names=["row", "t", "atom"]))
        stock_delta = [indexed({m: -loss[known] for m, loss in losses.items()}, t)]
        flow_delta = [indexed({"out_left": removed[known]}, t)]
        if calendar:
            stock_delta.append(indexed({"m_base_pf": -removed[known]}, t + 1))
            flow_delta[0]["out_temp_left"] = removed[known]
            flow_delta.append(indexed({"in_outside": removed[known],
                                       "in_temp_outside": removed[known]}, t + 1))
        # Нули здесь — отсутствие изменения, не подстановка исходного наблюдения.
        q.org_stock = _cell_changes(q.org_stock, [d.reindex(columns=stock_measures, fill_value=0.)
                                                 for d in stock_delta])
        q.org_flows = _cell_changes(q.org_flows, [d.reindex(columns=list(CELL_MEAS), fill_value=0.)
                                                 for d in flow_delta])
    return q


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






# --------------------------------------------------------------------------- #
# Шум ряда и выбор целей
# --------------------------------------------------------------------------- #
def net(p: Panel, t: int) -> np.ndarray:
    return p.x["pf_recip"][:, t] - p.x["m_base_pf"][:, t]


EVENT_MEASURE = {"leave": "m_out_left_bank", "stop": "m_out_left_bank", "dip": "m_out_left_bank",
                 "hire": "m_in_from_outside", "codes": "m_out_codes",
                 "reorg": "m_out_to_other_org", "conc": "m_out_to_other_org"}
FLOW_ALIAS = {"m_in_from_outside": "in_outside", "m_in_from_other_org": "in_other",
              "m_in_codes": "in_codes", "m_out_left_bank": "out_left",
              "m_out_to_other_org": "out_other", "m_out_codes": "out_codes"}


def row_noise(p: Panel, t: int, rows: np.ndarray, hist: int = 12,
              measure: str | None = None) -> np.ndarray:
    """MAD именно изменяемого потока, а не разности прихода и ухода."""
    lo = max(0, t - hist)
    h = (p.x[measure][rows, lo:t] if measure else
         np.stack([net(p, s)[rows] for s in range(lo, t)], axis=1))
    from .watch import _nanmedian  # ленивый импорт: watch сам импортирует bench

    # Ряд без истории даёт NaN (шум неизвестен) — без предупреждений NumPy в тетрадке.
    med = _nanmedian(h, axis=1, keepdims=True)
    return np.maximum(1.4826 * _nanmedian(np.abs(h - med), axis=1), 1.0)


def pick_events(p: Panel, rng, kind: str, k: float, t: int, n_ev: int,
                min_people: int = 300, people: int | None = None) -> list[Event]:
    """n_ev событий типа kind размером k шумов в месяце t, в разных рядах."""
    org_kinds = {"stop", "reorg", "conc", "codes"}
    pool = p.orgs if kind in org_kinds or rng.random() < 0.5 else p.atoms
    if kind == "dip" and t >= len(p.months) - 1:
        return []  # в последнем месяце возврат ещё не наблюдается
    base = np.nan_to_num(p.x["pf_recip"][pool, t])
    nz = row_noise(p, t, pool, measure=EVENT_MEASURE[kind])
    size = np.full(len(pool), people) if people is not None else np.maximum(min_people, np.round(k * nz))
    ok = (base >= 2 * size) if kind != "stop" else (base >= min_people)
    cand = pool[ok]
    if len(cand) == 0:
        return []
    out = []
    for r in rng.permutation(cand)[:100]:
        i = int(np.flatnonzero(pool == r)[0])
        n = int(base[i]) if kind == "stop" else int(size[i])
        ev = Event(kind, t, n, row=int(r), sign=(1 if kind == "hire" else -1),
                   extra={"k": k, "noise": float(nz[i]), "effective_k": n / nz[i],
                          "size_mode": "people" if people is not None else "noise"})
        if kind == "reorg":
            others = cand[cand != r]
            if not len(others):
                continue
            ev.row2 = int(rng.choice(others))
        if kind == "conc":
            cap = quiet_nan(np.nanmin, _atom_stocks(p, "multi_recip")[:, t:], axis=1)
            available = p.atoms[(cap >= n) & (p.atoms != p.meta.at[r, "atom"])]
            if not len(available):
                continue
            ev.extra["other_atom"] = int(rng.choice(available))
        if kind != "hire" and sum(cohort_capacity(p, int(r), t, kind).values()) + 1e-6 < n:
            continue
        if not np.isfinite(p.x[EVENT_MEASURE[kind]][r, t]):
            continue
        out.append(ev)
        if len(out) >= n_ev:
            break
    return out


def pick_recode(p: Panel, rng, t: int) -> Event | None:
    """Перенос части одной реальной ячейки в другой ГОСБ того же сегмента и ТБ."""
    order = p.orgs[np.argsort(np.nan_to_num(p.x["pf_recip"][p.orgs, t]))[::-1]]
    for r in order:
        caps = cohort_capacity(p, int(r), t, "recode")
        for a, cap in sorted(caps.items(), key=lambda item: -item[1]):
            dst = p.atoms[(p.meta.loc[p.atoms, "sys_tb_id"].to_numpy() == p.meta.at[a, "sys_tb_id"])
                          & (p.meta.loc[p.atoms, "seg_rep"].to_numpy() == p.meta.at[a, "seg_rep"])
                          & (p.meta.loc[p.atoms, "sys_gosb_id"].to_numpy() != p.meta.at[a, "sys_gosb_id"])]
            if cap >= 600 and len(dst):
                return Event("recode", t, int(cap / 2), row=int(r), row2=int(rng.choice(dst)),
                             extra={"source_atom": a})
    return None


# --------------------------------------------------------------------------- #
# Простой соперник: «где изменение дальше всего от своей медианы»
# --------------------------------------------------------------------------- #
def baseline(p: Panel, t: int, z_min: float = 4.0, min_people: int = 100,
             top: int = 25) -> list[Card]:
    from .watch import _nanmedian  # ленивый импорт: watch сам импортирует bench

    rows = np.arange(len(p.meta))
    best: dict[int, Card] = {}
    series = {"net": lambda s: net(p, s)}
    for m in IN_M + OUT_M:
        series[m] = (lambda mm: (lambda s: p.x[mm][:, s]))(m)
    for name, f in series.items():
        cur = f(t)
        h = np.stack([f(s) for s in range(max(0, t - 12), t)], axis=1)
        med = _nanmedian(h, axis=1)
        mad = 1.4826 * _nanmedian(np.abs(h - med[:, None]), axis=1) + 1
        dev = cur - med
        z = np.abs(dev) / mad
        for r in rows[(z >= z_min) & (np.abs(dev) >= min_people)]:
            if r not in best or z[r] > best[r].score:
                lvl = p.meta.at[r, "lvl"]
                sign = -1 if name in OUT_M else 1
                best[r] = Card(lvl, int(r), name, sign * float(dev[r]), score=float(z[r]))
    return sorted(best.values(), key=lambda c: -abs(c.delta))[:top]


# --------------------------------------------------------------------------- #
# Метрики
# --------------------------------------------------------------------------- #
def _where(p: Panel, r: int, t: int | None = None) -> dict:
    a = int(p.meta.at[r, "atom"])
    if t is not None and p.meta.at[r, "lvl"] == "org" and not p.org_stock.empty:
        try:
            loc = p.org_stock.xs((r, t), level=("row", "t"))["pf_recip"]
            if len(loc):
                a = int(loc.idxmax())
        except KeyError:
            pass
    if a < 0:
        return {"org": r, "atom": None, "gosb": None, "tb": None, "seg": None}
    m = p.meta.iloc[a]
    return {"org": r if p.meta.at[r, "lvl"] == "org" else None, "atom": a,
            "gosb": int(m["sys_gosb_id"]), "tb": int(m["sys_tb_id"]), "seg": m["seg_rep"]}


def match(p: Panel, cards: list[Card], r: int, sign: int, before: list[Card] | None = None,
          measure: str | None = None, min_effect: float = 0., t: int | None = None) -> str | None:
    """Самый точный уровень карточки, указавшей на ряд r с нужным знаком."""
    w = _where(p, r, t)
    expected = FLOW_ALIAS.get(measure, measure)
    previous = {(c.level, c.key, c.info.get("scope", "all")): c for c in before or []}

    def effect(c):
        if expected is None or c.measure == "net" or FLOW_ALIAS.get(c.measure, c.measure) == expected:
            return c.delta
        return c.info.get("flows", {}).get(expected, {}).get("delta")

    got = []
    for c in cards:
        value = effect(c)
        if c.label in ARTIFACTS or value is None or np.sign(value) != sign:
            continue
        old = previous.get((c.level, c.key, c.info.get("scope", "all")))
        old_value = effect(old) if old is not None else None
        if before is not None and old_value is not None and sign * (value - old_value) < min_effect:
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


def make_plan(p: Panel, seed: int = 7, months_back: int = 6,
              sizes=(3.0, 4.0, 6.0), per_cell: int = 10, kinds=EVENT_KINDS,
              people_sizes=(300, 1000, 3000), per_size: int = 3) -> dict:
    """Единая детерминированная жеребьёвка для стенда и его диагностики."""
    rng = np.random.default_rng(seed)
    ts = list(range(max(13, len(p.months) - months_back), len(p.months)))
    if not ts:
        raise ValueError("Для стенда нужно не менее 14 месяцев")
    trials, skipped = [], []
    for kind in kinds:
        bands = [(k, None, per_cell) for k in sizes] if kind != "stop" else [(0., None, per_cell)]
        if kind != "stop":
            bands += [(0., n, per_size) for n in people_sizes]
        for k, people, count in bands:
            for _ in range(count):
                eligible_ts = ts[:-1] if kind == "dip" else ts
                t = ts[-1] if kind == "stop" else int(rng.choice(eligible_ts or ts))
                events = pick_events(p, rng, kind, k, t, 1, people=people)
                if events:
                    trials.append(events[0])
                else:
                    skipped.append({"kind": kind, "t": t, "k": k, "people": people,
                                    "reason": "нет подходящей когорты"})
    echoes = []
    for _ in range(per_cell):
        t = int(rng.choice(ts[1:])) if len(ts) > 1 else ts[0]
        echoes.extend(pick_events(p, rng, "dip", 4., t - 1, 1))
    t = ts[-1]
    artifacts = {"calendar": Event("calendar", t - 1, 0, q=.005),
                 "underload": Event("underload", t, 0, tb=int(rng.choice(
                     p.meta.loc[p.atoms, "sys_tb_id"].unique())), q=.08)}
    recode = pick_recode(p, rng, t)
    if recode is not None:
        artifacts["recode"] = recode
    return {"trials": trials, "echoes": echoes, "artifacts": artifacts,
            "skipped": skipped, "ts": ts, "seed": seed}


def evaluate(detect, p: Panel, seed: int = 7, months_back: int = 6,
             sizes=(3.0, 4.0, 6.0), per_cell: int = 10, kinds=EVENT_KINDS,
             people_sizes=(300, 1000, 3000), per_size: int = 3, plan: dict | None = None,
             clean_is_known: bool = False, progress=None, completed: dict | None = None) -> dict:
    """Экзамен: полнота по типу × размеру, точность места, ложные на чистых месяцах,
    эхо (провал прошлого месяца), утечка артефактов."""
    plan = plan or make_plan(p, seed, months_back, sizes, per_cell, kinds, people_sizes, per_size)
    ts = plan["ts"]
    if completed is None:
        backgrounds = {t: detect(p, t) for t in sorted(set(ts + [ts[-1] - 1]))}
        rec = []
        invalid = []
        for index, ev in enumerate(plan["trials"]):
            t, kind = ev.t, ev.kind
            try:
                q = inject(p, ev)
            except ValueError as exc:
                invalid.append({"kind": kind, "t": t, "row": ev.row, "n": ev.n, "error": str(exc)})
                continue
            cards = detect(q, t)
            targets = [(ev.row, ev.sign, EVENT_MEASURE[kind])]
            if kind == "reorg":
                targets.append((ev.row2, 1, "m_in_from_other_org"))
            for r, sgn, measure in targets:
                noise = float(row_noise(p, t, np.array([r]), measure=measure)[0])
                lv = match(p, cards, r, sgn, backgrounds[t], measure, max(1., .5 * ev.n), t)
                rec.append({"kind": kind, "k": ev.extra["k"], "effective_k": ev.n / noise,
                            "mode": ev.extra["size_mode"], "noise": noise,
                            "n": ev.n, "t": t, "row": r, "measure": measure,
                            "lvl": p.meta.at[r, "lvl"], "found": lv is not None and LEVELS.index(lv) <= LEVELS.index("tb"),
                            "exact": lv in ("org", "atom"), "level": lv})
            if progress and (index + 1) % 25 == 0:
                progress(f"Проверено событий: {index + 1}/{len(plan['trials'])}")
        recall = pd.DataFrame(rec)

        clean = [sum(c.label not in ARTIFACTS for c in backgrounds[t]) for t in ts]

        echo = []
        for ev in plan["echoes"]:
            try:
                q = inject(p, ev)
            except ValueError as exc:
                invalid.append({"kind": "echo", "t": ev.t, "error": str(exc)})
                continue
            t = ev.t + 1
            if t not in backgrounds:
                backgrounds[t] = detect(p, t)
            echo.append(match(p, detect(q, t), ev.row, 1, backgrounds[t],
                              "m_in_from_outside", max(1., .5 * ev.n), t) is not None)

    else:
        if completed.get("plan") is not plan:
            raise ValueError("Продолжение стенда относится к другому плану")
        backgrounds = dict(completed["backgrounds"])
        recall = completed["recall"].copy()
        clean = list(completed["clean_cards"])
        echo = list(completed["echo_values"])
        invalid = []
        if progress:
            progress("Фон, события и эхо восстановлены из завершённых этапов; проверяются артефакты.")

    leak = {}
    details = {}
    for name, ev in plan["artifacts"].items():
        try:
            q = inject(p, ev)
        except ValueError as exc:
            invalid.append({"kind": name, "t": ev.t, "error": str(exc)})
            continue
        months = (ev.t, ev.t + 1) if name == "calendar" else (ev.t,)
        frames, total = [], 0
        for t in months:
            after = detect(q, t)
            before = backgrounds[t]
            extra = extra_cards(after, before, p=p, changed=q, t=t)
            total += len(extra)
            frames.append({"t": t, "before": before, "after": after, "extra": extra})
        leak[name] = total
        details[name] = {"event": ev, "frames": frames}
    return {"recall": recall, "clean_cards": clean, "echo": float(np.mean(echo)) if echo else None,
            "leak": leak, "artifact_details": details, "invalid": pd.DataFrame(invalid),
            "skipped": pd.DataFrame(plan["skipped"]), "clean_is_known": clean_is_known,
            "protocol": 3, "plan": plan, "backgrounds": backgrounds}


def _extra_cards(cards: list[Card], clean: list[Card]) -> int:
    """Карточки, которых не было в чистом месяце и которые не помечены как артефакт."""
    return len(extra_cards(cards, clean))


def extra_cards(cards: list[Card], clean: list[Card], p: Panel | None = None,
                 changed: Panel | None = None, t: int | None = None) -> list[Card]:
    """Новая мера/направление либо материальное усиление существующего сигнала."""
    was = {(c.level, c.key, c.measure, int(np.sign(c.delta)), c.info.get("scope", "all")): c for c in clean
           if c.label not in ARTIFACTS}
    result = []
    def replaced(c):
        # У остатка своя совокупность. Карточка крупной организации не может
        # объяснить его изменение, даже если общий итог атома не изменился.
        if c.info.get("scope") == "other_orgs":
            return False
        if p is None or changed is None or t is None:
            return False
        measure = FLOW_ALIAS.get(c.measure, c.measure)
        col = next((m for m, f in FLOW_ALIAS.items() if f == measure), None)
        if col is None:
            return False
        if c.level in ("org", "atom"):
            rows = np.array([c.key])
        else:
            rows = p.atoms
            dim = {"gosb": "sys_gosb_id", "tb": "sys_tb_id", "seg": "seg_rep"}.get(c.level)
            if dim:
                rows = rows[(p.meta.loc[rows, dim] == c.key).to_numpy()]
        if not np.isclose(np.nansum(p.x[col][rows, t]), np.nansum(changed.x[col][rows, t]), atol=1e-6, rtol=0):
            return False
        amounts = {}
        for old in clean:
            if (old.label in ARTIFACTS or FLOW_ALIAS.get(old.measure, old.measure) != measure
                    or np.sign(old.delta) != np.sign(c.delta)):
                continue
            contained = False
            if old.level == "org" and c.level in p.org_scopes:
                contained = p.org_scopes[c.level][old.key, t] == c.key
            elif old.level == "atom" and c.level in ("gosb", "tb"):
                contained = p.meta.at[old.key, "sys_" + c.level + "_id"] == c.key
            if contained:
                amounts[old.level] = amounts.get(old.level, 0.) + abs(old.delta)
        return max(amounts.values(), default=0.) >= .7 * abs(c.delta)
    for c in cards:
        if c.label in ARTIFACTS:
            continue
        old = was.get((c.level, c.key, c.measure, int(np.sign(c.delta)), c.info.get("scope", "all")))
        if old is None or abs(c.delta) - abs(old.delta) >= max(100., .1 * abs(old.delta)):
            if not replaced(c):
                result.append(c)
    return result


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
              людей_медиана=("n", "median"), шумов_медиана=("effective_k", "median"))
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


def acceptance_groups(r: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Маски целей приёмки: крупные (≥ 4 шумов, ≥ 2 000 чел.) и «около трёх шумов» (≥ 300 чел.).
    Одно определение для вердикта и для разбора пропусков."""
    actual_k = r["effective_k"] if "effective_k" in r else r["k"]
    big = (actual_k >= 3.95) & (r["n"] >= 2000) & (r["kind"] != "stop")
    mid = (abs(actual_k - 3) <= .15) & (r["n"] >= 300)
    return big, mid


def verdict(res: dict) -> list[tuple[str, bool, str]]:
    """Пороги приёмки. Карточка требует ≥ 100 лишних людей с запасом на шум, поэтому
    события меньше 300 человек упираются в этот пол по построению: порог «3 шума»
    засчитывается от 300 человек, полнота по всем размерам показывается рядом."""
    r = res["recall"]
    if r.empty:
        return [("события заложить не удалось: ряды меньше 100 человек", False, "")]
    big_mask, mid_mask = acceptance_groups(r)
    big, mid = r[big_mask], r[mid_mask]
    out = [
        ("полнота: ≥ 4 шумов и ≥ 2 тыс. чел.", len(big) > 0 and big["found"].mean() >= ACCEPT["recall_4"],
         f"{big['found'].mean():.2f} из {len(big)}" if len(big) else "нет событий"),
        ("полнота: 3 шума, от 300 чел.", len(mid) > 0 and mid["found"].mean() >= ACCEPT["recall_3"],
         f"{mid['found'].mean():.2f} из {len(mid)} (все размеры: {r[r['k'] == 3]['found'].mean():.2f})"),
        ("эхо провала", res["echo"] is not None and res["echo"] <= ACCEPT["echo_max"], f"{res['echo']}"),
    ]
    if res.get("clean_is_known", True):
        out.append(("ложных карточек на чистом фоне", np.mean(res["clean_cards"]) <= ACCEPT["clean_max"],
                    f"{np.mean(res['clean_cards']):.1f}"))
    invalid = res.get("invalid", pd.DataFrame())
    out.append(("закладки без ошибок и отрицательных остатков", invalid.empty, f"ошибок {len(invalid)}"))
    planned = {ev.kind for ev in res.get("plan", {}).get("trials", [])}
    for kind in sorted(planned):
        n = int((r["kind"] == kind).sum())
        out.append((f"покрытие типа {kind}", n > 0, f"целей {n}"))
    for a, v in res["leak"].items():
        out.append((f"артефакт «{a}» не даёт карточек", v <= ACCEPT["leak_max"], f"лишних {v}"))
    for a in ARTIFACTS - res["leak"].keys():
        out.append((f"артефакт «{a}» проверен", False, "нет допустимой закладки"))
    return out
