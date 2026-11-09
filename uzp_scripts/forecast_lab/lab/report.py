"""Шаг 4: результаты в обезличенном виде.

Из закрытого контура наружу едет ТОЛЬКО этот комплект, поэтому в нём не должно
быть ни ИНН, ни названий организаций, ни настоящих номеров подразделений: ТБ и
ГОСБ идут псевдонимами (`ТБ-01`, `ГОСБ-045`). Таблица соответствий пишется рядом,
но помечена как непересылаемая — она нужна только на проме, чтобы вернуться от
псевдонима к подразделению.

Сегменты (ММБ, КСБ, …) псевдонимами НЕ заменяются: это классы клиентов, а не
идентификаторы, и без них разбор нечитаем.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from uzp_dash import progress
from uzp_dash.dashboards.tb_health import segments

from . import backtest as B

# Файл с соответствием псевдонимов — остаётся на проме.
MAP_FILE = "pseudonyms_НЕ_ПЕРЕСЫЛАТЬ.csv"
# Сколько лучших строк каждого грейна оставлять в выгружаемой таблице лидеров.
LEADERBOARD_KEEP = 2000


def family_best(lead: pd.DataFrame, grain: str) -> pd.DataFrame:
    """Лучший результат КАЖДОГО семейства слагаемых.

    В таблице лидеров верхушку занимают клоны победителя, отличающиеся в
    четвёртом знаке, и по ней не видно главного: помогает ли вообще отток,
    помогает ли приход. Здесь на каждый вариант слагаемого — его лучший
    достигнутый результат при любых остальных.
    """
    g = lead[(lead["grain"] == grain) & (lead["base"] != "—")]
    if g.empty:
        return pd.DataFrame()
    g = g[np.isfinite(g["wape"])]
    if g.empty:
        return pd.DataFrame()
    rows = []
    for role in ("out_term", "in_term", "pipe_term", "calib", "base"):
        sub = g.loc[g.groupby(role)["wape"].idxmin()]
        for r in sub.itertuples():
            rows.append({"role": role, "family": getattr(r, role),
                         "best_wape": r.wape, "bias": r.bias,
                         "exec_pp": r.exec_pp, "with_variant": r.variant})
    return (pd.DataFrame(rows)
            .sort_values(["role", "best_wape"]).reset_index(drop=True))


class Pseudonyms:
    """Устойчивые псевдонимы подразделений: номер по порядку, а не по id."""

    def __init__(self, tb_ids: list, gosb_ids: list):
        self.tb = {int(t): f"ТБ-{i + 1:02d}" for i, t in enumerate(sorted(tb_ids))}
        self.gosb = {int(g): f"ГОСБ-{i + 1:03d}"
                     for i, g in enumerate(sorted(gosb_ids))}

    def frame(self) -> pd.DataFrame:
        rows = [{"kind": "ТБ", "real_id": k, "pseudonym": v}
                for k, v in self.tb.items()]
        rows += [{"kind": "ГОСБ", "real_id": k, "pseudonym": v}
                 for k, v in self.gosb.items()]
        return pd.DataFrame(rows)


def unit_labels(ui: B.UnitIndex, grain: str, ps: Pseudonyms) -> pd.DataFrame:
    lab = ui.labels(grain).copy()
    lab["unit"] = [
        ps.gosb.get(int(g), "—") if int(g) >= 0 else
        (ps.tb.get(int(t), "—") if int(t) >= 0 else "СБ")
        for g, t in zip(lab["gosb_id"], lab["tb_id"])]
    lab["tb"] = [ps.tb.get(int(t), "—") for t in lab["tb_id"]]
    lab["segment"] = [segments.short(s) if int(s) != 1 else "все"
                      for s in lab["seg_id"]]
    return lab[["row", "unit", "tb", "segment"]]


# --------------------------------------------------------------------------- #
def write_all(out_dir: Path, res: dict) -> list:
    """Записать комплект результатов. Возвращает список файлов."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []

    lead = res["leaderboard"].copy()
    lead = lead.sort_values(["grain", "wape"]).reset_index(drop=True)
    lead["rank_in_grain"] = lead.groupby("grain").cumcount() + 1
    # Полная таблица — это десятки тысяч почти одинаковых строк на добрый десяток
    # мегабайт. Наружу едет верхушка каждого грейна плюс ВСЕ конкуренты без
    # модели: остальное отличается в четвёртом знаке и решения не меняет.
    keep = (lead["rank_in_grain"] <= LEADERBOARD_KEEP) | (lead["base"] == "—")
    if (~keep).any():
        progress.done(f"leaderboard: оставлено {int(keep.sum()):,} строк из "
                      f"{len(lead):,} (по {LEADERBOARD_KEEP} на грейн + все "
                      f"конкуренты без модели)")
    files.append(_csv(out_dir / "leaderboard.csv", lead[keep]))
    # Сводка «лучший в каждом семействе» — по ней видно, какое слагаемое реально
    # помогает, а не какие из клонов победителя стоят рядом в таблице
    fam = family_best(res["leaderboard"], res["primary_grain"])
    if not fam.empty:
        files.append(_csv(out_dir / "family_best.csv", fam))
        res["family_best"] = fam

    for name in ("by_month", "by_unit", "ablation", "honest_selection", "by_orgs",
                 "coverage"):
        df = res.get(name)
        if df is not None and not df.empty:
            files.append(_csv(out_dir / f"{name}.csv", df))

    ps: Pseudonyms = res["pseudonyms"]
    p = out_dir / MAP_FILE
    ps.frame().to_csv(p, index=False)
    progress.warn(f"Таблица псевдонимов {p} — НЕ пересылать из контура")

    (out_dir / "RESULTS.md").write_text(_summary(res), encoding="utf-8")
    files.append(out_dir / "RESULTS.md")

    total = sum(f.stat().st_size for f in files) / 1e6
    progress.done(f"Результаты записаны: {len(files)} файлов, {total:.1f} МБ "
                  f"в {out_dir}")
    return files


def _csv(path: Path, df: pd.DataFrame) -> Path:
    df.to_csv(path, index=False, float_format="%.6g")
    return path


# --------------------------------------------------------------------------- #
def _summary(res: dict) -> str:
    """Короткая сводка. Пишется скриптом, чтобы её нельзя было забыть обновить."""
    probe = res.get("probe") or {}
    lead = res["leaderboard"]
    grain = res["primary_grain"]
    g = lead[lead["grain"] == grain].sort_values("wape")
    base = g[g["base"] == "—"]
    model = g[g["base"] != "—"]

    out = ["# forecast_lab — результаты перебора", ""]
    out.append(f"Контур: `{probe.get('contour_schema', '—')}` · "
               f"месяцев истории: {probe.get('history', {}).get('n_months', '—')} · "
               f"оцениваемых месяцев: {len(res['eval_months'])} "
               f"({', '.join(res['eval_months'])})")
    out.append("")
    out.append(f"Горизонт прогноза — **начало месяца (1–5 число)**: наблюдаемого "
               f"оттока ещё нет, работает только история. Основной грейн отбора — "
               f"`{grain}`, метрика — WAPE и ошибка выполнения плана в п.п.")
    out.append("")

    cov = res.get("coverage")
    if cov is not None and not cov.empty:
        tot = cov[cov["ym"] == "ИТОГО"]
        out.append("## Что учётные столбцы вообще объясняют")
        out.append("")
        out.append("Прирост и убыль считаются по самой численности "
                   "(`Σ max(Δfl, 0)` и `Σ max(−Δfl, 0)` по организациям) — это то, "
                   "что с портфелем произошло. Дальше — доля, которую покрывают "
                   "столбцы витрины.")
        out.append("")
        disp = tot.copy()
        for c in ("new_fl_of_up", "out_q_of_down", "fo_out_of_down"):
            # форматируем строкой: общий формат таблицы даёт четыре знака после
            # запятой, а доле в процентах хватает целых
            disp[c] = disp[c].map(
                lambda v: f"{v * 100:.0f}" if np.isfinite(v) else "—")
        out.append(_table(disp, [
            ("growth_up", "прирост, чел"), ("growth_down", "убыль, чел"),
            ("new_fl_of_up", "new_fl_cnt, % прироста"),
            ("out_q_of_down", "fl_outflow_qty, % убыли"),
            ("fo_out_of_down", "fact_outflow, % убыли")]))
        out.append("")
        if not tot.empty:
            r = tot.iloc[0]
            named = (("new_fl_cnt", r["new_fl_of_up"], "прироста"),
                     ("fl_outflow_qty", r["out_q_of_down"], "убыли"),
                     ("fact_outflow", r["fo_out_of_down"], "убыли"))
            low = [n for n, v, _ in named if np.isfinite(v) and v < 0.25]
            high = [(n, v, w) for n, v, w in named if np.isfinite(v) and v > 1.5]
            if low:
                out.append(f"**{' и '.join(low)} объясняют меньше четверти "
                           f"движения портфеля.** Модель, построенная на них, — "
                           f"это модель работы сотрудников, а не портфеля; "
                           f"приход и отток надёжнее брать из самой численности.")
                out.append("")
            for n, v, w in high:
                out.append(f"**{n} даёт {v * 100:.0f}% {w}** — то есть с "
                           f"движением численности он не сходится вовсе. Столбец "
                           f"считает что-то своё, и строить на нём слагаемое "
                           f"нельзя, пока не выяснено, что именно.")
                out.append("")

    if res.get("dropped_combos"):
        out.append(f"Из перебора исключено {res['dropped_combos']:,} сочетаний с "
                   f"двойным счётом: приход, оценённый по истории, уже содержит "
                   f"привлечение по сделкам, и складывать его с планом пайплайна "
                   f"нельзя. `new_fl_cnt` попадает под запрет по природе столбца — "
                   f"он заполняется только там, где заведена сделка."
                   + (" Запрет ослаблен параметром allow_double_count."
                      if res.get("allow_double_count") else ""))
        out.append("")

    out.append("## Лучшие варианты")
    out.append("")
    out.append(_table(model.head(15), [
        ("variant", "вариант"), ("wape", "WAPE"), ("bias", "смещение"),
        ("exec_pp", "ошибка вып., п.п."), ("wape_worst", "худший месяц"),
        ("wape_std", "разброс"), ("spearman", "ранг. корр.")]))
    out.append("")

    out.append("## Что нужно побить: конкуренты без модели")
    out.append("")
    out.append(_table(base, [
        ("variant", "вариант"), ("wape", "WAPE"), ("bias", "смещение"),
        ("exec_pp", "ошибка вып., п.п.")]))
    out.append("")

    cur = model[model["out_term"] == "out:cur"]
    cur = cur[(cur["in_term"] == "in:cur_season") & (cur["calib"] == "none")
              & (cur["base"] == "metrics")]
    if not cur.empty and not model.empty:
        b, c = model.iloc[0], cur.iloc[0]
        out.append(f"Формула дэша как она есть — WAPE {c['wape']:.4f}, смещение "
                   f"{c['bias']:+.4f}. Лучший вариант перебора — WAPE "
                   f"{b['wape']:.4f}, то есть **в "
                   f"{c['wape'] / max(b['wape'], 1e-9):.2f} раза точнее**.")
        out.append("")

    fam = res.get("family_best")
    if fam is not None and not fam.empty:
        out.append("## Что помогает, а что нет: лучший результат каждого семейства")
        out.append("")
        out.append("Верхушку таблицы лидеров занимают клоны победителя, "
                   "отличающиеся в четвёртом знаке. Здесь — лучшее, чего вообще "
                   "удалось достичь с каждым вариантом слагаемого.")
        out.append("")
        for role, title in (("out_term", "Отток"), ("in_term", "Приход"),
                            ("pipe_term", "Пайплайн"), ("calib", "Калибровка"),
                            ("base", "База")):
            sub = fam[fam["role"] == role]
            if sub.empty:
                continue
            out.append(f"**{title}**")
            out.append("")
            out.append(_table(sub, [("family", "вариант"), ("best_wape", "WAPE"),
                                    ("bias", "смещение"),
                                    ("exec_pp", "ошибка вып., п.п.")]))
            out.append("")

    ab = res.get("ablation")
    if ab is not None and not ab.empty:
        out.append("## Вклад слагаемых победителя")
        out.append("")
        out.append(_table(ab, [("case", "конфигурация"), ("wape", "WAPE"),
                               ("bias", "смещение"), ("exec_pp", "ошибка вып., п.п.")]))
        out.append("")

    hs = res.get("honest_selection")
    if hs is not None and not hs.empty:
        gap = float(hs["wape_month"].mean())
        best_w = float(model["wape"].min()) if not model.empty else float("nan")
        out.append("## Сколько в таблице лидеров подгонки")
        out.append("")
        out.append("Победитель выбирался по ПРОШЛЫМ месяцам и применялся к текущему.")
        out.append("")
        out.append(_table(hs, [("ym", "месяц"), ("chosen", "выбран по прошлому"),
                               ("wape_month", "WAPE месяца")]))
        out.append("")
        out.append(f"Честный WAPE {gap:.4f} против {best_w:.4f} у лучшего по всем "
                   f"месяцам. Разница — цена отбора по той же выборке, на которой "
                   f"считается метрика.")
        out.append("")

    orgs = res.get("by_orgs")
    if orgs is not None and not orgs.empty:
        out.append("## Точность на грейне организаций (вспомогательная)")
        out.append("")
        out.append(_table(orgs.sort_values("wape_orgs").head(10),
                          [("variant", "вариант"), ("wape_orgs", "WAPE по орг."),
                           ("bias_orgs", "смещение")]))
        out.append("")

    out.append("## Ограничения")
    out.append("")
    for w in probe.get("warnings", []):
        out.append(f"* {w}")
    out.append("* Воронка сделок — снимок на сегодня, а не на момент прогноза: "
               "слагаемое пайплайна оценено с этой оговоркой.")
    out.append("* Прогон на синтетике проверяет только механику. Выводы о том, "
               "какая формула точнее, верны только для прома.")
    out.append("")
    out.append("Все подразделения обезличены. Полные таблицы — в CSV рядом.")
    return "\n".join(out)


def _table(df: pd.DataFrame, cols: list) -> str:
    if df is None or df.empty:
        return "_нет данных_"
    head = "| " + " | ".join(t for _, t in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    rows = []
    for r in df.itertuples():
        cells = []
        for key, _ in cols:
            v = getattr(r, key, None)
            if isinstance(v, float) and np.isfinite(v):
                cells.append(f"{v:.4f}" if abs(v) < 100 else f"{v:,.0f}")
            else:
                # идентификатор варианта разделён вертикальной чертой, а она же
                # разделяет ячейки таблицы — без экранирования разъезжается вся
                # разметка, и отчёт читать невозможно
                cells.append(str(v).replace("|", "\\|"))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, sep] + rows)


def dump_config(out_dir: Path, cfg: dict) -> None:
    (out_dir / "run_config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
