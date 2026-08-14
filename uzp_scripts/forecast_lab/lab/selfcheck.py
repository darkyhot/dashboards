"""Самопроверки forecast_lab. Запускаются из тетрадки ПЕРЕД прогоном.

Проверяется не «работает ли код», а то, из-за чего результату нельзя было бы
верить:

1. **Паритет с дэшем.** Векторизованная копия текущей формулы обязана давать те
   же числа, что настоящий `forecast.outflow_model`. Иначе «базовый вариант» в
   таблице лидеров — не та формула, что стоит на проме.
2. **Защита от подглядывания.** Признаки не должны зависеть от прогнозного месяца
   и от будущего. Проверяется провокацией: столбцы >= j подменяются мусором, и
   каждое слагаемое обязано остаться прежним.
3. **Скоринг.** Подсунутая цель обязана дать нулевую ошибку, а «факт закрытого
   месяца» — совпасть с соответствующим наивным вариантом.
4. **Лимит строк.** Заниженный порог обязан уронить выгрузку, а не обрезать её.
5. **Запрещённые колонки.** Ни один SQL лаборатории не читает столбцы, смотрящие
   вперёд.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab import backtest as B          # noqa: E402
from lab import fetch, models          # noqa: E402
from lab import queries as LQ          # noqa: E402
from uzp_dash.dashboards.tb_health import forecast as F   # noqa: E402


class CheckFailed(AssertionError):
    pass


def _ok(name: str) -> None:
    print(f"  ✓ {name}")


# --------------------------------------------------------------------------- #
def fake_chunk(n: int = 500, m: int = 24, seed: int = 7) -> fetch.Chunk:
    """Синтетический чанк с намеренно разной историей: и сезонники, и разовый
    отток, и пары, появившиеся в середине окна."""
    rng = np.random.default_rng(seed)
    months = [str(p) for p in pd.period_range("2024-07", periods=m, freq="M")]
    fl = np.rint(rng.lognormal(4.0, 1.0, (n, m))).astype("float32")
    season = np.isin([int(x[5:7]) for x in months], (7, 8, 9))
    fl[: n // 4] *= np.where(season, 1.5, 1.0).astype("float32")
    out_q = (rng.random((n, m)) < 0.2) * np.rint(fl * 0.05).astype("float32")
    present = np.ones((n, m), dtype=bool)
    present[n // 2:, :5] = False              # четверть пар появилась позже
    fl = np.where(present, fl, 0).astype("float32")
    out_q = np.where(present, out_q, 0).astype("float32")
    var = {
        "fl": fl, "out_q": out_q,
        "new_fl": np.rint(rng.random((n, m)) * 12).astype("float32"),
        "np": np.rint(rng.random((n, m)) * 5).astype("float32"),
        "fot": (fl * 60000).astype("float32"),
        "emp": (fl * 1.5).astype("float32"),
        "pot": np.rint(rng.random((n, m)) * 30).astype("float32"),
        "fo_out": np.rint(out_q + rng.random((n, m)) * 2).astype("float32"),
        "fo_plan": (fl + out_q).astype("float32"),
        "fo_fact": fl.astype("float32"),
        "fo_calc": fl.astype("float32"),
        "pipe_fwd": np.rint(rng.random((n, m)) * 8).astype("float32"),
    }
    return fetch.Chunk(
        gosb=rng.integers(1, 6, n).astype("int32"),
        tb=rng.integers(1, 4, n).astype("int32"),
        inn=(7_000_000_000 + np.arange(n)).astype("int64"),
        seg=rng.choice([21, 22, 1092], n).astype("int16"),
        months=months, present=present, var=var)


def _pooled(ch: fetch.Chunk, eval_idx: list) -> dict:
    """Минимальные пулированные величины для автономной проверки моделей."""
    return {
        "calendar_obs": {j: models.calendar_obs(ch.months, j) for j in eval_idx},
        "seg_rate": {src: {k: {j: np.full(models.SEG_SIZE, 0.03)
                               for j in eval_idx}
                           for k in (1, 3, 6, None)}
                     for src in ("out_q", "fo_out")},
        "growth": {"all": {j: 0.01 for j in eval_idx},
                   "tb": {j: np.full(models.TB_SIZE, 0.01) for j in eval_idx},
                   "seg": {j: np.full(models.SEG_SIZE, 0.01) for j in eval_idx}},
        "pipe_k": {j: 0.6 for j in eval_idx},
    }


# --------------------------------------------------------------------------- #
def check_current_formula() -> None:
    """Копия текущей формулы против оригинала — число в число."""
    ch = fake_chunk()
    for j in (13, 20, 23):
        agg = models.current_agg_frame(ch, j)
        ref_cur = pd.Period(ch.months[j], freq="M").to_timestamp("M").date()
        real = F.outflow_model(agg, ref_cur)
        n_cur, n_cls = models.calendar_obs(ch.months, j)
        mine = models.current_pred(ch, j, F.ONE_OFF_K, F.RECOVERED_K, F.SEASON_UP,
                                   F.SEASON_DOWN, "mean", n_cur, n_cls)
        # порядок строк оригинал не меняет, но полагаться на это нельзя
        key = pd.MultiIndex.from_arrays([real["new_gosb_id"], real["inn"]])
        order = pd.Series(np.arange(len(real)), index=key).reindex(
            pd.MultiIndex.from_arrays([ch.gosb.astype("int64"),
                                       ch.inn.astype("int64")])).to_numpy()
        ref = real["pred"].to_numpy()[order.astype(int)]
        # допуск ОТНОСИТЕЛЬНЫЙ: оригинал считает в float64 по значениям, которые
        # в кэше лежат как float32, и на численности в тысячи это даёт разницу в
        # четвёртом знаке. Расхождение формулы такой малой быть не может — там
        # ветвление, и любая ошибка меняет число в разы
        close = np.isclose(ref, mine, rtol=1e-5, atol=1e-3)
        if not np.all(close):
            bad = int(np.argmax(np.abs(ref - mine)))
            raise CheckFailed(
                f"месяц {ch.months[j]}: копия формулы разошлась с forecast."
                f"outflow_model у {int((~close).sum())} организаций, "
                f"худшая {ref[bad]:.4f} против {mine[bad]:.4f}")
    _ok("копия текущей формулы совпадает с forecast.outflow_model")


def check_no_lookahead() -> None:
    """Признаки не зависят от прогнозного месяца и будущего."""
    ch = fake_chunk()
    eval_idx = [14, 20]
    pooled = _pooled(ch, eval_idx)
    specs = (models.out_variants(True) + models.in_variants()
             + models.pipe_variants(True))
    rng = np.random.default_rng(1)
    for j in eval_idx:
        before = {s["id"]: models.eval_term(ch, j, s, pooled) for s in specs}
        spoiled = fetch.Chunk(
            gosb=ch.gosb, tb=ch.tb, inn=ch.inn, seg=ch.seg, months=ch.months,
            present=ch.present.copy(),
            var={k: v.copy() for k, v in ch.var.items()})
        for v in spoiled.var.values():
            v[:, j:] = rng.random(v[:, j:].shape).astype("float32") * 1e6
        spoiled.present[:, j:] = rng.random(spoiled.present[:, j:].shape) < 0.5
        for s in specs:
            after = models.eval_term(spoiled, j, s, pooled)
            if not np.allclose(before[s["id"]], after, equal_nan=True):
                raise CheckFailed(
                    f"слагаемое {s['id']} изменилось после подмены будущего — "
                    f"значит оно читает месяц {ch.months[j]} или правее")
    _ok(f"ни одно из {len(specs)} слагаемых не заглядывает в прогнозный месяц")


def check_scoring() -> None:
    """Скоринг меряет то, что нужно: цель даёт ноль, наив совпадает с наивом."""
    months = [str(p) for p in pd.period_range("2025-01", periods=6, freq="M")]
    eval_idx = [3, 4, 5]
    rng = np.random.default_rng(3)
    y = {j: rng.random(40) * 1000 + 10 for j in eval_idx}
    plan = {j: y[j] * 1.1 for j in eval_idx}
    valid = {j: np.ones(40, dtype=bool) for j in eval_idx}

    m = B._metrics({j: y[j].copy() for j in eval_idx}, y, plan, valid, eval_idx)
    if abs(m["wape"]) > 1e-12 or abs(m["bias"]) > 1e-12:
        raise CheckFailed(f"идеальный прогноз дал WAPE {m['wape']}, ожидался ноль")

    shifted = {j: y[j] * 1.1 for j in eval_idx}
    m2 = B._metrics(shifted, y, plan, valid, eval_idx)
    if abs(m2["bias"] - 0.1) > 1e-9 or abs(m2["wape"] - 0.1) > 1e-9:
        raise CheckFailed(f"прогноз, завышенный на 10%, дал смещение {m2['bias']:.4f} "
                          f"и WAPE {m2['wape']:.4f} — ожидалось 0.1 и 0.1")
    if abs(m2["exec_pp"] - 100 * 0.1 / 1.1) > 1e-6:
        raise CheckFailed(f"ошибка выполнения {m2['exec_pp']:.4f} п.п. не сходится "
                          f"с завышением на 10% при плане 110%")
    # худший месяц не может быть лучше среднего
    if m2["wape_worst"] + 1e-12 < m2["wape"]:
        raise CheckFailed("худший месяц оказался лучше общего WAPE")
    _ok("скоринг: идеальный прогноз даёт ноль, смещение и п.п. считаются верно")


def check_row_limit() -> None:
    """Лимит строк роняет выгрузку, а не обрезает её молча."""
    df = pd.DataFrame({"a": range(10)})
    try:
        fetch.guard_rows(df, "тест", limit=5)
    except fetch.RowLimitError:
        _ok("превышение лимита строк поднимает ошибку")
        return
    raise CheckFailed("guard_rows пропустил выборку больше лимита")


def check_chunks() -> None:
    """Разложение по чанкам: ни один не больше лимита, ни один ГОСБ не потерян."""
    olds_of = {1: [1], 2: [2, 20], 3: [3], 4: [4], 5: [5]}
    rows_of = {1: 90, 2: 120, 3: 1_000, 4: 10, 5: 300}   # третий не помещается
    target = 200
    chunks = fetch.pack_chunks(olds_of, rows_of, target)
    for c in chunks:
        if c["n_parts"] == 1 and c["rows"] > target:
            raise CheckFailed(f"чанк на {c['rows']} строк больше лимита {target}")
        if c["n_parts"] > 1 and c["rows"] > target:
            raise CheckFailed(f"часть дроблёного ГОСБ на {c['rows']} строк "
                              f"больше лимита {target}")
    seen = {g for c in chunks for g in c["gosb"]}
    if seen != set(olds_of):
        raise CheckFailed(f"потеряны ГОСБ: {set(olds_of) - seen}")
    # у дроблёного ГОСБ ровно n_parts частей с номерами 0..n_parts-1
    big = [c for c in chunks if c["gosb"] == [3]]
    parts = sorted(c["part"] for c in big)
    if parts != list(range(len(big))) or big[0]["n_parts"] != len(big):
        raise CheckFailed(f"дробление ГОСБ 3 сломано: части {parts}, "
                          f"заявлено {big[0]['n_parts']}")
    # старые id ГОСБ, склеенного из двух, попадают в чанк целиком
    with_two = [c for c in chunks if 2 in c["gosb"]][0]
    if not {2, 20} <= set(with_two["old_ids"]):
        raise CheckFailed("склеенный ГОСБ потерял часть своих old_gosb_id")
    _ok(f"разложение по чанкам корректно ({len(chunks)} чанков, лимит соблюдён)")


def check_forbidden_columns() -> None:
    """Ни один SQL не читает колонки, смотрящие вперёд."""
    sql = "\n".join(v for k, v in vars(LQ).items()
                    if isinstance(v, str) and k.isupper())
    hits = [c for c in LQ.FORBIDDEN_COLUMNS if re.search(rf"\b{c}\b", sql)]
    if hits:
        raise CheckFailed(f"в SQL лаборатории встречаются запрещённые колонки: {hits}")
    _ok(f"запрещённых колонок в SQL нет ({len(LQ.FORBIDDEN_COLUMNS)} проверено)")


def check_aggregation() -> None:
    """Сумма по организациям совпадает с тем, что попало в свёртку по единицам."""
    ch = fake_chunk(n=300)
    eval_idx = [10]
    pooled = _pooled(ch, eval_idx)
    pooled.update({"months": ch.months, "seg_codes": sorted(set(int(s) for s in ch.seg)),
                   "tb_ids": sorted(set(int(t) for t in ch.tb)),
                   "gosb_ids": sorted(set(int(g) for g in ch.gosb)),
                   "tb_of_gosb": {int(g): int(t) for g, t in zip(ch.gosb, ch.tb)}})
    ui = B.UnitIndex(pooled)
    spec = {"id": "out:cur", "kind": "cur", **models.CUR_DEFAULTS}
    val = models.eval_term(ch, 10, spec, pooled)
    terms = {g: {10: {}} for g in B.GRAINS}
    idx = {g: ui.idx(ch, g) for g in B.GRAINS}
    ok = {g: idx[g] >= 0 for g in B.GRAINS}
    B._scatter(terms, idx, ok, ui, 10, "out:cur", val)
    for grain in B.GRAINS:
        got = terms[grain][10]["out:cur"].sum()
        want = float(val[ok[grain]].sum())
        if abs(got - want) > 1e-3:
            raise CheckFailed(f"грейн {grain}: свёртка дала {got:.3f}, "
                              f"сумма по организациям {want:.3f}")
    _ok("свёртка к единицам сходится с суммой по организациям на всех грейнах")


def check_combos() -> None:
    """Двойной счёт не проходит в перебор ни одним из двух путей."""
    specs = {"out": models.out_variants(True), "in": models.in_variants(),
             "pipe": models.pipe_variants(True)}
    hist = {s["id"] for s in specs["in"] if s.get("from_history")}
    solo = {s["id"] for s in specs["in"] if s.get("needs_out_none")}
    cmb, dropped = B.combos(specs)

    bad = [(a, b) for a, b, _ in cmb if b in solo and a != "out:none"]
    if bad:
        raise CheckFailed(f"чистая дельта сочетается с оттоком: {bad[:3]}")
    bad = [(b, c) for _, b, c in cmb if b in hist and c != "pipe:none"]
    if bad:
        raise CheckFailed(f"приход из истории сочетается с пайплайном — "
                          f"привлечение учтено дважды: {bad[:3]}")
    # приход по new_fl_cnt обязан попадать под запрет: столбец заполняется только
    # там, где заведена сделка, то есть измеряет тот же пайплайн
    if not {s["id"] for s in specs["in"] if "newfl" in s["id"]} <= hist:
        raise CheckFailed("варианты по new_fl_cnt не помечены как приход из "
                          "истории — они сложатся с пайплайном")
    # сегодняшняя формула дэша обязана остаться представимой
    if ("out:cur", "in:cur_season", "pipe:plan_x1.0") not in cmb:
        raise CheckFailed("текущая формула дэша выпала из перебора — "
                          "сравнивать станет не с чем")
    # ослабление запрета обязано возвращать отброшенное
    wide, dropped_wide = B.combos(specs, allow_double_count=True)
    if len(wide) <= len(cmb) or dropped_wide >= dropped:
        raise CheckFailed("allow_double_count не расширяет сетку")
    _ok(f"сетка комбинаций корректна: {len(cmb):,} сочетаний, "
        f"{dropped:,} отброшено из-за двойного счёта")


def run_all() -> None:
    print("Самопроверки forecast_lab:")
    for fn in (check_forbidden_columns, check_row_limit, check_chunks,
               check_current_formula, check_no_lookahead, check_scoring,
               check_aggregation, check_combos):
        fn()
    print("Все проверки пройдены.")


if __name__ == "__main__":
    run_all()
