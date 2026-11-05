"""Входы сторожа: панель рядов «месяц × мера» из поколения `yva_ptf_vintage`.

Ряды двух уровней:
* `atom` — сегмент отчётного месяца × ТБ × ГОСБ (вся сводка, без пропусков);
* `org`  — крупная организация целиком (итог по всем её ГОСБ) и её главная ячейка
  (атом, где у неё больше всего получателей) — туда же ложится закладка стенда.
  Помесячное полное вложение в атом / ГОСБ / ТБ хранится отдельно в org_scopes.

Панель — массивы numpy [ряд × месяц] по каждой мере: сторож и стенд считают на них
векторно. Всё, где есть человек, сюда не попадает — только агрегаты поколения.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import config, db
from . import sql as Q

MEAS = list(Q.VINT_MEAS)
IN_M = ["m_in_from_outside", "m_in_from_other_org", "m_in_codes"]
OUT_M = ["m_out_left_bank", "m_out_to_other_org", "m_out_codes"]
LB_M = ["m_lb_seasonal", "m_lb_org_stopped", "m_lb_one_time", "m_lb_unknown"]
IN_Y = ["y_in_from_outside", "y_in_from_other_org", "y_in_codes"]
OUT_Y = ["y_out_left_bank", "y_out_to_other_org", "y_out_codes"]
ATOM_KEYS = ["seg_rep", "sys_tb_id", "sys_gosb_id"]
CELL_MEAS = dict(zip(("in_outside", "in_other", "in_codes", "out_left", "out_other", "out_codes",
                      "in_temp_outside", "out_temp_left"),
                     IN_M + OUT_M + ["m_in_temp_outside", "m_out_temp_left"]))


@dataclass
class Panel:
    months: list[str]                      # 'ГГГГ-ММ-ДД', по возрастанию
    meta: pd.DataFrame                     # ряд: lvl, org_id, seg_rep, sys_tb_id, sys_gosb_id, atom
    x: dict[str, np.ndarray] = field(default_factory=dict)   # мера → [ряд × месяц], NaN — нет
    # Территория, целиком содержащая организацию в данном месяце. -1 — несколько
    # территорий или привязка неизвестна. Главная ячейка из meta этого не гарантирует.
    org_scopes: dict[str, np.ndarray] = field(default_factory=dict)
    # Потоки между организациями по фактическим ячейкам: индекс (row, t, atom).
    # Нужны для локализации смены ГОСБ без угадывания принимающей ячейки.
    org_flows: pd.DataFrame = field(default_factory=pd.DataFrame)
    # Остатки по фактическим ячейкам: ограничивают размер когорты стенда.
    org_stock: pd.DataFrame = field(default_factory=pd.DataFrame)
    run_id: int | None = None
    scope: str = "all"                    # all / other_orgs — разные совокупности
    material_base: np.ndarray | None = None  # база полного атома для материальности остатка

    @property
    def atoms(self) -> np.ndarray:
        return np.flatnonzero(self.meta["lvl"].to_numpy() == "atom")

    @property
    def orgs(self) -> np.ndarray:
        return np.flatnonzero(self.meta["lvl"].to_numpy() == "org")

    def copy(self) -> "Panel":
        return Panel(list(self.months), self.meta.copy(), {k: v.copy() for k, v in self.x.items()},
                     {k: v.copy() for k, v in self.org_scopes.items()}, self.org_flows.copy(),
                     self.org_stock.copy(), self.run_id, self.scope,
                     None if self.material_base is None else self.material_base.copy())

    def identity_gap(self, t: int) -> np.ndarray:
        """Тождество месяца t по каждому ряду: портфель − база − (пришли − ушли)."""
        x = self.x
        return (x["pf_recip"][:, t] - x["m_base_pf"][:, t]
                - sum(x[m][:, t] for m in IN_M) + sum(x[m][:, t] for m in OUT_M))


# --------------------------------------------------------------------------- #
# Чтение с БД
# --------------------------------------------------------------------------- #
def latest_run(conn) -> int:
    return int(db.scalar(conn, "SELECT max(run_id) FROM {schema_t}.yva_ptf_vintage"))


def load(conn, run_id: int | None = None) -> Panel:
    """Панель поколения run_id (по умолчанию — последнего). Читается по месяцу:
    строк организаций в месяце ~150 тыс., выгрузка за раз ≤ 1 млн."""
    run_id = run_id or latest_run(conn)
    # у старых прогонов новых мер нет (столбцы добавляются прогоном) — читаем, что есть
    have = set(db.read_sql(conn, Q.VINT_COLS, {"schema": config.SCHEMA_T})["column_name"])
    cols = ", ".join(m if m in have else f"CAST(NULL AS float8) AS {m}" for m in MEAS)
    months = [str(m) for m in db.read_sql(
        conn, "SELECT DISTINCT report_dt FROM {schema_t}.yva_ptf_vintage WHERE run_id = :r "
              "ORDER BY 1", {"r": run_id})["report_dt"]]
    atoms, orgs = [], []
    for m in months:
        p = {"r": run_id, "m": m}
        atoms.append(db.read_sql(conn, f"""
            SELECT report_dt, seg_rep, sys_tb_id, sys_gosb_id, {cols}
            FROM {{schema_t}}.yva_ptf_vintage
            WHERE run_id = :r AND lvl = 'atom' AND report_dt = CAST(:m AS date)""", p))
        o = db.read_sql(conn, f"""
            SELECT report_dt, org_id, seg_rep, sys_tb_id, sys_gosb_id, {cols}
            FROM {{schema_t}}.yva_ptf_vintage
            WHERE run_id = :r AND lvl = 'org' AND report_dt = CAST(:m AS date)""", p)
        orgs.append(o)
    p = from_frames(months, pd.concat(atoms, ignore_index=True), pd.concat(orgs, ignore_index=True))
    p.run_id = run_id
    empty = [m for m in MEAS if np.isnan(p.x[m]).all()]
    if empty and set(empty) <= {"m_in_temp_outside", "m_out_temp_left"}:
        print("Старое поколение: нет точных мер маршрутов возврата. "
              "Диагностика старого отчёта доступна; новый сторож требует обновлённой сборки.")
    elif empty:
        print(f"ВНИМАНИЕ: в поколении прогона {run_id} пусты меры {', '.join(empty)} — прогон "
              "был до их добавления. Сторож без них ошибается; нужен новый прогон подложки.")
    return p


def from_frames(months: list[str], atoms: pd.DataFrame, org_cells: pd.DataFrame) -> Panel:
    """Панель из длинных таблиц: атомы и ячейки организаций (организация × атом)."""
    months = [str(m) for m in months]
    mi = {m: i for i, m in enumerate(months)}
    atoms = atoms.assign(report_dt=atoms["report_dt"].astype(str))
    org_cells = org_cells.assign(report_dt=org_cells["report_dt"].astype(str))
    for k in ATOM_KEYS:
        atoms[k] = atoms[k].fillna(-1 if k != "seg_rep" else "—")
        org_cells[k] = org_cells[k].fillna(-1 if k != "seg_rep" else "—")

    akeys = atoms[ATOM_KEYS].drop_duplicates().sort_values(ATOM_KEYS).reset_index(drop=True)
    aidx = {tuple(r): i for i, r in enumerate(akeys.itertuples(index=False))}
    # главная ячейка организации — по последнему месяцу, где она есть
    last = (org_cells.sort_values(["org_id", "report_dt", "pf_recip"])
            .groupby("org_id").tail(1)[["org_id"] + ATOM_KEYS])
    last["atom"] = [aidx.get(tuple(r), -1) for r in last[ATOM_KEYS].itertuples(index=False)]
    otot = org_cells.groupby(["org_id", "report_dt"])[MEAS].sum(min_count=1).reset_index()

    meta = pd.concat([akeys.assign(lvl="atom", org_id=-1, atom=np.arange(len(akeys))),
                      last.assign(lvl="org")], ignore_index=True)
    meta = meta[["lvl", "org_id"] + ATOM_KEYS + ["atom"]]
    n, T = len(meta), len(months)
    x = {m: np.full((n, T), np.nan) for m in MEAS}

    ar = np.array([aidx[tuple(r)] for r in atoms[ATOM_KEYS].itertuples(index=False)])
    at = atoms["report_dt"].map(mi).to_numpy()
    orow = {o: len(akeys) + i for i, o in enumerate(last["org_id"])}
    orr = otot["org_id"].map(orow).to_numpy()
    ot = otot["report_dt"].map(mi).to_numpy()
    for m in MEAS:
        x[m][ar, at] = atoms[m].to_numpy(dtype=float)
        x[m][orr, ot] = otot[m].to_numpy(dtype=float)
    cells = org_cells[["org_id", "report_dt"] + ATOM_KEYS].copy()
    cells["atom"] = [aidx.get(tuple(r), -1) for r in cells[ATOM_KEYS].itertuples(index=False)]
    scopes = {}
    for level, col in (("atom", "atom"), ("gosb", "sys_gosb_id"), ("tb", "sys_tb_id")):
        loc = cells.groupby(["org_id", "report_dt"])[col].agg(["min", "max"])
        values = np.where(loc["min"] == loc["max"], loc["min"], -1)
        a = np.full((n, T), -1, dtype=np.int64)
        rr = [orow[o] for o in loc.index.get_level_values("org_id")]
        tt = [mi[m] for m in loc.index.get_level_values("report_dt")]
        a[rr, tt] = values
        scopes[level] = a
    flows = pd.DataFrame({
        "row": org_cells["org_id"].map(orow).to_numpy(),
        "t": org_cells["report_dt"].map(mi).to_numpy(), "atom": cells["atom"].to_numpy(),
        **{flow: org_cells[measure].to_numpy(dtype=float) for flow, measure in CELL_MEAS.items()},
    }).groupby(["row", "t", "atom"])[list(CELL_MEAS)].sum(min_count=1)
    stock = pd.DataFrame({"row": org_cells["org_id"].map(orow).to_numpy(),
                          "t": org_cells["report_dt"].map(mi).to_numpy(),
                          "atom": cells["atom"].to_numpy(),
                          **{m: org_cells[m].to_numpy(dtype=float)
                             for m in ("pf_recip", "m_base_pf", "pf_fl", "multi_recip")}})
    stock = stock.groupby(["row", "t", "atom"]).sum(min_count=1)
    return Panel(months, meta, x, scopes, flows, stock)


def has_cell_detail(p: Panel) -> bool:
    return (not p.org_flows.empty and set(CELL_MEAS) <= set(p.org_flows.columns)
            and not p.org_stock.empty
            and {"pf_recip", "m_base_pf", "pf_fl", "multi_recip"} <= set(p.org_stock.columns))


def residual_panel(p: Panel) -> Panel | None:
    """Атом минус отслеживаемые организации по их фактическим ячейкам.

    Вычитание исходных счётчиков предшествует коррекции возвратов/календаря.
    Отрицательный остаток или неизвестная привязка — ошибка данных, не ноль.
    Для старой панели без полного набора ячеечных потоков канал недоступен.
    """
    if not has_cell_detail(p):
        return None
    atoms = p.atoms
    x = {}
    for frame, mapping in ((p.org_stock, {m: m for m in ("pf_recip", "m_base_pf", "pf_fl", "multi_recip")}),
                           (p.org_flows, CELL_MEAS)):
        if (frame.index.get_level_values("atom") < 0).any():
            raise ValueError("Остаток организаций: неизвестна фактическая ячейка организации")
        for column, measure in mapping.items():
            grouped = frame[column].groupby(level=["atom", "t"])
            sums = grouped.sum(min_count=1)
            # Один пропуск не должен исчезать в сумме остальных организаций.
            sums[grouped.count() != grouped.size()] = np.nan
            total = np.zeros((len(atoms), len(p.months)))
            aa = pd.Index(atoms).get_indexer(sums.index.get_level_values("atom"))
            if (aa < 0).any():
                raise ValueError("Остаток организаций: ячейки организации нет в сводке")
            total[aa, sums.index.get_level_values("t").to_numpy(dtype=int)] = sums.to_numpy()
            remainder = p.x[measure][atoms] - total
            bad = np.argwhere(remainder < -1e-6)
            if len(bad):
                a, t = bad[0]
                raise ValueError(f"Остаток организаций отрицателен для {measure}: "
                                 f"Атом R{int(atoms[a]):05d}, {p.months[t]}, "
                                 f"итог {p.x[measure][atoms[a], t]:.6g}, "
                                 f"организации {total[a, t]:.6g}. "
                                 "Нужна проверка согласованности ячеек и сводки")
            # Только округлительный след вычитания, не исправление отрицательных данных.
            remainder[(remainder < 0) & (remainder >= -1e-6)] = 0.
            x[measure] = remainder
    meta = p.meta.loc[atoms].reset_index(drop=True).copy()
    meta["atom"] = np.arange(len(atoms))
    return Panel(list(p.months), meta, x, run_id=p.run_id, scope="other_orgs",
                 material_base=p.x["m_base_pf"][atoms])
