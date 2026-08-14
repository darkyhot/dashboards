"""Шаг 3: перебор вариантов по кэшу и скоринг на грейнах отчёта.

Протокол — rolling origin: для месяца M признаки берутся только из месяцев < M,
любые ОБУЧАЕМЫЕ величины (пулированные доли, калибровка) считаются тоже только по
месяцам < M. Никакой вариант не видит ни собственного месяца, ни будущих.

Три прохода по кэшу, и все три нужны:
  A — пулированные величины (доли по сегментам, темпы прироста, разметка единиц).
      Они глобальные: чанк своей доли сегмента не знает;
  B — слагаемые всех вариантов, свёрнутые до единиц разбора. Комбинации вариантов
      собираются уже на грейне единиц, где их 700, а не 5.7 млн;
  C — точность на грейне ОРГАНИЗАЦИЙ, но только для короткого списка лидеров:
      покрывать им весь перебор незачем, отбор идёт по единицам.

Отбор ведётся по ГОСБ×сегмент и ГОСБ. На уровне ТБ наблюдений 12 на месяц —
выбирать по ним нельзя, они идут проверкой.
"""
from __future__ import annotations

import itertools
import time

import numpy as np
import pandas as pd

from uzp_dash import progress

from . import fetch, models

GRAINS = ("gosb_seg", "gosb", "tb", "sb")
# По каким грейнам гоняется ВЕСЬ перебор (остальные — только короткий список).
FULL_GRID_GRAINS = ("gosb_seg", "gosb")

BASES = ("metrics", "orgs")
CALIBS = ("none", "global", "tb", "seg", "affine")

# Сколько лидеров уносим в проходы по организациям и в помесячную детализацию.
TOP_N = 25


# --------------------------------------------------------------------------- #
# Проход A: пулированные величины и разметка единиц
# --------------------------------------------------------------------------- #
def pass_pooled(cache_dir, man: dict, eval_idx: list) -> dict:
    """Глобальные суммы по сегментам / ТБ / банку и производные от них."""
    months = man["months"]
    m = len(months)
    progress.step("Проход A: пулированные доли и разметка единиц")

    seg_codes, tb_ids, gosb_ids = set(), set(), set()
    tb_of_gosb: dict = {}
    cov = {k: {} for k in ("up", "down", "new_fl", "out_q", "fo_out", "np")}
    sums = {lvl: {} for lvl in ("seg", "tb", "all")}
    vars_needed = [v for v in ("fl", "out_q", "new_fl", "fo_out")
                   if v in man["vars"]]
    for lvl in sums:
        sums[lvl] = {v: {} for v in vars_needed}

    for ch in fetch.iter_chunks(cache_dir, man):
        seg_codes.update(int(s) for s in np.unique(ch.seg))
        tb_ids.update(int(t) for t in np.unique(ch.tb))
        gosb_ids.update(int(g) for g in np.unique(ch.gosb))
        # уникальные пары, а не цикл по строкам: чанк — сотни тысяч организаций,
        # а различных ГОСБ в нём единицы
        for g, t in np.unique(np.stack([ch.gosb.astype("int64"),
                                        ch.tb.astype("int64")], 1), axis=0):
            tb_of_gosb.setdefault(int(g), int(t))
        # --- покрытие: сколько РЕАЛЬНОГО движения объясняют учётные столбцы --- #
        # Δfl по организациям — это то, что с портфелем произошло на самом деле.
        # new_fl_cnt и fl_outflow_qty заполняются только там, где заведена задача
        # или сделка, поэтому объяснять весь прирост и всю убыль они не обязаны.
        # Насколько не обязаны — вопрос не мнения, а этих двух отношений.
        d, dok = models._delta_matrix(ch.var["fl"], ch.present)
        up, down = np.maximum(d, 0) * dok, np.maximum(-d, 0) * dok
        for t in range(m):
            cov["up"][t] = cov["up"].get(t, 0.0) + float(up[:, t].sum())
            cov["down"][t] = cov["down"].get(t, 0.0) + float(down[:, t].sum())
            for v in ("new_fl", "out_q", "fo_out", "np"):
                if v in ch.var:
                    cov[v][t] = cov[v].get(t, 0.0) + float(ch.var[v][:, t].sum())

        si, ti = models.by_seg(ch), models.by_tb(ch)
        for v in vars_needed:
            a = ch.var[v]
            for t in range(m):
                col = a[:, t]
                sums["all"][v][t] = sums["all"][v].get(t, 0.0) + float(col.sum())
                sums["seg"][v].setdefault(t, np.zeros(models.SEG_SIZE))
                sums["tb"][v].setdefault(t, np.zeros(models.TB_SIZE))
                sums["seg"][v][t] += np.bincount(si, weights=col,
                                                 minlength=models.SEG_SIZE)
                sums["tb"][v][t] += np.bincount(ti, weights=col,
                                                minlength=models.TB_SIZE)

    pooled = {
        "months": months,
        "seg_codes": sorted(seg_codes), "tb_ids": sorted(tb_ids),
        "gosb_ids": sorted(gosb_ids), "tb_of_gosb": tb_of_gosb,
        "calendar_obs": {j: models.calendar_obs(months, j) for j in eval_idx},
        "seg_rate": {}, "growth": {"all": {}, "tb": {}, "seg": {}},
        "pipe_k": {},
    }

    # --- доли оттока по сегментам (усадка мелких организаций) --------------- #
    for src in [v for v in ("out_q", "fo_out") if v in vars_needed]:
        pooled["seg_rate"][src] = {}
        for k in (1, 3, 6, None):
            pooled["seg_rate"][src][k] = {}
            for j in eval_idx:
                lo = 0 if k is None else max(0, j - k)
                num = sum(sums["seg"][src][t] for t in range(lo, j)) \
                    if j > lo else np.zeros(models.SEG_SIZE)
                den = sum(sums["seg"]["fl"][t] for t in range(lo, j)) \
                    if j > lo else np.zeros(models.SEG_SIZE)
                pooled["seg_rate"][src][k][j] = models.safe_div(num, den)

    # --- пулированный темп прироста ----------------------------------------- #
    for j in eval_idx:
        pooled["growth"]["all"][j] = _growth_scalar(
            [sums["all"]["fl"].get(t, 0.0) for t in range(m)], j)
        pooled["growth"]["tb"][j] = _growth_vector(sums["tb"]["fl"], j,
                                                   models.TB_SIZE)
        pooled["growth"]["seg"][j] = _growth_vector(sums["seg"]["fl"], j,
                                                    models.SEG_SIZE)

    # --- доля реализуемости пайплайна по закрытым месяцам -------------------- #
    plan_tot = man.get("pipe_plan_total") or {}
    fact_tot = man.get("pipe_fact_total") or {}
    for j in eval_idx:
        past = months[:j]
        pl = sum(float(plan_tot.get(t, 0.0)) for t in past)
        fc = sum(float(fact_tot.get(t, 0.0)) for t in past)
        pooled["pipe_k"][j] = float(np.clip(fc / pl, 0.0, 2.0)) if pl > 0 else 1.0

    pooled["coverage"] = coverage(cov, months, eval_idx)
    c = pooled["coverage"]
    tot = c[c["ym"] == "ИТОГО"]
    if not tot.empty:
        r = tot.iloc[0]
        progress.done(
            f"Покрытие учётных столбцов: new_fl_cnt объясняет "
            f"{_pct(r['new_fl_of_up'])} прироста, fl_outflow_qty — "
            f"{_pct(r['out_q_of_down'])} убыли"
            + (f", fact_outflow — {_pct(r['fo_out_of_down'])}"
               if np.isfinite(r["fo_out_of_down"]) else ""))

    progress.done(f"Проход A: {len(gosb_ids)} ГОСБ, {len(tb_ids)} ТБ, "
                  f"сегментов {len(seg_codes)}")
    return pooled


def _pct(x) -> str:
    return "—" if not np.isfinite(x) else f"{x * 100:.1f}%"


def coverage(cov: dict, months: list, eval_idx: list) -> pd.DataFrame:
    """Сколько наблюдаемого движения портфеля объясняют учётные столбцы.

    Прирост и убыль считаются по САМОЙ численности: `Σ max(Δfl, 0)` и
    `Σ max(−Δfl, 0)` на грейне организаций. Это то, что произошло. Дальше — доля,
    которую покрывают столбцы витрины.

    Число решает вопрос, который иначе решается на глаз: стоит ли вообще строить
    приход на `new_fl_cnt` (он заполняется только по организациям с заведённой
    сделкой) и отток на `fl_outflow_qty` (только там, где выставлена задача).
    Если столбец объясняет считанные проценты движения, модель на нём — это
    модель работы сотрудников, а не портфеля.
    """
    rows = []
    for t in eval_idx:
        up, down = cov["up"].get(t, 0.0), cov["down"].get(t, 0.0)
        rows.append({
            "ym": months[t], "growth_up": up, "growth_down": down,
            "new_fl": cov["new_fl"].get(t, np.nan),
            "np_cnt": cov["np"].get(t, np.nan),
            "out_q": cov["out_q"].get(t, np.nan),
            "fo_out": cov["fo_out"].get(t, np.nan),
            "new_fl_of_up": _safe(cov["new_fl"].get(t), up),
            "out_q_of_down": _safe(cov["out_q"].get(t), down),
            "fo_out_of_down": _safe(cov["fo_out"].get(t), down),
        })
    if rows:
        df = pd.DataFrame(rows)
        tot = {"ym": "ИТОГО"}
        for c in ("growth_up", "growth_down", "new_fl", "np_cnt", "out_q", "fo_out"):
            tot[c] = float(np.nansum(df[c])) if df[c].notna().any() else np.nan
        tot["new_fl_of_up"] = _safe(tot["new_fl"], tot["growth_up"])
        tot["out_q_of_down"] = _safe(tot["out_q"], tot["growth_down"])
        tot["fo_out_of_down"] = _safe(tot["fo_out"], tot["growth_down"])
        return pd.concat([df, pd.DataFrame([tot])], ignore_index=True)
    return pd.DataFrame()


def _safe(a, b):
    if a is None or b is None or not np.isfinite(a) or not b:
        return np.nan
    return float(a) / float(b)


GROWTH_WINDOW = 3      # по скольким последним месяцам меряем типичный прирост


def _growth_scalar(series: list, j: int) -> float:
    """Средний месячный прирост портфеля за последние месяцы ДО j."""
    rates = []
    for t in range(max(1, j - GROWTH_WINDOW), j):
        prev = series[t - 1]
        if prev > 0:
            rates.append((series[t] - prev) / prev)
    return float(np.mean(rates)) if rates else 0.0


def _growth_vector(sums_by_month: dict, j: int, size: int) -> np.ndarray:
    acc = np.zeros(size)
    cnt = np.zeros(size)
    for t in range(max(1, j - GROWTH_WINDOW), j):
        prev = sums_by_month.get(t - 1)
        cur = sums_by_month.get(t)
        if prev is None or cur is None:
            continue
        ok = prev > 0
        acc[ok] += (cur[ok] - prev[ok]) / prev[ok]
        cnt[ok] += 1
    return np.where(cnt > 0, acc / np.maximum(cnt, 1), 0.0)


# --------------------------------------------------------------------------- #
# Разметка единиц разбора
# --------------------------------------------------------------------------- #
class UnitIndex:
    """Соответствие организации -> строка таблицы единиц, по одному на грейн."""

    def __init__(self, pooled: dict):
        self.gosb_ids = pooled["gosb_ids"]
        self.tb_ids = pooled["tb_ids"]
        self.seg_codes = pooled["seg_codes"]
        self.tb_of_gosb = pooled.get("tb_of_gosb", {})
        self.gi_of = {g: i for i, g in enumerate(self.gosb_ids)}
        self.ti_of = {t: i for i, t in enumerate(self.tb_ids)}
        self.si_of = {s: i for i, s in enumerate(self.seg_codes)}
        self.n_seg = len(self.seg_codes)
        self.size = {"gosb_seg": len(self.gosb_ids) * self.n_seg,
                     "gosb": len(self.gosb_ids),
                     "tb": len(self.tb_ids), "sb": 1}
        self._g_size = (max(self.gosb_ids) + 2) if self.gosb_ids else 2
        self._dense_g = models.dense(dict(self.gi_of), self._g_size, -1)
        self._dense_t = models.dense(dict(self.ti_of), models.TB_SIZE, -1)
        self._dense_s = models.dense(dict(self.si_of), models.SEG_SIZE, -1)

    def idx(self, ch, grain: str) -> np.ndarray:
        gg = ch.gosb.astype("int64")
        # ГОСБ вне разметки — это ошибка сборки, а не повод молча приписать его
        # к чужой строке: выходящие за таблицу коды дают -1 и выпадают из свёртки
        g = np.where((gg >= 0) & (gg < self._g_size),
                     self._dense_g[np.clip(gg, 0, self._g_size - 1)], -1
                     ).astype("int64")
        if grain == "gosb":
            return g
        if grain == "tb":
            return self._dense_t[models.by_tb(ch)].astype("int64")
        if grain == "sb":
            return np.zeros(ch.n, dtype="int64")
        s = self._dense_s[models.by_seg(ch)].astype("int64")
        both = (g >= 0) & (s >= 0)
        return np.where(both, g * self.n_seg + np.maximum(s, 0), -1)

    def labels(self, grain: str) -> pd.DataFrame:
        """Разметка строк грейна. Всегда есть tb_id — по нему идёт и калибровка
        по ТБ, и обезличивание в отчёте."""
        if grain == "gosb":
            return pd.DataFrame({
                "row": range(len(self.gosb_ids)), "gosb_id": self.gosb_ids,
                "tb_id": [self.tb_of_gosb.get(g, -1) for g in self.gosb_ids],
                "seg_id": 1})
        if grain == "tb":
            return pd.DataFrame({"row": range(len(self.tb_ids)),
                                 "gosb_id": -1, "tb_id": self.tb_ids, "seg_id": 1})
        if grain == "sb":
            return pd.DataFrame({"row": [0], "gosb_id": [-1], "tb_id": [-1],
                                 "seg_id": [1]})
        rows = [(gi * self.n_seg + si, g, self.tb_of_gosb.get(g, -1), s)
                for gi, g in enumerate(self.gosb_ids)
                for si, s in enumerate(self.seg_codes)]
        return pd.DataFrame(rows, columns=["row", "gosb_id", "tb_id", "seg_id"])


# --------------------------------------------------------------------------- #
# Проход B: слагаемые, свёрнутые до единиц
# --------------------------------------------------------------------------- #
def pass_terms(cache_dir, man: dict, pooled: dict, eval_idx: list,
               specs: dict, ui: UnitIndex) -> dict:
    """Считает каждое слагаемое один раз и сворачивает его к единицам.

    Возвращает terms[grain][j][term_id] -> вектор по единицам. Комбинации
    вариантов собираются потом уже здесь, на 700 строках вместо 5.7 млн.
    """
    progress.step(f"Проход B: слагаемые ({sum(len(v) for v in specs.values())} шт) "
                  f"по {len(eval_idx)} месяцам")
    t0 = time.time()
    all_specs = [s for group in specs.values() for s in group]
    terms = {g: {j: {} for j in eval_idx} for g in GRAINS}
    # базы: сумма организаций за закрытый месяц и фактическая цель месяца
    for g in GRAINS:
        for j in eval_idx:
            for key in ("base_orgs", "truth_orgs"):
                terms[g][j][key] = np.zeros(ui.size[g])

    for n_ch, ch in enumerate(fetch.iter_chunks(cache_dir, man), 1):
        idx = {g: ui.idx(ch, g) for g in GRAINS}
        ok = {g: idx[g] >= 0 for g in GRAINS}
        for j in eval_idx:
            for spec in all_specs:
                val = models.eval_term(ch, j, spec, pooled)
                _scatter(terms, idx, ok, ui, j, spec["id"], val)
            _scatter(terms, idx, ok, ui, j, "base_orgs",
                     models.lag(ch.var["fl"], j, 1))
            # цель на грейне организаций — численность самого прогнозного месяца.
            # Единственное место во всём файле, где читается столбец j, и это
            # ФАКТ для сравнения, а не признак
            _scatter(terms, idx, ok, ui, j, "truth_orgs", ch.var["fl"][:, j])
        if n_ch % 10 == 0:
            progress.done(f"  чанков обработано: {n_ch}")
    progress.done(f"Проход B завершён за {time.time() - t0:.0f} с")
    return terms


def _scatter(terms, idx, ok, ui, j, key, val) -> None:
    for g in GRAINS:
        i, mask = idx[g], ok[g]
        acc = terms[g][j].setdefault(key, np.zeros(ui.size[g]))
        acc += np.bincount(i[mask], weights=val[mask].astype("float64"),
                           minlength=ui.size[g])


# --------------------------------------------------------------------------- #
# Таблицы плана и факта по единицам
# --------------------------------------------------------------------------- #
def unit_tables(units: pd.DataFrame, ui: UnitIndex, months: list, metric_id: int
                ) -> dict:
    """plan / fact / собственный прогноз витрины, разложенные по строкам грейна."""
    u = units[units["metric_id"] == metric_id].copy()
    out = {}
    for grain in GRAINS:
        n = ui.size[grain]
        tab = {k: {m: np.full(n, np.nan) for m in months}
               for k in ("plan", "fact", "pred")}
        if grain == "gosb_seg":
            sub = u[(u["level_name"] == "gosb") & (u["seg_id"] != 1)]
            rows = [ui.gi_of.get(int(g), -1) * ui.n_seg + ui.si_of.get(int(s), -1)
                    if (int(g) in ui.gi_of and int(s) in ui.si_of) else -1
                    for g, s in zip(sub["unit_id"], sub["seg_id"])]
        elif grain == "gosb":
            sub = u[(u["level_name"] == "gosb") & (u["seg_id"] == 1)]
            rows = [ui.gi_of.get(int(g), -1) for g in sub["unit_id"]]
        elif grain == "tb":
            sub = u[(u["level_name"] == "tb") & (u["seg_id"] == 1)]
            rows = [ui.ti_of.get(int(g), -1) for g in sub["unit_id"]]
        else:
            sub = u[(u["level_name"] == "sb") & (u["seg_id"] == 1)]
            rows = [0] * len(sub)
        for r, ym, pl, fa, pr in zip(rows, sub["ym"], sub["plan_amt"],
                                     sub["fact_amt"], sub["pred_amt"]):
            if r < 0 or ym not in tab["plan"]:
                continue
            for key, v in (("plan", pl), ("fact", fa), ("pred", pr)):
                cur = tab[key][ym][r]
                tab[key][ym][r] = (0.0 if np.isnan(cur) else cur) + float(v or 0)
        out[grain] = tab
    return out


# --------------------------------------------------------------------------- #
# Сборка комбинаций и скоринг
# --------------------------------------------------------------------------- #
def combos(specs: dict, allow_double_count: bool = False) -> tuple[list, int]:
    """Допустимые сочетания слагаемых. Возвращает (список, сколько отброшено).

    Два запрета, и оба — про ДВОЙНОЙ СЧЁТ. Он опаснее, чем кажется: вариант,
    считающий одно и то же дважды, может случайно погасить чужую ошибку и
    выиграть перебор, а на новых месяцах развалиться.

    1. **Чистая дельта против оттока.** Прирост численности уже включает ушедших,
       поэтому вычитать сверху ещё и модельный отток нельзя.
    2. **Приход из истории против пайплайна.** Любая величина, оценённая по
       прошлому — сглаженная дельта, темп прироста, среднее `new_fl_cnt`, — уже
       содержит привлечение, которое в те месяцы шло по сделкам. План пайплайна
       на прогнозный месяц добавляет привлечение ЕЩЁ РАЗ.

       `new_fl_cnt` попадает под этот запрет прямо по природе столбца: он
       заполняется только там, где заведена сделка, то есть измеряет ровно тот же
       пайплайн, а не органический набор сотрудников.

       Исключение — сезонный приход текущей формулы: это возврат ранее ушедших, а
       не новые сделки. Иначе сегодняшняя формула дэша не была бы представима в
       переборе, а сравнивать надо именно с ней.

    `allow_double_count=True` снимает второй запрет — чтобы при желании ИЗМЕРИТЬ,
    сколько на нём теряется. По умолчанию выключено: такие варианты не годятся к
    переносу в дэш, каким бы ни оказался их WAPE.
    """
    res, dropped = [], 0
    for a, b, c in itertools.product(specs["out"], specs["in"], specs["pipe"]):
        if b.get("needs_out_none") and a["kind"] != "none":
            dropped += 1
            continue
        if (not allow_double_count and b.get("from_history")
                and c["kind"] != "none"):
            dropped += 1
            continue
        res.append((a["id"], b["id"], c["id"]))
    return res, dropped


def score_grid(terms: dict, tabs: dict, ui: UnitIndex, months: list,
               eval_idx: list, specs: dict, grains=FULL_GRID_GRAINS,
               allow_double_count: bool = False) -> pd.DataFrame:
    """Посчитать метрики для всех комбинаций на заданных грейнах."""
    cmb, _ = combos(specs, allow_double_count)
    progress.step(f"Скоринг: {len(cmb) * len(BASES) * len(CALIBS):,} вариантов "
                  f"на грейнах {', '.join(grains)}")
    rows = []
    for grain in grains:
        y = {j: tabs[grain]["fact"][months[j]] for j in eval_idx}
        plan = {j: tabs[grain]["plan"][months[j]] for j in eval_idx}
        base = {
            "metrics": {j: tabs[grain]["fact"][months[j - 1]] for j in eval_idx},
            "orgs": {j: terms[grain][j]["base_orgs"] for j in eval_idx},
        }
        # строки, где факта нет вовсе, из скоринга выпадают: сравнивать не с чем
        valid = {j: np.isfinite(y[j]) & (y[j] > 0) for j in eval_idx}
        for out_id, in_id, pipe_id in cmb:
            raw = {j: (terms[grain][j][out_id], terms[grain][j][in_id],
                       terms[grain][j][pipe_id]) for j in eval_idx}
            for bname in BASES:
                fc0 = {}
                for j in eval_idx:
                    b = base[bname][j]
                    b = np.where(np.isfinite(b), b, 0.0)
                    o, i_, p = raw[j]
                    fc0[j] = np.maximum(b - o + i_ + p, 0.0)
                for calib in CALIBS:
                    fc = _calibrate(fc0, y, valid, eval_idx, calib, ui, grain)
                    met = _metrics(fc, y, plan, valid, eval_idx, full=False)
                    met.pop("_wape_by_month", None)   # помесячно — только для лидеров
                    # имена колонок с суффиксом _term намеренно: колонку «in»
                    # pandas.itertuples переименовывает (это ключевое слово), и
                    # обращение по имени в отчёте молча ломается
                    met.update({"grain": grain, "base": bname, "out_term": out_id,
                                "in_term": in_id, "pipe_term": pipe_id,
                                "calib": calib,
                                "variant": f"{bname}|{out_id}|{in_id}|{pipe_id}"
                                           f"|calib:{calib}"})
                    rows.append(met)
        progress.done(f"  грейн {grain}: {len(rows):,} строк результата")
    return pd.DataFrame(rows)


def forecast_of(variant: str, terms: dict, tabs: dict, ui: UnitIndex, months: list,
                eval_idx: list, grain: str) -> dict:
    """Прогноз одного варианта по месяцам — для помесячной детализации и разбора."""
    bname, out_id, in_id, pipe_id, cal = _parse(variant)
    y = {j: tabs[grain]["fact"][months[j]] for j in eval_idx}
    valid = {j: np.isfinite(y[j]) & (y[j] > 0) for j in eval_idx}
    base = ({j: tabs[grain]["fact"][months[j - 1]] for j in eval_idx}
            if bname == "metrics" else
            {j: terms[grain][j]["base_orgs"] for j in eval_idx})
    fc0 = {}
    for j in eval_idx:
        b = np.where(np.isfinite(base[j]), base[j], 0.0)
        fc0[j] = np.maximum(b - terms[grain][j][out_id] + terms[grain][j][in_id]
                            + terms[grain][j][pipe_id], 0.0)
    return _calibrate(fc0, y, valid, eval_idx, cal, ui, grain)


def refine_top(variants: list, terms: dict, tabs: dict, ui: UnitIndex, months: list,
               eval_idx: list, grain: str) -> pd.DataFrame:
    """Полные метрики для лидеров: процентили и ранговая корреляция.

    В общем переборе они не считаются — стоят сортировок и на десятки тысяч
    вариантов съедают минуты, а отбор всё равно идёт по WAPE.
    """
    y = {j: tabs[grain]["fact"][months[j]] for j in eval_idx}
    plan = {j: tabs[grain]["plan"][months[j]] for j in eval_idx}
    valid = {j: np.isfinite(y[j]) & (y[j] > 0) for j in eval_idx}
    rows = []
    for v in variants:
        fc = forecast_of(v, terms, tabs, ui, months, eval_idx, grain)
        m = _metrics(fc, y, plan, valid, eval_idx, full=True)
        m.pop("_wape_by_month", None)
        rows.append({"grain": grain, "variant": v,
                     **{k: m[k] for k in ("exec_pp_p90", "ape_p50", "ape_p90",
                                          "spearman")}})
    return pd.DataFrame(rows)


def score_months(variants: list, terms: dict, tabs: dict, ui: UnitIndex,
                 months: list, eval_idx: list, grain: str) -> pd.DataFrame:
    """Помесячные метрики лидеров: среднее скрывает провальный месяц."""
    rows = []
    y = {j: tabs[grain]["fact"][months[j]] for j in eval_idx}
    plan = {j: tabs[grain]["plan"][months[j]] for j in eval_idx}
    valid = {j: np.isfinite(y[j]) & (y[j] > 0) for j in eval_idx}
    for v in variants:
        fc = forecast_of(v, terms, tabs, ui, months, eval_idx, grain)
        for j in eval_idx:
            m = _metrics(fc, y, plan, valid, [j])
            rows.append({"variant": v, "grain": grain, "ym": months[j],
                         "wape": m["wape"], "bias": m["bias"],
                         "exec_pp": m["exec_pp"], "spearman": m["spearman"]})
    return pd.DataFrame(rows)


def ablation(best: str, terms: dict, tabs: dict, ui: UnitIndex, months: list,
             eval_idx: list, grain: str) -> pd.DataFrame:
    """Во что обходится отключение каждого слагаемого победителя.

    Это и есть ответ на вопрос «что менять в forecast.py»: если выключение
    оттока почти не портит метрику, значит вся модель оттока работает вхолостую.
    """
    bname, out_id, in_id, pipe_id, cal = _parse(best)
    y = {j: tabs[grain]["fact"][months[j]] for j in eval_idx}
    plan = {j: tabs[grain]["plan"][months[j]] for j in eval_idx}
    valid = {j: np.isfinite(y[j]) & (y[j] > 0) for j in eval_idx}
    rows = []
    cases = {
        "победитель целиком": best,
        "без оттока": f"{bname}|out:none|{in_id}|{pipe_id}|calib:{cal}",
        "без прихода": f"{bname}|{out_id}|in:none|{pipe_id}|calib:{cal}",
        "без пайплайна": f"{bname}|{out_id}|{in_id}|pipe:none|calib:{cal}",
        "без калибровки": f"{bname}|{out_id}|{in_id}|{pipe_id}|calib:none",
        "только база": f"{bname}|out:none|in:none|pipe:none|calib:none",
    }
    for name, variant in cases.items():
        try:
            fc = forecast_of(variant, terms, tabs, ui, months, eval_idx, grain)
        except KeyError:
            continue
        m = _metrics(fc, y, plan, valid, eval_idx)
        rows.append({"case": name, "variant": variant, "grain": grain,
                     "wape": m["wape"], "bias": m["bias"], "exec_pp": m["exec_pp"]})
    return pd.DataFrame(rows)


def _calibrate(fc0: dict, y: dict, valid: dict, eval_idx: list, calib: str,
               ui: UnitIndex, grain: str) -> dict:
    """Калибровка ТОЛЬКО по прошлым месяцам — иначе это подгонка под ответ."""
    if calib == "none":
        return fc0
    out = {}
    for pos, j in enumerate(eval_idx):
        past = eval_idx[:pos]
        if not past:
            out[j] = fc0[j]                     # первый месяц калибровать не по чему
            continue
        if calib == "global":
            num = sum(float(y[t][valid[t]].sum()) for t in past)
            den = sum(float(fc0[t][valid[t]].sum()) for t in past)
            k = num / den if den > 0 else 1.0
            out[j] = fc0[j] * float(np.clip(k, 0.5, 2.0))
        elif calib == "affine":
            X = np.concatenate([np.stack([fc0[t][valid[t]],
                                          np.ones(valid[t].sum())], 1) for t in past])
            Y = np.concatenate([y[t][valid[t]] for t in past])
            if len(Y) < 10:
                out[j] = fc0[j]
                continue
            coef, *_ = np.linalg.lstsq(X, Y, rcond=None)
            b_, a_ = float(coef[0]), float(coef[1])
            out[j] = np.maximum(a_ + b_ * fc0[j], 0.0)
        else:
            # множитель по группе строк (ТБ или сегмент): у грейна ГОСБ×сегмент
            # это разные разбиения одних и тех же строк
            grp = _groups(ui, grain, calib)
            num = np.zeros(int(grp.max()) + 1)
            den = np.zeros(int(grp.max()) + 1)
            for t in past:
                v = valid[t]
                num += np.bincount(grp[v], weights=y[t][v], minlength=len(num))
                den += np.bincount(grp[v], weights=fc0[t][v], minlength=len(den))
            k = np.where(den > 0, num / np.maximum(den, 1e-9), 1.0)
            out[j] = fc0[j] * np.clip(k, 0.5, 2.0)[grp]
    return out


_GROUP_CACHE: dict = {}


def _groups(ui: UnitIndex, grain: str, by: str) -> np.ndarray:
    """Номер группы для каждой строки грейна: по ТБ или по сегменту.

    Кэшируется: разметка не меняется, а калибровка запрашивает её на каждый
    вариант из десятков тысяч.
    """
    key = (id(ui), grain, by)
    if key in _GROUP_CACHE:
        return _GROUP_CACHE[key]
    lab = ui.labels(grain)
    if by == "seg":
        g = np.asarray([ui.si_of.get(int(s), 0) for s in lab["seg_id"]],
                       dtype="int64")
    else:
        g = np.asarray([ui.ti_of.get(int(t), 0) for t in lab["tb_id"]],
                       dtype="int64")
    _GROUP_CACHE[key] = g
    return g


def _metrics(fc: dict, y: dict, plan: dict, valid: dict, eval_idx: list,
             full: bool = True) -> dict:
    """WAPE, смещение, ошибка выполнения и устойчивость по месяцам.

    `full=False` — облегчённый режим для полного перебора: без процентилей и
    ранговой корреляции. Они стоят сортировок и на 25 тыс. вариантов × 4 грейна
    съедают минуты, а на отбор не влияют — отбор идёт по WAPE. Лидерам метрики
    потом пересчитываются полностью.
    """
    tot_abs = tot_y = tot_sign = 0.0
    wapes, execs, sp = [], [], []
    ape_all = []
    for j in eval_idx:
        v = valid[j]
        if not v.any():
            continue
        e = fc[j][v] - y[j][v]
        yy = y[j][v]
        tot_abs += float(np.abs(e).sum())
        tot_y += float(yy.sum())
        tot_sign += float(e.sum())
        wapes.append(float(np.abs(e).sum() / max(yy.sum(), 1e-9)))
        p = plan[j][v]
        has_plan = np.isfinite(p) & (p > 0)
        if has_plan.any():
            execs.append(np.abs(e[has_plan]) / p[has_plan] * 100)
        if full:
            ape_all.append(np.abs(e) / np.maximum(yy, 1e-9))
            if has_plan.any():
                sp.append(_spearman(fc[j][v][has_plan] / p[has_plan],
                                    yy[has_plan] / p[has_plan]))
    if not wapes:
        return {"wape": np.nan, "bias": np.nan, "exec_pp": np.nan,
                "exec_pp_p90": np.nan, "share_gt5pp": np.nan, "ape_p50": np.nan,
                "ape_p90": np.nan, "wape_worst": np.nan, "wape_std": np.nan,
                "spearman": np.nan, "months": 0, "_wape_by_month": []}
    ex = np.concatenate(execs) if execs else np.array([np.nan])
    res = {
        "wape": tot_abs / max(tot_y, 1e-9),
        "bias": tot_sign / max(tot_y, 1e-9),
        "exec_pp": float(np.nanmean(ex)),
        "share_gt5pp": float(np.nanmean(ex > 5)),
        "wape_worst": float(np.max(wapes)),
        "wape_std": float(np.std(wapes)),
        "months": len(wapes),
        "_wape_by_month": wapes,
    }
    if not full:
        return res
    ape = np.concatenate(ape_all) if ape_all else np.array([np.nan])
    sp = [s for s in sp if np.isfinite(s)]
    res.update({
        "exec_pp_p90": float(np.nanpercentile(ex, 90)),
        "ape_p50": float(np.nanpercentile(ape, 50)),
        "ape_p90": float(np.nanpercentile(ape, 90)),
        "spearman": float(np.mean(sp)) if sp else np.nan,
    })
    return res


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Ранговая корреляция без scipy: Пирсон по рангам."""
    if len(a) < 3:
        return np.nan
    ra, rb = _rank(a), _rank(b)
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    den = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / den) if den > 0 else np.nan


def _rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="mergesort")
    r = np.empty(len(x), dtype="float64")
    r[order] = np.arange(len(x), dtype="float64")
    return r


# --------------------------------------------------------------------------- #
# Честный отбор: победитель выбирается по ПРОШЛЫМ месяцам
# --------------------------------------------------------------------------- #
def honest_selection(terms, tabs, ui, months, eval_idx, specs, shortlist,
                     grain="gosb_seg") -> pd.DataFrame:
    """Насколько таблица лидеров — не шум.

    Для каждого месяца M победитель выбирается по месяцам < M и применяется к M.
    Разрыв с «лучшим по всем месяцам» показывает, сколько в отборе подгонки: при
    6–9 оцениваемых месяцах он может быть велик, и знать это обязательно.
    """
    y = {j: tabs[grain]["fact"][months[j]] for j in eval_idx}
    plan = {j: tabs[grain]["plan"][months[j]] for j in eval_idx}
    valid = {j: np.isfinite(y[j]) & (y[j] > 0) for j in eval_idx}
    cache: dict = {}

    def fc_of(variant, sub_idx):
        if variant not in cache:
            bname, out_id, in_id, pipe_id, cal = _parse(variant)
            base = ({j: tabs[grain]["fact"][months[j - 1]] for j in eval_idx}
                    if bname == "metrics"
                    else {j: terms[grain][j]["base_orgs"] for j in eval_idx})
            fc0 = {}
            for j in eval_idx:
                b = np.where(np.isfinite(base[j]), base[j], 0.0)
                fc0[j] = np.maximum(b - terms[grain][j][out_id]
                                    + terms[grain][j][in_id]
                                    + terms[grain][j][pipe_id], 0.0)
            cache[variant] = _calibrate(fc0, y, valid, eval_idx, cal, ui, grain)
        return {j: cache[variant][j] for j in sub_idx}

    rows = []
    for pos, j in enumerate(eval_idx):
        past = eval_idx[:pos]
        if not past:
            continue
        best, best_w = None, np.inf
        for variant in shortlist:
            f = fc_of(variant, past)
            w = _metrics(f, y, plan, valid, past)["wape"]
            if np.isfinite(w) and w < best_w:
                best, best_w = variant, w
        f = fc_of(best, [j])
        m = _metrics(f, y, plan, valid, [j])
        rows.append({"ym": months[j], "chosen": best, "wape_past": best_w,
                     "wape_month": m["wape"], "exec_pp_month": m["exec_pp"]})
    return pd.DataFrame(rows)


def _parse(variant: str) -> tuple:
    bname, out_id, in_id, pipe_id, cal = variant.split("|")
    return bname, out_id, in_id, pipe_id, cal.replace("calib:", "")


# --------------------------------------------------------------------------- #
# Проход C: точность на грейне организаций для короткого списка
# --------------------------------------------------------------------------- #
def pass_orgs(cache_dir, man, pooled, eval_idx, specs, shortlist) -> pd.DataFrame:
    """WAPE и смещение на грейне (ГОСБ, организация) для лидеров.

    Базой служит численность закрытого месяца самой организации: витринной строки
    уровня единицы у организации нет, и вариант базы здесь только один.
    """
    progress.step(f"Проход C: точность по организациям для {len(shortlist)} лидеров")
    spec_of = {s["id"]: s for group in specs.values() for s in group}
    need = sorted({t for v in shortlist for t in _parse(v)[1:4]})
    acc = {v: {"abs": 0.0, "y": 0.0, "signed": 0.0, "n": 0} for v in shortlist}
    for ch in fetch.iter_chunks(cache_dir, man):
        for j in eval_idx:
            vals = {t: models.eval_term(ch, j, spec_of[t], pooled) for t in need}
            base = models.lag(ch.var["fl"], j, 1)
            truth = ch.var["fl"][:, j]
            live = ch.present[:, j] & ch.present[:, j - 1]
            for v in shortlist:
                _, o, i_, p, _cal = _parse(v)
                fc = np.maximum(base - vals[o] + vals[i_] + vals[p], 0.0)
                e = (fc - truth)[live]
                acc[v]["abs"] += float(np.abs(e).sum())
                acc[v]["signed"] += float(e.sum())
                acc[v]["y"] += float(truth[live].sum())
                acc[v]["n"] += int(live.sum())
    rows = [{"variant": v, "wape_orgs": a["abs"] / max(a["y"], 1e-9),
             "bias_orgs": a["signed"] / max(a["y"], 1e-9), "n_org_months": a["n"]}
            for v, a in acc.items()]
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
def baselines(tabs: dict, terms: dict, months: list, eval_idx: list,
              grains=GRAINS) -> pd.DataFrame:
    """Конкуренты БЕЗ грейна организаций — нижняя планка для всей затеи.

    Если наивное «портфель закрытого месяца» или собственный прогноз витрины
    бьют формулу дэша, никакой перебор слагаемых этого не исправит, и вывод
    должен быть другим.
    """
    rows = []
    for grain in grains:
        y = {j: tabs[grain]["fact"][months[j]] for j in eval_idx}
        plan = {j: tabs[grain]["plan"][months[j]] for j in eval_idx}
        valid = {j: np.isfinite(y[j]) & (y[j] > 0) for j in eval_idx}
        cand = {
            "наив: факт закрытого месяца":
                {j: _nz(tabs[grain]["fact"][months[j - 1]]) for j in eval_idx},
            "наив: план как прогноз":
                {j: _nz(tabs[grain]["plan"][months[j]]) for j in eval_idx},
            "витрина: prediction_amt":
                {j: _nz(tabs[grain]["pred"][months[j]]) for j in eval_idx},
            "наив: сумма организаций закрытого месяца":
                {j: terms[grain][j]["base_orgs"] for j in eval_idx},
        }
        for k in (2, 3):
            cand[f"наив: тренд по {k} мес"] = {
                j: _trend(tabs[grain]["fact"], months, j, k) for j in eval_idx}
        for name, fc in cand.items():
            if all(float(np.nansum(np.abs(fc[j]))) == 0.0 for j in eval_idx):
                continue                      # источник пуст (напр. prediction_amt)
            met = _metrics(fc, y, plan, valid, eval_idx)
            met.pop("_wape_by_month", None)
            met.update({"grain": grain, "variant": name, "base": "—",
                        "out_term": "—", "in_term": "—", "pipe_term": "—",
                        "calib": "none"})
            rows.append(met)
    return pd.DataFrame(rows)


def _nz(a):
    return np.where(np.isfinite(a), a, 0.0)


def _trend(fact: dict, months: list, j: int, k: int) -> np.ndarray:
    """Факт закрытого месяца, продлённый средним приростом последних k месяцев."""
    last = _nz(fact[months[j - 1]])
    rates = []
    for t in range(max(1, j - k), j):
        prev, cur = _nz(fact[months[t - 1]]), _nz(fact[months[t]])
        with np.errstate(invalid="ignore", divide="ignore"):
            rates.append(np.where(prev > 0, (cur - prev) / prev, 0.0))
    g = np.mean(rates, axis=0) if rates else 0.0
    return np.maximum(last * (1 + g), 0.0)
