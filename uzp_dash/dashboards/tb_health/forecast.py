"""Прогноз получателей и ФОТ на ТЕКУЩИЙ (незакрытый) месяц.

Окончательная ЗП-ведомость есть только за закрытый месяц, поэтому вместо факта
текущего месяца считается прогноз:

    прогноз = факт закрытого месяца − ожидаемый отток + приход из пайплайна

Три источника, три функции:
  * `outflow_model`  — прогноз оттока по истории витрины (устойчивый отток два
    закрытых месяца подряд + сезонность год к году);
  * `reconcile`      — стыковка этого прогноза с ЕЖЕДНЕВНЫМ оттоком (фактом
    текущего месяца): факт — нижняя граница, прогноз — остаточный риск;
  * `pipeline_np`    — плановый приход НП, с поправкой на историческую
    реализуемость сделок.

Все функции — чистые преобразования DataFrame (без обращений к БД), поэтому
ими же пользуется генератор синтетики, чтобы план текущего месяца был согласован
с прогнозом.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RUB_TO_MLN = 1e6

# Сезонный сигнал засчитывается, только если календарный месяц наблюдался
# минимум дважды — иначе это шум одного года, а не сезонность.
MIN_SEASON_OBS = 2
# Фолбэк сезонности, когда индекса по ≥2 наблюдениям нет. Индекс требует глубины
# ~24 мес: на истории, кончающейся июнем, июль получает второе наблюдение только
# на 24-м месяце. При 13 мес доступен зато САМ ПЕРЕХОД «базовый → прогнозный
# месяц», наблюдённый год назад, — им и оцениваем сезонность.
# Порог по размеру: у микроорганизации переход 3→1 дал бы «сезонный спад −67%».
SEASON_FALLBACK_MIN_FL = 5
SRC_INDEX = "индекс"          # сезонность из индекса (≥2 наблюдения месяца)
SRC_YOY = "год назад"         # сезонность из перехода год назад (1 наблюдение)
SRC_NONE = "—"                # сезонного сигнала нет
# Пороги «есть сезонность»: ±10% к уровню базового месяца.
SEASON_UP = 1.10
SEASON_DOWN = 0.90
# Если год назад в этом же месяце клиент оттекал, а потом восстановился, — это
# цикл, а не потеря: ждём заметно меньший отток.
RECOVERED_K = 0.4
# Разовый отток (только в последнем закрытом месяце) — половинный вес.
ONE_OFF_K = 0.5
# Коэффициент реализуемости пайплайна держим в разумных пределах: 0 занулил бы
# весь пайплайн, >1 означал бы перевыполнение планов сделок.
CONV_MIN, CONV_MAX = 0.2, 1.0
# Свой коэффициент ГОСБ считается, только если за закрытые месяцы он планировал не
# меньше этого. На меньших объёмах отношение факт/план — шум: одна сделка даёт 0.0 или
# 1.0. Такие ГОСБ берут коэффициент ТБ, и в карточке это подписано явно.
CONV_MIN_PLAN = 30.0

CLS_SEASON_IN = "сезонный приход"
CLS_SEASON_OUT = "сезонный отток"
CLS_PERSIST = "устойчивый отток"
CLS_ONE_OFF = "разовый отток"
CLS_STABLE = "стабильно"

_MONTH_GEN = ("января февраля марта апреля мая июня июля августа сентября "
              "октября ноября декабря").split()


def _m(p: pd.Period) -> str:
    return f"{p.month:02d}.{p.year}"


# --- Безопасные агрегаты по строкам с пропусками ---------------------------- #
# На проме пара (ГОСБ, организация) присутствует НЕ во всех месяцах истории,
# поэтому строка может целиком состоять из NaN. Прямые np.nanmean/np.nanmax на
# таком срезе печатают RuntimeWarning («Mean of empty slice»), и лог выглядит как
# сбой. Считаем маску «есть хотя бы одно значение» явно: результат тот же (NaN),
# но без предупреждений и видно, что случай обработан осознанно.
def _row_mean(vals: np.ndarray) -> np.ndarray:
    n = np.count_nonzero(~np.isnan(vals), axis=1)
    s = np.nansum(vals, axis=1)
    return np.where(n > 0, s / np.maximum(n, 1), np.nan)


def _row_max(vals: np.ndarray) -> np.ndarray:
    if vals.size == 0:
        return np.full(vals.shape[0], np.nan)
    any_val = np.any(~np.isnan(vals), axis=1)
    filled = np.where(np.isnan(vals), -np.inf, vals)
    return np.where(any_val, filled.max(axis=1), np.nan)


def seasonal_depth_needed(ref_cur) -> int:
    """Сколько месяцев истории нужно, чтобы у ПРОГНОЗНОГО месяца было 2 наблюдения.

    История кончается ЗАКРЫТЫМ месяцем (`ref_cur − 1`), а прогнозный месяц в ней
    впервые встречается годом ранее и второй раз — ещё годом ранее. Прогнозный месяц
    всегда следующий за концом истории, то есть отстоит от него на 11 месяцев назад:
    прогноз июль, история по июнь → июль 2025 на 11-м месяце назад (нужно 12 месяцев),
    июль 2024 на 23-м (нужно 24). Отсюда 24 месяца.

    Для сравнения: у БАЗОВОГО месяца (конец истории) второе наблюдение появляется уже
    на 13-м месяце — поэтому на 13-месячной истории индекс есть только у него, а у
    прогнозного месяца нет, и отношение посчитать не из чего.
    """
    back = 11                                  # ref_cur всегда = ref_closed + 1 месяц
    return int(back + 1 + 12 * (MIN_SEASON_OBS - 1))


# --------------------------------------------------------------------------- #
def outflow_model(hist: pd.DataFrame, ref_cur) -> pd.DataFrame:
    """Прогноз оттока на месяц :ref_cur по истории витрины. Грейн (ГОСБ, ИНН).

    Признаки:
      out_1/out_2 — отток за два последних ЗАКРЫТЫХ месяца;
      base_fl     — получателей в закрытом месяце (база прогноза);
      seas_ratio  — сезонность прогнозного месяца ОТНОСИТЕЛЬНО базового
                    (индексы обоих месяцев к своему годовому среднему). Именно
                    отношение, а не индекс: если база и прогноз в одной фазе
                    сезона, сезонной дельты нет;
      yoy_*       — что было в этом же календарном месяце год назад и вернулся
                    ли клиент после того оттока.

    Отрицательный `pred` = ожидаемый ПРИТОК (сезонный бизнес).
    """
    cols = ["new_gosb_id", "inn", "base_fl", "pred", "out_class", "note",
            "out_1", "out_2", "seas_ratio", "seas_src", "hist_months"]
    if hist is None or hist.empty:
        return pd.DataFrame(columns=cols)

    h = hist.dropna(subset=["new_gosb_id"]).copy()
    h["new_gosb_id"] = h["new_gosb_id"].astype("int64")
    h["inn"] = h["inn"].astype("int64")
    h["ym"] = pd.PeriodIndex(pd.to_datetime(h["report_dt"]), freq="M")

    p_cur = pd.Period(pd.Timestamp(ref_cur), freq="M")
    p_closed = p_cur - 1

    fl = h.pivot_table(index=["new_gosb_id", "inn"], columns="ym",
                       values="current_fl_qty", aggfunc="max")
    out = h.pivot_table(index=["new_gosb_id", "inn"], columns="ym",
                        values="fl_outflow_qty", aggfunc="max")
    out = out.reindex(index=fl.index, columns=fl.columns)
    periods = list(fl.columns)

    def col(frame, p):
        return (frame[p].to_numpy(dtype="float64") if p in frame.columns
                else np.zeros(len(frame)))

    base_fl = np.nan_to_num(col(fl, p_closed))
    out_1 = np.nan_to_num(col(out, p_closed))
    out_2 = np.nan_to_num(col(out, p_closed - 1))

    # --- сезонные индексы: среднее по календарному месяцу / среднее за всё --- #
    vals = fl.to_numpy(dtype="float64")
    overall = _row_mean(vals)
    overall = np.where((overall > 0) & np.isfinite(overall), overall, np.nan)

    def month_idx(month: int):
        cols_m = [i for i, p in enumerate(periods) if p.month == month]
        if len(cols_m) < MIN_SEASON_OBS:
            return np.full(len(fl), np.nan)
        with np.errstate(invalid="ignore"):
            return _row_mean(vals[:, cols_m]) / overall

    idx_cur, idx_closed = month_idx(p_cur.month), month_idx(p_closed.month)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio_idx = np.where(idx_closed > 0, idx_cur / idx_closed, np.nan)

    # --- год назад: был ли отток в этом месяце и вернулся ли клиент --------- #
    p_yoy = p_cur - 12
    yoy_out = np.nan_to_num(col(out, p_yoy))
    fl_yoy = col(fl, p_yoy)                      # прогнозный месяц год назад
    before = np.nan_to_num(col(fl, p_yoy - 1))   # базовый месяц год назад
    after_cols = [p_yoy + k for k in (1, 2, 3) if (p_yoy + k) in fl.columns]
    after = (_row_max(np.column_stack([col(fl, p) for p in after_cols]))
             if after_cols else np.full(len(fl), np.nan))
    yoy_recovered = (yoy_out > 0) & (before > 0) & (np.nan_to_num(after) >= 0.95 * before)

    # --- ФОЛБЭК сезонности: тот же переход «база → прогноз», но год назад ---- #
    # Индексу нужно ~24 мес истории (см. seasonal_depth_needed); переход год назад
    # доступен уже на 13 мес, и это ровно то, что описывает бизнес: «год назад
    # клиент в этом месяце тоже оттекал». Оценка по ОДНОМУ наблюдению — помечаем.
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio_yoy = np.where(before >= SEASON_FALLBACK_MIN_FL,
                             np.nan_to_num(fl_yoy) / np.maximum(before, 1), np.nan)
    use_idx = np.isfinite(ratio_idx)
    seas_ratio = np.where(use_idx, ratio_idx, ratio_yoy)
    seas_src = np.where(use_idx, SRC_INDEX,
                        np.where(np.isfinite(ratio_yoy), SRC_YOY, SRC_NONE))

    hist_months = int(len(periods))
    keys = fl.index.to_frame(index=False)
    rows = []
    for i in range(len(fl)):
        b, o1, o2 = float(base_fl[i]), float(out_1[i]), float(out_2[i])
        ratio = float(seas_ratio[i]) if np.isfinite(seas_ratio[i]) else None
        persist = (o1 + o2) / 2 if (o1 > 0 and o2 > 0) else 0.0
        # оценка по одному наблюдению слабее индекса — говорим об этом прямо в причине
        weak = " (по одному наблюдению год назад)" if seas_src[i] == SRC_YOY else ""

        if ratio is not None and ratio > SEASON_UP:
            cls = CLS_SEASON_IN
            pred = -b * (ratio - 1)
            note = (f"сезонный бизнес: в этом месяце обычно "
                    f"+{(ratio - 1) * 100:.0f}%{weak}")
        elif ratio is not None and ratio < SEASON_DOWN:
            cls = CLS_SEASON_OUT
            pred = max(persist, b * (1 - ratio))
            note = f"сезонный спад: в этом месяце обычно −{(1 - ratio) * 100:.0f}%{weak}"
            if yoy_recovered[i]:
                pred *= RECOVERED_K
                note += (f" · год назад тоже оттекал и восстановился "
                         f"к {_m(p_yoy + 3)} — вероятно вернётся")
        elif o1 > 0 and o2 > 0:
            cls = CLS_PERSIST
            pred = persist
            note = (f"оттекает 2 месяца подряд ({_m(p_closed)}: {o1:.0f}, "
                    f"{_m(p_closed - 1)}: {o2:.0f})")
        elif o1 > 0:
            cls = CLS_ONE_OFF
            pred = o1 * ONE_OFF_K
            note = f"отток только в {_m(p_closed)} ({o1:.0f} чел)"
        else:
            cls, pred, note = CLS_STABLE, 0.0, ""

        if pred > 0:
            pred = min(pred, b)          # нельзя оттечь больше, чем есть
        rows.append((cls, pred, note))

    res = keys.copy()
    res["base_fl"] = base_fl
    res["out_1"] = out_1
    res["out_2"] = out_2
    res["seas_ratio"] = seas_ratio
    res["seas_src"] = seas_src
    res["out_class"] = [r[0] for r in rows]
    res["pred"] = [r[1] for r in rows]
    res["note"] = [r[2] for r in rows]
    res["hist_months"] = hist_months
    # Диагностика: без неё молчаливый сбой (нет базового месяца в истории →
    # col() вернёт нули → всё «стабильно») выглядит как нормальный результат.
    res.attrs["diag"] = {
        "hist_months": hist_months,
        "hist_from": str(periods[0]) if periods else "—",
        "hist_to": str(periods[-1]) if periods else "—",
        "base_month": str(p_closed),
        "base_present": bool(p_closed in fl.columns),
        "prev_present": bool((p_closed - 1) in fl.columns),
        "yoy_present": bool(p_yoy in fl.columns),
        "need_months": seasonal_depth_needed(ref_cur),
        "n_pairs": int(len(fl)),
        "n_with_outflow": int((out_1 > 0).sum()),
        "seas_src": {k: int(v) for k, v in
                     pd.Series(seas_src).value_counts().to_dict().items()},
    }
    return res[cols]


# --------------------------------------------------------------------------- #
def reconcile(day: pd.DataFrame, pred: pd.DataFrame, month_elapsed: float) -> pd.DataFrame:
    """Стыковка ежедневного (факт) и прогнозного оттока.

        risk    = max(pred, 0) × (1 − settled)      -- остаточный риск месяца
        out_exp = min(base_fl, max(факт_дня, risk)) -- факт как нижняя граница
        in_exp  = max(−pred, 0) × (1 − settled)     -- сезонный приход отдельно,
                                                       иначе max(...) его съест

    `settled` — какая доля месячного риска уже отыграна. Берём максимум из двух
    независимых оценок, чтобы не зависеть от формы данных витрины:
      * доля прошедшего месяца (act_dt к концу месяца) — есть всегда;
      * доля получателей прошлого месяца, которые УЖЕ зачислились (paid_mtd /
        fl_prev_m) — прямое измерение по организации.

    Отсюда оба бизнес-кейса: прогноз 100 при факте 3 и почти отыгранном месяце
    даёт 3 («прогноз не оправдался, 97 уже зачислились»), а прогноз 3 при факте
    100 даёт 100.

    Организации без строки в ежедневной витрине (выплат в этом месяце не было)
    наблюдения не имеют — у них остаётся полный прогноз.
    """
    out_cols = ["new_gosb_id", "inn", "base_fl", "pred", "out_class", "note",
                "out_observed", "settled", "out_exp", "in_exp", "why",
                "seg_day", "avg_salary_m", "has_day"]
    p = pred if pred is not None and not pred.empty else pd.DataFrame(
        columns=["new_gosb_id", "inn", "base_fl", "pred", "out_class", "note"])
    d = day if day is not None and not day.empty else pd.DataFrame(
        columns=["new_gosb_id", "inn", "seg_day", "out_observed", "paid_mtd",
                 "fl_prev_m", "avg_salary_m"])
    for f in (p, d):
        for c in ("new_gosb_id", "inn"):
            if c in f:
                f[c] = pd.to_numeric(f[c], errors="coerce").astype("Int64")
    p = p.dropna(subset=["new_gosb_id", "inn"]) if len(p) else p
    d = d.dropna(subset=["new_gosb_id", "inn"]) if len(d) else d

    m = p.merge(d, on=["new_gosb_id", "inn"], how="outer")
    if m.empty:
        return pd.DataFrame(columns=out_cols)

    m["has_day"] = m["out_observed"].notna() if "out_observed" in m else False
    for c in ("pred", "base_fl", "out_observed", "paid_mtd", "fl_prev_m", "avg_salary_m"):
        m[c] = num(m, c)
    m["out_class"] = m.get("out_class").fillna(CLS_STABLE) if "out_class" in m else CLS_STABLE
    m["note"] = m.get("note").fillna("") if "note" in m else ""
    # база: закрытый месяц из витрины, фолбэк — ФЛ прошлого месяца из дневной
    m["base_fl"] = np.where(m["base_fl"] > 0, m["base_fl"], m["fl_prev_m"])

    with np.errstate(invalid="ignore", divide="ignore"):
        paid_share = np.where(m["fl_prev_m"] > 0, m["paid_mtd"] / m["fl_prev_m"], 0.0)
    settled = np.clip(np.maximum(float(month_elapsed), paid_share), 0.0, 1.0)
    settled = np.where(m["has_day"], settled, 0.0)      # нет наблюдения — нет и отыгрыша

    pred_pos = np.maximum(m["pred"].to_numpy(), 0.0)
    pred_neg = np.maximum(-m["pred"].to_numpy(), 0.0)
    risk = pred_pos * (1 - settled)
    out_exp = np.minimum(m["base_fl"].to_numpy(),
                         np.maximum(m["out_observed"].to_numpy(), risk))
    out_exp = np.maximum(out_exp, 0.0)
    in_exp = pred_neg * (1 - settled)

    m["settled"] = settled
    m["out_exp"] = out_exp
    m["in_exp"] = in_exp
    m["why"] = [
        _why(pp, oo, s, oe, ie, has)
        for pp, oo, s, oe, ie, has in zip(m["pred"], m["out_observed"], m["settled"],
                                          m["out_exp"], m["in_exp"], m["has_day"])
    ]
    for c in out_cols:
        if c not in m:
            m[c] = np.nan
    return m[out_cols]


# --------------------------------------------------------------------------- #
def _why(pred, observed, settled, out_exp, in_exp, has_day) -> str:
    """Однострочное объяснение, откуда взялся ожидаемый отток организации."""
    if pred < 0:
        tail = f"отыграно {settled * 100:.0f}% → приход +{in_exp:.0f}" if has_day \
            else "выплат в этом месяце нет"
        return f"сезонный приход {-pred:.0f}, {tail}"
    if not has_day:
        return f"выплат в этом месяце нет — остаётся прогноз {out_exp:.0f}"
    return (f"прогноз {pred:.0f}, факт дня {observed:.0f}, "
            f"отыграно {settled * 100:.0f}% → {out_exp:.0f}")


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


def conversion_by_month(plan_m: pd.DataFrame, fact_m: pd.DataFrame,
                        ref_cur) -> tuple[dict, float, dict]:
    """Реализуемость пайплайна: ФАКТ продаж против плана по ЗАКРЫТЫМ месяцам.

    Грейн сравнения — (месяц, ГОСБ, ИНН, сотрудник). Это принципиально: сотрудник мог
    завести две сделки по одной организации и обе запланировать на один месяц (10 и 15),
    и пришедшие люди относятся к их СУММЕ (25), а не к каждой сделке по отдельности.
    Группировку даёт запрос, здесь остаётся свернуть по ГОСБ.

    Текущий месяц исключён: он не отработан, его неполный факт занизил бы коэффициент.

    Диагностика возвращает СЫРОЕ значение до клипа: если реальная конверсия ниже
    CONV_MIN, клип поднимает её до пола и тем самым ЗАВЫШАЕТ вклад пайплайна. Молчать
    об этом нельзя — иначе в логе видно ровно «0.20» и непонятно, это настоящая
    конверсия или сработавшая граница.
    """
    diag = {"months": 0, "plan": 0.0, "fact": 0.0, "tb_raw": None, "tb_clipped": False,
            "n_gosb": 0, "n_gosb_clipped": 0, "n_gosb_fallback": 0}
    if plan_m is None or plan_m.empty:
        return {}, 1.0, diag
    cur = pd.Period(pd.Timestamp(ref_cur), freq="M")
    p = _as_period(plan_m)
    p = p[p["per"] < cur]                       # только закрытые месяцы
    if p.empty:
        return {}, 1.0, diag

    keys = ["new_gosb_id", "inn", "saphr_id", "per"]
    if fact_m is not None and not fact_m.empty:
        f = _as_period(fact_m)
        p = p.merge(f[keys + ["fact_np"]], on=keys, how="left")
    p["fact_np"] = num(p, "fact_np")
    p["plan_np"] = pd.to_numeric(p["plan_np"], errors="coerce").fillna(0.0)
    diag.update({"months": int(p["per"].nunique()),
                 "plan": float(p["plan_np"].sum()), "fact": float(p["fact_np"].sum())})

    by_gosb, n_clipped, n_fallback = {}, 0, 0
    for gid, g in p.dropna(subset=["new_gosb_id"]).groupby("new_gosb_id"):
        pl = float(g["plan_np"].sum())
        if pl < CONV_MIN_PLAN:
            # объёма мало — свой коэффициент был бы шумом, ГОСБ уйдёт на коэффициент ТБ
            n_fallback += 1
            continue
        raw = float(g["fact_np"].sum()) / pl
        by_gosb[int(gid)] = float(np.clip(raw, CONV_MIN, CONV_MAX))
        if not (CONV_MIN <= raw <= CONV_MAX):
            n_clipped += 1
    tb_raw = (diag["fact"] / diag["plan"]) if diag["plan"] > 0 else None
    diag.update({"tb_raw": tb_raw, "n_gosb": len(by_gosb), "n_gosb_clipped": n_clipped,
                 "n_gosb_fallback": n_fallback,
                 "tb_clipped": tb_raw is not None and not (CONV_MIN <= tb_raw <= CONV_MAX)})
    return by_gosb, float(np.clip(tb_raw if tb_raw is not None else 1.0,
                                  CONV_MIN, CONV_MAX)), diag


def pipeline_current(plan_m: pd.DataFrame, fact_m: pd.DataFrame, ref_cur,
                     by_gosb: dict, tb_k: float, time_left: float = 1.0) -> pd.DataFrame:
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
    agg["conv"] = [by_gosb.get(int(g), tb_k) for g in agg["new_gosb_id"]]
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
def org_forecast(rec: pd.DataFrame, pipe: pd.DataFrame, seg_of: dict) -> pd.DataFrame:
    """Свести отток и пайплайн в одну строку на (ГОСБ, ИНН) + дельта прогноза.

    `seg_of` — фолбэк-сегмент по ИНН из uzp_dim_company (короткое имя): нужен там,
    где организации нет в ежедневной витрине и сегмент взять больше неоткуда.
    """
    cols = ["new_gosb_id", "inn", "seg_name", "out_exp", "in_exp", "pipe_np",
            "pipe_np_raw", "pipe_fact_mtd", "pipe_rest", "pipe_expect",
            "pipe_fot", "pipe_fot_raw", "n_deals",
            "out_observed", "pred", "out_class", "note", "why", "settled",
            "avg_salary_m", "delta_fl"]
    base = rec if rec is not None and not rec.empty else pd.DataFrame(
        columns=["new_gosb_id", "inn"])
    p = pipe if pipe is not None and not pipe.empty else pd.DataFrame(
        columns=["new_gosb_id", "inn", "pipe_np", "pipe_np_raw", "seg_funnel"])
    if base.empty and p.empty:
        return pd.DataFrame(columns=cols)

    m = base.merge(p, on=["new_gosb_id", "inn"], how="outer")
    for c in ("out_exp", "in_exp", "pipe_np", "pipe_np_raw", "pipe_fact_mtd",
              "pipe_rest", "pipe_expect", "pipe_fot",
              "pipe_fot_raw", "n_deals", "out_observed",
              "pred", "settled", "avg_salary_m"):
        m[c] = num(m, c)
    for c in ("out_class", "note", "why"):
        m[c] = m.get(c).fillna("") if c in m else ""
    m["out_class"] = m["out_class"].replace("", CLS_STABLE)

    seg_day = m.get("seg_day")
    seg_fun = m.get("seg_funnel")
    m["seg_name"] = [
        _first_seg(a, b, seg_of.get(int(i)) if pd.notna(i) else None)
        for a, b, i in zip(
            seg_day if seg_day is not None else [None] * len(m),
            seg_fun if seg_fun is not None else [None] * len(m),
            m["inn"])
    ]
    m["delta_fl"] = m["in_exp"] + m["pipe_np"] - m["out_exp"]
    return m[cols]


def _first_seg(*candidates):
    for c in candidates:
        if c is not None and pd.notna(c) and str(c).strip():
            return str(c).strip()
    return "—"


# --------------------------------------------------------------------------- #
def build_matrix(base_seg: pd.DataFrame, plan_seg: pd.DataFrame,
                 orgs_fc: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Матрица ГОСБ×сегмент по ПРОГНОЗУ: план текущего месяца против прогноза.

    База — факт закрытого месяца из uzp_dwh_metrics (та же методика, что у плана),
    дельта — с грейна организаций. Дельта организаций, чей сегмент неизвестен или
    отсутствует в плане этого ГОСБ, разносится по сегментам ГОСБ пропорционально
    базовому факту: иначе сумма ячеек не сойдётся с итогом по ГОСБ.
    """
    base = base_seg[["new_gosb_id", "seg_name", "fact_amt"]].copy() \
        if not base_seg.empty else pd.DataFrame(columns=["new_gosb_id", "seg_name", "fact_amt"])
    plan = plan_seg[["new_gosb_id", "seg_name", "plan_amt"]].copy() \
        if not plan_seg.empty else pd.DataFrame(columns=["new_gosb_id", "seg_name", "plan_amt"])
    for f in (base, plan):
        if not f.empty:
            f["new_gosb_id"] = f["new_gosb_id"].astype("int64")
    m = plan.merge(base, on=["new_gosb_id", "seg_name"], how="outer")
    m["plan_amt"] = pd.to_numeric(m["plan_amt"], errors="coerce").fillna(0.0)
    m["fact_amt"] = pd.to_numeric(m["fact_amt"], errors="coerce").fillna(0.0)

    known = {(int(r.new_gosb_id), r.seg_name) for r in m.itertuples()}
    agg: dict = {}
    resid: dict = {}
    stats = {"unattributed": 0.0, "matched": 0.0}
    if orgs_fc is not None and not orgs_fc.empty:
        for r in orgs_fc.itertuples():
            if pd.isna(r.new_gosb_id):
                continue
            gid = int(r.new_gosb_id)
            d = (float(r.out_exp), float(r.in_exp), float(r.pipe_np), float(r.pipe_np_raw))
            if (gid, r.seg_name) in known:
                cur = agg.setdefault((gid, r.seg_name), [0.0, 0.0, 0.0, 0.0])
                stats["matched"] += d[2] + d[1] - d[0]
            else:
                cur = resid.setdefault(gid, [0.0, 0.0, 0.0, 0.0])
                stats["unattributed"] += abs(d[0]) + abs(d[1]) + abs(d[2])
            for k in range(4):
                cur[k] += d[k]

    # разнос неатрибутированного остатка по сегментам ГОСБ (пропорционально базе)
    for gid, d in resid.items():
        sub = m[m["new_gosb_id"] == gid]
        total = float(sub["fact_amt"].sum())
        if sub.empty:
            # ГОСБ вообще нет в плановой матрице — разносить некуда; такую дельту
            # считаем потерянной и показываем в диагностике, а не гасим молча
            stats["lost_gosb"] = stats.get("lost_gosb", 0) + 1
            stats["lost_delta"] = stats.get("lost_delta", 0.0) + abs(d[2] + d[1] - d[0])
            continue
        for r in sub.itertuples():
            share = (float(r.fact_amt) / total) if total > 0 else 1.0 / len(sub)
            cur = agg.setdefault((gid, r.seg_name), [0.0, 0.0, 0.0, 0.0])
            for k in range(4):
                cur[k] += d[k] * share

    vals = [agg.get((int(r.new_gosb_id), r.seg_name), [0.0, 0.0, 0.0, 0.0])
            for r in m.itertuples()]
    m["out_exp"] = [v[0] for v in vals]
    m["in_exp"] = [v[1] for v in vals]
    m["pipe_np"] = [v[2] for v in vals]
    m["pipe_np_raw"] = [v[3] for v in vals]
    m["base_amt"] = m["fact_amt"]
    m["fact_amt"] = (m["base_amt"] - m["out_exp"] + m["in_exp"] + m["pipe_np"]).clip(lower=0)
    m["execution_percent"] = m["fact_amt"] / m["plan_amt"].replace(0, np.nan)
    m["nedobor"] = m["plan_amt"] - m["fact_amt"]
    return m, stats


def waterfall(base: float, orgs_fc: pd.DataFrame, plan: float,
              base_fot: float = 0.0, plan_fot: float = 0.0) -> dict:
    """Слагаемые прогноза по ТБ — для блока «Из чего складывается прогноз».

    `observed` и `risk` разделены намеренно: первое уже случилось (люди не
    зачислились в свою выплатную дату), второе — то, что ещё может не случиться
    до конца месяца, и на что как раз можно повлиять.

    ФОТ считается по колонкам out_fot / in_fot / pipe_fot, если они есть
    (их добавляет analyze, где известна средняя ЗП организации).
    """
    z = {c: 0.0 for c in ("out_exp", "in_exp", "pipe_np", "pipe_np_raw", "pipe_fact_mtd",
                          "pipe_rest", "pipe_expect", "observed", "out_fot", "in_fot",
                          "pipe_fot", "pipe_fot_raw")}
    if orgs_fc is not None and not orgs_fc.empty:
        for c in ("out_exp", "in_exp", "pipe_np", "pipe_np_raw", "pipe_fact_mtd",
                  "pipe_rest", "pipe_expect",
                  "out_fot", "in_fot", "pipe_fot", "pipe_fot_raw"):
            if c in orgs_fc:
                z[c] = float(pd.to_numeric(orgs_fc[c], errors="coerce").fillna(0).sum())
        z["observed"] = float(np.minimum(orgs_fc["out_observed"].fillna(0),
                                         orgs_fc["out_exp"].fillna(0)).sum())
    fc = max(0.0, base - z["out_exp"] + z["in_exp"] + z["pipe_np"])
    fc_fot = max(0.0, base_fot - z["out_fot"] + z["in_fot"] + z["pipe_fot"])
    return {
        "base": base, "out_exp": z["out_exp"], "observed": z["observed"],
        "risk": max(0.0, z["out_exp"] - z["observed"]), "in_exp": z["in_exp"],
        "pipe": z["pipe_np"], "pipe_raw": z["pipe_np_raw"],
        "pipe_fact": z["pipe_fact_mtd"], "pipe_rest": z["pipe_rest"],
        "pipe_expect": z["pipe_expect"],
        "pipe_upside": max(0.0, z["pipe_np_raw"] - z["pipe_np"]),
        "forecast": fc, "plan": plan,
        "exec": (fc / plan) if plan else None,
        "ceiling": max(0.0, base - z["out_exp"] + z["in_exp"] + z["pipe_np_raw"]),
        "base_fot": base_fot, "plan_fot": plan_fot, "forecast_fot": fc_fot,
        "out_fot": z["out_fot"], "pipe_fot": z["pipe_fot"],
        "exec_fot": (fc_fot / plan_fot) if plan_fot else None,
    }


def build_totals(base_tot: pd.DataFrame, plan_tot: pd.DataFrame,
                 orgs_fc: pd.DataFrame) -> pd.DataFrame:
    """То же, что build_matrix, но на грейне ГОСБ (без сегмента).

    Отдельная функция, а не свёртка матрицы: план по ГОСБ берётся из строки
    «все сегменты» витрины, а она не обязана в точности равняться сумме сегментов.
    """
    base = (base_tot[["new_gosb_id", "gosb_name", "fact_amt"]].copy()
            if base_tot is not None and not base_tot.empty
            else pd.DataFrame(columns=["new_gosb_id", "gosb_name", "fact_amt"]))
    plan = (plan_tot[["new_gosb_id", "gosb_name", "plan_amt"]].copy()
            if plan_tot is not None and not plan_tot.empty
            else pd.DataFrame(columns=["new_gosb_id", "gosb_name", "plan_amt"]))
    m = plan.merge(base.drop(columns=["gosb_name"]), on="new_gosb_id", how="outer")
    if m.empty:
        return pd.DataFrame(columns=["new_gosb_id", "gosb_name", "plan_amt",
                                     "fact_amt", "execution_percent", "nedobor"])
    m["plan_amt"] = pd.to_numeric(m["plan_amt"], errors="coerce").fillna(0.0)
    m["fact_amt"] = pd.to_numeric(m["fact_amt"], errors="coerce").fillna(0.0)
    delta = {}
    if orgs_fc is not None and not orgs_fc.empty:
        g = orgs_fc.dropna(subset=["new_gosb_id"]).groupby("new_gosb_id")
        delta = (g["in_exp"].sum() + g["pipe_np"].sum() - g["out_exp"].sum()).to_dict()
    m["base_amt"] = m["fact_amt"]
    m["fact_amt"] = (m["base_amt"]
                     + m["new_gosb_id"].map(lambda x: delta.get(x, 0.0))).clip(lower=0)
    m["execution_percent"] = m["fact_amt"] / m["plan_amt"].replace(0, np.nan)
    m["nedobor"] = m["plan_amt"] - m["fact_amt"]
    return m.sort_values("nedobor", ascending=False).reset_index(drop=True)
