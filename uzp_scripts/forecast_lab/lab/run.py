"""Оркестратор: разведка -> выгрузка -> перебор -> результаты.

Вызывается из тетрадки одной строкой. Все параметры — там же, в ячейке запуска.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from uzp_dash import config, db, progress

from . import backtest as B
from . import fetch, models, probe, report
from . import queries as LQ

DEFAULTS = dict(
    # Сколько месяцев истории нужно паре, чтобы месяц вообще оценивался. Меньше
    # трёх — и почти все варианты выродятся в «база без слагаемых».
    min_history=3,
    # Ограничить число оцениваемых месяцев (последние N). None — все доступные.
    max_eval_months=None,
    # Основной грейн отбора. На уровне ТБ наблюдений 12 на месяц — по ним
    # выбирать нельзя, они идут проверкой.
    primary_grain="gosb_seg",
    # Метрика перебора. ФОТ считается победившей формулой отдельно, проверкой.
    metric="recipients",
    use_fact_outflow=True,
    use_pipeline=True,
    # Разрешить сочетания с двойным счётом (приход из истории + план пайплайна).
    # По умолчанию запрещены: такой вариант нельзя переносить в дэш, каким бы ни
    # оказался его WAPE. Включать только чтобы ИЗМЕРИТЬ, сколько на этом теряется.
    allow_double_count=False,
    top_n=B.TOP_N,
    force_fetch=False,
)


def run(conn: str | None = None, cache_dir: str | Path | None = None,
        out_dir: str | Path | None = None, verbose: bool = True,
        show_sql: bool = False, **params) -> dict:
    """Полный прогон. Возвращает словарь результатов, файлы кладёт в out_dir."""
    opts = {**DEFAULTS, **params}
    if not progress.ENABLED:
        progress.enable(verbose=verbose, show_sql=show_sql, llm_log_dir=None)
    t_start = time.time()

    root = Path(__file__).resolve().parents[3]
    cache_dir = Path(cache_dir) if cache_dir else root / "output" / "forecast_lab_cache"
    out_dir = Path(out_dir) if out_dir else root / "output" / "forecast_lab"
    engine = db.get_engine(config.db_url(conn))
    db.ping(engine)
    progress.done(f"Контур: {config.CONTOUR} · схема {config.SCHEMA}")

    # --- шаг 0: разведка ---------------------------------------------------- #
    pr = probe.run(engine, out_dir)
    months = pr["history"]["months"]
    eval_months = months[opts["min_history"]:]
    if opts["max_eval_months"]:
        eval_months = eval_months[-int(opts["max_eval_months"]):]
    if not eval_months:
        raise RuntimeError(
            f"истории всего {len(months)} мес — оценивать нечего "
            f"(нужно минимум {opts['min_history'] + 1})")
    eval_idx = [months.index(m) for m in eval_months]
    progress.done(f"Оценка по {len(eval_months)} месяцам: "
                  f"{eval_months[0]} … {eval_months[-1]}")

    # --- шаг 1: выгрузка ---------------------------------------------------- #
    man = fetch.fetch_all(engine, cache_dir, months,
                          use_fact_outflow=opts["use_fact_outflow"],
                          force=opts["force_fetch"])
    if not man["chunks"]:
        raise RuntimeError("кэш пуст — выгрузка ничего не вернула")
    units = fetch.fetch_units(engine, cache_dir, months, force=opts["force_fetch"])

    # --- шаг 2–3: перебор --------------------------------------------------- #
    res = backtest_from_cache(cache_dir, man, units, months, eval_idx, opts, pr)

    report.dump_config(out_dir, {**opts, "months": months,
                                 "eval_months": eval_months,
                                 "cache_dir": str(cache_dir),
                                 "manifest": {k: v for k, v in man.items()
                                              if k != "chunks"}})
    report.write_all(out_dir, res)
    progress.done(f"Готово за {time.time() - t_start:.0f} с. "
                  f"Отправить наружу: содержимое {out_dir} "
                  f"КРОМЕ {report.MAP_FILE}")
    return res


def backtest_from_cache(cache_dir: Path, man: dict, units: pd.DataFrame,
                        months: list, eval_idx: list, opts: dict,
                        pr: dict | None = None) -> dict:
    """Перебор по готовому кэшу — без единого обращения к БД.

    Отдельная функция намеренно: выгрузка на проме идёт около часа, а сетку после
    неё правят и перезапускают многократно.
    """
    has_fo = bool(man.get("has_fact_outflow"))
    has_pipe = bool(man.get("has_pipeline")) and opts.get("use_pipeline", True)
    specs = {"out": models.out_variants(has_fo),
             "in": models.in_variants(),
             "pipe": models.pipe_variants(has_pipe)}
    dbl = bool(opts.get("allow_double_count", False))
    cmb, dropped = B.combos(specs, dbl)
    n_comb = len(cmb) * len(B.BASES) * len(B.CALIBS)
    progress.done(f"Сетка: {len(specs['out'])} вариантов оттока × "
                  f"{len(specs['in'])} прихода × {len(specs['pipe'])} пайплайна × "
                  f"{len(B.BASES)} баз × {len(B.CALIBS)} калибровок = "
                  f"{n_comb:,} комбинаций")
    progress.done(f"Отброшено {dropped:,} сочетаний с двойным счётом"
                  + (" (запрет ослаблен: allow_double_count=True)" if dbl else
                     " — приход из истории уже содержит привлечение, складывать "
                     "его с планом пайплайна нельзя"))

    pooled = B.pass_pooled(cache_dir, man, eval_idx)
    ui = B.UnitIndex(pooled)
    terms = B.pass_terms(cache_dir, man, pooled, eval_idx, specs, ui)

    metric_id = (LQ.METRIC_RECIPIENTS if opts["metric"] == "recipients"
                 else LQ.METRIC_FOT)
    tabs = B.unit_tables(units, ui, months, metric_id)

    grid = B.score_grid(terms, tabs, ui, months, eval_idx, specs,
                        allow_double_count=dbl)
    base = B.baselines(tabs, terms, months, eval_idx)
    lead = pd.concat([grid, base], ignore_index=True)

    grain = opts["primary_grain"]
    top = (lead[(lead["grain"] == grain) & (lead["base"] != "—")]
           .sort_values("wape").head(int(opts["top_n"]))["variant"].tolist())
    if not top:
        raise RuntimeError("ни один вариант не получил метрику — проверьте, что "
                           "витрина метрик покрывает оцениваемые месяцы")
    best = top[0]
    progress.done(f"Лучший вариант на грейне {grain}: {best}")

    # лидерам метрики досчитываются полностью (процентили, ранговая корреляция):
    # в общем переборе они пропущены сознательно, ради времени
    fine = B.refine_top(top, terms, tabs, ui, months, eval_idx, grain)
    if not fine.empty:
        lead = lead.merge(fine, on=["grain", "variant"], how="left",
                          suffixes=("", "_top"))
        for c in ("exec_pp_p90", "ape_p50", "ape_p90", "spearman"):
            if f"{c}_top" in lead:
                lead[c] = lead[c].fillna(lead.pop(f"{c}_top"))

    by_month = B.score_months(top[:10], terms, tabs, ui, months, eval_idx, grain)
    abl = B.ablation(best, terms, tabs, ui, months, eval_idx, grain)
    honest = B.honest_selection(terms, tabs, ui, months, eval_idx, specs, top, grain)
    by_orgs = B.pass_orgs(cache_dir, man, pooled, eval_idx, specs, top[:5])
    by_unit = _by_unit(top[:5], terms, tabs, ui, months, eval_idx, grain, pooled)

    # ФОТ — проверка: та же формула, другая метрика витрины
    fot = pd.DataFrame()
    if opts.get("metric") == "recipients":
        tabs_fot = B.unit_tables(units, ui, months, LQ.METRIC_FOT)
        if any(np.isfinite(tabs_fot[grain]["fact"][months[j]]).any()
               for j in eval_idx):
            fot = B.score_months(top[:5], terms, tabs_fot, ui, months, eval_idx, grain)
            fot["metric"] = "ФОТ"
            by_month = pd.concat([by_month.assign(metric="получатели"), fot],
                                 ignore_index=True)
        else:
            progress.warn("метрика ФОТ в витрине не покрывает оцениваемые месяцы — "
                          "проверка по ФОТ пропущена")

    ps = report.Pseudonyms(pooled["tb_ids"], pooled["gosb_ids"])
    return {"leaderboard": lead, "by_month": by_month, "by_unit": by_unit,
            "coverage": pooled.get("coverage"), "dropped_combos": dropped,
            "allow_double_count": dbl,
            "ablation": abl, "honest_selection": honest, "by_orgs": by_orgs,
            "pseudonyms": ps, "primary_grain": grain, "probe": pr or {},
            "eval_months": [months[j] for j in eval_idx],
            "terms": terms, "tabs": tabs, "ui": ui, "pooled": pooled,
            "specs": specs, "best": best, "top": top}


def _by_unit(variants: list, terms, tabs, ui, months, eval_idx, grain, pooled
             ) -> pd.DataFrame:
    """Ошибка по каждой единице — где именно прогноз врёт."""
    ps = report.Pseudonyms(pooled["tb_ids"], pooled["gosb_ids"])
    lab = report.unit_labels(ui, grain, ps)
    y = {j: tabs[grain]["fact"][months[j]] for j in eval_idx}
    plan = {j: tabs[grain]["plan"][months[j]] for j in eval_idx}
    rows = []
    for v in variants:
        fc = B.forecast_of(v, terms, tabs, ui, months, eval_idx, grain)
        num = np.zeros(ui.size[grain])
        den = np.zeros(ui.size[grain])
        sgn = np.zeros(ui.size[grain])
        pp = np.zeros(ui.size[grain])
        cnt = np.zeros(ui.size[grain])
        for j in eval_idx:
            ok = np.isfinite(y[j]) & (y[j] > 0)
            e = np.where(ok, fc[j] - y[j], 0.0)
            num += np.abs(e)
            sgn += e
            den += np.where(ok, y[j], 0.0)
            p = plan[j]
            hp = ok & np.isfinite(p) & (p > 0)
            pp += np.where(hp, np.abs(e) / np.maximum(p, 1e-9) * 100, 0.0)
            cnt += hp.astype(float)
        keep = den > 0
        df = lab[keep].copy()
        df["variant"] = v
        df["wape"] = num[keep] / np.maximum(den[keep], 1e-9)
        df["bias"] = sgn[keep] / np.maximum(den[keep], 1e-9)
        df["exec_pp"] = np.where(cnt[keep] > 0, pp[keep] / np.maximum(cnt[keep], 1), np.nan)
        rows.append(df.drop(columns=["row"]))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
