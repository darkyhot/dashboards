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
L = |факт − ожидание| − z·s ≥ max(100, 0,1% базы); порог z на уровне такой, что в
прошлых месяцах (эмпирический нуль) срабатываний ≤ 0,5 в месяц, и не ниже поправки на
число проверок.

**Порядок**: качество данных (недогруз ТБ в последнем месяце) → организации (их
отклонения вычитаются из атомов) → атомы → ГОСБ и ТБ → банк и сегменты.
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
def flow_obs(p: Panel, f: str) -> np.ndarray:
    """Факт меры по всем рядам и месяцам. «Пришли извне» — без вернувшихся из провала
    прошлого месяца (они уже объяснены «ушёл и вернулся»)."""
    col = FLOWS[f][0]
    x = p.x[col].copy()
    if f == "in_outside":
        back = np.zeros_like(x)
        back[:, 1:] = np.nan_to_num(p.x["m_out_temp"][:, :-1])
        x = x - back
    return x


def _groups(p: Panel, rows: np.ndarray) -> np.ndarray:
    """Группа сравнения: атом — сегмент; организация — сегмент × класс размера."""
    seg = p.meta.loc[rows, "seg_rep"].astype(str).to_numpy(dtype=str)   # из БД приходит object
    if p.meta.at[rows[0], "lvl"] == "org":
        size = np.nanmedian(p.x["pf_recip"][rows], axis=1)
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
    g = _groups(p, rows)
    tb = p.meta.loc[rows, "sys_tb_id"].to_numpy()
    base = p.x["m_base_pf"][rows]
    cls = np.digitize(np.nan_to_num(np.nanmedian(p.x["pf_recip"][rows], axis=1)), SIZE_EDGES)
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


# --------------------------------------------------------------------------- #
# Сторож
# --------------------------------------------------------------------------- #
def detect(p: Panel, t: int) -> list[Card]:
    cards: list[Card] = []
    obs = {f: flow_obs(p, f) for f in FLOWS}
    recoded: set = set()                     # (атом, мера), объяснённые сменой ГОСБ
    last = t == len(p.months) - 1

    # 1. Организации
    orgs = p.orgs
    if len(orgs):
        so = scan_rows(p, orgs, t, obs)
        hits = pick(so, orgs)
        by_row = {}
        for r, f, L, d, i in hits:
            by_row[r] = (f, L, d, i)
        for r, (f, L, d, i) in by_row.items():
            label = None
            # смена ГОСБ внутри организации: ушли и пришли одновременно поровну
            oi, ii = so["out_other"], so["in_other"]
            a, b = oi["d"][i], ii["d"][i]
            if f in ("out_other", "in_other") and a > 0 and b > 0 and min(a, b) >= 0.6 * max(a, b):
                label = "recode"
            elif f == "out_left":
                lb = np.nan_to_num(p.x["m_lb_org_stopped"][r, t])
                if lb >= 0.5 * d:
                    label = "stop"
            cards.append(_card(p, "org", r, f, so[f], i, L, label))
            # вычесть отклонение организации из её атома (и для смены ГОСБ — из атома,
            # куда пришли)
            a_row = int(p.meta.at[r, "atom"])
            if label == "recode":
                dst = _recode_dst(p, obs, r, b, t)
                obs["out_other"][a_row, t] -= a
                obs["in_other"][dst, t] -= b
                recoded |= {(a_row, "out_other"), (dst, "in_other")}
            elif a_row >= 0:
                obs[f][a_row, t] -= d

    # 2. Атомы
    atoms = p.atoms
    sa = scan_rows(p, atoms, t, obs)
    hits = pick(sa, atoms)
    under = _underload(p, atoms, sa, t) if last else set()
    for r, f, L, d, i in hits:
        tb = int(p.meta.at[r, "sys_tb_id"])
        label = "underload" if (tb in under and f == "out_left") else None
        if (r, f) in recoded:
            label = "recode"
        cards.append(_card(p, "atom", r, f, sa[f], i, L, label))
    for tb in under:
        cards.append(Card("tb", tb, "out_left", 0.0, label="underload"))
    for c in cards:
        if c.level == "org" and int(p.meta.at[c.key, "sys_tb_id"]) in under and c.measure == "out_left":
            c.label = c.label or "underload"

    # 3. ГОСБ и ТБ — сумма атомов, ещё не объяснённая карточками атомов
    flagged = {(c.key, c.measure) for c in cards if c.level == "atom"}
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
                expl = sum(sc["d"][j] for j in idx if (int(atoms[j]), f) in flagged)
                if abs(expl) >= 0.7 * abs(d):
                    continue
                zs = NormalDist().inv_cdf(1 - FALSE_PER_LEVEL / 2 / (len(np.unique(keys)) * len(FLOWS)))
                L = abs(d) - max(zs, 4.0) * s
                if L >= max(MIN_PEOPLE, MIN_SHARE * base):
                    label = "underload" if (lvl == "tb" and k in under and f == "out_left") else None
                    if label is None and lvl == "gosb" and f == "out_left":
                        tb = int(p.meta.loc[atoms[idx[0]], "sys_tb_id"])
                        label = "underload" if tb in under else None
                    cards.append(Card(lvl, int(k), f, FLOWS[f][1] * d, label=label, score=L,
                                      obs=float(np.nansum(sc["obs"][idx])),
                                      exp=float(np.nansum(sc["E"][idx]))))

    # 4. Банк и сегменты — по тому же месяцу год назад
    tcards = _temporal(p, atoms, obs, t)
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


def _recode_dst(p: Panel, obs: dict, r: int, n: float, t: int) -> int:
    """Атом того же ТБ, куда пришли люди организации, сменившей ГОСБ: наибольший приход
    из других организаций, близкий к n. Нет такого — свой атом."""
    a_row = int(p.meta.at[r, "atom"])
    tb = p.meta.at[r, "sys_tb_id"]
    atoms = p.atoms
    same = atoms[(p.meta.loc[atoms, "sys_tb_id"].to_numpy() == tb) & (atoms != a_row)]
    if len(same) == 0:
        return a_row
    hist = obs["in_other"][same, max(0, t - HIST):t]
    v = np.nan_to_num(obs["in_other"][same, t] - np.nanmedian(hist, axis=1))
    j = int(np.argmax(v))
    return int(same[j]) if v[j] >= 0.6 * n else a_row


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
    return pd.DataFrame([{
        "месяц": p.months[t], "уровень": c.level, "где": where(p, c),
        "что": MEASURE_RU.get(c.measure, c.measure),
        "факт": round(c.obs) if np.isfinite(c.obs) else None,
        "ожидание": round(c.exp) if np.isfinite(c.exp) else None,
        "влияние на портфель": round(c.delta),
        "лишних не меньше": round(c.score),
        "объяснение": LABEL_RU.get(c.label, "") if c.label else "",
    } for c in cards])
