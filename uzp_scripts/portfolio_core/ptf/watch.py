"""Сторож: сам находит в поколении отклонения движений портфеля и объясняет их.

Проверяются помесячные движения по меткам подложки (пришли извне / из другой
организации / с не зп кодов, ушли из банка / в другую организацию / на не зп коды).

**Ожидание ячейки** = база × ставка той же меры в похожей группе **в других ТБ** этого
месяца × собственный устойчивый сдвиг ячейки (медиана за 12 месяцев, подтянутая к 1 у
малых). Сезон, календарь выплат и общий фон банка есть во всех ТБ — сравнение с другими
ТБ их снимает. Для банка и сегмента других ТБ нет — там ожидание по тому же месяцу год
назад с поправкой на текущий уровень, а найденное подписывается по признакам.

**Шум** — пуассоновский с «комковатостью» организаций: s² = E + κ²E², κ по классу
размера из прошлых месяцев. **Отбор** — нижняя граница лишних людей
L = |факт − ожидание| − z·s ≥ max(100, 0,1% базы); порог каждого потока учитывает число
проверок и прошлые отклонения. Эмпирический бюджет — 0,5 на поток в месяц;
общее число ложных тревог по всем потокам и уровням проверяет стенд.

**Порядок**: организации → атомы (включая недогруз ТБ) → ГОСБ и ТБ → банк и сегменты.
Все уровни считаются по исходным наблюдениям. Повторные карточки убираются только
при подтверждённом вложении территории и совпадении направления отклонения.
"""
from __future__ import annotations

from statistics import NormalDist

import numpy as np
import pandas as pd

from .bench import Card
from .inputs import Panel

FLOWS = {   # имя → (столбец, знак влияния на портфель)
    "in_outside": ("m_in_from_outside", +1),
    "in_other": ("m_in_from_other_org", +1),
    "in_codes": ("m_in_codes", +1),
    "out_left": ("m_out_left_bank", -1),
    "out_other": ("m_out_to_other_org", -1),
    "out_codes": ("m_out_codes", -1),
}
HIST = 12            # месяцев истории для сдвига ячейки и шума
CALIB = 12           # месяцев для эмпирического нуля
MIN_PEOPLE = 100
MIN_SHARE = 0.001
FALSE_PER_LEVEL = 0.5
PRIOR = 20.0         # сила подтягивания сдвига к 1, в ожидаемых людях
MAX_CARDS = 25
SIZE_EDGES = [3_000, 10_000]


# --------------------------------------------------------------------------- #
# Ряды и ожидания
# --------------------------------------------------------------------------- #
def calendar_component(p: Panel) -> np.ndarray:
    """Общая добавка временных уходов на базу. Ищется по большинству ТБ, с ростом
    одиночных выплат и подтверждённым возвратом. Отдельный провал организации
    этим правилом не снимается. Последний месяц без будущего не корректируется."""
    base = p.x["m_base_pf"]
    temp = p.x["m_out_temp"]
    atoms = p.atoms
    tbs = p.meta.loc[atoms, "sys_tb_id"].to_numpy()
    keys = np.unique(tbs)
    result = np.zeros_like(base)
    if len(keys) < 3:
        return result
    b = np.array([np.nansum(base[atoms[tbs == k]], axis=0) for k in keys])
    v = np.array([np.nansum(temp[atoms[tbs == k]], axis=0) for k in keys])
    rate = v / np.maximum(b, 1)
    single = np.nansum(p.x["single_pay_recip"][atoms], axis=0)
    for t in range(12, len(p.months)):
        if not np.isfinite(temp[atoms, t]).any() or single[t] <= single[t - 1]:
            continue
        delta = rate[:, t] - rate[:, t - 12]
        error = 3 * np.sqrt(v[:, t] + v[:, t - 12] + 1) / np.maximum(b[:, t], 1)
        valid = (b[:, t] >= 5000) & (b[:, t - 12] >= 5000)
        if valid.sum() < 3 or np.mean(delta[valid] > error[valid]) < .6:
            continue
        common = float(np.median(delta[valid]))
        if common > 0:
            result[:, t] = np.minimum(np.nan_to_num(temp[:, t]), common * np.nan_to_num(base[:, t]))
    return result


def flow_obs(p: Panel, f: str) -> np.ndarray:
    """Факт меры по всем рядам и месяцам. «Пришли извне» — без вернувшихся из провала
    прошлого месяца (они уже объяснены «ушёл и вернулся»)."""
    col = FLOWS[f][0]
    x = p.x[col].copy()
    if f == "in_outside":
        back = np.zeros_like(x)
        back[:, 1:] = np.nan_to_num(p.x["m_out_temp"][:, :-1])
        x = x - back
    elif f == "out_left":
        x -= calendar_component(p)
    return x


def _groups(p: Panel, rows: np.ndarray, t: int | None = None) -> np.ndarray:
    """Группа сравнения: атом — сегмент; организация — сегмент × класс размера."""
    seg = p.meta.loc[rows, "seg_rep"].astype(str).to_numpy(dtype=str)   # из БД приходит object
    if p.meta.at[rows[0], "lvl"] == "org":
        end = len(p.months) if t is None else t
        size = np.nanmedian(p.x["pf_recip"][rows, max(0, end - HIST):end], axis=1)
        cls = np.digitize(np.nan_to_num(size), SIZE_EDGES).astype(str)
        seg = np.char.add(np.char.add(seg, "|"), cls)
    _, g = np.unique(seg, return_inverse=True)
    return g


def raw_expect(obs: np.ndarray, base: np.ndarray, g: np.ndarray, tb: np.ndarray) -> np.ndarray:
    """База × ставка группы в других ТБ (по каждому месяцу). Ставка — медиана ставок
    других ТБ: недогруз или событие одного ТБ не искажает ожидание остальным. Где в
    группе мало ТБ с заметной базой — ставка всех рядов уровня в других ТБ."""
    n, T = obs.shape
    _, ti = np.unique(tb, return_inverse=True)
    G, K = g.max() + 1, ti.max() + 1
    ok = ~np.isnan(obs) & ~np.isnan(base)
    o = np.where(ok, np.nan_to_num(obs), 0.0)
    b = np.where(ok, np.nan_to_num(base), 0.0)
    so = np.zeros((G, K, T)); sb = np.zeros((G, K, T))
    np.add.at(so, (g, ti), o)
    np.add.at(sb, (g, ti), b)
    with np.errstate(divide="ignore", invalid="ignore"):
        r_gk = np.where(sb >= 5_000, so / sb, np.nan)              # [G, K, T]
        ko, kb = so.sum(0), sb.sum(0)                               # [K, T]
        r_k = np.where(kb >= 5_000, ko / kb, np.nan)
    loo_g = np.full((G, K, T), np.nan)
    loo_all = np.full((K, T), np.nan)
    for k in range(K):
        others = [j for j in range(K) if j != k]
        cnt = np.sum(~np.isnan(r_gk[:, others, :]), axis=1)
        med = np.nanmedian(r_gk[:, others, :], axis=1)
        loo_g[:, k, :] = np.where(cnt >= 3, med, np.nan)
        loo_all[k] = np.nanmedian(r_k[others], axis=0)
    rate = loo_g[g, ti]
    rate = np.where(np.isnan(rate), loo_all[ti], rate)
    e = base * rate
    e[~ok] = np.nan
    return e


def offsets(obs: np.ndarray, e: np.ndarray, t: int) -> np.ndarray:
    """Устойчивый сдвиг ряда: медиана факт/ожидание за HIST мес. до t, подтянутая к 1."""
    w = slice(max(0, t - HIST), t)
    o, ee = obs[:, w], e[:, w]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(ee > 0, o / ee, np.nan)
    med = np.nanmedian(np.where(np.isnan(r).all(1, keepdims=True), 1.0, r), axis=1)
    se = np.nansum(ee, axis=1)
    return (se * med + PRIOR) / (se + PRIOR)


def kappa2(obs: np.ndarray, e: np.ndarray, th: np.ndarray, t: int, cls: np.ndarray) -> np.ndarray:
    """«Комковатость» по классу размера: устойчивая оценка (дисперсия − E) / E² за прошлые
    месяцы (медиана квадратов, приведённая к дисперсии)."""
    w = slice(max(0, t - HIST), t)
    E = e[:, w] * th[:, None]
    # медиана квадрата нормальной величины ≈ 0,455 дисперсии — отсюда деление
    u = ((obs[:, w] - E) ** 2 / 0.455 - E) / np.maximum(E, 1) ** 2
    u[~(E >= 5)] = np.nan
    out = np.zeros(len(obs))
    for c in np.unique(cls):
        v = u[cls == c]
        k = np.nanmedian(v) if np.isfinite(v).any() else 0.0
        out[cls == c] = min(max(k, 0.0), 1.0)
    return out


def score(obs: np.ndarray, e: np.ndarray, base: np.ndarray, cls: np.ndarray, t: int) -> dict:
    """Факт, ожидание, шум и z месяца t (по данным до t). Шум — не меньше собственного
    разброса ряда в прошлом (устойчивая оценка): ряд, где сидит «комковатая»
    организация, шумит сильнее среднего по классу."""
    th = offsets(obs, e, t)
    k2 = kappa2(obs, e, th, t, cls)
    w = slice(max(0, t - HIST), t)
    R = obs[:, w] - e[:, w] * th[:, None]                       # прошлые отклонения
    med = np.nanmedian(R, axis=1, keepdims=True)
    own = 1.4826 * np.nanmedian(np.abs(R - med), axis=1)
    E = e[:, t] * th
    s = np.sqrt(np.maximum(np.maximum(E, 0) + k2 * E ** 2 + 1, np.nan_to_num(own) ** 2))
    d = obs[:, t] - E
    return {"obs": obs[:, t], "E": E, "s": s, "z": d / s, "d": d, "base": base[:, t], "R": R}


def floor_of(base: np.ndarray) -> np.ndarray:
    return np.maximum(MIN_PEOPLE, MIN_SHARE * np.nan_to_num(base))


def z_star(n_tests: int, past_z: list[np.ndarray]) -> float:
    """Порог: поправка на число проверок и эмпирический нуль (≤ 0,5 в месяц в прошлом)."""
    zb = NormalDist().inv_cdf(1 - FALSE_PER_LEVEL / 2 / max(n_tests, 1))
    if not past_z:
        return zb
    pool = np.sort(np.abs(np.concatenate(past_z)))[::-1]
    k = int(FALSE_PER_LEVEL * len(past_z))
    ze = pool[k] + 1e-9 if len(pool) > k else 0.0
    return max(zb, ze)


# --------------------------------------------------------------------------- #
# Уровень рядов (организации или атомы)
# --------------------------------------------------------------------------- #
def scan_rows(p: Panel, rows: np.ndarray, t: int, obs_all: dict) -> dict:
    """Для каждой меры: оценка месяца t и порог z по эмпирическому нулю."""
    g = _groups(p, rows, t)
    tb = p.meta.loc[rows, "sys_tb_id"].to_numpy()
    base = p.x["m_base_pf"][rows]
    cls = np.digitize(np.nan_to_num(np.nanmedian(p.x["pf_recip"][rows, max(0, t - HIST):t], axis=1)), SIZE_EDGES)
    out = {}
    for f in FLOWS:
        obs = obs_all[f][rows]
        e = raw_expect(obs, base, g, tb)
        cur = score(obs, e, base, cls, t)
        past = []
        for h in range(max(HIST + 1, t - CALIB), t):
            sh = score(obs, e, base, cls, h)
            big = np.abs(sh["d"]) >= floor_of(sh["base"])
            past.append(np.nan_to_num(sh["z"][big]))
        cur["zs"] = z_star(len(rows) * len(FLOWS), past)
        out[f] = cur
    return out


def pick(scan: dict, rows: np.ndarray) -> list[tuple]:
    """(ряд, мера, L, d) для прошедших отбор; одна лучшая мера на ряд."""
    best = {}
    for f, sc in scan.items():
        L = np.abs(sc["d"]) - sc["zs"] * sc["s"]
        ok = (L >= floor_of(sc["base"])) & np.isfinite(L)
        for i in np.flatnonzero(ok):
            r = int(rows[i])
            if r not in best or L[i] > best[r][2]:
                best[r] = (r, f, float(L[i]), float(sc["d"][i]), i)
    return list(best.values())


def _card(p: Panel, level: str, key, f: str, sc: dict, i: int, L: float, label=None, **info) -> Card:
    sign = FLOWS[f][1]
    return Card(level, key, f, sign * float(sc["d"][i]), label=label, score=L,
                obs=float(sc["obs"][i]), exp=float(sc["E"][i]), info=info)


def _qualified(scan: dict, i: int) -> dict:
    """Все прошедшие отбор потоки строки, даже если заголовок карточки один."""
    result = {}
    for f, s in scan.items():
        L = abs(s["d"][i]) - s["zs"] * s["s"][i]
        if np.isfinite(L) and L >= floor_of(s["base"][i]):
            result[f] = {"obs": float(s["obs"][i]), "exp": float(s["E"][i]),
                         "delta": FLOWS[f][1] * float(s["d"][i]), "score": float(L)}
    return result


# --------------------------------------------------------------------------- #
# Сторож
# --------------------------------------------------------------------------- #
def _contained(p: Panel, t: int, child: Card, level: str, key, f: str) -> bool:
    """Весь ряд дочерней карточки входит в территорию родителя, без догадки
    по главной ячейке. Для прихода без возвратов нужна также привязка прошлого месяца."""
    if isinstance(key, (int, np.integer)) and key < 0:
        return False
    if child.level == "org":
        if level == "bank":
            return True
        scopes = getattr(p, "org_scopes", {}).get(level)
        if scopes is None or scopes[child.key, t] != key:
            return False
        return (f != "in_outside" or t == 0
                or not np.nan_to_num(p.x["m_out_temp"][child.key, t - 1])
                or scopes[child.key, t - 1] == key)
    if child.level == "atom" and level in ("gosb", "tb"):
        return p.meta.at[child.key, "sys_" + level + "_id"] == key
    if child.level == "gosb" and level == "tb":
        rows = p.meta.loc[p.atoms]
        tbs = rows.loc[rows["sys_gosb_id"] == child.key, "sys_tb_id"]
        return len(tbs) > 0 and bool((tbs == key).all())
    return False


def _recode_part(p: Panel, t: int, c: Card, level: str, key, f: str, d: float) -> float:
    """Часть смены ГОСБ по фактическим потокам организации в данной территории.
    Ожидание организации распределяется по медиане долей её потока за прошлые
    месяцы. Без достаточной истории вычитается всё ожидание организации.
    Наблюдения не меняются; чужой всплеск не используется как адрес перехода."""
    cells = getattr(p, "org_flows", None)
    if c.label != "recode" or f not in ("in_other", "out_other") or d <= 0 or cells is None or cells.empty:
        return 0.0
    try:
        local = cells.xs((c.key, t), level=("row", "t"))
    except KeyError:
        return 0.0
    def inside(atoms):
        valid = np.asarray(atoms) >= 0
        if level == "bank":
            return valid
        if level == "atom":
            return valid & (np.asarray(atoms) == key)
        col = {"gosb": "sys_gosb_id", "tb": "sys_tb_id", "seg": "seg_rep"}.get(level)
        mask = np.zeros(len(atoms), dtype=bool)
        if col:
            mask[valid] = (p.meta.loc[np.asarray(atoms)[valid], col] == key).to_numpy()
        return mask

    identified = c.info.get("cell_explains", {}).get(f)
    if identified is not None:
        keys = list(identified)
        mask = inside(keys)
        return sum(identified[a] for a, valid in zip(keys, mask) if valid)
    local = local.loc[inside(local.index)]
    total = p.x[FLOWS[f][0]][c.key, t]
    expected = max(0.0, total - d)
    history = cells.xs(c.key, level="row")
    times = history.index.get_level_values("t")
    history = history.loc[(times >= max(0, t - HIST)) & (times < t)]
    totals = history[f].groupby(level="t").sum(min_count=1)
    totals = totals.loc[totals > 0]
    known = (history.index.get_level_values("atom") >= 0).all()
    if len(totals) >= 4 and known:
        part = history.loc[inside(history.index.get_level_values("atom")), f].groupby(level="t").sum()
        share = float(np.median(part.reindex(totals.index, fill_value=0) / totals))
        expected *= share
    # Местный всплеск может быть больше общего: в другой ячейке поток одновременно
    # оказался ниже ожидания. Ограничение общим d создало бы ложный остаток здесь.
    return max(0.0, float(local[f].sum()) - expected) if np.isfinite(total) else 0.0


def _cell_recodes(p: Panel, t: int) -> list[Card]:
    """Внутренний переход виден по ячейкам даже у организации с шумным общим итогом.
    Паруются противоположные изменения по одной организации, сегменту и ТБ в
    разных ГОСБ. Оборот входа/выхода в одной ячейке сам по себе не является сменой ГОСБ."""
    if p.org_flows.empty or t < 4:
        return []
    try:
        current = p.org_flows.xs(t, level="t").copy()
    except KeyError:
        return []
    times = p.org_flows.index.get_level_values("t")
    history = p.org_flows.loc[(times >= max(0, t - HIST)) & (times < t)].reset_index(level="t", drop=True)
    med = history.groupby(level=["row", "atom"]).median()
    mad = (history - med.reindex(history.index)).abs().groupby(level=["row", "atom"]).median() * 1.4826
    center = med.reindex(current.index).fillna(0.)
    noise = np.maximum(mad.reindex(current.index).fillna(0.), np.sqrt(center + 1))
    deviation = current - center
    # Вычитается встречный оборот той же ячейки: устойчивое совместное увеличение
    # найма и ухода внутри одного ГОСБ не даёт ложной пары с соседней ячейкой.
    net = deviation["out_other"] - deviation["in_other"]
    sd = np.sqrt(noise["out_other"] ** 2 + noise["in_other"] ** 2)
    active = abs(net) >= MIN_PEOPLE + 4 * sd
    candidates = current.loc[active].copy()
    if candidates.empty:
        return []
    candidates["net"] = net.loc[active]
    candidates["uncertainty"] = (4 * sd).loc[active]
    candidates = candidates.reset_index()
    candidates = candidates.loc[candidates["atom"] >= 0]
    for col in ("seg_rep", "sys_tb_id", "sys_gosb_id"):
        candidates[col] = p.meta.loc[candidates["atom"], col].to_numpy()
    evidence = {}
    for (row, _, _), group in candidates.groupby(["row", "seg_rep", "sys_tb_id"]):
        outgoing = group.loc[group["net"] > 0].sort_values("net", ascending=False)
        incoming = group.loc[group["net"] < 0].sort_values("net")
        a, b = outgoing["net"].sum(), -incoming["net"].sum()
        if min(a, b) <= 0 or min(a, b) < .6 * max(a, b):
            continue
        remaining = {int(x.atom): -float(x.net) for x in incoming.itertuples()}
        parts = evidence.setdefault(int(row), {"out_other": {}, "in_other": {}})
        bounds = {"out_other": {}, "in_other": {}}
        for source in outgoing.itertuples():
            left = float(source.net)
            for target in incoming.itertuples():
                if source.sys_gosb_id == target.sys_gosb_id:
                    continue
                n = min(left, remaining[int(target.atom)])
                if n <= 0:
                    continue
                for f, atom in (("out_other", int(source.atom)), ("in_other", int(target.atom))):
                    parts[f][atom] = parts[f].get(atom, 0.) + n
                # Остаток пары оценивается с погрешностью противоположной ячейки.
                # Жёсткое равенство точечных оценок превращало обычный шум источника
                # в «новый приход» в принимающем ГОСБ.
                bounds["out_other"][int(source.atom)] = bounds["out_other"].get(int(source.atom), 0.) + float(target.uncertainty)
                bounds["in_other"][int(target.atom)] = bounds["in_other"].get(int(target.atom), 0.) + float(source.uncertainty)
                left -= n
                remaining[int(target.atom)] -= n
        for f in parts:
            for atom, uncertainty in bounds[f].items():
                observed = max(0., float(deviation.at[(row, atom), f]))
                parts[f][atom] = min(observed, parts[f][atom] + uncertainty)
    result = []
    for row, parts in evidence.items():
        total = sum(parts["out_other"].values())
        if total >= MIN_PEOPLE:
            result.append(Card("org", row, "out_other", -total, label="recode",
                               info={"explains": {f: sum(v.values()) for f, v in parts.items()},
                                     "cell_explains": parts}))
    return result


def _covered(p: Panel, t: int, level: str, key, f: str, d: float,
             cards: list[Card], score: float = 0.0, minimum: float = MIN_PEOPLE) -> bool:
    """Уже показанные события объясняют ≥70% отклонения того же направления.
    Суммы организаций, атомов и ГОСБ проверяются отдельно: смешивать их нельзя,
    иначе одна и та же группа людей будет учтена несколько раз. Если остаток всё
    ещё проходит исходный порог значимости, родительская карточка сохраняется."""
    if not np.isfinite(d) or d == 0:
        return False
    units = {}
    for c in cards:
        explained = c.info.get("explains", {})
        value = explained.get(f, c.delta / FLOWS[f][1] if c.measure == f else 0.0)
        if not np.isfinite(value) or np.sign(value) != np.sign(d):
            continue
        if not _contained(p, t, c, level, key, f):
            value = _recode_part(p, t, c, level, key, f, value)
        level_units = units.setdefault(c.level, {})
        level_units[c.key] = max(level_units.get(c.key, 0.), abs(value))
    amount = max((sum(v.values()) for v in units.values()), default=0.0)
    return amount >= 0.7 * abs(d) and score - amount < minimum


def detect(p: Panel, t: int) -> list[Card]:
    cards: list[Card] = []
    obs = {f: flow_obs(p, f) for f in FLOWS}
    last = t == len(p.months) - 1

    # 1. Организации
    orgs = p.orgs
    if len(orgs):
        so = scan_rows(p, orgs, t, obs)
        hits = pick(so, orgs)
        by_row = {}
        for r, f, L, d, i in hits:
            by_row[r] = (f, L, d, i)
        evidence = _cell_recodes(p, t)
        recode_rows = {c.key: c for c in evidence}
        for r, (f, L, d, i) in by_row.items():
            label = None
            recode = recode_rows.get(r)
            amount = recode.info["explains"].get(f, 0.) if recode else 0.
            if (f in ("out_other", "in_other") and d > 0 and amount >= .7 * d
                    and L - amount < floor_of(so[f]["base"][i])):
                label = "recode"
            elif f == "out_left":
                lb = np.nan_to_num(p.x["m_lb_org_stopped"][r, t])
                if d > 0 and lb >= 0.5 * d:
                    label = "stop"
            info = {"flows": _qualified(so, i)}
            if label == "recode":
                info.update(recode.info)
            cards.append(_card(p, "org", r, f, so[f], i, L, label, **info))
    else:
        evidence = []

    # 2. Атомы
    atoms = p.atoms
    sa = scan_rows(p, atoms, t, obs)
    hits = pick(sa, atoms)
    under = _underload(p, atoms, sa, t) if last else set()
    for r, f, L, d, i in hits:
        explanations = cards + [c for c in evidence if not any(
            old.level == "org" and old.key == c.key and old.label == "recode" for old in cards)]
        if _covered(p, t, "atom", r, f, d, explanations, L, floor_of(sa[f]["base"][i])):
            continue
        tb = int(p.meta.at[r, "sys_tb_id"])
        label = "underload" if (tb in under and f == "out_left") else None
        cards.append(_card(p, "atom", r, f, sa[f], i, L, label, flows=_qualified(sa, i)))
    for tb in under:
        cards.append(Card("tb", tb, "out_left", 0.0, label="underload"))
    for c in cards:
        if (c.level == "org" and c.measure == "out_left"
                and any(_contained(p, t, c, "tb", tb, "out_left") for tb in under)):
            c.label = c.label or "underload"

    # 3. ГОСБ и ТБ — исходные суммы атомов; повторные события не показываем.
    for lvl, col in (("gosb", "sys_gosb_id"), ("tb", "sys_tb_id")):
        keys = p.meta.loc[atoms, col].to_numpy()
        for f, sc in sa.items():
            for k in np.unique(keys):
                m = keys == k
                idx = np.flatnonzero(m)
                d = np.nansum(sc["d"][idx])
                Rg = np.nansum(sc["R"][idx], axis=0)
                own = 1.4826 * np.median(np.abs(Rg - np.median(Rg)))
                s = max(np.sqrt(np.nansum(sc["s"][idx] ** 2)), own)
                base = np.nansum(sc["base"][idx])
                zs = NormalDist().inv_cdf(1 - FALSE_PER_LEVEL / 2 / (len(np.unique(keys)) * len(FLOWS)))
                L = abs(d) - max(zs, 4.0) * s
                if L >= max(MIN_PEOPLE, MIN_SHARE * base):
                    explanations = cards + [c for c in evidence if not any(
                        old.level == "org" and old.key == c.key and old.label == "recode" for old in cards)]
                    if _covered(p, t, lvl, int(k), f, d, explanations, L, max(MIN_PEOPLE, MIN_SHARE * base)):
                        continue
                    label = "underload" if (lvl == "tb" and k in under and f == "out_left") else None
                    if label is None and lvl == "gosb" and f == "out_left":
                        tb = int(p.meta.loc[atoms[idx[0]], "sys_tb_id"])
                        label = "underload" if tb in under else None
                    cards.append(Card(lvl, int(k), f, FLOWS[f][1] * d, label=label, score=L,
                                      obs=float(np.nansum(sc["obs"][idx])),
                                      exp=float(np.nansum(sc["E"][idx]))))

    # 4. Банк и сегменты — по тому же месяцу год назад
    tcards = _temporal(p, atoms, obs, t)
    recodes = evidence
    for c in tcards:
        if not c.label and _covered(p, t, c.level, c.key, c.measure, c.obs - c.exp,
                                    recodes, c.score):
            c.label = "recode"
    if under:
        tbs = p.meta.loc[atoms, "sys_tb_id"].to_numpy()
        segs = p.meta.loc[atoms, "seg_rep"].astype(str).to_numpy()
        ud = sa["out_left"]["d"]
        for c in tcards:
            if c.measure != "out_left" or c.label:
                continue
            m = np.isin(tbs, list(under)) & ((segs == c.key) if c.level == "seg" else True)
            if np.nansum(ud[m]) >= 0.5 * abs(c.obs - c.exp):
                c.label = "underload"
    cards += tcards

    cards.sort(key=lambda c: -c.score)
    return cards[:MAX_CARDS]


def _underload(p: Panel, atoms: np.ndarray, sa: dict, t: int) -> set:
    """ТБ, где в последнем месяце уход из банка выше ожидания почти во всех атомах сразу,
    и вместе с этим просели строки ведомостей — недогруз, а не уход."""
    out = set()
    tbs = p.meta.loc[atoms, "sys_tb_id"].to_numpy()
    z = sa["out_left"]["z"]
    for tb in np.unique(tbs):
        m = tbs == tb
        big = np.nan_to_num(sa["out_left"]["base"][m]) >= 1000
        if big.sum() < 3:
            continue
        share = np.mean(np.nan_to_num(z[m][big]) >= 2)
        rr = p.x["raw_rows"][atoms[m]]
        drop = np.nansum(rr[:, t]) < 0.97 * np.nansum(rr[:, t - 1]) if t > 0 else False
        if share >= 0.6 and drop:
            out.add(int(tb))
    return out


def _temporal(p: Panel, atoms: np.ndarray, obs: dict, t: int) -> list[Card]:
    """Банк и сегменты: ожидание = база × ставка того же месяца год назад × поправка на
    текущий уровень (ставки трёх прошлых месяцев к тем же месяцам год назад). Шум — по
    ошибкам такого прогноза в прошлых месяцах. Подпись: календарь, если отклонение ухода
    почти целиком из «вернулся в следующем месяце» и выросло число людей с одной выплатой."""
    if t < 16:
        return []
    cards = []
    segs = p.meta.loc[atoms, "seg_rep"].astype(str).to_numpy()
    slices = [("bank", "bank", np.ones(len(atoms), bool))]
    slices += [("seg", s, segs == s) for s in np.unique(segs)]
    zs = NormalDist().inv_cdf(1 - FALSE_PER_LEVEL / 2 / (len(slices) * len(FLOWS)))
    for lvl, key, m in slices:
        rows = atoms[m]
        base = np.nansum(p.x["m_base_pf"][rows], axis=0)
        for f in FLOWS:
            o = np.nansum(obs[f][rows], axis=0)
            rate = o / np.maximum(base, 1)

            def pred(h):
                if h < 16:
                    return np.nan
                lvl_now = np.nanmean(rate[h - 3:h])
                lvl_ago = np.nanmean(rate[h - 15:h - 12])
                return rate[h - 12] * (lvl_now / lvl_ago if lvl_ago > 0 else 1.0)

            errs = [rate[h] - pred(h) for h in range(16, t)]
            errs = [e for e in errs if np.isfinite(e)]
            if len(errs) < 4:
                continue
            sd = 1.4826 * np.median(np.abs(np.array(errs) - np.median(errs))) * base[t]
            E = pred(t) * base[t]
            d = o[t] - E
            s = max(sd, np.sqrt(max(E, 1)))
            L = abs(d) - max(zs, 4.0) * s
            if not np.isfinite(L) or L < max(MIN_PEOPLE, MIN_SHARE * base[t]):
                continue
            label = None
            if f == "out_left" and d > 0:
                temp = np.nansum(p.x["m_out_temp"][rows], axis=0)
                single = np.nansum(p.x["single_pay_recip"][rows], axis=0)
                temp_d = temp[t] - temp[t - 12] * (base[t] / max(base[t - 12], 1))
                if np.isfinite(temp_d) and temp_d >= 0.5 * d and single[t] > single[t - 1]:
                    label = "calendar"
            cards.append(Card(lvl, key, f, FLOWS[f][1] * d, label=label, score=L,
                              obs=float(o[t]), exp=float(E)))
    return cards


# --------------------------------------------------------------------------- #
# Карточки таблицей
# --------------------------------------------------------------------------- #
MEASURE_RU = {"in_outside": "пришли извне", "in_other": "пришли из других организаций",
              "in_codes": "пришли с не зп кодов", "out_left": "ушли из банка",
              "out_other": "ушли в другие организации", "out_codes": "ушли на не зп коды"}
LABEL_RU = {"stop": "организация перестала платить", "recode": "смена ГОСБ внутри организации",
            "underload": "недогруз данных ТБ", "calendar": "календарный сдвиг выплат"}


def where(p: Panel, c: Card) -> str:
    if c.level in ("org", "atom"):
        m = p.meta.iloc[c.key]
        place = f"{m['seg_rep']}, ТБ {m['sys_tb_id']}, ГОСБ {m['sys_gosb_id']}"
        return f"организация {m['org_id']} (главная ячейка: {place})" if c.level == "org" else place
    return {"gosb": f"ГОСБ {c.key}", "tb": f"ТБ {c.key}", "seg": f"сегмент {c.key}",
            "bank": "банк"}[c.level]


def cards_frame(p: Panel, cards: list[Card], t: int) -> pd.DataFrame:
    """Факт — исходная мера поколения. Для прихода извне ожидание переведено
    в тот же масштаб: к нему возвращена поправка на возвраты после провала.
    delta — отклонение вклада потока от ожидания, а не изменение всего портфеля."""
    records = []
    for c in cards:
        actual, expected = c.obs, c.exp
        if c.measure in ("in_outside", "out_left") and np.isfinite(c.obs):
            if c.level in ("org", "atom"):
                rows = [int(c.key)]
            else:
                rows = p.atoms
                col = {"gosb": "sys_gosb_id", "tb": "sys_tb_id", "seg": "seg_rep"}.get(c.level)
                if col:
                    rows = rows[(p.meta.loc[rows, col] == c.key).to_numpy()]
            raw = p.x[FLOWS[c.measure][0]][rows, t]
            actual = float(np.nansum(raw)) if np.isfinite(raw).any() else np.nan
            expected = c.exp + actual - c.obs
        what = MEASURE_RU.get(c.measure, c.measure)
        if np.isfinite(c.obs) and np.isfinite(c.exp):
            what += ": больше ожидания" if c.obs > c.exp else ": меньше ожидания" if c.obs < c.exp else ""
        records.append({
            "месяц": p.months[t], "уровень": c.level, "где": where(p, c),
            "что": what,
            "факт": round(actual) if np.isfinite(actual) else None,
            "ожидание": round(expected) if np.isfinite(expected) else None,
            "отклонение вклада в портфель": round(c.delta),
            "сверх шума, не меньше": round(c.score),
            "объяснение": LABEL_RU.get(c.label, "") if c.label else "",
        })
    return pd.DataFrame(records, columns=["месяц", "уровень", "где", "что", "факт", "ожидание",
                                          "отклонение вклада в портфель", "сверх шума, не меньше", "объяснение"])
