"""Лента шага 2: отклонения на любом срезе и сравнении, их разложение и реальное изменение.

Срезы — вся сводка `yva_ptf_month_org` (все организации): банк, сегмент (один на организацию
по всем ГОСБ) в двух видах — текущий по ЕПК («Сегмент», основные цифры) и на дату выплаты
(история сегментов), ТБ, ГОСБ, отрасль, класс размера, холдинг (от 2 организаций), крупная
организация, оба сегмента × ТБ и × отрасль. Холдинг, отрасль и сегмент — текущий срез ЕПК.
Расхождение видов сегмента — пересегментация (страница «Пересегментация»; у сегмента на дату
выплаты переводы видны строкой «учёта»).

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
# Срез «на дату выплаты» → тот же срез по текущему сегменту (основные цифры).
PAY_VIEW = {"Сегмент на дату выплаты": "Сегмент",
            "Сегмент на дату выплаты × ТБ": "Сегмент × ТБ",
            "Сегмент на дату выплаты × отрасль": "Сегмент × отрасль"}
SAME_STORY = 0.10       # сюжеты двух видов сегмента совпадают, если отклонения ближе 10%
PAGE_GROUPS = [("Банк, сегменты, размер организаций", ["Банк", *Q.SEGMENT_VIEWS, "Размер организации"]),
               ("Территории", ["ТБ", "ГОСБ", "Сегмент × ТБ", "Сегмент на дату выплаты × ТБ"]),
               ("Отрасли", ["Отрасль", "Сегмент × отрасль", "Сегмент на дату выплаты × отрасль"]),
               ("Холдинги и организации", ["Холдинг", "Организация"])]


# --------------------------------------------------------------------------- #
# Данные
# --------------------------------------------------------------------------- #
def load(connection, hold_min: int = 1000, org_min: int = 1000, progress=print) -> pd.DataFrame:
    """Ряды срезов из сводки: длинная таблица slice_type, slice_key, report_dt, меры, nn_*."""
    t0 = time.time()
    gp = db.is_greenplum(connection)
    from . import config
    have = set(db.read_sql(connection, Q.MONTH_ORG_COLS, {"schema": config.SCHEMA_T})["column_name"])
    missing = [c for c in Q.LENTA_MEAS if c not in have]
    if missing:
        progress(f"Лента: в сводке нет столбцов {', '.join(missing)} — они «неизвестны» до новой сборки")
    db.execute(connection, "DROP TABLE IF EXISTS t_lenta")
    ddl = "CREATE TEMP TABLE t_lenta AS\n" + Q.t_lenta(have) + ("\nDISTRIBUTED RANDOMLY" if gp else "")
    n = db.execute(connection, ddl, {"hold_min": hold_min, "org_min": org_min})
    progress(f"Лента: сводка свёрнута до {n:,} строк, {time.time() - t0:.0f} с")
    # Вид среза — отдельный запрос: ошибка одного вида видна одной строкой и не губит остальные.
    parts, failed = [], []
    for kind, key in Q.LENTA_SLICES:
        t1 = time.time()
        try:
            part = db.read_sql(connection, Q.lenta_part(kind, key))
        except Exception as ex:                                     # noqa: BLE001
            db.rollback(connection)
            reason = _db_reason(ex)
            failed.append((kind, reason))
            progress(f"Лента: срез «{kind}» пропущен — {reason}")
            continue
        parts.append(part)
        progress(f"Лента: {kind} — {part['slice_key'].nunique():,} срезов, {time.time() - t1:.0f} с")
    if not parts or "Банк" in [k for k, _ in failed]:
        raise RuntimeError("Лента: ряды срезов не получены — " + "; ".join(f"{k}: {r}" for k, r in failed))
    frame = pd.concat(parts, ignore_index=True)
    frame.attrs["failed"] = failed
    try:
        conf = db.read_sql(connection, Q.LENTA_SEG_CONFLICTS)
        frame.attrs["seg_conflicts"] = {
            M.iso(r.report_dt): {"n_org": int(r.n_org), "pf": float(r.pf),
                                 "n_mix": int(r.n_mix), "pf_mix": float(r.pf_mix)}
            for r in conf.itertuples()}
    except Exception as ex:                                         # noqa: BLE001
        db.rollback(connection)
        progress(f"Лента: проверка сегмента по ГОСБ не выполнена — {_db_reason(ex)}")
    return frame


def _db_reason(ex: Exception) -> str:
    """Первая строка сообщения БД без текста запроса (он длинный и закрывает причину)."""
    orig = getattr(ex, "orig", None)
    msg = str(orig if orig is not None else ex).strip().splitlines()
    return f"{type(orig if orig is not None else ex).__name__}: {msg[0][:300] if msg else ''}"


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
    # Пока не виден следующий месяц, неизвестно, кто ушёл насовсем: реальная часть
    # неизвестна целиком (иначе «реальное» = один приход), уход — в «неизвестно».
    c["left"] = np.where(temp_known, c["left"], np.nan)
    # Без базы (нет m_base_pf) все составляющие неизвестны.
    unknown = ~np.isfinite(x["m_base_pf"]) | ~np.isfinite(prev)
    for k in c:
        c[k] = np.where(unknown, np.nan, c[k])
    c["reported"] = np.where(unknown, np.nan, reported)
    fl_prev = np.full_like(pf, np.nan)
    fl_prev[:, 1:] = x["pf_fl"][:, :-1]
    c["people"] = np.where(unknown, np.nan, x["pf_fl"] - fl_prev)
    # Люди и совместительство (по присутствию в портфеле банка): человек ушёл целиком —
    # у него не осталось ни одной тройки; «другие работы» — его тройки сверх одного человека.
    for k, v in person_parts(x["m_fl_in"], x["m_fl_out"], x["m_in_person_new"],
                             x["m_out_person_gone"], c["reported"],
                             x["m_in_conc"], x["m_out_conc"]).items():
        c[k] = np.where(unknown, np.nan, v)
    return c


PERSON = {"p_out": "Люди ушли из портфеля банка целиком (ФЛ; вкл. временный отток)",
          "p_out_extra": "  их другие работы (совместители ушли целиком: было 3 — стало 0)",
          "p_in": "Люди пришли в портфель банка (ФЛ; вкл. возврат)",
          "p_in_extra": "  их другие работы (совместители пришли)",
          "p_conc": "Остались в банке: стало больше/меньше работ (было 2 — стало 1)",
          "p_move": "Остались в банке: перешли между организациями / срезами"}
# Строки таблицы «Люди»; p_stay = p_conc + p_move — итог оставшихся (в тексте).
PERSON_ROWS = ["p_out", "p_out_extra", "p_in", "p_in_extra", "p_conc", "p_move"]


def person_parts(fl_in, fl_out, in_new, out_gone, reported, in_conc=None, out_conc=None) -> dict:
    """Изменение троек = люди ушли/пришли целиком + их другие работы + оставшиеся.
    Оставшиеся (человек в портфеле банка в обоих месяцах) = совместительство (у него
    стало больше/меньше троек) + переходы (то же число троек, другая организация/срез).
    Сумма частей = изменению; нет счётчика (старая сводка) — неизвестно, не 0."""
    stay = reported - (in_new - out_gone)
    conc = np.nan * stay if in_conc is None else in_conc - out_conc
    return {"p_out": -fl_out, "p_out_extra": -(out_gone - fl_out),
            "p_in": fl_in, "p_in_extra": in_new - fl_in,
            "p_conc": conc, "p_move": stay - conc, "p_stay": stay}


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
    """Все значимые отклонения (срез × месяц × окно × база) в месяцах отчёта.

    Значимо, если отчётное отклонение или его реальная часть — от Z_MIN шумов своего
    сравнения и не меньше порога существенности. «Без базы» — только остаток к году назад
    (окно 12 мес.) или срез без прошлого года: месячное изменение без базы несёт сезон.
    Сравнение с неизвестной составляющей (нет истории для разложения) — не сюжет.
    """
    cols = [s.months.index(m) for m in report_months if m in s.months]
    level = s.x["pf_recip"]
    rows = []
    for L in WINDOWS:
        has_y1 = np.isfinite(base_of(window(comp["reported"], L), "y1"))
        for base in BASES:
            d = compare(comp, L, base)
            e = d["reported"]
            g = _group_sums(d)
            floor = np.maximum(MAT_ABS, MAT_REL * np.nan_to_num(_shift(level, L)))
            with np.errstate(all="ignore"):
                z = e / np.maximum(_noise(e)[:, None], floor)
                zr = g["real"] / np.maximum(_noise(g["real"])[:, None], floor)
            # Реальная часть может быть неизвестна (последний месяц) — тогда сюжет только по отчётному.
            complete = np.isfinite(e) & np.all([np.isfinite(v) for k, v in g.items() if k != "real"], axis=0)
            if base == "none" and L != 12:
                complete &= ~has_y1
            sig = (np.abs(np.nan_to_num(z)) >= Z_MIN) & (np.abs(np.nan_to_num(e)) >= floor)
            sig_r = (np.abs(np.nan_to_num(zr)) >= Z_MIN) & (np.abs(np.nan_to_num(g["real"])) >= floor)
            for t in cols:
                for i in np.flatnonzero(complete[:, t] & (sig[:, t] | sig_r[:, t])):
                    rows.append({"row": int(i), "t": t, "L": L, "base": base, "z": float(z[i, t]),
                                 "z_real": float(zr[i, t]), "real_sig": bool(sig_r[i, t]),
                                 "excess": float(e[i, t]), "this": float(d["_this"][i, t]),
                                 "base_value": float(d["_base"][i, t]),
                                 **{f"g_{k}": float(v[i, t]) for k, v in g.items()}})
    return pd.DataFrame(rows)


def stories(s: Series, found: pd.DataFrame) -> pd.DataFrame:
    """Один сюжет на срез × месяц: сравнение с наибольшим превышением шума; прочие — «видно также»."""
    if found.empty:
        return pd.DataFrame()
    found = found.assign(az=np.maximum(found["z"].abs(), found["z_real"].abs())).sort_values("az", ascending=False)
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
    best = _drop_same_segment_story(best, found, s)
    return best.sort_values("excess", key=np.abs, ascending=False).reset_index(drop=True)


def _drop_same_segment_story(best: pd.DataFrame, found: pd.DataFrame, s: Series) -> pd.DataFrame:
    """Сюжет «на дату выплаты», который повторяет текущий сегмент (то же сравнение того же
    ключа там тоже значимо, отклонения ближе SAME_STORY), — тот же сюжет: остаётся текущий.
    Расходящиеся остаются: их разница и есть пересегментация."""
    types, keys = s.meta["slice_type"].to_numpy(), s.meta["slice_key"].to_numpy()
    cur = {(types[r.row], keys[r.row], r.t, r.L, r.base): r.excess
           for r in found.itertuples() if types[r.row] in PAY_VIEW.values()}
    keep = []
    for r in best.itertuples():
        twin = cur.get((PAY_VIEW.get(r.slice_type), r.slice_key, r.t, r.L, r.base))
        keep.append(twin is None or abs(r.excess - twin) > max(MAT_ABS, SAME_STORY * abs(twin)))
    return best[np.array(keep, dtype=bool)]


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
    st = st.copy()
    return _unknown(pd.DataFrame({
        "Срез": [f"{k} (на дату выпл.)" if t in PAY_VIEW else k
                 for t, k in zip(st["slice_type"], st["slice_key"])],
        "Месяц": [m[:7] for m in st["month"]],
        "Сравнение": [f"{WIN_LABEL[a]} {_short(b)}" for a, b in zip(st["L"], st["base"])],
        "Отклонение": st["excess"].round(), "Реальное": st["g_real"].round(),
        "Внутри банка": st["g_inside"].round(), "Учёт и календарь": st["g_account"].round(),
        "Спорное": st["g_disputed"].round(), "Неизвестно": st["g_unknown"].round(),
        "Шумов": st["z"].round(1), "Шумов реальн.": st["z_real"].round(1)}))


def _unknown(frame: pd.DataFrame) -> pd.DataFrame:
    """Пустое значение на странице — словом «неизв.», не NaN и не 0."""
    out = frame.astype(object)
    return out.where(frame.notna(), "неизв.")


# --------------------------------------------------------------------------- #
# Карточка
# --------------------------------------------------------------------------- #
def card(s: Series, comp: dict, slice_type: str, key: str, month: str, L: int, base: str) -> dict:
    """Разложение одного сравнения: составляющие за окно, база, отклонение; люди и
    совместительство; для остатка к году назад — то же по меткам y_."""
    i, t = s.index(slice_type, key), s.months.index(M.iso(month))
    d = compare(comp, L, base)

    def row(k, label, grp=""):
        w = window(comp[k], L)[i, t]
        return {"Составляющая": label, "Группа": grp, "Окно": w,
                "База": base_of(window(comp[k], L), base)[i, t], "Отклонение": d[k][i, t]}

    table = pd.DataFrame([row(k, label, GROUPS[grp].split(":")[0]) for k, (label, grp) in COMPONENTS.items()]
                         + [row("reported", "Итого отчётное")])
    people = pd.DataFrame([row(k, PERSON[k]) for k in PERSON_ROWS] + [row("reported", "Итого отчётное")])
    year = None
    if L == 12 and base == "none":
        x = s.x
        rep12 = window(comp["reported"], 12)[i, t]
        parts = person_parts(x["y_fl_in"][i, t], x["y_fl_out"][i, t], x["y_in_person_new"][i, t],
                             x["y_out_person_gone"][i, t], rep12, x["y_in_conc"][i, t], x["y_out_conc"][i, t])
        year = pd.DataFrame([{"Составляющая": PERSON[k], "К году назад": parts[k]} for k in PERSON_ROWS]
                            + [{"Составляющая": "Итого отчётное", "К году назад": rep12}])
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
    pe = {k: float(d[k][i, t]) for k in [*PERSON_ROWS, "p_stay"]}
    if all(np.isfinite(pe[k]) for k in ("p_out", "p_out_extra", "p_in", "p_in_extra", "p_stay")):
        text += (f" Люди: ушли целиком {_fmt(pe['p_out'])} (и их другие работы {_fmt(pe['p_out_extra'])}), "
                 f"пришли {_fmt(pe['p_in'])} ({_fmt(pe['p_in_extra'])}); у оставшихся {_fmt(pe['p_stay'])}")
        text += (f": совместительство {_fmt(pe['p_conc'])}, переходы {_fmt(pe['p_move'])}."
                 if np.isfinite(pe["p_conc"]) else ".")
    return {"title": title, "text": text, "table": table, "people": people, "year": year,
            "groups": g, "reported": rep, "sql": check_sql(slice_type, key, start, month)}


def _fmt(v: float) -> str:
    return "неизвестно" if v is None or not np.isfinite(v) else f"{v:+,.0f}".replace(",", " ")


def check_sql(slice_type: str, key: str, first: str, last: str) -> str:
    """SQL для проверки внутри контура: те же суммы сводки по срезу и месяцам окна."""
    first, last = M.iso(M.shift(first, -1)), M.iso(last)
    where = {
        "Банк": "TRUE",
        "Сегмент на дату выплаты": f"m.seg_rep = '{key}'  /* сегмент ГОСБ с наибольшим числом получателей */",
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
# Пересегментация
# --------------------------------------------------------------------------- #
def resegmentation(s: Series, comp: dict, month: str) -> pd.DataFrame:
    """Получатели сегмента в двух видах в месяце и переводы за 12 месяцев.

    Текущий − на дату выплаты: сколько получателей сегмент «получил» пересегментацией (по ЕПК
    организация уже в сегменте, по истории на дату выплаты — ещё нет, или наоборот). Переводы за 12 мес. —
    составляющая «переводы между срезами» сегмента на дату выплаты: сколько ушло / пришло
    сменой сегмента без движения людей."""
    t = s.months.index(M.iso(month))
    pf = s.x["pf_recip"][:, t]
    moved = window(comp["reclass"], 12)[:, t]
    views = {}
    for kind in Q.SEGMENT_VIEWS:
        sel = np.flatnonzero(s.meta["slice_type"].to_numpy() == kind)
        views[kind] = {s.meta.at[i, "slice_key"]: i for i in sel}
    keys = sorted(set().union(*[set(v) for v in views.values()]))
    rows = []
    for k in keys:
        get = lambda kind, a: a[views[kind][k]] if k in views[kind] else np.nan   # noqa: E731
        cur, rep = (get(v, pf) for v in Q.SEGMENT_VIEWS)
        rows.append({"Сегмент": k, "Текущий (ЕПК)": cur, "На дату выплаты": rep,
                     "Текущий − на дату выплаты": cur - rep,
                     "Переводы за 12 мес.": get("Сегмент на дату выплаты", moved)})
    out = pd.DataFrame(rows)
    if not out.empty:                      # строки без получателей в обоих видах — не показывать
        out = out[out[["Текущий (ЕПК)", "На дату выплаты"]].fillna(0).abs().sum(axis=1) > 0]
        out = out.reindex(out["Текущий (ЕПК)"].fillna(0).abs().sort_values(ascending=False).index)
    return out.reset_index(drop=True)


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
    real = st[st["real_sig"]].sort_values("g_real", key=np.abs, ascending=False) if not st.empty else st
    pages = [("Главное реальное изменение (люди ушли / пришли), все срезы",
              _table(real.head(TOP) if not real.empty else real), len(real))]
    for title, types in PAGE_GROUPS:
        part = st[st["slice_type"].isin(types)] if not st.empty else st
        pages.append((title, _table(part.head(TOP) if not part.empty else part), len(part)))
    picked = []
    if not st.empty:                       # половина — главное реальное, половина — крупнейшее
        for r in [*real.head(cards // 2).itertuples(), *st.itertuples()]:
            key = (r.slice_type, r.slice_key, r.month, r.L, r.base)
            if key not in picked:
                picked.append(key)
            if len(picked) >= cards:
                break
    top_cards = [card(s, comp, *k) for k in picked]
    reseg = resegmentation(s, comp, month)
    pages.append((f"Пересегментация, {M.label(month)}: получатели по двум видам сегмента",
                  _unknown(reseg.round(0).head(TOP)), len(reseg)))
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
    rep["failed"] = list(frame.attrs.get("failed", []))
    rep["seg_conflicts"] = frame.attrs.get("seg_conflicts")
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
        body += [f"<h2>{html.escape(c['title'])}</h2>", f"<p><b>{html.escape(c['text'])}</b></p>"]
        for part in (c["table"], c.get("people"), c.get("year")):
            if part is not None and not part.empty:
                body.append(part.round(0).to_html(index=False, border=0, na_rep="неизвестно"))
        body.append(f"<pre>{html.escape(c['sql'])}</pre>")
    st = rep["stories"]
    if not st.empty:
        body += ["<h2>Все сюжеты</h2>", _table(st).to_html(index=False, border=0, na_rep="неизвестно")]
    body.append("</html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")
    return path
