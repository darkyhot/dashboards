"""Расчёты поверх свёрток. Ни одного обращения к БД.

Главное тождество отчёта:

    ΔYoY(m) − ΔYoY(m−1) = [N26(m) − N26(m−1)] − [N25(m) − N25(m−1)]
                       = MoM-2026(m−1→m) − MoM-2025(m−1→m)

Ухудшение годовой динамики в месяце m ровно равно тому, насколько месячный
переход в этом году хуже того же перехода год назад. Оба перехода раскладываются
ОДНОЙ лестницей (`decomp`), и разница считается по слагаемым («разница разниц»).

Одно разложение на весь отчёт (по получателям, в скобках — по ФЛ):

    было − перестали получать ЗП в Сбере + начали получать ЗП в Сбере
         ± совместительство (у продолжающих стало меньше/больше ИНН или ГОСБ) = стало

По ФЛ третье слагаемое всегда ноль. Других разложений в отчёте нет.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import months as M
from . import segments as S

# --------------------------------------------------------------------------- #
# Словарь. Одни подписи на HTML и документ.
# --------------------------------------------------------------------------- #
GONE = ["left_bank", "below_threshold", "other_codes"]
COME = ["new_to_bank", "above_threshold", "back_to_codes"]

T = {
    "left_bank": "Нет зачислений в банке",
    "below_threshold": "Зарплата в банке ниже 2 500 ₽",
    "other_codes": "Только незарплатные выплаты",
    "new_to_bank": "Раньше не было зачислений в банке",
    "above_threshold": "Зарплата выросла выше 2 500 ₽",
    "back_to_codes": "Раньше были только незарплатные выплаты",
    "other_seg_out": "Ушли в другой сегмент",
    "other_seg_in": "Пришли из другого сегмента",
    "inside": "Внутри сегмента: сменили организацию или ГОСБ, число мест работы",
}
T_LOST = "Перестали получать ЗП в Сбере"
T_GAINED = "Начали получать ЗП в Сбере"
T_INSIDE = "Совместительство: у продолжающих изменилось число организаций или ГОСБ"
T_INSIDE_INN = "число организаций у ФЛ"
T_INSIDE_GOSB = "число ГОСБ в одной организации"
DEF_REC = ("Получатель — тройка ФЛ × организация × ГОСБ. Засчитывается, если "
           "зарплатные зачисления ФЛ в эту организацию за месяц больше 2 500 ₽. "
           "Совместитель в двух организациях — два получателя.")
DEF_GETS = ("Получает ЗП в Сбере — у ФЛ в месяце есть хотя бы одна тройка-получатель. "
            "Перестал — нет ни одной, где бы то ни было в банке.")

OUTSIDE = "Вне получателей"     # строка/столбец матрицы перетоков: не получатель ЗП


def _num(df: pd.DataFrame, col: str, default=0.0) -> pd.Series:
    if col in df:
        return pd.to_numeric(df[col], errors="coerce").fillna(default)
    return pd.Series(default, index=df.index, dtype="float64")


def _flag(s: pd.Series) -> pd.Series:
    """Метки 0/1/NULL из SQL → float с NaN (NULL = месяц вне набора)."""
    return pd.to_numeric(s, errors="coerce")


# --------------------------------------------------------------------------- #
# Разложение перехода b → c по ФЛ-потоку (FL_FLOW)
# --------------------------------------------------------------------------- #
def decomp(fl: pd.DataFrame) -> dict:
    """Одно разложение перехода. Все числа — из одной выборки, поэтому сходятся
    ТОЖДЕСТВЕННО; невязка означает ошибку в запросе, а не в данных."""
    if fl is None or fl.empty:
        return {}
    f = fl.copy()
    for c in ("n_epk", "tr_b", "tr_c", "inn_b", "inn_c"):
        f[c] = _num(f, c)
    lost = f[(f["seg_to"] == "—") & (f["seg_from"] != "—")]
    gained = f[(f["seg_from"] == "—") & (f["seg_to"] != "—")]
    stay = f[(f["seg_from"] != "—") & (f["seg_to"] != "—")]
    d = {
        "base_epk": f.loc[f["seg_from"] != "—", "n_epk"].sum(),
        "cur_epk": f.loc[f["seg_to"] != "—", "n_epk"].sum(),
        "base_tr": f["tr_b"].sum(), "cur_tr": f["tr_c"].sum(),
        "lost_epk": lost["n_epk"].sum(), "lost_tr": lost["tr_b"].sum(),
        "gained_epk": gained["n_epk"].sum(), "gained_tr": gained["tr_c"].sum(),
        "inside_tr": (stay["tr_c"] - stay["tr_b"]).sum(),
        "inside_inn": (stay["inn_c"] - stay["inn_b"]).sum(),
        "base_inn": f["inn_b"].sum(), "cur_inn": f["inn_c"].sum(),
    }
    d["inside_gosb"] = d["inside_tr"] - d["inside_inn"]
    d["delta_tr"] = d["cur_tr"] - d["base_tr"]
    d["delta_epk"] = d["cur_epk"] - d["base_epk"]
    d["extra_base"] = d["base_tr"] - d["base_epk"]
    d["extra_cur"] = d["cur_tr"] - d["cur_epk"]
    d["residual_tr"] = d["base_tr"] - d["lost_tr"] + d["gained_tr"] + d["inside_tr"] - d["cur_tr"]
    d["residual_epk"] = d["base_epk"] - d["lost_epk"] + d["gained_epk"] - d["cur_epk"]
    for c in GONE:
        x = lost[lost["cause"] == c]
        d[f"lost_{c}_tr"], d[f"lost_{c}_epk"] = x["tr_b"].sum(), x["n_epk"].sum()
    for c in COME:
        x = gained[gained["cause"] == c]
        d[f"gained_{c}_tr"], d[f"gained_{c}_epk"] = x["tr_c"].sum(), x["n_epk"].sum()
    # Стаж переставших (относительно b) и возврат в следующем месяце.
    ten = lost["tenure"].fillna("unknown") if "tenure" in lost else pd.Series(dtype=str)
    for k in ("new1", "new2", "old", "unknown"):
        x = lost[ten == k]
        d[f"lost_ten_{k}_tr"], d[f"lost_ten_{k}_epk"] = x["tr_b"].sum(), x["n_epk"].sum()
    back = _flag(lost["in_cn1"]) if "in_cn1" in lost else pd.Series(dtype=float)
    d["has_next"] = bool(back.notna().any()) if len(back) else False
    d["lost_back_epk"] = lost.loc[back == 1, "n_epk"].sum() if d["has_next"] else np.nan
    d["lost_back_tr"] = lost.loc[back == 1, "tr_b"].sum() if d["has_next"] else np.nan
    gap = _flag(gained["was_bp1"]) if "was_bp1" in gained else pd.Series(dtype=float)
    d["has_prev"] = bool(gap.notna().any()) if len(gap) else False
    for k, mask in (("gap", gap == 1), ("fresh", gap != 1)):
        x = gained[mask]
        d[f"gained_{k}_tr"], d[f"gained_{k}_epk"] = x["tr_c"].sum(), x["n_epk"].sum()
    stay_n = _flag(gained["in_cn1"]) if "in_cn1" in gained else pd.Series(dtype=float)
    d["gained_stay_next_epk"] = (gained.loc[stay_n == 1, "n_epk"].sum()
                                 if stay_n.notna().any() else np.nan)
    return d


def decomposition(d: dict, b, c) -> list[dict]:
    """ОДНО разложение — строки для шапки, таблиц и документа."""
    if not d:
        return []
    inside_title = T_INSIDE
    return [
        {"key": "base", "title": f"Получателей в {M.prep(b)} {M.parse(b).year}",
         "tr": d["base_tr"], "epk": d["base_epk"]},
        {"key": "lost", "title": T_LOST, "tr": -d["lost_tr"], "epk": -d["lost_epk"]},
        {"key": "gained", "title": T_GAINED, "tr": d["gained_tr"], "epk": d["gained_epk"]},
        {"key": "inside", "title": inside_title, "tr": d["inside_tr"], "epk": 0.0},
        {"key": "inside_inn", "title": "  в т.ч. " + T_INSIDE_INN, "tr": d["inside_inn"],
         "epk": np.nan, "sub": True},
        {"key": "inside_gosb", "title": "  в т.ч. " + T_INSIDE_GOSB, "tr": d["inside_gosb"],
         "epk": np.nan, "sub": True},
        {"key": "cur", "title": f"Получателей в {M.prep(c)} {M.parse(c).year}",
         "tr": d["cur_tr"], "epk": d["cur_epk"]},
        {"key": "delta", "title": "Изменение", "tr": d["delta_tr"], "epk": d["delta_epk"]},
    ]


def causes(d: dict) -> pd.DataFrame:
    """Почему перестали / откуда начали — по получателям и ФЛ."""
    rows = []
    for c in GONE:
        rows.append({"side": "lost", "key": c, "title": T[c],
                     "tr": d.get(f"lost_{c}_tr", 0), "epk": d.get(f"lost_{c}_epk", 0)})
    for c in COME:
        rows.append({"side": "gained", "key": c, "title": T[c],
                     "tr": d.get(f"gained_{c}_tr", 0), "epk": d.get(f"gained_{c}_epk", 0)})
    out = pd.DataFrame(rows)
    for side in ("lost", "gained"):
        m = out["side"] == side
        tot = out.loc[m, "epk"].sum()
        out.loc[m, "share"] = out.loc[m, "epk"] / tot if tot else np.nan
    return out


# --------------------------------------------------------------------------- #
# Матрица перетоков между сегментами (ФЛ по основному сегменту)
# --------------------------------------------------------------------------- #
def flow_matrix(fl: pd.DataFrame) -> pd.DataFrame:
    if fl is None or fl.empty:
        return pd.DataFrame()
    f = fl.copy()
    f["n_epk"] = _num(f, "n_epk")
    f["from"] = f["seg_from"].replace("—", OUTSIDE)
    f["to"] = f["seg_to"].replace("—", OUTSIDE)
    mx = f.pivot_table(index="from", columns="to", values="n_epk", aggfunc="sum", fill_value=0)
    order = S.ordered(set(mx.index) | set(mx.columns) - {OUTSIDE})
    order = [s for s in order if s != OUTSIDE] + [OUTSIDE]
    mx = mx.reindex(index=order, columns=order, fill_value=0)
    mx.loc[OUTSIDE, OUTSIDE] = 0
    return mx


def seg_fl(mx: pd.DataFrame) -> pd.DataFrame:
    """Численность ФЛ по основному сегменту: было, стало, ушли/пришли вне, перетоки."""
    if mx.empty:
        return pd.DataFrame()
    rows = []
    for s in mx.index:
        if s == OUTSIDE:
            continue
        rows.append({"seg": s,
                     "base": mx.loc[s].sum(),
                     "cur": mx[s].sum(),
                     "stopped": mx.loc[s, OUTSIDE],
                     "started": mx.loc[OUTSIDE, s],
                     "to_other": mx.loc[s].drop([s, OUTSIDE]).sum(),
                     "from_other": mx[s].drop([s, OUTSIDE]).sum()})
    out = pd.DataFrame(rows)
    out["delta"] = out["cur"] - out["base"]
    return out


# --------------------------------------------------------------------------- #
# Разложение по сегментам — по получателям (TRIPLE_FLOW)
# --------------------------------------------------------------------------- #
SEG_COLS = [("stopped", "Перестали в Сбере"), ("started", "Начали в Сбере"),
            ("other_seg", "Перетоки между сегментами"), ("inside", "Внутри сегмента")]


def seg_decomp(tr: pd.DataFrame, base: pd.Series, cur: pd.Series) -> pd.DataFrame:
    """Изменение получателей сегмента = −перестали + начали ± перетоки ± внутри.

    `base`/`cur` — численность сегмента в b и c (из итогов набора), чтобы тождество
    проверялось против НЕЗАВИСИМО посчитанных итогов, а не против самих себя.
    """
    if tr is None or tr.empty:
        return pd.DataFrame()
    t = tr.copy()
    t["n_triples"] = _num(t, "n_triples")
    rows = []
    for s in S.ordered(set(t["seg"]) | set(base.index) | set(cur.index)):
        x = t[t["seg"] == s]
        lo, ga = x[x["side"] == "lost"], x[x["side"] == "gained"]
        r = {"seg": s, "base": float(base.get(s, 0)), "cur": float(cur.get(s, 0)),
             "stopped": -lo.loc[lo["cause"].isin(GONE), "n_triples"].sum(),
             "started": ga.loc[ga["cause"].isin(COME), "n_triples"].sum(),
             "other_seg": (ga.loc[ga["cause"] == "other_seg", "n_triples"].sum()
                           - lo.loc[lo["cause"] == "other_seg", "n_triples"].sum()),
             "inside": (ga.loc[ga["cause"] == "inside", "n_triples"].sum()
                        - lo.loc[lo["cause"] == "inside", "n_triples"].sum())}
        for c in GONE:
            r[f"lost_{c}"] = lo.loc[lo["cause"] == c, "n_triples"].sum()
        for c in COME:
            r[f"gained_{c}"] = ga.loc[ga["cause"] == c, "n_triples"].sum()
        rows.append(r)
    out = pd.DataFrame(rows)
    out["delta"] = out["cur"] - out["base"]
    out["residual"] = out["base"] + out[[c for c, _ in SEG_COLS]].sum(axis=1) - out["cur"]
    return out


# --------------------------------------------------------------------------- #
# Разница разниц
# --------------------------------------------------------------------------- #
def did_components(dc: dict, dp: dict, c_cur, c_prev) -> pd.DataFrame:
    """Слагаемые месячного перехода в этом году против того же перехода год назад.

    Уровни: группа (перестали / начали / совместительство) и её разбивка.
    Знак: вклад в изменение числа получателей (перестали — минус).
    """
    b_cur = M.shift(c_cur, -1)
    nm1, nm2 = M.prep(b_cur), M.prep(M.shift(b_cur, -1))

    def g(d, k):
        return float(d.get(k, 0) or 0)

    rows = [
        ("lost", None, T_LOST, -g(dc, "lost_tr"), -g(dp, "lost_tr")),
        ("lost", "new1", f"пришли в {nm1}", -g(dc, "lost_ten_new1_tr"), -g(dp, "lost_ten_new1_tr")),
        ("lost", "new2", f"пришли в {nm2}", -g(dc, "lost_ten_new2_tr"), -g(dp, "lost_ten_new2_tr")),
        ("lost", "old", f"получали и до {M.gen(M.shift(b_cur, -1))}",
         -g(dc, "lost_ten_old_tr"), -g(dp, "lost_ten_old_tr")),
        ("lost", "unknown", "стаж неизвестен (нет месяцев в наборе)",
         -g(dc, "lost_ten_unknown_tr"), -g(dp, "lost_ten_unknown_tr")),
        ("gained", None, T_GAINED, g(dc, "gained_tr"), g(dp, "gained_tr")),
        ("gained", "gap", f"вернулись после перерыва в {M.prep(b_cur)}",
         g(dc, "gained_gap_tr"), g(dp, "gained_gap_tr")),
        ("gained", "fresh", "впервые за два месяца", g(dc, "gained_fresh_tr"), g(dp, "gained_fresh_tr")),
        ("inside", None, T_INSIDE, g(dc, "inside_tr"), g(dp, "inside_tr")),
        ("inside", "inn", T_INSIDE_INN, g(dc, "inside_inn"), g(dp, "inside_inn")),
        ("inside", "gosb", T_INSIDE_GOSB, g(dc, "inside_gosb"), g(dp, "inside_gosb")),
    ]
    out = pd.DataFrame(rows, columns=["group", "sub", "title", "cur", "prev"])
    out["diff"] = out["cur"] - out["prev"]
    # Пустые подстроки (например, стаж «неизвестен», когда он везде известен) не нужны.
    keep = out["sub"].isna() | (out[["cur", "prev"]].abs().sum(axis=1) > 0)
    return out[keep].reset_index(drop=True)


def did_lost_causes(dc: dict, dp: dict) -> pd.DataFrame:
    rows = []
    for c in GONE:
        rows.append({"title": T[c], "cur": -float(dc.get(f"lost_{c}_tr", 0)),
                     "prev": -float(dp.get(f"lost_{c}_tr", 0))})
    for c in COME:
        rows.append({"title": T[c], "cur": float(dc.get(f"gained_{c}_tr", 0)),
                     "prev": float(dp.get(f"gained_{c}_tr", 0))})
    out = pd.DataFrame(rows)
    out["diff"] = out["cur"] - out["prev"]
    return out


def did_segments(sc: pd.DataFrame, sp: pd.DataFrame) -> pd.DataFrame:
    """Сегмент × слагаемое: разница месячных переходов двух лет."""
    if sc.empty or sp.empty:
        return pd.DataFrame()
    cols = [c for c, _ in SEG_COLS] + ["delta"]
    a = sc.set_index("seg")[cols]
    b = sp.set_index("seg")[cols]
    idx = S.ordered(set(a.index) | set(b.index))
    a, b = a.reindex(idx, fill_value=0), b.reindex(idx, fill_value=0)
    out = (a - b).reset_index()
    return out


# --------------------------------------------------------------------------- #
# Ряд, обрывы, сезонный профиль
# --------------------------------------------------------------------------- #
STEP_MAD = 3.5


def series_frame(series: pd.DataFrame) -> pd.DataFrame:
    """Помесячно: банк и сегменты, получатели и ФЛ."""
    if series.empty:
        return pd.DataFrame()
    s = series.copy()
    s["report_dt"] = s["report_dt"].astype(str)
    for c in ("n_triples", "n_epk", "n_inn", "t0", "t1000", "t5000", "t10000"):
        s[c] = _num(s, c)
    return s.sort_values(["report_dt", "seg"]).reset_index(drop=True)


def bank_series(sf: pd.DataFrame) -> pd.DataFrame:
    b = sf[sf["seg"] == "__ALL__"][["report_dt", "n_triples", "n_epk", "n_inn",
                                      "t0", "t1000", "t5000", "t10000"]].copy()
    b = b.set_index("report_dt").sort_index()
    b["extra"] = b["n_triples"] - b["n_epk"]
    idx = list(b.index)
    yoy, mom = {}, {}
    for d in idx:
        p12, p1 = M.iso(M.shift(d, -12)), M.iso(M.shift(d, -1))
        if p12 in b.index:
            yoy[d] = b.loc[d, "n_triples"] - b.loc[p12, "n_triples"]
        if p1 in b.index:
            mom[d] = b.loc[d, "n_triples"] - b.loc[p1, "n_triples"]
    b["yoy"] = pd.Series(yoy)
    b["yoy_pct"] = b["yoy"] / (b["n_triples"] - b["yoy"])
    b["mom"] = pd.Series(mom)
    b["mom_pct"] = b["mom"] / (b["n_triples"] - b["mom"])
    b["yoy_epk"] = pd.Series({d: b.loc[d, "n_epk"] - b.loc[M.iso(M.shift(d, -12)), "n_epk"]
                              for d in idx if M.iso(M.shift(d, -12)) in b.index})
    return b.reset_index()


def seg_pivot(sf: pd.DataFrame, value: str = "n_triples") -> pd.DataFrame:
    x = sf[sf["seg"] != "__ALL__"].pivot_table(index="seg", columns="report_dt",
                                                values=value, aggfunc="sum")
    return x.reindex(S.ordered(x.index))


def yoy_pct_table(pv: pd.DataFrame) -> pd.DataFrame:
    """Сегмент × месяц: изменение к тому же месяцу год назад, %."""
    out = {}
    for d in pv.columns:
        p = M.iso(M.shift(d, -12))
        if p in pv.columns:
            out[d] = (pv[d] - pv[p]) / pv[p]
    return pd.DataFrame(out)


def yoy_abs_table(pv: pd.DataFrame) -> pd.DataFrame:
    out = {}
    for d in pv.columns:
        p = M.iso(M.shift(d, -12))
        if p in pv.columns:
            out[d] = pv[d] - pv[p]
    return pd.DataFrame(out)


def mom_profile(bs: pd.DataFrame) -> pd.DataFrame:
    """Сезонный профиль: изменение к прошлому месяцу по календарным месяцам двух лет."""
    if bs.empty:
        return pd.DataFrame()
    x = bs.dropna(subset=["mom"]).copy()
    x["cal"] = [M.parse(d).month for d in x["report_dt"]]
    last = M.parse(x["report_dt"].max())
    # «Год» профиля — 12 месяцев, кончающихся последним месяцем ряда, и предыдущие 12.
    x["k"] = [(last.year * 12 + last.month - (M.parse(d).year * 12 + M.parse(d).month)) // 12
              for d in x["report_dt"]]
    x = x[x["k"] <= 1]
    return x[["report_dt", "cal", "k", "mom", "mom_pct"]]


def steps(bs: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Месяцы-обрывы: изменение год к году отклоняется от медианы больше чем на
    STEP_MAD медианных абсолютных отклонений. Год к году, а не к соседнему месяцу:
    иначе каждый январь и август — «событие». Нулевой MAD (одна ступень на ровном
    ряде) огрубляется до среднего отклонения, иначе пропустим ровно тот случай,
    ради которого функция написана."""
    if bs.empty:
        return pd.DataFrame(), "ряд не построен"
    yo = bs.dropna(subset=["yoy"])
    use, measured = ("yoy", "год к году") if len(yo) >= 3 else ("mom", "к предыдущему месяцу")
    x = bs.dropna(subset=[use]).copy()
    if len(x) < 3:
        return pd.DataFrame(), measured
    v = x[use].diff().dropna()
    if v.empty:
        return pd.DataFrame(), measured
    med = v.median()
    mad = (v - med).abs().median()
    if not mad:
        mad = (v - med).abs().mean()
    if not mad:
        return pd.DataFrame(), measured
    score = (v - med) / mad
    hit = score[score.abs() > STEP_MAD]
    out = pd.DataFrame({"report_dt": x.loc[hit.index, "report_dt"],
                        "delta": v.loc[hit.index], "score": hit})
    return out.reset_index(drop=True), measured


def load_health(load: pd.DataFrame) -> pd.DataFrame:
    if load.empty:
        return pd.DataFrame()
    x = load.copy()
    x["report_dt"] = x["report_dt"].astype(str)
    for c in ("n_rows", "n_inn_ok", "n_no_epk"):
        x[c] = _num(x, c)
    x = x.sort_values("report_dt")
    med = x["n_rows"].median()
    x["bad_inn_share"] = 1 - x["n_inn_ok"] / x["n_rows"].where(x["n_rows"] > 0)
    x["underloaded"] = x["n_rows"] < 0.5 * med
    return x


def thresholds(bs: pd.DataFrame, report: list, amt_min: int) -> pd.DataFrame:
    rows = []
    b = bs.set_index("report_dt")
    for m in report:
        m, p = M.iso(m), M.iso(M.shift(m, -12))
        if m not in b.index or p not in b.index:
            continue
        for col, thr in (("t0", 0), ("t1000", 1000), ("n_triples", amt_min),
                         ("t5000", 5000), ("t10000", 10000)):
            rows.append({"report_dt": m, "threshold": thr, "base": b.loc[p, col],
                         "cur": b.loc[m, col], "delta": b.loc[m, col] - b.loc[p, col]})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Совместительство
# --------------------------------------------------------------------------- #
def multi_bank(mb: pd.DataFrame) -> pd.DataFrame:
    if mb.empty:
        return pd.DataFrame()
    x = mb.copy()
    x["report_dt"] = x["report_dt"].astype(str)
    for c in x.columns:
        if c != "report_dt":
            x[c] = _num(x, c)
    x["extra"] = x["n_triples"] - x["n_epk"]
    return x.sort_values("report_dt").reset_index(drop=True)


def multi_seg(ms: pd.DataFrame) -> pd.DataFrame:
    if ms.empty:
        return pd.DataFrame()
    x = ms.copy()
    x["report_dt"] = x["report_dt"].astype(str)
    for c in ("n_epk", "n_triples", "n_pairs", "extra_inn", "extra_gosb"):
        x[c] = _num(x, c)
    x["extra"] = x["n_triples"] - x["n_epk"]
    return x


def multi_yoy(mb: pd.DataFrame, ms: pd.DataFrame, report: list) -> pd.DataFrame:
    """Изменение «лишних» получателей год к году: банк и сегменты, ИНН и ГОСБ."""
    rows = []
    for m in report:
        m, p = M.iso(m), M.iso(M.shift(m, -12))
        a, b = mb[mb["report_dt"] == m], mb[mb["report_dt"] == p]
        if not a.empty and not b.empty:
            rows.append({"report_dt": m, "seg": "Банк",
                         "extra_inn": float(a["extra_inn"].iloc[0] - b["extra_inn"].iloc[0]),
                         "extra_gosb": float(a["extra_gosb"].iloc[0] - b["extra_gosb"].iloc[0]),
                         "extra": float(a["extra"].iloc[0] - b["extra"].iloc[0]),
                         "share_base": float(b["extra"].iloc[0] / b["n_epk"].iloc[0]),
                         "share_cur": float(a["extra"].iloc[0] / a["n_epk"].iloc[0])})
        for s in S.ordered(set(ms["seg"])):
            a = ms[(ms["report_dt"] == m) & (ms["seg"] == s)]
            b = ms[(ms["report_dt"] == p) & (ms["seg"] == s)]
            if a.empty or b.empty:
                continue
            rows.append({"report_dt": m, "seg": s,
                         "extra_inn": float(a["extra_inn"].iloc[0] - b["extra_inn"].iloc[0]),
                         "extra_gosb": float(a["extra_gosb"].iloc[0] - b["extra_gosb"].iloc[0]),
                         "extra": float(a["extra"].iloc[0] - b["extra"].iloc[0]),
                         "share_base": float(b["extra"].iloc[0] / b["n_epk"].iloc[0]),
                         "share_cur": float(a["extra"].iloc[0] / a["n_epk"].iloc[0])})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Когорты пришедших
# --------------------------------------------------------------------------- #
def cohort_table(raw: dict) -> pd.DataFrame:
    """raw: {(kind, k): df(seg, report_dt, n_alive)} → строка на когорту:
    размер и доля живых через 1, 2, 3 месяца (банк)."""
    rows = []
    for (kind, k), df in raw.items():
        if df is None or df.empty:
            continue
        x = df.copy()
        x["report_dt"] = x["report_dt"].astype(str)
        x["n_alive"] = _num(x, "n_alive")
        tot = x.groupby("report_dt")["n_alive"].sum()
        size = float(tot.get(M.iso(k), 0))
        r = {"kind": kind, "k": M.iso(k), "size": size}
        for h in (1, 2, 3):
            d = M.iso(M.shift(k, h))
            r[f"alive{h}"] = float(tot[d]) if d in tot.index else np.nan
            r[f"surv{h}"] = r[f"alive{h}"] / size if size and d in tot.index else np.nan
        rows.append(r)
    return pd.DataFrame(rows).sort_values(["kind", "k"]).reset_index(drop=True) if rows else pd.DataFrame()


def cohort_seg(raw: dict, kind: str, k, h: int) -> pd.DataFrame:
    df = raw.get((kind, M.parse(k)))
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x["report_dt"] = x["report_dt"].astype(str)
    x["n_alive"] = _num(x, "n_alive")
    size = x[x["report_dt"] == M.iso(k)].set_index("seg")["n_alive"]
    alive = x[x["report_dt"] == M.iso(M.shift(k, h))].set_index("seg")["n_alive"]
    out = pd.DataFrame({"size": size, "alive": alive}).fillna(0)
    out["surv"] = out["alive"] / out["size"].where(out["size"] > 0)
    return out.reindex(S.ordered(out.index)).reset_index().rename(columns={"index": "seg"})


def dissolved(ct: pd.DataFrame, report: list) -> pd.DataFrame:
    """Растворились ли пришедшие: для когорт месяц-к-месяцу этого года — сколько
    дожило до последнего отчётного месяца против того же у когорт год назад.
    «Лишняя убыль» = размер когорты × (доживаемость год назад − в этом году)."""
    if ct.empty:
        return pd.DataFrame()
    last = M.parse(report[-1])
    rows = []
    mom = ct[ct["kind"] == "mom"].set_index("k")
    for k in mom.index:
        kd = M.parse(k)
        if kd.year != last.year or kd > last:
            continue
        h = (last.year * 12 + last.month) - (kd.year * 12 + kd.month)
        pk = M.iso(M.shift(kd, -12))
        if pk not in mom.index:
            continue
        def surv(row, hh):
            if hh == 0:
                return 1.0
            return row.get(f"surv{hh}", np.nan)
        s_cur, s_prev = surv(mom.loc[k], h), surv(mom.loc[pk], h)
        rows.append({"k": k, "h": h, "size_cur": mom.loc[k, "size"], "size_prev": mom.loc[pk, "size"],
                     "surv_cur": s_cur, "surv_prev": s_prev,
                     "excess_loss": mom.loc[k, "size"] * (s_prev - s_cur)
                     if pd.notna(s_cur) and pd.notna(s_prev) else np.nan})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Сезонность, коды, территория
# --------------------------------------------------------------------------- #
def seasonal_table(raw: dict, have_next: dict) -> pd.DataFrame:
    rows = []
    for m, df in raw.items():
        if df is None or df.empty:
            continue
        x = df.copy()
        for c in x.columns:
            if c != "seg":
                x[c] = _num(x, c)
        tot = x.drop(columns="seg").sum()
        r = {"report_dt": M.iso(m), **{k: float(v) for k, v in tot.items()}}
        r["share_seasonal"] = r["n_seasonal"] / r["n_gone_cur"] if r["n_gone_cur"] else np.nan
        r["back_cur_share"] = (r["n_back_cur"] / r["n_gone_cur"]
                               if have_next.get(M.iso(m)) and r["n_gone_cur"] else np.nan)
        r["back_base_share"] = r["n_back_base"] / r["n_gone_base"] if r["n_gone_base"] else np.nan
        rows.append(r)
    return pd.DataFrame(rows)


def code_table(cm: pd.DataFrame, m) -> pd.DataFrame:
    """Зарплатные коды: ФЛ (сумма по сегментам) в m, m−1 и год назад; разница разниц."""
    if cm.empty:
        return pd.DataFrame()
    x = cm.copy()
    x["report_dt"] = x["report_dt"].astype(str)
    x["n_epk"] = _num(x, "n_epk")
    pv = x.pivot_table(index=["code", "code_name"], columns="report_dt", values="n_epk",
                       aggfunc="sum", fill_value=0)
    m, mp = M.iso(m), M.iso(M.shift(m, -1))
    y, yp = M.iso(M.shift(m, -12)), M.iso(M.shift(m, -13))
    if not all(c in pv.columns for c in (m, mp, y, yp)):
        return pd.DataFrame()
    out = pd.DataFrame({"cur": pv[m], "cur_prev": pv[mp], "base": pv[y], "base_prev": pv[yp]})
    out["yoy"] = out["cur"] - out["base"]
    out["mom_cur"] = out["cur"] - out["cur_prev"]
    out["mom_prev"] = out["base"] - out["base_prev"]
    out["did"] = out["mom_cur"] - out["mom_prev"]
    out = out.reset_index()
    out["code_name"] = out["code_name"].fillna(out["code"].astype(str))
    return out.sort_values("did").reset_index(drop=True)


def tb_table(tbs: pd.DataFrame, tbd: pd.DataFrame, m) -> pd.DataFrame:
    """ТБ × сегмент: изменение получателей год к году."""
    if tbs.empty:
        return pd.DataFrame()
    x = tbs.copy()
    x["report_dt"] = x["report_dt"].astype(str)
    x["n_triples"] = _num(x, "n_triples")
    names = dict(zip(tbd["tb_id"], tbd["tb_short_name"])) if not tbd.empty else {}
    x["tb"] = [names.get(t, f"ТБ № {int(t)}" if pd.notna(t) else "ТБ не указан") for t in x["tb_id"]]
    m, p = M.iso(m), M.iso(M.shift(m, -12))
    a = x[x["report_dt"] == m].pivot_table(index="tb", columns="seg", values="n_triples", aggfunc="sum")
    b = x[x["report_dt"] == p].pivot_table(index="tb", columns="seg", values="n_triples", aggfunc="sum")
    idx = sorted(set(a.index) | set(b.index))
    cols = S.ordered(set(a.columns) | set(b.columns))
    a = a.reindex(index=idx, columns=cols).fillna(0)
    b = b.reindex(index=idx, columns=cols).fillna(0)
    d = a - b
    d["Итого"] = d.sum(axis=1)
    d["base_total"] = b.sum(axis=1)
    return d.sort_values("Итого")


# --------------------------------------------------------------------------- #
# Организации
# --------------------------------------------------------------------------- #
ORG_CLS = {
    "cut": "Реальное сокращение (в списке)",
    "down_small": "Снизились, но мало ушло из Сбера",
    "down_moved": "Снизились только за счёт перетока",
    "flat": "Нетто ноль (сколько ушло, столько пришло)",
    "grew": "Выросли",
    "reorg": "Реорганизация: ушли в организацию-приёмник",
    "small": "Малые (база меньше порога)",
}


def org_list(df: pd.DataFrame, tbd: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    names = dict(zip(tbd["tb_id"], tbd["tb_short_name"])) if not tbd.empty else {}
    x["tb"] = [names.get(t, f"ТБ № {int(t)}" if pd.notna(t) else "—") for t in x["tb_id"]]
    for c in ("base_fl", "cur_fl", "net", "net_ex_reorg", "out_stopped", "out_left_bank",
              "out_below", "out_other_codes", "out_moved", "out_reorg", "in_new", "in_moved",
              "in_reorg", "real_cut"):
        x[c] = _num(x, c)
    x["real_share"] = x["real_cut"] / x["base_fl"]
    x["company_name"] = x["company_name"].fillna("Организация не в справочнике")
    return x


def org_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    for c in x.columns:
        if c not in ("cls", "seg"):
            x[c] = _num(x, c)
    g = x.groupby("cls")[[c for c in x.columns if c not in ("cls", "seg")]].sum()
    g = g.reindex([k for k in ORG_CLS if k in g.index])
    g["title"] = [ORG_CLS[k] for k in g.index]
    return g.reset_index()


# --------------------------------------------------------------------------- #
# Вывод по правилам
# --------------------------------------------------------------------------- #
def pattern(yoy: dict) -> str:
    """Тип картины по году к году отчётных месяцев: знаки и замедление."""
    ks = sorted(yoy)
    vals = [yoy[k] for k in ks]
    signs = "".join("+" if v > 0 else ("−" if v < 0 else "0") for v in vals)
    worse = [M.name(ks[i]) for i in range(1, len(ks)) if vals[i] < vals[i - 1]]
    if worse:
        return f"{signs}; хуже предыдущего месяца: {', '.join(worse)}"
    return f"{signs}; ухудшения от месяца к месяцу нет"


def drivers(did: pd.DataFrame, n: int = 3) -> list[dict]:
    """Крупнейшие по модулю слагаемые разницы разниц (подстроки, а не группы)."""
    if did.empty:
        return []
    sub = did[did["sub"].notna()].copy()
    sub["abs"] = sub["diff"].abs()
    sub = sub.sort_values("abs", ascending=False)
    grp = {"lost": "перестали", "gained": "начали", "inside": "совместительство"}
    return [{"title": f"{grp[r.group]}: {r.title}", "diff": r.diff, "cur": r.cur, "prev": r.prev}
            for r in sub.head(n).itertuples()]


# --------------------------------------------------------------------------- #
# Проверки сходимости
# --------------------------------------------------------------------------- #
def check(name: str, value: float, tol: float = 0.5, detail: str = "") -> dict:
    ok = bool(pd.notna(value) and abs(value) <= tol)
    return {"check": name, "ok": ok, "residual": float(value) if pd.notna(value) else None,
            "detail": detail}



# --------------------------------------------------------------------------- #
# Август: потеря или перенос в следующий месяц
# --------------------------------------------------------------------------- #
def bridge(mb: pd.DataFrame, m) -> pd.DataFrame:
    """Мост m−1 → m → m+1 для этого года и года назад: уровни, месяц к месяцу,
    год к году, разница переходов. Строки — показатели, столбцы — месяцы."""
    t = mb.set_index("report_dt")["n_triples"]
    cols = {"prev": M.shift(m, -1), "cur": M.parse(m), "next": M.shift(m, 1)}
    need = [M.iso(d) for d in cols.values()] + [M.iso(M.shift(d, -12)) for d in cols.values()]
    if any(d not in t.index for d in need):
        return pd.DataFrame()
    rows = []
    v26 = {k: float(t[M.iso(d)]) for k, d in cols.items()}
    v25 = {k: float(t[M.iso(M.shift(d, -12))]) for k, d in cols.items()}
    rows.append({"key": "lvl_cur", **v26})
    rows.append({"key": "lvl_prev", **v25})
    rows.append({"key": "yoy", **{k: v26[k] - v25[k] for k in cols}})
    mom26 = {"prev": np.nan, "cur": v26["cur"] - v26["prev"], "next": v26["next"] - v26["cur"]}
    mom25 = {"prev": np.nan, "cur": v25["cur"] - v25["prev"], "next": v25["next"] - v25["cur"]}
    rows.append({"key": "mom_cur", **mom26})
    rows.append({"key": "mom_prev", **mom25})
    rows.append({"key": "did", **{k: mom26[k] - mom25[k] for k in cols}})
    out = pd.DataFrame(rows)
    out.attrs["months"] = {k: M.iso(d) for k, d in cols.items()}
    out.attrs["two"] = {"cur": v26["next"] - v26["prev"], "prev": v25["next"] - v25["prev"]}
    return out


def temp_perm(dc: dict, dp: dict) -> pd.DataFrame:
    """Переставшие в отчётном месяце: вернулись в следующем (временно) или нет."""
    rows = []
    for key, title, fc, fp in (
            ("temp", "вернулись в следующем месяце (временно)",
             dc.get("lost_back_tr"), dp.get("lost_back_tr")),
            ("perm", "не вернулись",
             dc["lost_tr"] - dc.get("lost_back_tr", 0), dp["lost_tr"] - dp.get("lost_back_tr", 0)),
            ("all", T_LOST, dc["lost_tr"], dp["lost_tr"])):
        rows.append({"key": key, "title": title, "cur": -float(fc), "prev": -float(fp)})
    out = pd.DataFrame(rows)
    out["diff"] = out["cur"] - out["prev"]
    return out


def august_verdict(br: pd.DataFrame, tp: pd.DataFrame) -> str:
    """Одна фраза по правилу: вернулся ли минус отчётного месяца к следующему."""
    if br.empty:
        return ""
    b = br.set_index("key")
    did_cur, did_next = b.loc["did", "cur"], b.loc["did", "next"]
    two = br.attrs["two"]["cur"] - br.attrs["two"]["prev"]
    if did_cur < 0 and did_next >= -did_cur:
        return (f"минус {M.gen(br.attrs['months']['cur'])} к {M.prep(br.attrs['months']['next'])} вернулся с "
                f"избытком: за два месяца этот год лучше прошлого на {two:,.0f}. Это сдвиг во времени, а не "
                f"потеря людей.").replace(",", "\u00a0")
    if did_cur < 0 and did_next > 0:
        return (f"к {M.prep(br.attrs['months']['next'])} вернулась часть минуса: "
                f"{did_next:,.0f} из {-did_cur:,.0f}.").replace(",", "\u00a0")
    if did_cur < 0:
        return "в следующем месяце минус не вернулся — это потеря, а не сдвиг."
    return ""


def hole_summary(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"n_orgs": 0, "sum_hole": 0.0}
    return {"n_orgs": int(df["n_orgs"].iloc[0]), "sum_hole": float(df["sum_hole"].iloc[0])}
