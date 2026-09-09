"""Шаг 2: расчёты. Ведомства, территории, тенденции, причины, конкуренты.

Главная содержательная идея файла — РАЗЛОЖЕНИЕ ПАДЕНИЯ ЧИСЛЕННОСТИ НА ПРИЧИНЫ,
см. `causes()`. Всё остальное — разрезы вокруг него.

Две меры оттока, которые нельзя путать (обе нужны, и они не равны):

* **валовый отток за месяц** (`out_qty`) и **невозвращённый** (`out_kept`) — из
  витрины оттока; отвечает на вопрос «сколько человек ушло»;
* **чистое падение численности за окно** (`drop`) — из витрины организаций;
  отвечает на вопрос «на сколько уменьшился портфель». Приход новых получателей
  первый показатель не видит, а второй учитывает.

Причины раскладываются по ВТОРОЙ мере: только у неё есть штат, а без штата
сокращение штата от ухода к конкуренту не отличить.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from uzp_dash import progress

from . import agency as AG

# Окно, на котором меряется падение численности при разборе причин. Шесть месяцев:
# на одном месяце шум кадрового учёта сравним с самим эффектом, на годе — картина
# размывается сменой руководства и реорганизациями.
CAUSE_WINDOW_M = 6
# Доля, с которой причина считается доминирующей. Ниже — «смешанная»: приписать
# организацию к одной причине при 55/45 значило бы выдать шум за вывод.
CAUSE_DOMINANT = 0.60
# Материальность: ведомства и регионы мельче этого вклада в отдельную строку не
# выносятся, а сворачиваются в «прочие» — с честной подписью, сколько свёрнуто.
MATERIAL_SHARE = 0.01

CAUSE_STAFF = "Сокращение штата"
CAUSE_COMPETITOR = "Уход к конкуренту"
CAUSE_MIXED = "Смешанная причина"
CAUSE_NONE = "Численность не падала"


def num(df: pd.DataFrame, col: str, default=0.0) -> pd.Series:
    """Числовая колонка или колонка из значений по умолчанию — ВСЕГДА Series.

    У отсутствующей колонки `df.get()` возвращает скаляр nan, и следующий
    `.fillna()` падает с AttributeError уже где-то дальше по коду.
    """
    if col in df:
        return pd.to_numeric(df[col], errors="coerce").fillna(default)
    return pd.Series(default, index=df.index, dtype="float64")


def _check(name: str, left: float, right: float, tol: float = 0.5) -> dict:
    """Сходимость: сумма частей обязана совпадать с итогом.

    Расхождение не прячется — печатается в прогресс и уезжает в отчёт.
    """
    residual = float(left) - float(right)
    ok = abs(residual) <= tol
    if not ok:
        progress.warn(f"не сходится «{name}»: невязка {residual:+.2f} — "
                      f"часть эффекта потеряна")
    return {"name": name, "left": float(left), "right": float(right),
            "residual": residual, "ok": ok}


# --------------------------------------------------------------------------- #
# Разметка: ведомство и территория
# --------------------------------------------------------------------------- #

def enrich(fact: pd.DataFrame, names: pd.DataFrame, gosb: pd.DataFrame,
           competitors: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Приклеить к строкам оттока ведомство, территорию и конкурента.

    Ведомство определяется ОДИН раз на организацию, а не на строку: одна и та же
    организация встречается в нескольких месяцах и может обслуживаться в двух
    ГОСБ. Разное ведомство у её строк развалило бы сходимость сумм.
    """
    df = fact.copy()

    org = pd.DataFrame({"inn": df["inn"].unique()})
    if not names.empty:
        org = org.merge(names, on="inn", how="left")
    if not competitors.empty:
        org = org.merge(competitors, on="inn", how="left")
    # Силовой флаг витрины перевешивает разбор имени — берём его по организации
    force = df.groupby("inn")["is_force"].max() if "is_force" in df else None
    if force is not None:
        org = org.merge(force.rename("is_force").reset_index(), on="inn", how="left")

    org["agency"] = AG.classify_frame(org)
    known, total, share = AG.coverage(org["agency"])
    progress.done(f"ведомства: разобрано {known:,} из {total:,} организаций "
                  f"({share:.0%}); «{AG.UNKNOWN}» — {total - known:,}")

    keep = [c for c in ("inn", "agency", "company_name", "holding_name",
                        "bank_competitor", "captive_bank_name", "strategy_name",
                        "industry_name") if c in org]
    df = df.merge(org[keep], on="inn", how="left")
    df["agency"] = df["agency"].fillna(AG.UNKNOWN)

    if not gosb.empty:
        df = df.merge(gosb[["new_gosb_id", "tb_short_name", "gosb_name", "region_name"]],
                      on="new_gosb_id", how="left")
    # Неизвестная территория подписывается словами, а не прочерком. Прочерк
    # уходит в текст вывода как «регион "—"» — читателю это ничего не говорит,
    # а модель принимает его за название и строит на нём фразу.
    for c, label in (("tb_short_name", "ТБ не указан"),
                     ("gosb_name", "ГОСБ не указан"),
                     ("region_name", "Регион не указан")):
        if c in df:
            df[c] = df[c].replace("", pd.NA).fillna(label)
    df["subject_code"] = df.get("subject_code", pd.Series("", index=df.index)).fillna("")

    meta = {"agency_known": known, "agency_total": total, "agency_share": share}
    return df, meta


def report_month(df: pd.DataFrame, col: str = "report_dt") -> pd.Timestamp:
    """Последний месяц выборки. Период задаётся параметром — здесь только край."""
    return pd.to_datetime(df[col]).max()


# --------------------------------------------------------------------------- #
# 1. Где основной отток и в каких ведомствах
# --------------------------------------------------------------------------- #

def by_agency(df: pd.DataFrame, month) -> tuple[pd.DataFrame, dict]:
    """Отток отчётного месяца по ведомствам."""
    m = df[pd.to_datetime(df["report_dt"]) == pd.Timestamp(month)]
    if m.empty:
        return pd.DataFrame(), _check("отток по ведомствам", 0, 0)
    g = m.groupby("agency", as_index=False).agg(
        out_qty=("out_qty", "sum"),
        ret_qty=("ret_qty", "sum"),
        out_kept=("out_kept", "sum"),
        base_fl=("calc_fl", "sum"),
        n_org=("inn", "nunique"),
    )
    g["out_rate"] = g["out_qty"] / g["base_fl"].clip(lower=1)
    g["ret_rate"] = g["ret_qty"] / g["out_qty"].clip(lower=1)
    total = g["out_kept"].sum()
    g["share"] = g["out_kept"] / max(total, 1)
    g = g.sort_values("out_kept", ascending=False).reset_index(drop=True)
    chk = _check("отток по ведомствам", g["out_kept"].sum(), m["out_kept"].sum())
    return g, chk


# --------------------------------------------------------------------------- #
# 2. Тенденции
# --------------------------------------------------------------------------- #

def trend(df: pd.DataFrame, top_agencies: list[str] | None = None) -> dict:
    """Помесячный ряд: итог сегмента и разбивка по ведомствам."""
    d = df.copy()
    d["ym"] = pd.to_datetime(d["report_dt"]).dt.to_period("M").dt.to_timestamp("M")
    total = d.groupby("ym", as_index=False).agg(
        out_qty=("out_qty", "sum"), ret_qty=("ret_qty", "sum"),
        out_kept=("out_kept", "sum"), base_fl=("calc_fl", "sum"),
        n_org=("inn", "nunique"))
    total["out_rate"] = total["out_qty"] / total["base_fl"].clip(lower=1)

    by_ag = d.groupby(["ym", "agency"], as_index=False).agg(
        out_kept=("out_kept", "sum"), out_qty=("out_qty", "sum"),
        base_fl=("calc_fl", "sum"))
    if top_agencies:
        by_ag = by_ag[by_ag["agency"].isin(top_agencies)]

    return {"total": total, "by_agency": by_ag,
            "direction": _direction(total, "out_kept")}


def _direction(series_df: pd.DataFrame, col: str, k: int = 3) -> dict:
    """Куда идёт ряд: сравнение последних k месяцев с предыдущими k.

    Именно так, а не «последний месяц против предыдущего»: один месяц гуляет от
    даты выплаты и числа рабочих дней, и по нему «рост» объявляется на ровном месте.
    """
    v = series_df.sort_values("ym")[col].to_numpy(dtype=float)
    if len(v) < 2:
        return {"measurable": False, "reason": f"в ряду {len(v)} точек"}
    k = min(k, len(v) // 2)
    last, prev = v[-k:].mean(), v[-2 * k:-k].mean()
    delta = last - prev
    rel = delta / prev if prev else 0.0
    if abs(rel) < 0.05:
        word = "держится"
    elif delta > 0:
        word = "растёт"
    else:
        word = "снижается"
    return {"measurable": True, "window": k, "last": last, "prev": prev,
            "delta": delta, "rel": rel, "word": word}


def agency_trends(df: pd.DataFrame, k: int = 3) -> pd.DataFrame:
    """Направление ряда по каждому ведомству — что растёт, что успокоилось."""
    d = df.copy()
    d["ym"] = pd.to_datetime(d["report_dt"]).dt.to_period("M").dt.to_timestamp("M")
    rows = []
    for name, g in d.groupby("agency"):
        ser = g.groupby("ym", as_index=False)["out_kept"].sum()
        dr = _direction(ser, "out_kept", k)
        rows.append({"agency": name, "out_kept": ser["out_kept"].iloc[-1],
                     "direction": dr.get("word", "—"), "rel": dr.get("rel"),
                     "measurable": dr["measurable"]})
    return (pd.DataFrame(rows).sort_values("out_kept", ascending=False)
            .reset_index(drop=True))


# --------------------------------------------------------------------------- #
# 3. Территории и регионы
# --------------------------------------------------------------------------- #

def by_territory(df: pd.DataFrame, month, level: str) -> pd.DataFrame:
    """Отток отчётного месяца по территориальному разрезу.

    `level` — 'tb_short_name' | 'region_name' | 'gosb_name' | 'subject_code'.
    """
    m = df[pd.to_datetime(df["report_dt"]) == pd.Timestamp(month)]
    if m.empty or level not in m:
        return pd.DataFrame()
    src = m[m[level].astype(str).str.strip() != ""]
    if src.empty:
        return pd.DataFrame()
    g = src.groupby(level, as_index=False).agg(
        out_qty=("out_qty", "sum"), ret_qty=("ret_qty", "sum"),
        out_kept=("out_kept", "sum"), base_fl=("calc_fl", "sum"),
        n_org=("inn", "nunique"))
    g["out_rate"] = g["out_qty"] / g["base_fl"].clip(lower=1)
    g["ret_rate"] = g["ret_qty"] / g["out_qty"].clip(lower=1)
    g["share"] = g["out_kept"] / max(g["out_kept"].sum(), 1)
    return g.sort_values("out_kept", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 4. Причины: сокращение штата или уход к конкуренту
# --------------------------------------------------------------------------- #

def causes(panel: pd.DataFrame, org_meta: pd.DataFrame, month,
           window: int = CAUSE_WINDOW_M) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Разложить падение численности получателей на две причины.

    Формула на организацию, за окно от base-месяца до отчётного:

        drop        = max(fl_base − fl_now, 0)          сколько получателей потеряно
        staff_cut   = clip(emp_base − emp_now, 0, drop) объяснено сокращением ШТАТА
        competitor  = drop − staff_cut                  остальное — ушли из Сбера

    Смысл: если вместе с получателями уменьшился и штат, людей в организации
    просто стало меньше — Сбер тут ни при чём. Если штат на месте, а получателей
    стало меньше, те же самые люди получают зарплату где-то ещё.

    Раскладка АДДИТИВНА по построению: `staff_cut + competitor = drop` для каждой
    организации, а значит и для любой их группы. Поэтому суммы по ведомствам и
    регионам сходятся с итогом, и это проверяется в коде, а не на глаз.

    Возвращает (по организациям, по ведомствам, проверка сходимости).
    """
    if panel.empty:
        return pd.DataFrame(), pd.DataFrame(), _check("причины оттока", 0, 0)

    p = panel.copy()
    p["ym"] = pd.to_datetime(p["ym"]).dt.to_period("M").dt.to_timestamp("M")
    # свернуть по организации: она может обслуживаться в нескольких ГОСБ
    p = p.groupby(["inn", "ym"], as_index=False)[["fl", "emp", "fot", "pot"]].sum()

    now = pd.Timestamp(month).to_period("M").to_timestamp("M")
    months = sorted(p["ym"].unique())
    if now not in months:
        now = months[-1]
    i = months.index(now)
    base = months[max(0, i - window)]
    if base == now:
        progress.warn("для разбора причин нужен хотя бы один месяц истории — "
                      "раздел пропускается")
        return pd.DataFrame(), pd.DataFrame(), _check("причины оттока", 0, 0)
    used = i - months.index(base)
    if used < window:
        progress.warn(f"окно разбора причин сжато до {used} мес. вместо {window} — "
                      f"столько истории есть в витрине")

    a = p[p["ym"] == base].set_index("inn")
    b = p[p["ym"] == now].set_index("inn")
    # Только организации, ПРИСУТСТВУЮЩИЕ в обоих месяцах: у появившейся посреди
    # окна «падение» равнялось бы всей её численности — это не отток, а её
    # отсутствие в базе сравнения.
    common = a.index.intersection(b.index)
    if len(common) == 0:
        return pd.DataFrame(), pd.DataFrame(), _check("причины оттока", 0, 0)

    fl_a, fl_b = a.loc[common, "fl"].to_numpy(float), b.loc[common, "fl"].to_numpy(float)
    emp_a = a.loc[common, "emp"].to_numpy(float)
    emp_b = b.loc[common, "emp"].to_numpy(float)

    drop = np.clip(fl_a - fl_b, 0, None)
    staff = np.clip(emp_a - emp_b, 0, None)
    staff_cut = np.minimum(staff, drop)
    competitor = drop - staff_cut

    zp_a = fl_a / np.maximum(emp_a, 1)
    zp_b = fl_b / np.maximum(emp_b, 1)

    org = pd.DataFrame({
        "inn": common,
        "fl_base": fl_a, "fl_now": fl_b,
        "emp_base": emp_a, "emp_now": emp_b,
        "zp_base": zp_a.round(4), "zp_now": zp_b.round(4),
        "drop": drop, "staff_cut": staff_cut, "competitor": competitor,
    })
    share_staff = np.divide(staff_cut, np.maximum(drop, 1e-9))
    org["cause"] = np.where(
        drop <= 0, CAUSE_NONE,
        np.where(share_staff >= CAUSE_DOMINANT, CAUSE_STAFF,
                 np.where(1 - share_staff >= CAUSE_DOMINANT, CAUSE_COMPETITOR,
                          CAUSE_MIXED)))
    org["base_month"] = base
    org["report_month"] = now

    if not org_meta.empty:
        org = org.merge(org_meta, on="inn", how="left")
    org["agency"] = org.get("agency", pd.Series(AG.UNKNOWN, index=org.index)).fillna(
        AG.UNKNOWN)

    by_ag = org.groupby("agency", as_index=False).agg(
        drop=("drop", "sum"), staff_cut=("staff_cut", "sum"),
        competitor=("competitor", "sum"), n_org=("inn", "nunique"))
    by_ag["staff_share"] = by_ag["staff_cut"] / by_ag["drop"].clip(lower=1)
    by_ag = by_ag.sort_values("drop", ascending=False).reset_index(drop=True)

    chk = _check("причины: сумма частей = падение",
                 org["staff_cut"].sum() + org["competitor"].sum(), org["drop"].sum())
    progress.done(
        f"причины: окно {used} мес. ({base:%m.%Y} → {now:%m.%Y}), "
        f"{len(org):,} организаций; падение {org['drop'].sum():,.0f} ФЛ = "
        f"сокращение штата {org['staff_cut'].sum():,.0f} + "
        f"уход к конкуренту {org['competitor'].sum():,.0f}")
    return org, by_ag, chk


def causes_by_region(org: pd.DataFrame, level: str = "region_name") -> pd.DataFrame:
    """Причины в территориальном разрезе — какой регион чем болен."""
    if org.empty or level not in org:
        return pd.DataFrame()
    g = org.groupby(level, as_index=False).agg(
        drop=("drop", "sum"), staff_cut=("staff_cut", "sum"),
        competitor=("competitor", "sum"), n_org=("inn", "nunique"))
    g["staff_share"] = g["staff_cut"] / g["drop"].clip(lower=1)
    return g[g["drop"] > 0].sort_values("drop", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 5. Макро-прокси по зарплатным проектам
# --------------------------------------------------------------------------- #

def macro(panel: pd.DataFrame, org_meta: pd.DataFrame, metrics: pd.DataFrame,
          gosb: pd.DataFrame, month) -> pd.DataFrame:
    """Прокси «как идут зарплатные проекты в регионе».

    ВАЖНО, и так же подписано в отчёте: это витрины Сбера, а не государственная
    статистика. «Численность региона» здесь — численность получателей зарплаты
    в Сбере у бюджетных организаций региона, а не занятость по региону.

    Проникновение считается ИЗ СУММ (`sum(fl)/sum(emp)`), а не усреднением
    готовых долей по организациям: среднее долей даёт крупной больнице тот же вес,
    что сельскому клубу, и региональная картина смещается.
    """
    if panel.empty:
        return pd.DataFrame()
    p = panel.copy()
    p["ym"] = pd.to_datetime(p["ym"]).dt.to_period("M").dt.to_timestamp("M")
    if not org_meta.empty:
        p = p.merge(org_meta[["inn", "region_name"]].drop_duplicates("inn"),
                    on="inn", how="left")
    p["region_name"] = p.get("region_name", pd.Series("—", index=p.index)).fillna("—")

    now = pd.Timestamp(month).to_period("M").to_timestamp("M")
    months = sorted(p["ym"].unique())
    if now not in months:
        now = months[-1]
    y_ago = now - pd.DateOffset(years=1)
    y_ago = y_ago.to_period("M").to_timestamp("M")

    def agg(ym):
        s = p[p["ym"] == ym]
        if s.empty:
            return pd.DataFrame()
        g = s.groupby("region_name", as_index=False)[["fl", "emp", "fot", "pot"]].sum()
        g["zp_perc"] = g["fl"] / g["emp"].clip(lower=1)
        g["avg_salary"] = g["fot"] / g["fl"].clip(lower=1)
        return g

    cur, prev = agg(now), agg(y_ago)
    if cur.empty:
        return pd.DataFrame()
    out = cur.rename(columns={"fl": "fl_now", "emp": "emp_now", "pot": "pot_now",
                              "zp_perc": "zp_now", "avg_salary": "salary_now"})
    if not prev.empty:
        out = out.merge(
            prev[["region_name", "fl", "emp", "zp_perc", "avg_salary"]].rename(
                columns={"fl": "fl_y1", "emp": "emp_y1", "zp_perc": "zp_y1",
                         "avg_salary": "salary_y1"}),
            on="region_name", how="left")
    else:
        for c in ("fl_y1", "emp_y1", "zp_y1", "salary_y1"):
            out[c] = np.nan
    out["fl_y1_diff"] = out["fl_now"] - num(out, "fl_y1")
    out["emp_y1_diff"] = out["emp_now"] - num(out, "emp_y1")
    out["salary_growth"] = np.where(
        num(out, "salary_y1") > 0, out["salary_now"] / num(out, "salary_y1", 1.0) - 1, np.nan)

    # План/факт по сегменту: витринный ориентир «как регион идёт к плану»
    if not metrics.empty and not gosb.empty:
        mm = metrics.copy()
        mm["end_dt"] = pd.to_datetime(mm["end_dt"]).dt.to_period("M").dt.to_timestamp("M")
        mm = mm[mm["end_dt"] == now]
        if not mm.empty:
            gm = gosb.rename(columns={"new_gosb_id": "old_gosb_id"})
            # метрики лежат на СТАРОМ идентификаторе ГОСБ; регион берём по нему
            reg = gosb[["new_gosb_id", "region_name"]].rename(
                columns={"new_gosb_id": "old_gosb_id"})
            mm = mm.merge(reg, on="old_gosb_id", how="left")
            r = mm.groupby("region_name", as_index=False)[["plan_amt", "fact_amt"]].sum()
            r["plan_exec"] = r["fact_amt"] / r["plan_amt"].replace(0, np.nan)
            out = out.merge(r[["region_name", "plan_exec"]], on="region_name", how="left")
    if "plan_exec" not in out:
        out["plan_exec"] = np.nan
    return out.sort_values("fl_now", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 6. Банки-конкуренты
# --------------------------------------------------------------------------- #

def competitors(df: pd.DataFrame, month, coverage: float) -> dict:
    """Кто назван конкурентом там, где отток есть.

    Витрина знает конкурента только у ключевых клиентов, поэтому доля покрытия
    возвращается вместе с результатом и печатается в отчёте: без неё «Банк А —
    3 организации» читается как «конкурентов почти нет».
    """
    m = df[pd.to_datetime(df["report_dt"]) == pd.Timestamp(month)]
    if m.empty or "bank_competitor" not in m:
        return {"banks": pd.DataFrame(), "coverage": coverage, "known": 0, "total": 0}
    org = m.groupby("inn", as_index=False).agg(
        out_kept=("out_kept", "sum"),
        bank=("bank_competitor", "first"),
        captive=("captive_bank_name", "first") if "captive_bank_name" in m else
                ("bank_competitor", "first"),
        strategy=("strategy_name", "first") if "strategy_name" in m else
                 ("bank_competitor", "first"),
        agency=("agency", "first"))
    known = int(org["bank"].notna().sum())
    banks = (org[org["bank"].notna()]
             .groupby("bank", as_index=False)
             .agg(out_kept=("out_kept", "sum"), n_org=("inn", "nunique"))
             .sort_values("out_kept", ascending=False).reset_index(drop=True))
    strategies = pd.DataFrame()
    if "strategy" in org and org["strategy"].notna().any():
        strategies = (org.groupby("strategy", as_index=False)
                      .agg(out_kept=("out_kept", "sum"), n_org=("inn", "nunique"))
                      .sort_values("out_kept", ascending=False).reset_index(drop=True))
    return {"banks": banks, "strategies": strategies, "coverage": coverage,
            "known": known, "total": int(org["inn"].nunique()),
            "out_kept_known": float(org.loc[org["bank"].notna(), "out_kept"].sum()),
            "out_kept_total": float(org["out_kept"].sum())}


# --------------------------------------------------------------------------- #
# Материальность: что показывать построчно, а что свернуть
# --------------------------------------------------------------------------- #

def materialize(df: pd.DataFrame, key: str, value: str,
                min_share: float = MATERIAL_SHARE, keep_last: str | None = None
                ) -> tuple[pd.DataFrame, dict]:
    """Свернуть мелкие строки в «прочие». Отсечение сопровождается подписью.

    `keep_last` — строка, которую нельзя сворачивать никогда (у нас это
    «Не классифицировано»: спрятать её значило бы скрыть неполноту разбора).
    """
    if df.empty:
        return df, {"hidden": 0, "hidden_value": 0.0, "shown": 0}
    total = df[value].sum()
    if total <= 0:
        return df, {"hidden": 0, "hidden_value": 0.0, "shown": len(df)}
    big = df[value] / total >= min_share
    if keep_last is not None:
        big |= df[key] == keep_last
    shown, small = df[big], df[~big]
    if small.empty:
        return shown.reset_index(drop=True), {"hidden": 0, "hidden_value": 0.0,
                                              "shown": len(shown)}
    tail = {key: f"Прочие ({len(small)})"}
    for c in df.columns:
        if c == key:
            continue
        tail[c] = small[c].sum() if pd.api.types.is_numeric_dtype(df[c]) else ""
    out = pd.concat([shown, pd.DataFrame([tail])], ignore_index=True)
    return out, {"hidden": int(len(small)), "hidden_value": float(small[value].sum()),
                 "shown": int(len(shown))}
