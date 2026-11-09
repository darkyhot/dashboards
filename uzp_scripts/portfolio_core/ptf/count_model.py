"""Положительные прогнозные интервалы потоков; история событий не задаёт порог.

Модель log(1 + счётчик): собственный и групповой MAD, пуассоновская нижняя
граница разброса. Группа та же, что у ожидания: сегмент, для организаций ещё
размер. Общий масштаб оценивается по центру прошлых стандартизованных ошибок,
включая обычные малые отклонения. Крупный исторический переход не становится
обязательным размером будущего события. Проверяемое предположение о хвостах
модели контролируется стендом; номинальный бюджет не заменяет его приёмку.
"""
from __future__ import annotations

import numpy as np

NORMAL_MEDIAN = .6744897501960817
MAD_SCALE = 1. / NORMAL_MEDIAN

# Верхняя граница ряда:
#   "log"    — E·exp(z·σ) в логарифмах (принятая версия протокола 3);
#   "hybrid" — не дальше аддитивной E + z·s при том же z (эксперимент 09.11.2026).
# Аддитивное событие (+N человек) при лог-границе должно быть (e^{zσ}−1)/σ шумов модели:
# 4,1 при σ=0,1; 7,0 при σ=0,36. Нижняя граница не меняется (лог-нижняя ближе к E).
UPPER_BOUND = "log"


def positive_exp(a):
    with np.errstate(over="ignore", invalid="ignore"):
        return np.maximum(np.expm1(a), 0.)


def normal_tail_bound(u):
    """Верхняя граница P(N(0,1)>u), u≥0: минимум двух границ Миллса."""
    weight = np.exp(-.5 * u * u)
    return np.minimum(.5 * weight, weight / (np.sqrt(2 * np.pi) * np.maximum(u, 1e-300)))


def materiality_threshold(scans, normal, budget):
    """Общий z, ограничивающий сумму вероятностей после обеих границ и пола.

    Отбор O>exp(C+zS)-1+F либо O<exp(C-zS)-1-F. Поэтому число проверок
    учитывается одновременно с F, без двойного запаса. z не подбирается по
    текущим ошибкам: используются прогнозы, масштабы, базы и доступность.
    """
    from . import watch

    centers, sigmas, floors = [], [], []
    for s in scans.values():
        valid = np.isfinite(s["d"])
        centers.append(s["center"][valid])
        sigmas.append(s["log_s"][valid] * s["zs"] / normal)
        floors.append(watch.floor_of(s["base"])[valid])
    c, sd, floor = np.concatenate(centers), np.concatenate(sigmas), np.concatenate(floors)
    log_floor = np.log(floor)

    def expected(z):
        upper = (np.logaddexp(c + z * sd, log_floor) - c) / sd
        a = c - z * sd
        has_lower = a > np.log1p(floor)
        lower = np.zeros(len(a))
        lower[has_lower] = normal_tail_bound((c[has_lower] - a[has_lower]
            - np.log1p(-np.exp(log_floor[has_lower] - a[has_lower]))) / sd[has_lower])
        return float(normal_tail_bound(upper).sum() + lower.sum())

    low, high = 0., normal
    while expected(high) > budget:
        high *= 1.5
    for _ in range(24):
        mid = (low + high) / 2
        if expected(mid) > budget:
            low = mid
        else:
            high = mid
    return high, expected(high)


def score(obs, expected, base, groups, t):
    from . import watch

    raw = expected * watch.offsets(obs, expected, t)[:, None]
    w = slice(max(0, t - watch.HIST), t)
    valid = (np.isfinite(obs[:, w]) & (obs[:, w] >= 0)
             & np.isfinite(raw[:, w]) & (raw[:, w] >= 0) & (base[:, w] > 0))
    errors = np.where(valid, np.log1p(np.maximum(obs[:, w], 0))
                      - np.log1p(np.maximum(raw[:, w], 0)), np.nan)
    location = watch._nanmedian(errors, axis=1, keepdims=True)
    centered = errors - location
    own = MAD_SCALE * watch._nanmedian(np.abs(centered), axis=1)
    pooled = np.zeros(len(obs))
    for group in np.unique(groups):
        mask = groups == group
        values = np.abs(centered[mask])[raw[mask, w] >= watch.MIN_NULL_MEAN]
        values = values[np.isfinite(values)]
        pooled[mask] = np.median(values) * MAD_SCALE if len(values) else 0.
    shape = np.maximum(pooled, np.nan_to_num(own))
    center = np.log1p(np.maximum(raw[:, t], 0)) + location[:, 0]
    median = positive_exp(center)
    sigma = np.maximum(shape, np.sqrt(np.log1p(1. / (median + 1))))
    mean = positive_exp(center + .5 * sigma ** 2)
    with np.errstate(over="ignore", invalid="ignore"):
        sd = (mean + 1) * np.sqrt(np.expm1(sigma ** 2))
    delta = obs[:, t] - mean
    n_history = valid.sum(axis=1)
    supported = ((n_history >= watch.MIN_HISTORY) & (base[:, t] > 0)
                 & np.isfinite(obs[:, t]) & (obs[:, t] >= 0)
                 & np.isfinite(mean) & np.isfinite(sd) & (raw[:, t] >= 0))
    hist_center = np.log1p(np.maximum(raw[:, w], 0)) + location
    hist_median = positive_exp(hist_center)
    hist_sigma = np.maximum(shape[:, None], np.sqrt(np.log1p(1. / (hist_median + 1))))
    residuals = np.where(valid, obs[:, w] - positive_exp(hist_center + .5 * hist_sigma ** 2), np.nan)
    z = (np.log1p(np.maximum(obs[:, t], 0)) - center) / sigma
    return {"obs": obs[:, t], "E": np.where(supported, mean, np.nan),
            "s": np.where(supported, sd, np.nan), "d": np.where(supported, delta, np.nan),
            "z": np.where(supported, z, np.nan), "base": base[:, t], "R": residuals,
            "center": center, "log_s": sigma, "n_history": n_history,
            # Для диагностики: из чего взят разброс (σ = max(групповой, собственный)).
            "own_log_s": own, "pooled_log_s": pooled,
            "eligible": supported & (median >= watch.MIN_NULL_MEAN),
            "unsupported_material": ~supported & (np.abs(obs[:, t] - raw[:, t]) >= watch.floor_of(base[:, t]))}


def scan_rows(p, rows, t, obs_all, details=False):
    from . import watch
    from .inputs import has_cell_detail
    from .presentation import org_code

    groups = watch._groups(p, rows, t)
    tb = p.meta.loc[rows, "sys_tb_id"].to_numpy()
    base = p.x["m_base_pf"][rows]
    is_atom = p.meta.at[rows[0], "lvl"] == "atom"
    # Полный атом и остаток делят один бюджет уровня, а не получают два бюджета.
    channels = 2 if is_atom and (has_cell_detail(p) or getattr(p, "scope", "all") == "other_orgs") else 1
    normal = watch.z_star(len(rows) * len(watch.FLOWS) * channels, [])
    out = {}
    for flow in watch.FLOWS:
        obs = obs_all[flow][rows]
        expected = watch.raw_expect(obs, base, groups, tb)
        current = score(obs, expected, base, groups, t)
        if getattr(p, "material_base", None) is not None:
            current["base"] = p.material_base[rows, t]
        central, excluded, witnesses = [], [], []
        excluded_count = 0
        times = range(max(watch.HIST + 1, t - watch.CALIB), t)
        for h in times:
            past = score(obs, expected, base, groups, h)
            central.extend(past["z"][past["eligible"]].tolist())
            omit = np.isfinite(past["z"]) & ~past["eligible"]
            big = np.abs(past["d"]) >= watch.floor_of(past["base"])
            excluded_count += int((omit & big).sum() + past["unsupported_material"].sum())
            if details:
                ranked = np.argsort(np.where(np.isfinite(past["z"]), -abs(past["z"]), np.inf))[:3]
                for i in ranked:
                    if not np.isfinite(past["z"][i]):
                        continue
                    row = int(rows[i])
                    witnesses.append({"месяц": p.months[h],
                        "ряд": org_code(row) if not is_atom else f"Атом R{row:05d}",
                        "факт": float(past["obs"][i]), "ожидание": float(past["E"][i]),
                        "шум": float(past["s"][i]), "модуль z": float(abs(past["z"][i])),
                        "задаёт порог": False})
                for i in np.flatnonzero(omit & big):
                    row = int(rows[i])
                    excluded.append({"месяц": p.months[h],
                        "ряд": org_code(row) if not is_atom else f"Атом R{row:05d}",
                        "факт": float(past["obs"][i]), "ожидание": float(past["E"][i]),
                        "модуль z": float(abs(past["z"][i])), "причина": "малое ожидание"})
        values = np.asarray(central)
        values = values[np.isfinite(values)]
        scale = max(1., float(np.median(np.abs(values))) / NORMAL_MEDIAN) if len(values) else 1.
        current["zs"] = normal * scale
        if details:
            current["calibration"] = {
                "normal_z": normal, "empirical_z": normal * scale, "scale": scale,
                "model": "positive_count", "history_months": len(times), "null_points": len(values),
                "contributors": sorted(witnesses, key=lambda r: -r["модуль z"])[:3],
                "excluded_count": excluded_count,
                "excluded": sorted(excluded, key=lambda r: -r["модуль z"])[:3],
                "unsupported_current": int((~np.isfinite(current["z"]) & np.isfinite(obs[:, t])).sum()),
                "legacy_replay": False}
        out[flow] = current
    critical, false_bound = materiality_threshold(out, normal, watch.FALSE_PER_LEVEL / channels)
    for current in out.values():
        # В физических единицах: запас относительно прогнозной границы,
        # а не log-z, ошибочно умноженный на счётчиковый шум.
        current["zs"] *= critical / normal
        upper = positive_exp(current["center"] + current["zs"] * current["log_s"])
        if UPPER_BOUND == "hybrid":
            upper = np.fmin(upper, current["E"] + current["zs"] * current["s"])
        current["upper"] = np.maximum(upper, current["E"])
        current["lower"] = np.minimum(positive_exp(current["center"] - current["zs"] * current["log_s"]), current["E"])
        if details:
            current["calibration"].update(critical_z=critical, empirical_z=current["zs"],
                expected_false_bound=false_bound, budget=watch.FALSE_PER_LEVEL / channels)
    return out
