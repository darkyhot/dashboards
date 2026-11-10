"""Лента шага 2: отклонения на любом срезе и сравнении, их разложение и реальное изменение.

Срезы — вся сводка `yva_ptf_month_org` (все организации): банк, сегмент, ТБ, ГОСБ, отрасль,
класс размера, холдинг (от 2 организаций), крупная организация, сегмент × ТБ, сегмент ×
отрасль. Холдинг и отрасль — текущий срез ЕПК.

Изменение среза за месяц точно раскладывается на составляющие (сумма = изменению):

    отчётное = реальное + внутри банка + учёт и календарь + спорное + неизвестно

* реальное — люди ушли из банка насовсем и пришли извне (кроме возвратов);
* внутри банка — переходы между организациями и совместительство (для банка — только
  совместительство, людей это не меняет);
* учёт и календарь — временный отток с возвратом в следующем месяце, возврат после него,
  переводы между срезами (сегмент);
* спорное — смена кодов зачисления и порог 2 500 ₽: решение аналитика, куда относить;
* неизвестно — уход последнего месяца (временность ещё не видна) и невязка расчёта.

Сравнение = окно (1, 3, 6, 12 мес.; сумма месячных составляющих) × база: без базы (само
изменение), то же окно год назад, два года назад, среднее двух лет. Отклонение = окно − база,
по каждой составляющей. Отбор — по превышению шума того же сравнения на том же срезе
(медиана абсолютных отклонений по истории), а не по размеру; изменение «без базы» не
считается отклонением, если к прошлому году оно обычное (известный сезон).
Пустое значение — «неизвестно», не 0.
"""
from __future__ import annotations

import html
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import db
from . import months as M
from . import sql as Q

WINDOWS = (1, 3, 6, 12)
BASES = {"none": "к самому изменению", "y1": "к тому же периоду год назад",
         "y2": "к тому же периоду два года назад", "avg": "к среднему двух прошлых лет"}
WIN_LABEL = {1: "месяц", 3: "3 мес.", 6: "6 мес.", 12: "12 мес."}
GROUPS = {"real": "Реальное: люди ушли из банка / пришли извне",
          "inside": "Внутри банка: переходы и совместительство",
          "account": "Учёт и календарь",
          "disputed": "Спорное: коды и порог",
          "unknown": "Неизвестно"}
# Составляющая → (подпись, группа).
COMPONENTS = {
    "left": ("Ушли из банка насовсем", "real"),
    "came": ("Пришли извне (кроме возвратов)", "real"),
    "moves": ("Переходы между организациями и совместительство", "inside"),
    "temp_out": ("Временный отток (вернулись в следующем месяце)", "account"),
    "temp_back": ("Возврат после временного оттока", "account"),
    "reclass": ("Переводы между срезами (сегмент)", "account"),
    "codes": ("Смена кодов зачисления", "disputed"),
    "threshold": ("Порог 2 500 ₽", "disputed"),
    "left_unk": ("Ушли из банка: временно или насовсем — ещё неизвестно", "unknown"),
    "gap": ("Невязка расчёта", "unknown"),
}
Z_MIN = 4.0             # отклонение — от стольких шумов
Z_SEASON = 2.0          # «без базы» — только если и к прошлому году не обычное
MIN_HISTORY = 6         # точек истории сравнения для оценки шума
MAT_ABS = 500           # существенность: не меньше стольких троек
MAT_REL = 0.002         # и не меньше такой доли уровня среза
TOP = 10                # сюжетов на странице
PAGE_GROUPS = [("Банк, сегменты, размер организаций", ["Банк", "Сегмент", "Размер организации"]),
               ("Территории", ["ТБ", "ГОСБ", "Сегмент × ТБ"]),
               ("Отрасли", ["Отрасль", "Сегмент × отрасль"]),
               ("Холдинги и организации", ["Холдинг", "Организация"])]


# --------------------------------------------------------------------------- #
# Данные
# --------------------------------------------------------------------------- #
def load(connection, hold_min: int = 1000, org_min: int = 1000, progress=print) -> pd.DataFrame:
    """Ряды срезов из сводки: длинная таблица slice_type, slice_key, report_dt, меры, nn_*."""
    t0 = time.time()
    gp = db.is_greenplum(connection)
    db.execute(connection, "DROP TABLE IF EXISTS t_lenta")
    ddl = "CREATE TEMP TABLE t_lenta AS\n" + Q.T_LENTA + ("\nDISTRIBUTED RANDOMLY" if gp else "")
    n = db.execute(connection, ddl, {"hold_min": hold_min, "org_min": org_min})
    progress(f"Лента: сводка свёрнута до {n:,} строк, {time.time() - t0:.0f} с")
    frame = db.read_sql(connection, Q.LENTA_SERIES)
    progress(f"Лента: {frame[['slice_type', 'slice_key']].drop_duplicates().shape[0]:,} срезов, "
             f"{time.time() - t0:.0f} с")
    return frame


class Series:
    """Ряды срезов как массивы [срез × месяц]; NaN — неизвестно."""

    def __init__(self, frame: pd.DataFrame):
        frame = frame.copy()
        frame["report_dt"] = [M.iso(d) for d in frame["report_dt"]]
        self.months = sorted(frame["report_dt"].unique())
        keys = frame[["slice_type", "slice_key"]].drop_duplicates()
        order = {k: i for i, k in enumerate(t for t, _ in Q.LENTA_SLICES)}
        keys = keys.assign(o=keys["slice_type"].map(order)).sort_values(["o", "slice_key"])
        self.meta = keys[["slice_type", "slice_key"]].reset_index(drop=True)
        ri = {(t, k): i for i, (t, k) in enumerate(zip(self.meta["slice_type"], self.meta["slice_key"]))}
        mi = {m: i for i, m in enumerate(self.months)}
        rows = np.array([ri[(t, k)] for t, k in zip(frame["slice_type"], frame["slice_key"])])
        cols = np.array([mi[m] for m in frame["report_dt"]])
        shape = (len(self.meta), len(self.months))
        self.x = {}
        for c in Q.LENTA_MEAS:
            a = np.full(shape, np.nan)
            v = pd.to_numeric(frame[c], errors="coerce").to_numpy(float)
            nn = pd.to_numeric(frame[f"nn_{c}"], errors="coerce").fillna(0).to_numpy()
            v = np.where(nn > 0, np.nan, v)
            a[rows, cols] = v
            self.x[c] = a
        # Срез без строки в месяце: портфеля нет (0), но потоки неизвестны, если нет и базы.
        present = np.zeros(shape, bool)
        present[rows, cols] = True
        self.x["pf_recip"] = np.where(present, self.x["pf_recip"], 0.)
        self.x["pf_fl"] = np.where(present, self.x["pf_fl"], 0.)

    def index(self, slice_type: str, key: str) -> int:
        hit = np.flatnonzero((self.meta["slice_type"] == slice_type) & (self.meta["slice_key"] == key))
        if not len(hit):
            raise KeyError(f"нет среза {slice_type}: {key}")
        return int(hit[0])


def components(s: Series) -> dict[str, np.ndarray]:
    """Месячные составляющие изменения среза [срез × месяц]; сумма = изменению."""
    x = s.x
    pf = x["pf_recip"]
    prev = np.full_like(pf, np.nan)
    prev[:, 1:] = pf[:, :-1]
    reported = pf - prev
    temp_known = np.isfinite(x["m_out_temp_left"])
    left_all = x["m_out_left_bank"]
    c = {
        "left": np.where(temp_known, -(left_all - x["m_out_temp_left"]), 0.),
        "came": x["m_in_from_outside"] - x["m_in_temp_outside"],
        "moves": x["m_in_from_other_org"] - x["m_out_to_other_org"],
        "temp_out": np.where(temp_known, -x["m_out_temp_left"], 0.),
        "temp_back": x["m_in_temp_outside"],
        "reclass": x["m_base_pf"] - prev,
        "codes": x["m_in_from_nonzp"] - x["m_out_to_nonzp"],
        "threshold": x["m_in_from_zp_out"] - x["m_out_zp_out"],
        "left_unk": np.where(temp_known, 0., -left_all),
    }
    flows = sum(c.values())
    c["gap"] = reported - flows
    # Без базы (нет m_base_pf) все составляющие неизвестны.
    unknown = ~np.isfinite(x["m_base_pf"]) | ~np.isfinite(prev)
    for k in c:
        c[k] = np.where(unknown, np.nan, c[k])
    c["reported"] = np.where(unknown, np.nan, reported)
    fl_prev = np.full_like(pf, np.nan)
    fl_prev[:, 1:] = x["pf_fl"][:, :-1]
    c["people"] = np.where(unknown, np.nan, x["pf_fl"] - fl_prev)
    return c


def window(a: np.ndarray, L: int) -> np.ndarray:
    """Сумма за окно из L месяцев, оканчивающееся в t; NaN, если в окне есть неизвестное."""
    out = np.full_like(a, np.nan)
    if L > a.shape[1]:
        return out
    acc = np.zeros((a.shape[0], a.shape[1] - L + 1))
    for k in range(L):
        acc = acc + a[:, k:a.shape[1] - L + 1 + k]
    out[:, L - 1:] = acc
    return out


def _shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(a, np.nan)
    if k < a.shape[1]:
        out[:, k:] = a[:, :a.shape[1] - k]
    return out


def base_of(a: np.ndarray, base: str) -> np.ndarray:
    if base == "none":
        return np.where(np.isfinite(a), 0., np.nan)
    if base == "y1":
        return _shift(a, 12)
    if base == "y2":
        return _shift(a, 24)
    return (_shift(a, 12) + _shift(a, 24)) / 2.


def _noise(e: np.ndarray) -> np.ndarray:
    """Шум сравнения по срезу: 1,4826 × медиана абсолютных отклонений по истории."""
    out = np.full(e.shape[0], np.nan)
    ok = np.isfinite(e).sum(axis=1) >= MIN_HISTORY
    if ok.any():
        sub = e[ok]
        med = np.nanmedian(sub, axis=1)
        out[ok] = np.nanmedian(np.abs(sub - med[:, None]), axis=1) * 1.4826
    return out


def compare(comp: dict, L: int, base: str) -> dict[str, np.ndarray]:
    """Отклонение «окно − база» по каждой составляющей и итог; плюс окно и база отчётного."""
    out = {}
    for k, a in comp.items():
        w = window(a, L)
        out[k] = w - base_of(w, base)
    w = window(comp["reported"], L)
    out["_this"], out["_base"] = w, base_of(w, base)
    return out


def _group_sums(d: dict, idx=None) -> dict[str, np.ndarray]:
    g = {}
    for key, (_, grp) in COMPONENTS.items():
        v = d[key] if idx is None else d[key][idx]
        g[grp] = v if grp not in g else g[grp] + v
    return g


# --------------------------------------------------------------------------- #
# Поиск отклонений
# --------------------------------------------------------------------------- #
def scan(s: Series, comp: dict, report_months: list[str]) -> pd.DataFrame:
    """Все значимые отклонения (срез × месяц × окно × база) в месяцах отчёта."""
    cols = [s.months.index(m) for m in report_months if m in s.months]
    level = s.x["pf_recip"]
    yoy_z = {}
    rows = []
    for L in WINDOWS:
        for base in BASES:
            d = compare(comp, L, base)
            e = d["reported"]
            sig = _noise(e)
            lvl = _shift(level, L)
            floor = np.maximum(MAT_ABS, MAT_REL * np.nan_to_num(lvl))
            with np.errstate(all="ignore"):
                z = e / np.maximum(sig[:, None], floor)
            if base == "y1":
                yoy_z[L] = z
            g = _group_sums(d)
            for t in cols:
                zt = z[:, t]
                hit = np.flatnonzero(np.isfinite(zt) & (np.abs(zt) >= Z_MIN) & (np.abs(e[:, t]) >= floor[:, t]))
                for i in hit:
                    rows.append({"row": int(i), "t": t, "L": L, "base": base, "z": float(zt[i]),
                                 "excess": float(e[i, t]), "this": float(d["_this"][i, t]),
                                 "base_value": float(d["_base"][i, t]),
                                 **{f"g_{k}": float(v[i, t]) for k, v in g.items()}})
    found = pd.DataFrame(rows)
    if found.empty:
        return found
    # «Без базы» при обычном отличии от прошлого года — известный сезон, не отклонение.
    keep = []
    for r in found.itertuples():
        if r.base == "none" and r.L in yoy_z:
            zy = yoy_z[r.L][r.row, r.t]
            if np.isfinite(zy) and abs(zy) < Z_SEASON:
                keep.append(False)
                continue
        keep.append(True)
    return found[keep].reset_index(drop=True)


def stories(s: Series, found: pd.DataFrame) -> pd.DataFrame:
    """Один сюжет на срез × месяц: сравнение с наибольшим превышением шума; прочие — «видно также»."""
    if found.empty:
        return pd.DataFrame()
    found = found.assign(az=found["z"].abs()).sort_values("az", ascending=False)
    best = found.groupby(["row", "t"], sort=False).head(1).copy()
    also: dict = {}
    for r in found.itertuples():           # уже по убыванию превышения: первое — лучшее
        also.setdefault((r.row, r.t), []).append(f"{WIN_LABEL[r.L]} {_short(r.base)}")
    best["also"] = [", ".join(also[(r, t)][1:]) for r, t in zip(best["row"], best["t"])]
    # Срез, совпадающий с более общим во всех месяцах (сегмент = сегмент × единственная
    # отрасль), — тот же сюжет: остаётся общий.
    dup = _duplicate_rows(s)
    best = best[~best["row"].isin(dup)]
    # Одно событие видно во всех окнах, которые его накрывают: на срезе остаётся
    # сравнение с наибольшим превышением шума, перекрывающиеся окна того же знака — его эхо.
    kept, spans = [], {}
    for r in best.sort_values("az", ascending=False).itertuples():
        lo, hi, sign = r.t - r.L + 1, r.t, np.sign(r.excess)
        if any(sg == sign and lo <= h and l <= hi for l, h, sg in spans.get(r.row, [])):
            continue
        spans.setdefault(r.row, []).append((lo, hi, sign))
        kept.append(r.Index)
    best = best.loc[kept].copy()
    best["slice_type"] = s.meta["slice_type"].to_numpy()[best["row"]]
    best["slice_key"] = s.meta["slice_key"].to_numpy()[best["row"]]
    best["month"] = [s.months[t] for t in best["t"]]
    return best.sort_values("excess", key=np.abs, ascending=False).reset_index(drop=True)


def _duplicate_rows(s: Series) -> set[int]:
    """Срезы, ряд которых совпадает с рядом более общего среза (meta упорядочена от общего)."""
    seen, dup = {}, set()
    pf = np.nan_to_num(s.x["pf_recip"])
    for i in range(len(s.meta)):
        key = pf[i].round(3).tobytes()
        if key in seen:
            dup.add(i)
        else:
            seen[key] = i
    return dup


def _short(base: str) -> str:
    return {"none": "без базы", "y1": "к году назад", "y2": "к двум годам", "avg": "к среднему лет"}[base]


def _table(st: pd.DataFrame) -> pd.DataFrame:
    if st.empty:
        return pd.DataFrame({"Сообщение": ["Значимых отклонений нет"]})
    return pd.DataFrame({
        "Срез": st["slice_key"], "Месяц": [m[:7] for m in st["month"]],
        "Сравнение": [f"{WIN_LABEL[a]} {_short(b)}" for a, b in zip(st["L"], st["base"])],
        "Отклонение": st["excess"].round(), "Реальное": st["g_real"].round(),
        "Внутри банка": st["g_inside"].round(), "Учёт и календарь": st["g_account"].round(),
        "Спорное": st["g_disputed"].round(), "Неизвестно": st["g_unknown"].round(),
        "Шумов": st["z"].round(1)})


# --------------------------------------------------------------------------- #
# Карточка
# --------------------------------------------------------------------------- #
def card(s: Series, comp: dict, slice_type: str, key: str, month: str, L: int, base: str) -> dict:
    """Разложение одного сравнения: составляющие за окно, база, отклонение."""
    i, t = s.index(slice_type, key), s.months.index(M.iso(month))
    d = compare(comp, L, base)
    rows = []
    for k, (label, grp) in COMPONENTS.items():
        w = window(comp[k], L)[i, t]
        b = base_of(window(comp[k], L), base)[i, t]
        rows.append({"Составляющая": label, "Группа": GROUPS[grp].split(":")[0],
                     "Окно": w, "База": b, "Отклонение": d[k][i, t]})
    for k, label in (("people", "в т. ч. люди (ФЛ, доли)"), ("reported", "Итого отчётное")):
        w = window(comp[k], L)[i, t]
        b = base_of(window(comp[k], L), base)[i, t]
        rows.append({"Составляющая": label, "Группа": "", "Окно": w, "База": b, "Отклонение": d[k][i, t]})
    table = pd.DataFrame(rows)
    g = {k: float(v[i, t]) for k, v in _group_sums(d).items()}
    rep = float(d["reported"][i, t])
    start = M.iso(M.shift(month, -(L - 1)))
    period = M.label(month) if L == 1 else f"{M.label(start)} — {M.label(month)}"
    title = f"{slice_type}: {key}, {period}, {WIN_LABEL[L]} {BASES[base]}"
    text = (f"Отклонение {_fmt(rep)} = реальное {_fmt(g['real'])} + внутри банка {_fmt(g['inside'])} "
            f"+ учёт и календарь {_fmt(g['account'])} + спорное {_fmt(g['disputed'])} "
            f"+ неизвестно {_fmt(g['unknown'])}.")
    if np.isfinite(rep) and np.isfinite(g["real"]) and rep:
        text += f" Реальное изменение — {abs(g['real']) / abs(rep) * 100:.0f}% отчётного."
    return {"title": title, "text": text, "table": table, "groups": g, "reported": rep,
            "sql": check_sql(slice_type, key, start, month)}


def _fmt(v: float) -> str:
    return "неизвестно" if v is None or not np.isfinite(v) else f"{v:+,.0f}".replace(",", " ")


def check_sql(slice_type: str, key: str, first: str, last: str) -> str:
    """SQL для проверки внутри контура: те же суммы сводки по срезу и месяцам окна."""
    first, last = M.iso(M.shift(first, -1)), M.iso(last)
    where = {
        "Банк": "TRUE",
        "Сегмент": f"m.seg_rep = '{key}'",
        "ТБ": f"m.sys_tb_id = {key.split()[-1]}",
        "ГОСБ": f"m.sys_gosb_id = {key.split()[-1]}",
        "Организация": f"substr(md5(CAST(m.org_id AS text)), 1, 8) = '{key.split()[-1]}'",
    }.get(slice_type)
    if where is None:
        where = "/* срез ЕПК или сочетание: условие как в sql.T_LENTA */ TRUE"
    return ("SELECT m.report_dt, sum(m.pf_recip) AS pf, sum(m.m_base_pf) AS base,\n"
            "       sum(m.m_out_left_bank) AS left_bank, sum(m.m_out_temp_left) AS temp_left,\n"
            "       sum(m.m_in_from_outside) AS from_outside, sum(m.m_in_temp_outside) AS temp_back\n"
            "FROM s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.yva_ptf_month_org m\n"
            f"WHERE {where}\n  AND m.report_dt BETWEEN DATE '{first}' AND DATE '{last}'\n"
            "GROUP BY m.report_dt ORDER BY 1;")


# --------------------------------------------------------------------------- #
# Отчёт
# --------------------------------------------------------------------------- #
def report(frame: pd.DataFrame, months_back: int = 12, month: str | None = None,
           checks: list | None = None, cards: int = 6) -> dict:
    s = Series(frame)
    comp = components(s)
    report_months = s.months[-months_back:]
    found = scan(s, comp, report_months)
    st = stories(s, found)
    # Банк: последний месяц, где временный отток уже виден (нужен следующий месяц).
    bank = s.index("Банк", "банк")
    known = [m for m in report_months
             if np.isfinite(comp["temp_out"][bank, s.months.index(m)])
             and np.isfinite(s.x["m_out_temp_left"][bank, s.months.index(m)])]
    month = M.iso(month) if month else (known[-1] if known else report_months[-1])
    pulse = [card(s, comp, "Банк", "банк", month, 1, b) for b in ("none", "y1")]
    pages = []
    for title, types in PAGE_GROUPS:
        part = st[st["slice_type"].isin(types)] if not st.empty else st
        pages.append((title, _table(part.head(TOP) if not part.empty else part), len(part)))
    top_cards = [card(s, comp, r.slice_type, r.slice_key, r.month, r.L, r.base)
                 for r in st.head(cards).itertuples()] if not st.empty else []
    check_cards = []
    for c in checks or []:
        try:
            check_cards.append(card(s, comp, *c))
        except (KeyError, ValueError) as ex:
            check_cards.append({"title": " / ".join(map(str, c)), "text": f"Нет данных: {ex}",
                                "table": pd.DataFrame(), "groups": {}, "reported": np.nan, "sql": ""})
    return {"months": s.months, "report_months": report_months, "month": month,
            "n_slices": len(s.meta), "n_found": len(found), "n_stories": len(st),
            "pulse": pulse, "pages": pages, "cards": top_cards, "checks": check_cards,
            "stories": st}


def run(connection, months_back: int = 12, month: str | None = None, checks: list | None = None,
        hold_min: int = 1000, org_min: int = 1000, progress=print) -> dict:
    t0 = time.time()
    frame = load(connection, hold_min, org_min, progress)
    rep = report(frame, months_back, month, checks)
    rep["duration_min"] = (time.time() - t0) / 60.
    progress(f"Лента: {rep['n_found']:,} значимых сравнений → {rep['n_stories']:,} сюжетов, "
             f"{rep['duration_min']:.1f} мин")
    return rep


def write_html(rep: dict, path) -> Path:
    """Полная лента: все сюжеты, карточки, SQL проверки. Номера организаций не выводятся."""
    path = Path(path)
    body = ["<!doctype html><html lang='ru'><meta charset='utf-8'>",
            "<title>Лента портфеля</title><style>body{font:15px sans-serif;margin:24px}"
            "table{border-collapse:collapse;margin-bottom:20px}td,th{padding:4px 10px;border:1px solid #ccc}"
            "pre{background:#f4f4f4;padding:8px}p{max-width:1100px}</style>",
            f"<h1>Лента портфеля: {html.escape(rep['report_months'][0][:7])} — "
            f"{html.escape(rep['report_months'][-1][:7])}</h1>",
            f"<p>Срезов: {rep['n_slices']:,}; значимых сравнений: {rep['n_found']:,}; "
            f"сюжетов: {rep['n_stories']:,}.</p>"]
    for c in [*rep["pulse"], *rep["cards"], *rep["checks"]]:
        body += [f"<h2>{html.escape(c['title'])}</h2>", f"<p><b>{html.escape(c['text'])}</b></p>",
                 c["table"].round(0).to_html(index=False, border=0, na_rep="неизвестно"),
                 f"<pre>{html.escape(c['sql'])}</pre>"]
    st = rep["stories"]
    if not st.empty:
        body += ["<h2>Все сюжеты</h2>", _table(st).to_html(index=False, border=0, na_rep="неизвестно")]
    body.append("</html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")
    return path
