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
STEP_T = {"1": "+1", "2-5": "+2…5", "6-20": "+6…20", "21-100": "+21…100", "100+": "больше +100"}
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
    steps_fl = x[x["grp"] == "grow"].pivot_table(index="step", columns="pair", values="d",
                                                 aggfunc="sum").reindex(STEPS).fillna(0)
    steps_d = x[x["grp"] == "decl"].pivot_table(index="step", columns="pair", values="n_cells",
                                                aggfunc="sum").reindex(STEPS).fillna(0)
    t = _num(ct.copy(), ["n_cells", "n_fl"])
    t["report_dt"] = t["report_dt"].astype(str)
    t = t.set_index("report_dt")
    return {"cls": cls, "grp": grp, "steps": steps, "steps_fl": steps_fl, "steps_decl": steps_d, "tot": t,
            "pairs": {"cur": (cells[1], cells[2]), "prev": (cells[0], cells[1])}}


def nfl_step_table(ns: pd.DataFrame) -> pd.DataFrame:
    """(год, ступень роста ячейки) × канал: НФЛ, пришедшие в растущие ячейки."""
    if ns is None or ns.empty:
        return pd.DataFrame()
    x = _num(ns.copy(), ["n"])
    idx = pd.MultiIndex.from_product([["prev", "cur"], STEPS], names=["yr", "step"])
    return x.pivot_table(index=["yr", "step"], columns="ch", values="n", aggfunc="sum").reindex(
        index=idx, columns=CH).fillna(0)


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
    res["nfl_step"] = nfl_step_table(raw.get("nfl_step"))
    ng = res["nfl_cell"]
    if not res["nfl_step"].empty and not ng.empty:
        for y in ("prev", "cur"):
            got = float(res["nfl_step"].xs(y, level="yr").to_numpy().sum()) if y in res["nfl_step"].index.get_level_values(0) else 0.0
            want = float(ng.loc[(y, "grow")].sum()) if (y, "grow") in ng.index else 0.0
            checks.append(check(f"НФЛ по ступеням роста = НФЛ в ячейках роста ({'этот год' if y == 'cur' else 'год назад'})",
                                got - want))
    res["checks"] = checks
    res["verdict"] = verdict(res)
    res["facts"] = facts(res)
    res["prop"] = proportions(res)
    res["prop_lines"] = proportion_lines(res["prop"])
    f = res["facts"].set_index("key")
    if "nfl_grow" in f.index:
        for y in ("prev", "cur"):
            checks.append(check(f"НФЛ в ячейках роста: Σ каналов = всего ({'этот год' if y == 'cur' else 'год назад'})",
                                float(sum(f.loc[f"nfl_grow_{c}", y] for c in CH) - f.loc["nfl_grow", y])))


# --------------------------------------------------------------------------- #
# Факты (абсолютные числа двух лет) и пропорции
# --------------------------------------------------------------------------- #
def facts(res: dict) -> pd.DataFrame:
    """Одна таблица в абсолютных числах: 2025, 2026, разница, %.
    Портфель и ячейки — на август; НФЛ и МЗП — за январь–отчётный месяц."""
    ct = res["cell_t"]
    pp, pc = ct["pairs"]["prev"], ct["pairs"]["cur"]
    tot, grp = ct["tot"], ct["grp"]
    rows = []

    def add(key, title, prev, cur, sub=False, digits=0, head=False):
        rows.append({"key": key, "title": title, "prev": prev, "cur": cur, "sub": sub, "digits": digits,
                     "head": head})

    def g(pair, k, col):
        try:
            return float(grp.loc[(pair, k), col])
        except KeyError:
            return 0.0
    tv = lambda d, c: float(tot.loc[M.iso(d), c]) if M.iso(d) in tot.index else np.nan
    add("h_port", f"Портфель, август", None, None, head=True)
    add("port", "Получателей ЗП в портфеле, ФЛ", tv(pp[1], "n_fl"), tv(pc[1], "n_fl"))
    add("cells", "Ячеек ГОСБ × организация", tv(pp[1], "n_cells"), tv(pc[1], "n_cells"))
    add("grow", "Ячеек роста (+1 ФЛ и больше) к августу прошлого года", g("prev", "grow", "n_cells"),
        g("cur", "grow", "n_cells"))
    add("grow_d", "ФЛ прибавили в ячейках роста", g("prev", "grow", "d"), g("cur", "grow", "d"), sub=True)
    add("decl", "Ячеек снижения (−1 ФЛ и больше)", g("prev", "decl", "n_cells"), g("cur", "decl", "n_cells"))
    add("decl_d", "ФЛ потеряли в ячейках снижения", -g("prev", "decl", "d"), -g("cur", "decl", "d"), sub=True)
    add("flat", "Ячеек без изменений", g("prev", "flat", "n_cells"), g("cur", "flat", "n_cells"))

    nt = res["nfl"]["tot"]
    add("h_nfl", f"Новые получатели (НФЛ), {res['period']}", None, None, head=True)
    add("nfl", "НФЛ всего", float(nt.loc["prev", "all"]), float(nt.loc["cur", "all"]))
    for c in CH:
        add(f"nfl_{c}", CH_T[c], float(nt.loc["prev", c]), float(nt.loc["cur", c]), sub=True)
    nc = res.get("nfl_cell")
    if nc is not None and not nc.empty:
        def ncv(y, c):
            return float(nc.loc[(y, "grow"), c]) if (y, "grow") in nc.index else 0.0
        add("nfl_grow", "НФЛ в ячейках роста", sum(ncv("prev", c) for c in CH), sum(ncv("cur", c) for c in CH))
        for c in CH:
            add(f"nfl_grow_{c}", CH_T[c], ncv("prev", c), ncv("cur", c), sub=True)

    mz = res["mzp"]["year"]
    add("h_mzp", "МЗП", None, None, head=True)
    for k, t, dg in (("staff", "Менеджеров МЗП", 0), ("deals", f"Сделок МЗП, {res['period']}", 0),
                     ("fact", "Факт по сделкам, ФЛ", 0), ("nfl_mzp", "НФЛ через МЗП", 0),
                     ("nfl_per_mgr", "НФЛ МЗП на менеджера", 1), ("deals_per_mgr", "Сделок на менеджера", 1),
                     ("fact_per_mgr", "Факт по сделкам на менеджера, ФЛ", 1)):
        add(k, t, float(mz.loc["prev", k]), float(mz.loc["cur", k]), digits=dg)
    out = pd.DataFrame(rows)
    out["delta"] = out["cur"] - out["prev"]
    out["pct"] = [pct(c, p) if pd.notna(p) and p else np.nan for c, p in zip(out["cur"], out["prev"])]
    return out


def proportions(res: dict) -> dict:
    """Пропорции МЗП: во сколько раз вырос штат и во сколько — продажи. Плюс сколько
    НФЛ дал бы новый штат при прошлогодней производительности."""
    mz = res["mzp"]["year"]
    k = {}
    for key in ("staff", "deals", "plan", "fact", "nfl_mzp", "nfl_per_mgr", "deals_per_mgr", "fact_per_mgr",
                "nfl_per_deal", "fact_per_deal"):
        a, b = float(mz.loc["cur", key]), float(mz.loc["prev", key])
        k[key] = a / b if b else np.nan
    expected = float(mz.loc["prev", "nfl_per_mgr"]) * float(mz.loc["cur", "staff"])
    yoy = res["nfl"]["yoy"].set_index("ch")
    return {"k": k, "expected_nfl": expected, "actual_nfl": float(mz.loc["cur", "nfl_mzp"]),
            "gap": float(mz.loc["cur", "nfl_mzp"]) - expected,
            "share_prev": float(yoy.loc["mzp", "share_prev"]), "share_cur": float(yoy.loc["mzp", "share_cur"]),
            "staff_prev": float(mz.loc["prev", "staff"]), "staff_cur": float(mz.loc["cur", "staff"]),
            "nfl_prev": float(mz.loc["prev", "nfl_mzp"]),
            "year_prev": int(mz.loc["prev", "year"]), "year_cur": int(mz.loc["cur", "year"])}


def proportion_lines(pr: dict) -> list[str]:
    from .charts import fnum, fpct
    k = pr["k"]

    def times(x):
        return "—" if pd.isna(x) else f"в {fnum(x, digits=2)} раза"

    def ch(x):
        return "" if pd.isna(x) else f" ({fpct(x - 1, True, 0)})"
    y0, y1 = pr["year_prev"], pr["year_cur"]
    out = [
        f"Штат МЗП вырос {times(k['staff'])}: {fnum(pr['staff_prev'])} → {fnum(pr['staff_cur'])} менеджеров.",
        f"НФЛ через МЗП изменились {times(k['nfl_mzp'])}{ch(k['nfl_mzp'])}: {fnum(pr['nfl_prev'])} → "
        f"{fnum(pr['actual_nfl'])}.",
        f"Производительность — НФЛ МЗП на менеджера — {times(k['nfl_per_mgr'])}{ch(k['nfl_per_mgr'])}.",
        f"Сделок {times(k['deals'])}{ch(k['deals'])}, на менеджера {times(k['deals_per_mgr'])}{ch(k['deals_per_mgr'])}; "
        f"факт по сделкам {times(k['fact'])}{ch(k['fact'])}, на менеджера {times(k['fact_per_mgr'])}"
        f"{ch(k['fact_per_mgr'])}.",
        f"При производительности {y0} года новый штат дал бы {fnum(pr['expected_nfl'])} НФЛ, фактически "
        f"{fnum(pr['actual_nfl'])} — {'недобор' if pr['gap'] < 0 else 'сверх'} {fnum(abs(pr['gap']))}.",
        f"Доля МЗП в НФЛ: {fpct(pr['share_prev'])} в {y0} → {fpct(pr['share_cur'])} в {y1}.",
    ]
    if not pd.isna(k["staff"]) and not pd.isna(k["nfl_mzp"]) and k["staff"] != 1:
        el = (k["nfl_mzp"] - 1) / (k["staff"] - 1)
        out.append(f"На каждый +1% штата НФЛ МЗП изменились на {fnum(el, True, 2)}%.")
    return out
