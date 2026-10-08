"""Расчёты поверх выгрузок. Ни одного обращения к БД.

Две истории отчёта:
* ячейки «ГОСБ × организация», август к августу: рост / снижение / ноль — сколько
  ситуаций роста в этом году против прошлого;
* НФЛ янв–авг по каналам (МЗП, ВСП, Digital, Остальное): что просело и в какой
  пропорции к росту штата МЗП.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import months as M
from . import names as N
from . import queries as Q
from . import segments as S

CH = [c for c, _, _ in Q.CHANNELS]
CH_T = {c: t for c, _, t in Q.CHANNELS}
CLS_T = {"grow": "Рост", "new": "Новая ячейка", "flat": "Без изменений", "decl": "Снижение",
         "closed": "Ячейка закрылась", "none": "Ячейки нет в ведомостях"}
CLS_GROUP = {"grow": "grow", "new": "grow", "decl": "decl", "closed": "decl", "flat": "flat", "none": "none"}
GROUP_T = {"grow": "Рост (+1 и больше)", "decl": "Снижение (−1 и больше)", "flat": "Без изменений",
           "none": "Ячейки нет в ведомостях"}
STEPS = ["1", "2-5", "6-20", "21-100", "100+"]
DIM_T = {"seg": "Сегмент", "holding": "Холдинг", "industry": "Отрасль", "tb": "ТБ"}
NO_INDUSTRY = "Отрасль не указана"


def _num(df: pd.DataFrame, cols) -> pd.DataFrame:
    for c in cols:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def check(name: str, value: float, tol: float = 0.5, detail: str = "") -> dict:
    ok = bool(pd.notna(value) and abs(value) <= tol)
    return {"check": name, "ok": ok, "residual": float(value) if pd.notna(value) else None, "detail": detail}


def pct(a, b):
    return (a - b) / b if b else np.nan


# --------------------------------------------------------------------------- #
# Подписи разрезов
# --------------------------------------------------------------------------- #
def dim_label(df: pd.DataFrame, tb_names: dict) -> pd.Series:
    hid = df["holding_name_hidden"] if "holding_name_hidden" in df else pd.Series(False, index=df.index)
    out = []
    for dim, key, hn, h in zip(df["dim"], df["key"], df["holding_name"], hid):
        if dim == "holding":
            out.append(N.holding_label(hn, bool(h)))
        elif dim == "industry":
            out.append(key if isinstance(key, str) and key.strip() else NO_INDUSTRY)
        elif dim == "tb":
            try:
                t = int(float(key))
                out.append(tb_names.get(t, f"ТБ {t}"))
            except (TypeError, ValueError):
                out.append("ТБ не указан")
        else:
            out.append(str(key))
    return pd.Series(out, index=df.index)


def order_labels(dim: str, labels) -> list:
    return S.ordered(labels) if dim == "seg" else sorted(labels)


# --------------------------------------------------------------------------- #
# НФЛ по каналам
# --------------------------------------------------------------------------- #
def nfl_tables(nm: pd.DataFrame, m_cur: list, m_prev: list) -> dict:
    x = _num(nm.copy(), ["n"])
    x["report_dt"] = x["report_dt"].astype(str)
    x["mo"] = [M.parse(d).month for d in x["report_dt"]]
    tot = x.pivot_table(index="yr", columns="ch", values="n", aggfunc="sum").reindex(
        index=["prev", "cur"], columns=CH).fillna(0)
    tot["all"] = tot[CH].sum(axis=1)
    month = x.pivot_table(index=["mo"], columns=["yr", "ch"], values="n", aggfunc="sum").fillna(0)
    months = sorted({M.parse(d).month for d in m_cur})
    month = month.reindex(months).fillna(0)
    rows = []
    for c in CH + ["all"]:
        a, b = float(tot.loc["cur", c]), float(tot.loc["prev", c])
        rows.append({"ch": c, "title": CH_T.get(c, "Всего НФЛ"), "prev": b, "cur": a, "delta": a - b,
                     "pct": pct(a, b), "share_prev": b / tot.loc["prev", "all"] if tot.loc["prev", "all"] else np.nan,
                     "share_cur": a / tot.loc["cur", "all"] if tot.loc["cur", "all"] else np.nan})
    yoy = pd.DataFrame(rows)
    # Вклад канала в изменение НФЛ: Δ канала / |Δ всего|.
    d_all = float(yoy.loc[yoy["ch"] == "all", "delta"].iloc[0])
    yoy["contrib"] = yoy["delta"] / abs(d_all) if d_all else np.nan
    src = x.pivot_table(index="report_dt", columns="src", values="n", aggfunc="sum").fillna(0)
    return {"tot": tot, "month": month, "months": months, "yoy": yoy, "src": src}


def overlap_table(ov: pd.DataFrame) -> pd.DataFrame:
    """Сколько каналов действовало у НФЛ и кто победил: год × победитель × набор каналов."""
    if ov is None or ov.empty:
        return pd.DataFrame()
    x = _num(ov.copy(), ["n"])
    for c in ("has_mzp", "has_vsp", "has_dig"):
        x[c] = x[c].astype(bool)
    x["k"] = x[["has_mzp", "has_vsp", "has_dig"]].sum(axis=1)
    x["combo"] = [" + ".join(t for f, t in ((a, "МЗП"), (b, "ВСП"), (c, "Digital")) if f) or "нет действий"
                  for a, b, c in zip(x["has_mzp"], x["has_vsp"], x["has_dig"])]
    return x


def lag_table(lg: pd.DataFrame) -> pd.DataFrame:
    if lg is None or lg.empty:
        return pd.DataFrame()
    x = _num(lg.copy(), ["n", "lag_m"])
    x["lag_m"] = x["lag_m"].astype(int)
    return x.pivot_table(index=["yr", "ch"], columns="lag_m", values="n", aggfunc="sum").fillna(0)


# --------------------------------------------------------------------------- #
# Ячейки
# --------------------------------------------------------------------------- #
def cell_tables(cs: pd.DataFrame, ct: pd.DataFrame, cells: list) -> dict:
    x = _num(cs.copy(), ["n_cells", "n_b", "n_c"])
    x["grp"] = x["cls"].map(CLS_GROUP)
    x["d"] = x["n_c"] - x["n_b"]
    cls = x.groupby(["pair", "cls"])[["n_cells", "n_b", "n_c", "d"]].sum()
    grp = x.groupby(["pair", "grp"])[["n_cells", "n_b", "n_c", "d"]].sum()
    steps = x[x["grp"] == "grow"].pivot_table(index="step", columns="pair", values="n_cells",
                                              aggfunc="sum").reindex(STEPS).fillna(0)
    steps_d = x[x["grp"] == "decl"].pivot_table(index="step", columns="pair", values="n_cells",
                                                aggfunc="sum").reindex(STEPS).fillna(0)
    t = _num(ct.copy(), ["n_cells", "n_fl"])
    t["report_dt"] = t["report_dt"].astype(str)
    t = t.set_index("report_dt")
    return {"cls": cls, "grp": grp, "steps": steps, "steps_decl": steps_d, "tot": t,
            "pairs": {"cur": (cells[1], cells[2]), "prev": (cells[0], cells[1])}}


def nfl_cell_table(nc: pd.DataFrame) -> pd.DataFrame:
    if nc is None or nc.empty:
        return pd.DataFrame()
    x = _num(nc.copy(), ["n"])
    x["grp"] = x["cls"].map(CLS_GROUP).fillna("none")
    return x.pivot_table(index=["yr", "grp"], columns="ch", values="n", aggfunc="sum").reindex(
        columns=CH).fillna(0)


# --------------------------------------------------------------------------- #
# Разрезы
# --------------------------------------------------------------------------- #
def dims(nd: pd.DataFrame, cd: pd.DataFrame, tb_names: dict) -> dict:
    """Разрез → таблица: НФЛ год назад / сейчас по каналам и всего, ячейки роста двух пар."""
    out = {}
    a = _num(nd.copy(), ["n"]) if nd is not None and not nd.empty else pd.DataFrame()
    b = _num(cd.copy(), ["n_grow", "n_decl", "n_flat", "n_b", "n_c", "d_grow", "d_decl"]) \
        if cd is not None and not cd.empty else pd.DataFrame()
    if not a.empty:
        a["label"] = dim_label(a, tb_names)
    if not b.empty:
        b["label"] = dim_label(b, tb_names)
    for dim in DIM_T:
        za = a[a["dim"] == dim] if not a.empty else a
        if za.empty:
            continue
        pv = za.pivot_table(index="label", columns=["yr", "ch"], values="n", aggfunc="sum").fillna(0)
        t = pd.DataFrame(index=pv.index)
        for y in ("prev", "cur"):
            for c in CH:
                t[f"{c}_{y}"] = pv[(y, c)] if (y, c) in pv.columns else 0.0
            t[f"all_{y}"] = t[[f"{c}_{y}" for c in CH]].sum(axis=1)
        for c in CH + ["all"]:
            t[f"{c}_d"] = t[f"{c}_cur"] - t[f"{c}_prev"]
        t["all_pct"] = t["all_d"] / t["all_prev"].where(t["all_prev"] > 0)
        zb = b[b["dim"] == dim] if not b.empty else b
        if not zb.empty:
            for y in ("prev", "cur"):
                g = zb[zb["pair"] == y].groupby("label")[["n_grow", "n_decl", "n_flat", "n_b", "n_c"]].sum()
                for c in g.columns:
                    t[f"{c}_{y}"] = g[c].reindex(t.index).fillna(0)
        t = t.reset_index().rename(columns={"index": "label"})
        if dim in ("seg", "tb"):
            t = t.set_index("label").reindex(order_labels(dim, t["label"])).reset_index()
        else:
            t = t.sort_values("all_d").reset_index(drop=True)
        out[dim] = t
    return out


# --------------------------------------------------------------------------- #
# МЗП: штат и продажи
# --------------------------------------------------------------------------- #
def mzp_tables(staff: pd.DataFrame, deals: pd.DataFrame, nfl: dict, tb_names: dict,
               last, nfl_dims: dict, staff_tot: pd.DataFrame | None = None) -> dict:
    s = _num(staff.copy(), ["n_staff"]) if staff is not None and not staff.empty else pd.DataFrame()
    st_all = _num(staff_tot.copy(), ["n_staff"]) if staff_tot is not None and not staff_tot.empty else s
    d = _num(deals.copy(), ["n_deals", "plan_qty", "fact_qty", "n_nfl", "n_emp"]) \
        if deals is not None and not deals.empty else pd.DataFrame()
    cur_dt, prev_dt = M.iso(last), M.iso(M.shift(last, -12))
    staff_tot = {y: float(st_all.loc[st_all["report_dt"].astype(str) == d, "n_staff"].sum())
                 if not st_all.empty else 0.0 for y, d in (("prev", prev_dt), ("cur", cur_dt))}
    yoy = nfl["yoy"].set_index("ch")
    rows = []
    if not d.empty:
        d["deal_m"] = d["deal_m"].astype(str)
        d["y"] = [M.parse(x).year for x in d["deal_m"]]
        d["mo"] = [M.parse(x).month for x in d["deal_m"]]
        d = d[d["mo"] <= M.parse(last).month]
    for y, yr in (("prev", M.parse(last).year - 1), ("cur", M.parse(last).year)):
        z = d[d["y"] == yr] if not d.empty else d
        st = staff_tot[y]
        nfl_mzp = float(yoy.loc["mzp", y])
        rows.append({"yr": y, "year": yr, "staff": st, "deals": float(z["n_deals"].sum()) if not z.empty else 0.0,
                     "plan": float(z["plan_qty"].sum()) if not z.empty else 0.0,
                     "fact": float(z["fact_qty"].sum()) if not z.empty else 0.0,
                     "nfl_mzp": nfl_mzp,
                     "nfl_deals": float(z["n_nfl"].sum()) if not z.empty else 0.0})
    t = pd.DataFrame(rows).set_index("yr")
    t["fact_plan"] = t["fact"] / t["plan"].where(t["plan"] > 0)
    t["deals_per_mgr"] = t["deals"] / t["staff"].where(t["staff"] > 0)
    t["nfl_per_mgr"] = t["nfl_mzp"] / t["staff"].where(t["staff"] > 0)
    t["fact_per_mgr"] = t["fact"] / t["staff"].where(t["staff"] > 0)
    t["nfl_per_deal"] = t["nfl_deals"] / t["deals"].where(t["deals"] > 0)
    t["fact_per_deal"] = t["fact"] / t["deals"].where(t["deals"] > 0)
    month = d.pivot_table(index="mo", columns="y", values=["n_deals", "fact_qty", "n_nfl"], aggfunc="sum").fillna(0) \
        if not d.empty else pd.DataFrame()
    # По ТБ: штат, НФЛ МЗП (из разреза НФЛ по ТБ), на менеджера.
    tb = pd.DataFrame()
    if not s.empty:
        s["y"] = np.where(s["report_dt"].astype(str) == cur_dt, "cur", "prev")
        sp = s.pivot_table(index="tb_id", columns="y", values="n_staff", aggfunc="sum").fillna(0)
        sp.index = [tb_names.get(int(i), f"ТБ {int(i)}") for i in sp.index]
        nt = nfl_dims.get("tb")
        tb = pd.DataFrame({"staff_prev": sp.get("prev", 0), "staff_cur": sp.get("cur", 0)})
        if nt is not None and not nt.empty:
            n = nt.set_index("label")
            tb["nfl_prev"] = n["mzp_prev"].reindex(tb.index).fillna(0)
            tb["nfl_cur"] = n["mzp_cur"].reindex(tb.index).fillna(0)
        tb["per_prev"] = tb.get("nfl_prev", 0) / tb["staff_prev"].where(tb["staff_prev"] > 0)
        tb["per_cur"] = tb.get("nfl_cur", 0) / tb["staff_cur"].where(tb["staff_cur"] > 0)
        tb = tb.reset_index().rename(columns={"index": "tb"})
    return {"year": t, "month": month, "tb": tb}


# --------------------------------------------------------------------------- #
# Вывод по правилам
# --------------------------------------------------------------------------- #
def verdict(res: dict) -> list[str]:
    from .charts import fnum, fpct
    out = []
    yoy = res["nfl"]["yoy"].set_index("ch")
    a = yoy.loc["all"]
    out.append(f"НФЛ за {res['period']}: {fnum(a['prev'])} → {fnum(a['cur'])} ({fpct(a['pct'], True)}).")
    ch = yoy.loc[CH].sort_values("delta")
    worst = ch.iloc[0]
    if worst["delta"] < 0:
        out.append(f"Больше всего просел канал «{worst['title']}»: {fnum(worst['delta'], True)} "
                   f"({fpct(worst['pct'], True)}).")
    best = ch.iloc[-1]
    if best["delta"] > 0:
        out.append(f"Вырос канал «{best['title']}»: {fnum(best['delta'], True)} ({fpct(best['pct'], True)}).")
    mz = res["mzp"]["year"]
    if mz.loc["prev", "staff"] and mz.loc["cur", "staff"]:
        s = pct(mz.loc["cur", "staff"], mz.loc["prev", "staff"])
        n = pct(mz.loc["cur", "nfl_mzp"], mz.loc["prev", "nfl_mzp"])
        p = pct(mz.loc["cur", "nfl_per_mgr"], mz.loc["prev", "nfl_per_mgr"])
        out.append(f"Штат МЗП {fpct(s, True, 0)}, НФЛ через МЗП {fpct(n, True)}: на одного менеджера "
                   f"{fnum(mz.loc['prev', 'nfl_per_mgr'], digits=1)} → {fnum(mz.loc['cur', 'nfl_per_mgr'], digits=1)} "
                   f"({fpct(p, True, 0)}).")
    g = res["cell_t"]["grp"]
    try:
        gc, gp = g.loc[("cur", "grow"), "n_cells"], g.loc[("prev", "grow"), "n_cells"]
        out.append(f"Ячеек роста (ГОСБ × организация, август к августу): {fnum(gp)} год назад → {fnum(gc)} "
                   f"сейчас ({fpct(pct(gc, gp), True)}).")
    except KeyError:
        pass
    return out


# --------------------------------------------------------------------------- #
def compute(res: dict) -> None:
    raw = res["raw"]
    checks: list[dict] = []
    tbd = raw.get("tb_dim", pd.DataFrame())
    tb_names = dict(zip(tbd["tb_id"].astype(int), tbd["tb_short_name"])) if not tbd.empty else {}
    res["tb_names"] = tb_names
    m_cur, m_prev = res["m_cur"], res["m_prev"]
    res["period"] = f"{M.RU_FULL[0]}–{M.name(m_cur[-1])}"
    nfl = nfl_tables(raw["nfl_month"], m_cur, m_prev)
    res["nfl"] = nfl
    rows = res.get("rows", {})
    if rows.get("t_nfl") is not None:
        checks.append(check("НФЛ с каналом = НФЛ (ровно один канал на НФЛ)",
                            float(rows["t_nflc"] - rows["t_nfl"])))
        checks.append(check("Σ каналов = НФЛ", float(nfl["tot"]["all"].sum() - rows["t_nfl"])))
    # Граница архива: месяц — только из одной таблицы, и ни один месяц не пустой.
    src = nfl["src"]
    both = int(((src > 0).sum(axis=1) > 1).sum()) if not src.empty else 0
    checks.append(check("архив и текущая витрина НФЛ не пересекаются по месяцам", both))
    empty = [m for m in m_prev + m_cur if m not in set(src.index)]
    checks.append(check("в каждом месяце есть НФЛ", len(empty), detail=", ".join(M.label(m) for m in empty)))

    res["overlap"] = overlap_table(raw.get("nfl_overlap"))
    res["lag"] = lag_table(raw.get("nfl_lag"))
    cells = cell_tables(raw["cell_sum"], raw["cell_tot"], res["cells"])
    res["cell"] = cells
    for tag, (b, c) in cells["pairs"].items():
        t = cells["tot"]
        if M.iso(b) in t.index and M.iso(c) in t.index:
            d = float(cells["grp"].loc[tag, "d"].sum()) if tag in cells["grp"].index.get_level_values(0) else 0.0
            checks.append(check(f"Σ изменений ячеек = изменение портфеля ({M.label(b)}→{M.label(c)})",
                                d - float(t.loc[M.iso(c), "n_fl"] - t.loc[M.iso(b), "n_fl"])))
    res["cell_t"] = cells              # res["cells"] — месяцы ячеек (список)
    res["cell_pairs"] = {k: f"{M.label(b)}→{M.label(c)}" for k, (b, c) in cells["pairs"].items()}
    res["nfl_cell"] = nfl_cell_table(raw.get("nfl_cell"))
    res["dims"] = dims(raw.get("nfl_dim"), raw.get("cell_dim"), tb_names)
    for dim in ("seg", "tb"):
        t = res["dims"].get(dim)
        if t is not None:
            for y in ("prev", "cur"):
                checks.append(check(f"Σ {DIM_T[dim].lower()}ов = НФЛ ({'этот год' if y == 'cur' else 'год назад'})",
                                    float(t[f"all_{y}"].sum() - nfl["tot"].loc[y, "all"])))
    res["mzp"] = mzp_tables(raw.get("staff"), raw.get("deal_month"), nfl, tb_names,
                            M.parse(res["report_month"]), res["dims"], raw.get("staff_tot"))
    top = raw.get("cell_top", pd.DataFrame())
    if top is not None and not top.empty:
        top = _num(top.copy(), ["n_b", "n_c", "delta", "nfl", "nfl_mzp", "nfl_vsp", "nfl_dig", "nfl_other"])
        top["org"] = [N.org_label(n, i) for n, i in zip(top["org_name"], top["inn"])]
        top["holding"] = [N.holding_label(n, bool(h)) for n, h in zip(top["holding_name"],
                                                                         top.get("holding_name_hidden", False))]
        top["tb"] = [tb_names.get(int(t), f"ТБ {int(t)}") if pd.notna(t) else "—" for t in top["tb_id"]]
    res["cell_top"] = top
    dm = raw.get("deal_match", pd.DataFrame())
    res["deal_match"] = dm
    res["checks"] = checks
    res["verdict"] = verdict(res)
