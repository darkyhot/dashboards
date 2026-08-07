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


def aggregate_history(hist: pd.DataFrame, ref_cur) -> pd.DataFrame:
    """Свернуть СЫРУЮ историю витрины в признаки модели — то же, что делает SQL.

    Дэш эту функцию не вызывает: по всему банку историю сворачивает БД
    (`queries.OUTFLOW_HIST_AGG`), и тянуть в память 17 млн месячных строк незачем.
    Нужна она там, где сырая история УЖЕ в памяти: генератору синтетики (он строит
    план текущего месяца согласованным с прогнозом) и сверке «SQL против pandas».

    Держать её рядом с моделью обязательно: это единственное место, где записано,
    ЧТО именно должен посчитать SQL, и разъехаться они молча не смогут.
    """
    cols = ["new_gosb_id", "inn", "n_months", "ym_min", "ym_max", "base_fl",
            "out_1", "out_2", "fl_avg_all", "fl_avg_mcur", "n_mcur",
            "fl_avg_mcls", "n_mcls", "yoy_out", "fl_yoy", "fl_before", "fl_after",
            "out_months"]
    if hist is None or hist.empty:
        return pd.DataFrame(columns=cols)
    h = hist.dropna(subset=["new_gosb_id"]).copy()
    h["new_gosb_id"] = h["new_gosb_id"].astype("int64")
    h["inn"] = h["inn"].astype("int64")
    h["ym"] = pd.to_datetime(h["report_dt"]).dt.to_period("M")
    # несколько old_gosb_id сворачиваются в один new_gosb_id — суммируем, как SQL
    # и как матрица метрик: два бывших отделения одного ГОСБ обслуживают разных людей
    m = (h.groupby(["new_gosb_id", "inn", "ym"], as_index=False)
          .agg(fl=("current_fl_qty", "sum"), out_q=("fl_outflow_qty", "sum")))
    m[["fl", "out_q"]] = m[["fl", "out_q"]].fillna(0)
    m["ym"] = m["ym"].astype("period[M]")

    p_cur = pd.Period(pd.Timestamp(ref_cur), freq="M")
    p_closed, p_yoy = p_cur - 1, p_cur - 12
    out_from = p_closed - (YOY_DEPTH - 1)

    def at(p, col):
        s = m[m["ym"] == p].set_index(["new_gosb_id", "inn"])[col]
        return s[~s.index.duplicated()]

    after_p = [p_yoy + k for k in (1, 2, 3)]
    g = m.groupby(["new_gosb_id", "inn"])
    res = g.agg(n_months=("ym", "size"), ym_min=("ym", "min"), ym_max=("ym", "max"),
                fl_avg_all=("fl", "mean"))
    for name, month in (("mcur", p_cur.month), ("mcls", p_closed.month)):
        sub = m[m["ym"].dt.month == month].groupby(["new_gosb_id", "inn"])
        res[f"fl_avg_{name}"] = sub["fl"].mean()
        res[f"n_{name}"] = sub["fl"].size()
    res["base_fl"] = at(p_closed, "fl")
    res["out_1"] = at(p_closed, "out_q")
    res["out_2"] = at(p_closed - 1, "out_q")
    res["yoy_out"] = at(p_yoy, "out_q")
    res["fl_yoy"] = at(p_yoy, "fl")
    res["fl_before"] = at(p_yoy - 1, "fl")
    aft = m[m["ym"].isin(after_p)].groupby(["new_gosb_id", "inn"])["fl"].max()
    res["fl_after"] = aft
    om = (m[(m["out_q"] > 0) & (m["ym"] >= out_from) & (m["ym"] <= p_closed)]
          .assign(lbl=lambda x: x["ym"].astype(str))
          .groupby(["new_gosb_id", "inn"])["lbl"].apply(lambda s: ",".join(s)))
    res["out_months"] = om
    res = res.reset_index()
    # начало месяца — как date_trunc('month') в SQL
    for c in ("ym_min", "ym_max"):
        res[c] = res[c].apply(lambda p: p.to_timestamp().date())
    res["n_mcur"] = res["n_mcur"].fillna(0)
    res["n_mcls"] = res["n_mcls"].fillna(0)
    return res[cols]


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
YOY_DEPTH = 13          # месяцев истории под вопрос «в каком месяце был отток»


def _out_months(col) -> list:
    """Месяцы, в которых у организации был отток, — свежие первыми.

    Годовое падение портфеля объясняется конкретными месяцами, и без них в блоке
    «Портфель год к году» остаётся одна дельта без всякой зацепки. Окно (YOY_DEPTH
    месяцев) отбирает SQL, здесь остаётся разобрать строку «YYYY-MM,YYYY-MM,…» и
    отсортировать: порядок в string_agg не задан намеренно — упорядоченные агрегаты
    в Greenplum ненадёжны, а сортировка десятка меток стоит ничего.

    Наружу метки идут в том же виде «MM.YYYY», что и раньше: по ним блок годового
    тренда сходится с индексом активностей по месяцам.
    """
    if col is None:
        return []
    out = []
    for v in col:
        if not isinstance(v, str) or not v.strip():
            out.append([])
            continue
        months = sorted({m.strip() for m in v.split(",") if m.strip()}, reverse=True)
        out.append([f"{m[5:7]}.{m[0:4]}" for m in months])
    return out


def outflow_model(agg: pd.DataFrame, ref_cur) -> pd.DataFrame:
    """Прогноз оттока на месяц :ref_cur по истории витрины. Грейн (ГОСБ, ИНН).

    На вход идёт УЖЕ СВЁРНУТАЯ история (`queries.OUTFLOW_HIST_AGG`): одна строка на
    пару вместо 24 месячных. Сворачивает БД, потому что по всему банку сырая история —
    порядка 17 млн строк, а модели из них нужны только агрегаты. Признаки те же:
      out_1/out_2 — отток за два последних ЗАКРЫТЫХ месяца;
      base_fl     — получателей в закрытом месяце (база прогноза);
      seas_ratio  — сезонность прогнозного месяца ОТНОСИТЕЛЬНО базового. Раньше это
                    было отношение двух индексов (среднее месяца к годовому среднему);
                    годовое среднее в отношении сокращается, поэтому достаточно
                    отношения средних по двум календарным месяцам;
      yoy_*       — что было в этом же календарном месяце год назад и вернулся
                    ли клиент после того оттока.

    Отрицательный `pred` = ожидаемый ПРИТОК (сезонный бизнес).
    """
    cols = ["new_gosb_id", "inn", "base_fl", "pred", "out_class", "note",
            "out_1", "out_2", "seas_ratio", "seas_src", "recovered", "hist_months",
            "out_months", "has_hist"]
    if agg is None or agg.empty:
        return pd.DataFrame(columns=cols)

    h = agg.dropna(subset=["new_gosb_id"]).copy()
    h["new_gosb_id"] = h["new_gosb_id"].astype("int64")
    h["inn"] = h["inn"].astype("int64")

    p_cur = pd.Period(pd.Timestamp(ref_cur), freq="M")
    p_closed = p_cur - 1

    def num_col(name):
        return pd.to_numeric(h.get(name), errors="coerce").to_numpy(dtype="float64")

    base_fl = np.nan_to_num(num_col("base_fl"))
    out_1 = np.nan_to_num(num_col("out_1"))
    out_2 = np.nan_to_num(num_col("out_2"))

    # --- сезонность: среднее по календарному месяцу прогнозного к базовому --- #
    # Порог MIN_SEASON_OBS проверяется ГЛОБАЛЬНО — хватает ли ГЛУБИНЫ истории, чтобы
    # календарный месяц вообще встречался дважды. Глобальное число наблюдений месяца
    # равно максимуму по парам: пара не может видеть месяцев больше, чем их есть.
    n_cur_global = int(np.nan_to_num(num_col("n_mcur")).max()) if len(h) else 0
    n_cls_global = int(np.nan_to_num(num_col("n_mcls")).max()) if len(h) else 0
    avg_cur, avg_cls = num_col("fl_avg_mcur"), num_col("fl_avg_mcls")
    if n_cur_global < MIN_SEASON_OBS or n_cls_global < MIN_SEASON_OBS:
        ratio_idx = np.full(len(h), np.nan)
    else:
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio_idx = np.where(avg_cls > 0, avg_cur / avg_cls, np.nan)

    # --- год назад: был ли отток в этом месяце и вернулся ли клиент --------- #
    p_yoy = p_cur - 12
    yoy_out = np.nan_to_num(num_col("yoy_out"))
    fl_yoy = num_col("fl_yoy")                   # прогнозный месяц год назад
    before = np.nan_to_num(num_col("fl_before"))  # базовый месяц год назад
    after = num_col("fl_after")                  # максимум за 3 месяца после
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

    # Глубина истории — максимум по парам: у пары месяцев не больше, чем их в витрине
    hist_months = int(np.nan_to_num(num_col("n_months")).max()) if len(h) else 0
    keys = h[["new_gosb_id", "inn"]].reset_index(drop=True)
    rows = []
    for i in range(len(h)):
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
    # признак «год назад тоже оттекал, а потом восстановился» нужен не только внутри
    # note: по нему в детализации ГОСБ считается, сколько сезонных клиентов вернётся.
    # Отдельная колонка, а не поиск подстроки в тексте — формулировку note можно менять.
    res["recovered"] = yoy_recovered
    res["out_class"] = [r[0] for r in rows]
    res["pred"] = [r[1] for r in rows]
    res["note"] = [r[2] for r in rows]
    res["hist_months"] = hist_months
    res["out_months"] = _out_months(h.get("out_months"))
    # признак «история по паре ЕСТЬ»: после left join к организациям он становится
    # False там, где витрина о паре вообще ничего не знает. Без него «месяцев с
    # оттоком нет» не отличить от «истории нет», и в блоке годового тренда вторые
    # молча уезжали бы в группу «таяли постепенно»
    res["has_hist"] = True
    # Диагностика: без неё молчаливый сбой (нет базового месяца в истории →
    # все признаки нулевые → всё «стабильно») выглядит как нормальный результат.
    ym_min, ym_max = h.get("ym_min"), h.get("ym_max")
    res.attrs["diag"] = {
        "hist_months": hist_months,
        "hist_from": _ym_str(ym_min, "min"),
        "hist_to": _ym_str(ym_max, "max"),
        "base_month": str(p_closed),
        "base_present": bool(h["base_fl"].notna().any()) if "base_fl" in h else False,
        "prev_present": bool(h["out_2"].notna().any()) if "out_2" in h else False,
        "yoy_present": bool(h["fl_yoy"].notna().any()) if "fl_yoy" in h else False,
        "need_months": seasonal_depth_needed(ref_cur),
        "n_pairs": int(len(h)),
        "n_with_outflow": int((out_1 > 0).sum()),
        "seas_src": {k: int(v) for k, v in
                     pd.Series(seas_src).value_counts().to_dict().items()},
    }
    return res[cols]


def _ym_str(col, how: str) -> str:
    """Край окна истории строкой «YYYY-MM» — для диагностики в прогрессе."""
    if col is None or col.empty:
        return "—"
    v = pd.to_datetime(col, errors="coerce")
    v = v.min() if how == "min" else v.max()
    return str(pd.Period(v, freq="M")) if pd.notna(v) else "—"


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
                "recovered", "out_observed", "settled", "out_exp", "in_exp", "why",
                "seg_day", "avg_salary_m", "has_day", "out_months", "has_hist"]
    p = pred if pred is not None and not pred.empty else pd.DataFrame(
        columns=["new_gosb_id", "inn", "base_fl", "pred", "out_class", "note",
                 "out_months", "has_hist"])
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
    # организации без истории в модели приходят из outer-merge с NaN — для флага это «нет»
    m["recovered"] = (m["recovered"].fillna(False).astype(bool) if "recovered" in m
                      else False)
    # тот же случай, но про сам факт наличия истории: у пары, которой в витрине не
    # было ни одного месяца, «месяцев с оттоком нет» означает незнание, а не ровный штат
    m["has_hist"] = (m["has_hist"].fillna(False).astype(bool) if "has_hist" in m
                     else False)
    # то же для колонки-списка: NaN нельзя оставлять, он проходит проверку `or []`
    m["out_months"] = ([v if isinstance(v, list) else [] for v in m["out_months"]]
                       if "out_months" in m else [[] for _ in range(len(m))])
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


def _conv_k(plan: float, fact: float) -> tuple[float, float | None, bool]:
    """Коэффициент реализуемости по паре (план, факт): значение, сырое, упёрся ли."""
    raw = (fact / plan) if plan > 0 else None
    k = float(np.clip(raw if raw is not None else 1.0, CONV_MIN, CONV_MAX))
    return k, raw, raw is not None and not (CONV_MIN <= raw <= CONV_MAX)


def conversion_by_month(plan_m: pd.DataFrame, fact_m: pd.DataFrame, ref_cur,
                        tb_of: dict | None = None) -> dict:
    """Реализуемость пайплайна: ФАКТ продаж против плана по ЗАКРЫТЫМ месяцам.

    Грейн сравнения — (месяц, ГОСБ, ИНН, сотрудник). Это принципиально: сотрудник мог
    завести две сделки по одной организации и обе запланировать на один месяц (10 и 15),
    и пришедшие люди относятся к их СУММЕ (25), а не к каждой сделке по отдельности.
    Группировку даёт запрос, здесь остаётся свернуть по ГОСБ.

    Считается сразу на ТРЁХ уровнях, потому что отчёт строится по всему банку и у
    каждого уровня свой коэффициент: у ГОСБ — свой, у ТБ — свой (он же фолбэк для
    ГОСБ с малым объёмом), у банка — свой. Раньше уровень СБ получал жёсткую
    единицу, и в водопаде банка всегда стояло «коэф. 1.00».

    Текущий месяц исключён: он не отработан, его неполный факт занизил бы коэффициент.

    Диагностика возвращает СЫРОЕ значение до клипа: если реальная конверсия ниже
    CONV_MIN, клип поднимает её до пола и тем самым ЗАВЫШАЕТ вклад пайплайна. Молчать
    об этом нельзя — иначе в логе видно ровно «0.20» и непонятно, это настоящая
    конверсия или сработавшая граница.

    `tb_of` — соответствие ГОСБ → ТБ (единственный источник — queries.GOSB_FLAGS).
    """
    empty_diag = {"months": 0, "plan": 0.0, "fact": 0.0, "tb_raw": None,
                  "tb_clipped": False, "n_gosb": 0, "n_gosb_clipped": 0,
                  "n_gosb_fallback": 0}
    out = {"by_gosb": {}, "by_tb": {}, "of_gosb": {}, "sb": 1.0,
           "diag_tb": {}, "diag_sb": dict(empty_diag)}
    if plan_m is None or plan_m.empty:
        return out
    tb_of = tb_of or {}
    cur = pd.Period(pd.Timestamp(ref_cur), freq="M")
    p = _as_period(plan_m)
    all_gosb = {int(g) for g in p["new_gosb_id"].dropna()}
    p = p[p["per"] < cur]                       # только закрытые месяцы
    if p.empty:
        return out

    keys = ["new_gosb_id", "inn", "saphr_id", "per"]
    if fact_m is not None and not fact_m.empty:
        f = _as_period(fact_m)
        p = p.merge(f[keys + ["fact_np"]], on=keys, how="left")
    p["fact_np"] = num(p, "fact_np")
    p["plan_np"] = pd.to_numeric(p["plan_np"], errors="coerce").fillna(0.0)
    p = p.dropna(subset=["new_gosb_id"]).copy()
    p["tb_id"] = [tb_of.get(int(g)) for g in p["new_gosb_id"]]

    # --- ГОСБ: свой коэффициент только при достаточном объёме плана --------- #
    by_gosb, gosb_clipped, gosb_fallback = {}, {}, {}
    for gid, g in p.groupby("new_gosb_id"):
        tb = tb_of.get(int(gid))
        pl = float(g["plan_np"].sum())
        if pl < CONV_MIN_PLAN:
            # объёма мало — свой коэффициент был бы шумом, ГОСБ уйдёт на коэффициент ТБ
            gosb_fallback[tb] = gosb_fallback.get(tb, 0) + 1
            continue
        k, raw, clipped = _conv_k(pl, float(g["fact_np"].sum()))
        by_gosb[int(gid)] = k
        if clipped:
            gosb_clipped[tb] = gosb_clipped.get(tb, 0) + 1

    # --- ТБ: коэффициент всего ТБ, он же фолбэк его ГОСБ -------------------- #
    n_own = {}
    for gid in by_gosb:
        tb = tb_of.get(int(gid))
        n_own[tb] = n_own.get(tb, 0) + 1
    for tb, g in p.dropna(subset=["tb_id"]).groupby("tb_id"):
        tb = int(tb)
        pl, fc = float(g["plan_np"].sum()), float(g["fact_np"].sum())
        k, raw, clipped = _conv_k(pl, fc)
        out["by_tb"][tb] = k
        out["diag_tb"][tb] = {
            "months": int(g["per"].nunique()), "plan": pl, "fact": fc,
            "tb_raw": raw, "tb_clipped": clipped,
            "n_gosb": n_own.get(tb, 0), "n_gosb_clipped": gosb_clipped.get(tb, 0),
            "n_gosb_fallback": gosb_fallback.get(tb, 0)}

    # --- Банк: тот же расчёт по всем строкам сразу -------------------------- #
    pl, fc = float(p["plan_np"].sum()), float(p["fact_np"].sum())
    k_sb, raw_sb, clipped_sb = _conv_k(pl, fc)
    n_tb_clipped = sum(1 for d in out["diag_tb"].values() if d["tb_clipped"])
    out["sb"] = k_sb
    out["diag_sb"] = {
        "months": int(p["per"].nunique()), "plan": pl, "fact": fc,
        "tb_raw": raw_sb, "tb_clipped": clipped_sb,
        # единица уровня СБ — ТБ, поэтому и «сколько единиц упёрлось» считается по ТБ
        "n_gosb": len(out["by_tb"]), "n_gosb_clipped": n_tb_clipped,
        "n_gosb_fallback": sum(gosb_fallback.values())}

    # Готовый коэффициент КАЖДОГО ГОСБ: свой, иначе своего ТБ, иначе банковский.
    # Собирается здесь, чтобы прогноз пайплайна не знал про иерархию вовсе.
    out["by_gosb"] = by_gosb
    out["of_gosb"] = {gid: by_gosb.get(gid, out["by_tb"].get(tb_of.get(gid), k_sb))
                      for gid in all_gosb}
    return out


def pipeline_current(plan_m: pd.DataFrame, fact_m: pd.DataFrame, ref_cur,
                     conv_of: dict, default_k: float = 1.0,
                     time_left: float = 1.0) -> pd.DataFrame:
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
    # `conv_of` уже разрешён по иерархии (свой ГОСБ → его ТБ → банк) в
    # conversion_by_month: здесь про уровни знать незачем
    agg["conv"] = [conv_of.get(int(g), default_k) for g in agg["new_gosb_id"]]
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
            "out_observed", "pred", "out_class", "note", "recovered", "why", "settled",
            "avg_salary_m", "delta_fl", "out_months", "has_hist"]
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
    m["recovered"] = (m["recovered"].fillna(False).astype(bool) if "recovered" in m
                      else False)
    m["has_hist"] = (m["has_hist"].fillna(False).astype(bool) if "has_hist" in m
                     else False)
    # колонка-список переживает outer-merge только с явной нормализацией: NaN здесь
    # истинно и молча просочился бы в детализацию
    m["out_months"] = ([v if isinstance(v, list) else [] for v in m["out_months"]]
                       if "out_months" in m else [[] for _ in range(len(m))])

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
                 orgs_fc: pd.DataFrame,
                 unit_src: str = "new_gosb_id") -> tuple[pd.DataFrame, dict]:
    """Матрица «единица × сегмент» по ПРОГНОЗУ: план текущего месяца против прогноза.

    Единица разбора — ГОСБ в отчёте ТБ и ТБ в отчёте СБ; здесь она везде называется
    `unit_id`, а `unit_src` говорит, по какой колонке грейна организаций её собирать
    (`new_gosb_id` или `tb_id`). Сама логика от уровня не зависит.

    База — факт закрытого месяца из uzp_dwh_metrics (та же методика, что у плана),
    дельта — с грейна организаций. Дельта организаций, чей сегмент неизвестен или
    отсутствует в плане этой единицы, разносится по её сегментам пропорционально
    базовому факту: иначе сумма ячеек не сойдётся с итогом единицы.
    """
    base = base_seg[["unit_id", "seg_name", "fact_amt"]].copy() \
        if not base_seg.empty else pd.DataFrame(columns=["unit_id", "seg_name", "fact_amt"])
    plan = plan_seg[["unit_id", "seg_name", "plan_amt"]].copy() \
        if not plan_seg.empty else pd.DataFrame(columns=["unit_id", "seg_name", "plan_amt"])
    for f in (base, plan):
        if not f.empty:
            f["unit_id"] = f["unit_id"].astype("int64")
    m = plan.merge(base, on=["unit_id", "seg_name"], how="outer")
    m["plan_amt"] = pd.to_numeric(m["plan_amt"], errors="coerce").fillna(0.0)
    m["fact_amt"] = pd.to_numeric(m["fact_amt"], errors="coerce").fillna(0.0)

    known = {(int(r.unit_id), r.seg_name) for r in m.itertuples()}
    agg: dict = {}
    resid: dict = {}
    stats = {"unattributed": 0.0, "matched": 0.0}
    if orgs_fc is not None and not orgs_fc.empty:
        units = orgs_fc[unit_src]
        for r, u in zip(orgs_fc.itertuples(), units):
            if pd.isna(u):
                continue
            gid = int(u)
            d = (float(r.out_exp), float(r.in_exp), float(r.pipe_np), float(r.pipe_np_raw))
            if (gid, r.seg_name) in known:
                cur = agg.setdefault((gid, r.seg_name), [0.0, 0.0, 0.0, 0.0])
                stats["matched"] += d[2] + d[1] - d[0]
            else:
                cur = resid.setdefault(gid, [0.0, 0.0, 0.0, 0.0])
                stats["unattributed"] += abs(d[0]) + abs(d[1]) + abs(d[2])
            for k in range(4):
                cur[k] += d[k]

    # разнос неатрибутированного остатка по сегментам единицы (пропорционально базе)
    for gid, d in resid.items():
        sub = m[m["unit_id"] == gid]
        total = float(sub["fact_amt"].sum())
        if sub.empty:
            # единицы вообще нет в плановой матрице — разносить некуда; такую дельту
            # считаем потерянной и показываем в диагностике, а не гасим молча
            stats["lost_gosb"] = stats.get("lost_gosb", 0) + 1
            stats["lost_delta"] = stats.get("lost_delta", 0.0) + abs(d[2] + d[1] - d[0])
            continue
        for r in sub.itertuples():
            share = (float(r.fact_amt) / total) if total > 0 else 1.0 / len(sub)
            cur = agg.setdefault((gid, r.seg_name), [0.0, 0.0, 0.0, 0.0])
            for k in range(4):
                cur[k] += d[k] * share

    vals = [agg.get((int(r.unit_id), r.seg_name), [0.0, 0.0, 0.0, 0.0])
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
                 orgs_fc: pd.DataFrame,
                 unit_src: str = "new_gosb_id") -> pd.DataFrame:
    """То же, что build_matrix, но на грейне единицы (без сегмента).

    Отдельная функция, а не свёртка матрицы: план единицы берётся из строки
    «все сегменты» витрины, а она не обязана в точности равняться сумме сегментов.
    """
    base = (base_tot[["unit_id", "unit_name", "fact_amt"]].copy()
            if base_tot is not None and not base_tot.empty
            else pd.DataFrame(columns=["unit_id", "unit_name", "fact_amt"]))
    plan = (plan_tot[["unit_id", "unit_name", "plan_amt"]].copy()
            if plan_tot is not None and not plan_tot.empty
            else pd.DataFrame(columns=["unit_id", "unit_name", "plan_amt"]))
    m = plan.merge(base.drop(columns=["unit_name"]), on="unit_id", how="outer")
    if m.empty:
        return pd.DataFrame(columns=["unit_id", "unit_name", "plan_amt",
                                     "fact_amt", "execution_percent", "nedobor"])
    m["plan_amt"] = pd.to_numeric(m["plan_amt"], errors="coerce").fillna(0.0)
    m["fact_amt"] = pd.to_numeric(m["fact_amt"], errors="coerce").fillna(0.0)
    delta = {}
    if orgs_fc is not None and not orgs_fc.empty:
        g = orgs_fc.dropna(subset=[unit_src]).groupby(unit_src)
        delta = (g["in_exp"].sum() + g["pipe_np"].sum() - g["out_exp"].sum()).to_dict()
    m["base_amt"] = m["fact_amt"]
    m["fact_amt"] = (m["base_amt"]
                     + m["unit_id"].map(lambda x: delta.get(x, 0.0))).clip(lower=0)
    m["execution_percent"] = m["fact_amt"] / m["plan_amt"].replace(0, np.nan)
    m["nedobor"] = m["plan_amt"] - m["fact_amt"]
    return m.sort_values("nedobor", ascending=False).reset_index(drop=True)
