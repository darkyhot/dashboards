"""Шаг 2: семейства кандидатов. Всё векторизовано и считается по срезам кэша.

Прогноз собирается из слагаемых:

    прогноз(единицы) = база − отток + приход + пайплайн

Слагаемые независимы, а свёртка к единице — обычная сумма, поэтому перебор не
считает произведение вариантов на грейне организаций: каждый вариант слагаемого
сворачивается к единицам ОДИН раз, и уже там варианты комбинируются. Иначе
25 тыс. комбинаций пришлось бы гонять по 5.7 млн организаций.

ГЛАВНОЕ ПРАВИЛО ФАЙЛА: все признаки берутся срезом `[:, :j]`, где j — индекс
прогнозного месяца. Ни одна функция здесь не имеет доступа к столбцу j и правее.
Проверяется тестом `check_no_lookahead` — подменяет столбцы >= j на мусор и
требует, чтобы результат не изменился.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from uzp_dash.dashboards.tb_health import forecast as F

# Значения по умолчанию у текущей формулы — из самого дэша, а не переписанные.
CUR_DEFAULTS = dict(one_off_k=F.ONE_OFF_K, recovered_k=F.RECOVERED_K,
                    season_up=F.SEASON_UP, season_down=F.SEASON_DOWN,
                    persist="mean")
SEASON_FALLBACK_MIN_FL = F.SEASON_FALLBACK_MIN_FL
MIN_SEASON_OBS = F.MIN_SEASON_OBS


# --------------------------------------------------------------------------- #
# Работа с историей: только столбцы < j
# --------------------------------------------------------------------------- #
def lag(vals: np.ndarray, j: int, k: int) -> np.ndarray:
    """Значение за k месяцев до прогнозного. Нет такого месяца — нули."""
    t = j - k
    if t < 0:
        return np.zeros(vals.shape[0], dtype="float32")
    return vals[:, t]


def mean_k(vals: np.ndarray, present: np.ndarray, j: int, k: int | None) -> np.ndarray:
    """Среднее за последние k месяцев ДО j — только по месяцам, где пара была.

    Ноль и «строки не было» — разные вещи: у организации, появившейся в витрине
    два месяца назад, среднее за 6 месяцев считается по двум, а не делится на шесть.
    """
    lo = 0 if k is None else max(0, j - k)
    if lo >= j:
        return np.zeros(vals.shape[0], dtype="float32")
    p = present[:, lo:j]
    cnt = p.sum(1)
    s = (vals[:, lo:j] * p).sum(1)
    return np.where(cnt > 0, s / np.maximum(cnt, 1), 0.0).astype("float32")


def median_k(vals: np.ndarray, present: np.ndarray, j: int, k: int) -> np.ndarray:
    lo = max(0, j - k)
    if lo >= j:
        return np.zeros(vals.shape[0], dtype="float32")
    v = np.where(present[:, lo:j], vals[:, lo:j], np.nan)
    with np.errstate(invalid="ignore"):
        m = np.nanmedian(v, axis=1)
    return np.nan_to_num(m).astype("float32")


def ewma(vals: np.ndarray, present: np.ndarray, j: int, alpha: float,
         max_k: int = 12) -> np.ndarray:
    """Экспоненциальное сглаживание по месяцам < j, с нормировкой на пропуски."""
    lo = max(0, j - max_k)
    if lo >= j:
        return np.zeros(vals.shape[0], dtype="float32")
    lags = j - np.arange(lo, j)                       # j-1 -> 1
    w = (alpha * (1 - alpha) ** (lags - 1)).astype("float32")
    p = present[:, lo:j]
    num = (vals[:, lo:j] * p * w).sum(1)
    den = (p * w).sum(1)
    return np.where(den > 0, num / np.maximum(den, 1e-9), 0.0).astype("float32")


def safe_div(a, b, default=0.0):
    b = np.asarray(b, dtype="float64")
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(b > 0, np.asarray(a, dtype="float64") / np.maximum(b, 1e-9),
                     default)
    return np.nan_to_num(r, nan=default, posinf=default, neginf=default)


# --------------------------------------------------------------------------- #
# Текущая формула дэша — векторизованная копия
# --------------------------------------------------------------------------- #
def current_pred(ch, j: int, one_off_k: float, recovered_k: float,
                 season_up: float, season_down: float, persist: str,
                 n_obs_cur: int, n_obs_cls: int) -> np.ndarray:
    """`forecast.outflow_model`, слово в слово, но без питоновского цикла.

    Цикл на 5.7 млн организаций стоит ~70 с за один вызов, а сетка по константам
    гоняет его десятки раз на каждый месяц — это часы. Поэтому здесь копия, а
    ПАРИТЕТ С ОРИГИНАЛОМ проверяется тестом: `selfcheck.check_current_formula`
    сравнивает результат с настоящим `outflow_model` число в число.

    Возвращает знаковый прогноз: > 0 — отток, < 0 — сезонный приход.

    `n_obs_cur` / `n_obs_cls` — сколько раз календарный месяц прогнозного и
    базового встречался в истории. Проверка глобальная, как в оригинале: порог
    MIN_SEASON_OBS отвечает на вопрос про ГЛУБИНУ истории, а не про пару.
    """
    fl, out_q, present = ch.var["fl"], ch.var["out_q"], ch.present
    b = lag(fl, j, 1).astype("float64")
    o1 = lag(out_q, j, 1).astype("float64")
    o2 = lag(out_q, j, 2).astype("float64")

    # --- сезонность из индекса: среднее по календарным месяцам --------------- #
    mon_cur = _month_num(ch.months[j])
    mon_cls = _month_num(ch.months[j - 1]) if j >= 1 else mon_cur
    if n_obs_cur < MIN_SEASON_OBS or n_obs_cls < MIN_SEASON_OBS:
        ratio_idx = np.full(len(b), np.nan)
    else:
        avg_cur = _avg_calendar_month(fl, present, ch.months, j, mon_cur)
        avg_cls = _avg_calendar_month(fl, present, ch.months, j, mon_cls)
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio_idx = np.where(avg_cls > 0, avg_cur / avg_cls, np.nan)

    # --- фолбэк сезонности и «год назад» ------------------------------------ #
    yoy_out = lag(out_q, j, 12).astype("float64")
    fl_yoy = lag(fl, j, 12).astype("float64")
    before = lag(fl, j, 13).astype("float64")
    after = np.maximum.reduce([lag(fl, j, 11), lag(fl, j, 10), lag(fl, j, 9)]) \
        .astype("float64")
    recovered = (yoy_out > 0) & (before > 0) & (after >= 0.95 * before)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio_yoy = np.where(before >= SEASON_FALLBACK_MIN_FL,
                             fl_yoy / np.maximum(before, 1), np.nan)
    use_idx = np.isfinite(ratio_idx)
    ratio = np.where(use_idx, ratio_idx, ratio_yoy)
    has_ratio = np.isfinite(ratio)

    both = (o1 > 0) & (o2 > 0)
    if persist == "max":
        base_persist = np.maximum(o1, o2)
    elif persist == "min":
        base_persist = np.minimum(o1, o2)
    else:
        base_persist = (o1 + o2) / 2
    persist_v = np.where(both, base_persist, 0.0)

    up = has_ratio & (ratio > season_up)
    down = has_ratio & (ratio < season_down)
    pred = np.zeros(len(b))
    # порядок ветвей тот же, что в оригинале: сезон важнее устойчивого оттока
    pred = np.where(up, -b * (np.where(has_ratio, ratio, 1.0) - 1), pred)
    down_val = np.maximum(persist_v, b * (1 - np.where(has_ratio, ratio, 1.0)))
    down_val = np.where(recovered, down_val * recovered_k, down_val)
    pred = np.where(down, down_val, pred)
    rest = ~up & ~down
    pred = np.where(rest & both, persist_v, pred)
    pred = np.where(rest & ~both & (o1 > 0), o1 * one_off_k, pred)
    # нельзя оттечь больше, чем есть
    pred = np.where(pred > 0, np.minimum(pred, b), pred)
    return pred.astype("float32")


def _month_num(ym: str) -> int:
    return int(str(ym)[5:7])


def _avg_calendar_month(vals, present, months, j, mon) -> np.ndarray:
    cols = [t for t in range(j) if _month_num(months[t]) == mon]
    if not cols:
        return np.zeros(vals.shape[0])
    v, p = vals[:, cols], present[:, cols]
    cnt = p.sum(1)
    return np.where(cnt > 0, (v * p).sum(1) / np.maximum(cnt, 1), 0.0)


def calendar_obs(months: list, j: int) -> tuple[int, int]:
    """Сколько раз календарные месяцы прогнозного и базового встречались до j."""
    mon_cur = _month_num(months[j])
    mon_cls = _month_num(months[j - 1]) if j >= 1 else mon_cur
    n_cur = sum(1 for t in range(j) if _month_num(months[t]) == mon_cur)
    n_cls = sum(1 for t in range(j) if _month_num(months[t]) == mon_cls)
    return n_cur, n_cls


def current_agg_frame(ch, j: int) -> pd.DataFrame:
    """Те же признаки, но в виде кадра для НАСТОЯЩЕГО `forecast.outflow_model`.

    Нужен ровно для одной цели — сверки паритета в тестах. В переборе не участвует:
    питоновский цикл оригинала на полном банке считается больше минуты.
    """
    fl, out_q, present = ch.var["fl"], ch.var["out_q"], ch.present
    mon_cur = _month_num(ch.months[j])
    mon_cls = _month_num(ch.months[j - 1])
    n_cur, n_cls = calendar_obs(ch.months, j)
    return pd.DataFrame({
        "new_gosb_id": ch.gosb.astype("int64"), "inn": ch.inn.astype("int64"),
        "n_months": present[:, :j].sum(1),
        "ym_min": pd.Period(ch.months[0], freq="M").to_timestamp().date(),
        "ym_max": pd.Period(ch.months[j - 1], freq="M").to_timestamp().date(),
        "base_fl": lag(fl, j, 1), "out_1": lag(out_q, j, 1), "out_2": lag(out_q, j, 2),
        "fl_avg_all": mean_k(fl, present, j, None),
        "fl_avg_mcur": _avg_calendar_month(fl, present, ch.months, j, mon_cur),
        "n_mcur": n_cur,
        "fl_avg_mcls": _avg_calendar_month(fl, present, ch.months, j, mon_cls),
        "n_mcls": n_cls,
        "yoy_out": lag(out_q, j, 12), "fl_yoy": lag(fl, j, 12),
        "fl_before": lag(fl, j, 13),
        "fl_after": np.maximum.reduce([lag(fl, j, 11), lag(fl, j, 10), lag(fl, j, 9)]),
        "out_months": None,
    })


# --------------------------------------------------------------------------- #
# Сетка вариантов слагаемых
# --------------------------------------------------------------------------- #
def out_variants(has_fo: bool) -> list[dict]:
    """Варианты ожидаемого ОТТОКА."""
    v: list[dict] = [{"id": "out:none", "kind": "none"}]
    v.append({"id": "out:cur", "kind": "cur", **CUR_DEFAULTS})
    # сетка по константам текущей формулы — по одному измерению за раз вокруг
    # значений по умолчанию: полный перебор дал бы 72 варианта, а интересен вклад
    # каждой константы по отдельности
    for k in (0.0, 0.25, 1.0):
        v.append({"id": f"out:cur(one_off={k})", "kind": "cur",
                  **{**CUR_DEFAULTS, "one_off_k": k}})
    for k in (0.2, 0.7, 1.0):
        v.append({"id": f"out:cur(recovered={k})", "kind": "cur",
                  **{**CUR_DEFAULTS, "recovered_k": k}})
    for up, dn in ((1.05, 0.95), (1.20, 0.80)):
        v.append({"id": f"out:cur(season={up}/{dn})", "kind": "cur",
                  **{**CUR_DEFAULTS, "season_up": up, "season_down": dn}})
    for p in ("max", "min"):
        v.append({"id": f"out:cur(persist={p})", "kind": "cur",
                  **{**CUR_DEFAULTS, "persist": p}})
    # доля оттока от численности, с усадкой к сегменту
    for k in (1, 3, 6, None):
        for lam in (0.0, 5.0):
            kk = "all" if k is None else k
            v.append({"id": f"out:rate_k{kk}_lam{lam:g}", "kind": "rate",
                      "k": k, "lam": lam, "src": "out_q"})
    for a in (0.3, 0.5, 0.7):
        v.append({"id": f"out:ewma{a}", "kind": "ewma", "alpha": a, "src": "out_q"})
    for k in (3, 6):
        v.append({"id": f"out:median_k{k}", "kind": "median", "k": k, "src": "out_q"})
    if has_fo:
        for k in (1, 3, 6):
            v.append({"id": f"out:fo_rate_k{k}", "kind": "rate", "k": k, "lam": 0.0,
                      "src": "fo_out"})
        v.append({"id": "out:fo_mean_k3", "kind": "mean", "k": 3, "src": "fo_out"})
        v.append({"id": "out:fo_gap", "kind": "fo_gap"})
    return v


def in_variants() -> list[dict]:
    """Варианты ПРИХОДА.

    В текущей формуле приход бывает только сезонный: органический набор
    сотрудников не моделируется вовсе.

    Прямого измерения органического прихода в витрине НЕТ. `new_fl_cnt` — это не
    он: столбец заполняется только там, где сотрудник завёл сделку, то есть по
    смыслу совпадает с фактом привлечения по пайплайну. Считаем его всё равно —
    покрытие меряется отдельно (см. `backtest.coverage`), — но основной способ
    поймать приход другой: ЧИСТАЯ ДЕЛЬТА самой численности, аддитивная и
    мультипликативная.

    Два поля разметки задают, с чем вариант НЕ сочетается:
      needs_out_none  — величина уже включает отток (чистая дельта);
      from_history    — величина оценена по прошлому и уже содержит привлечение,
                        поэтому складывать её с планом пайплайна значит считать
                        привлечение дважды.
    """
    v: list[dict] = [{"id": "in:none", "kind": "none"}]
    # сезонный приход текущей формулы — возврат ранее ушедших, а не новые сделки,
    # поэтому с пайплайном он сочетается: именно так устроен дэш сегодня
    v.append({"id": "in:cur_season", "kind": "cur_season", **CUR_DEFAULTS})

    # --- приход по new_fl_cnt: фактически тот же пайплайн, отсюда from_history - #
    for k in (1, 2, 3, 6):
        v.append({"id": f"in:newfl_mean_k{k}", "kind": "mean", "k": k,
                  "src": "new_fl", "from_history": True})
    for k in (3, 6):
        v.append({"id": f"in:newfl_median_k{k}", "kind": "median", "k": k,
                  "src": "new_fl", "from_history": True})
    for a in (0.3, 0.5, 0.7):
        v.append({"id": f"in:newfl_ewma{a}", "kind": "ewma", "alpha": a,
                  "src": "new_fl", "from_history": True})

    # --- пулированный темп прироста уровня ---------------------------------- #
    for level in ("all", "tb", "seg"):
        v.append({"id": f"in:growth_{level}", "kind": "growth", "level": level,
                  "from_history": True})

    # --- чистая дельта, аддитивная ------------------------------------------ #
    for a in (0.3, 0.5):
        v.append({"id": f"in:netdelta_ewma{a}", "kind": "netdelta", "alpha": a,
                  "needs_out_none": True, "from_history": True})
    for k in (3, 6):
        v.append({"id": f"in:netdelta_median_k{k}", "kind": "netdelta_median",
                  "k": k, "needs_out_none": True, "from_history": True})
    for k in (3, 6):
        v.append({"id": f"in:netdelta_mean_k{k}", "kind": "netdelta_mean", "k": k,
                  "needs_out_none": True, "from_history": True})

    # --- чистая дельта, МУЛЬТИПЛИКАТИВНАЯ ----------------------------------- #
    # Организации различаются по размеру на три порядка. Складывать их абсолютные
    # приросты — значит отдать сглаживание нескольким крупнейшим; отношение
    # «во сколько раз» от размера не зависит и ведёт себя ровнее.
    for k in (3, 6, 12):
        v.append({"id": f"in:ratio_mean_k{k}", "kind": "ratio_mean", "k": k,
                  "needs_out_none": True, "from_history": True})
    for a in (0.3, 0.5):
        v.append({"id": f"in:ratio_ewma{a}", "kind": "ratio_ewma", "alpha": a,
                  "needs_out_none": True, "from_history": True})
    # с усадкой к темпу сегмента: у мелкой организации собственное отношение —
    # шум, ровно как и собственная доля оттока
    for lam in (20.0, 100.0):
        v.append({"id": f"in:ratio_shrunk_lam{lam:g}", "kind": "ratio_shrunk",
                  "k": 6, "lam": lam, "needs_out_none": True, "from_history": True})
    return v


def pipe_variants(has_pipe: bool) -> list[dict]:
    v: list[dict] = [{"id": "pipe:none", "kind": "none"}]
    if has_pipe:
        for k in (0.3, 0.5, 0.7, 1.0):
            v.append({"id": f"pipe:plan_x{k}", "kind": "plan", "k": k})
        v.append({"id": "pipe:plan_x_hist", "kind": "plan_hist"})
    return v


# --------------------------------------------------------------------------- #
def eval_term(ch, j: int, spec: dict, pooled: dict) -> np.ndarray:
    """Посчитать одно слагаемое для всех организаций чанка. Знак — как в формуле:
    отток положительный, приход положительный, вычитание делает сборка."""
    kind = spec["kind"]
    if kind == "none":
        return np.zeros(ch.n, dtype="float32")

    present, fl = ch.present, ch.var["fl"]

    if kind in ("cur", "cur_season"):
        n_cur, n_cls = pooled["calendar_obs"][j]
        pred = current_pred(ch, j, spec["one_off_k"], spec["recovered_k"],
                            spec["season_up"], spec["season_down"], spec["persist"],
                            n_cur, n_cls)
        return (np.maximum(-pred, 0) if kind == "cur_season"
                else np.maximum(pred, 0)).astype("float32")

    if kind == "mean":
        return mean_k(ch.var[spec["src"]], present, j, spec["k"])
    if kind == "median":
        return median_k(ch.var[spec["src"]], present, j, spec["k"])
    if kind == "ewma":
        return ewma(ch.var[spec["src"]], present, j, spec["alpha"])

    if kind == "rate":
        # доля оттока за окно, усаженная к доле СЕГМЕНТА: у мелкой организации
        # собственная доля — шум, одна ушедшая пятёрка даёт 100%
        src = ch.var[spec["src"]]
        k = spec["k"]
        lo = 0 if k is None else max(0, j - k)
        num = (src[:, lo:j] * present[:, lo:j]).sum(1)
        den = (fl[:, lo:j] * present[:, lo:j]).sum(1)
        own = safe_div(num, den)
        lam = spec["lam"]
        if lam > 0:
            seg_rate = pooled["seg_rate"][spec["src"]][k][j][by_seg(ch)]
            w = den / (den + lam)
            own = w * own + (1 - w) * seg_rate
        return (own * lag(fl, j, 1)).astype("float32")

    if kind == "fo_gap":
        # план получателей минус факт за последний закрытый месяц: прямая оценка
        # «сколько человек не дошло» из отдельной витрины
        gap = lag(ch.var["fo_plan"], j, 1) - lag(ch.var["fo_fact"], j, 1)
        return np.maximum(gap, 0).astype("float32")

    if kind == "growth":
        # пулированный темп прироста уровня: на сколько процентов численность
        # прибавляет типичный портфель этого сегмента / ТБ / банка
        lvl = spec["level"]
        g = pooled["growth"][lvl][j]
        if lvl == "seg":
            g = g[by_seg(ch)]
        elif lvl == "tb":
            g = g[by_tb(ch)]
        return np.maximum(g * lag(fl, j, 1), 0).astype("float32")

    if kind in ("netdelta", "netdelta_median", "netdelta_mean"):
        d, ok = derived(ch, "delta", lambda: _delta_matrix(fl, present))
        if kind == "netdelta":
            return ewma(d, ok, j, spec["alpha"]).astype("float32")
        if kind == "netdelta_median":
            return median_k(d, ok, j, spec["k"]).astype("float32")
        return mean_k(d, ok, j, spec["k"]).astype("float32")

    if kind in ("ratio_mean", "ratio_ewma", "ratio_shrunk"):
        # сглаживаем ОТНОСИТЕЛЬНЫЙ прирост (r − 1), а не само отношение: тогда
        # «данных нет» естественно означает ноль прироста, а не единицу, которую
        # пришлось бы подставлять руками
        d, ok = derived(ch, "ratio", lambda: _ratio_matrix(fl, present))
        if kind == "ratio_ewma":
            g = ewma(d, ok, j, spec["alpha"])
        else:
            g = mean_k(d, ok, j, spec["k"])
        if kind == "ratio_shrunk":
            g_seg = pooled["growth"]["seg"][j][by_seg(ch)]
            b = lag(fl, j, 1)
            w = b / (b + spec["lam"])
            g = w * g + (1 - w) * g_seg
        return (g * lag(fl, j, 1)).astype("float32")

    if kind == "plan":
        return (lag(ch.var["pipe_fwd"], j, 1) * spec["k"]).astype("float32")
    if kind == "plan_hist":
        return (lag(ch.var["pipe_fwd"], j, 1)
                * float(pooled["pipe_k"][j])).astype("float32")

    raise ValueError(f"неизвестный вид слагаемого: {kind}")


# Коды сегментов витрины невелики (21…1092), номера ТБ тоже — поэтому пулированные
# величины разносятся по организациям обычной индексацией плотного массива, без
# питоновского цикла на миллионы строк.
SEG_SIZE = 2048
TB_SIZE = 256


def by_seg(ch) -> np.ndarray:
    """Индекс сегмента для выборки из плотной таблицы. Неизвестный -> нулевой слот."""
    s = ch.seg.astype("int64")
    return np.where((s >= 0) & (s < SEG_SIZE), s, 0)


def by_tb(ch) -> np.ndarray:
    t = ch.tb.astype("int64")
    return np.where((t >= 0) & (t < TB_SIZE), t, 0)


def dense(values: dict, size: int, default: float) -> np.ndarray:
    """Словарь код -> величина в плотный массив для быстрой индексации."""
    a = np.full(size, float(default), dtype="float64")
    for k, v in values.items():
        k = int(k)
        if 0 <= k < size and np.isfinite(v):
            a[k] = float(v)
    return a


def derived(ch, key: str, build):
    """Производная матрица (прирост, отношение) с кэшем на самом чанке.

    Один и тот же прирост нужен десятку вариантов на каждом из двух десятков
    месяцев; пересчитывать его каждый раз — впустую гонять миллионы операций.
    Кэш живёт вместе с чанком и умирает вместе с ним: ключ от месяца не зависит,
    матрицы полные, и подмешать в них будущее нельзя.
    """
    cache = getattr(ch, "_cache", None)
    if cache is None:
        cache = {}
        ch._cache = cache
    if key not in cache:
        cache[key] = build()
    return cache[key]


# Отношение месяц к месяцу зажимается: рост в 50 раз бывает у организации,
# пришедшей с тремя получателями, и в сглаженное среднее такое попадать не должно.
RATIO_MIN, RATIO_MAX = 0.5, 2.0
# Отношение вообще не считается, если в базовом месяце получателей меньше: у
# численности 1 -> 3 «рост втрое» о будущем не говорит ничего.
RATIO_MIN_FL = 5


def _delta_matrix(fl: np.ndarray, present: np.ndarray) -> tuple:
    """Помесячный прирост численности и маска месяцев, где он определён.

    Маска отдельная: у пары, появившейся в витрине в середине окна, прироста за
    первый её месяц нет, и подставлять туда ноль нельзя — он потянул бы вниз
    среднее наравне с настоящим нулевым приростом.
    """
    d = np.zeros_like(fl)
    d[:, 1:] = fl[:, 1:] - fl[:, :-1]
    ok = np.zeros(fl.shape, dtype=bool)
    ok[:, 1:] = present[:, 1:] & present[:, :-1]
    d = np.where(ok, d, 0.0).astype("float32")
    return d, ok


def _ratio_matrix(fl: np.ndarray, present: np.ndarray) -> tuple:
    """Относительный прирост (r − 1) помесячно и маска, где он определён."""
    prev, cur = fl[:, :-1], fl[:, 1:]
    ok = np.zeros(fl.shape, dtype=bool)
    ok[:, 1:] = present[:, 1:] & present[:, :-1] & (prev >= RATIO_MIN_FL)
    r = np.zeros(fl.shape, dtype="float32")
    with np.errstate(invalid="ignore", divide="ignore"):
        val = np.clip(cur / np.maximum(prev, 1e-9), RATIO_MIN, RATIO_MAX) - 1.0
    r[:, 1:] = np.where(ok[:, 1:], np.nan_to_num(val), 0.0)
    return r, ok
