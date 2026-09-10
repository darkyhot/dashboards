"""Шаг 0: разведка витрин. Что есть в данных — до того, как что-то считать.

Разведка отвечает на вопросы, от которых зависит СОСТАВ разбора, а не только его
цифры:

* как называется колонка кода зачисления — `enrollment_type` или
  `enrollment_type_id`. Неверное имя роняет КАЖДЫЙ запрос разбора, поэтому оно
  выясняется по `information_schema`, а не угадывается;
* есть ли в витрине оба опорных месяца и вся история между ними;
* какая доля строк непригодна к джойну по ИНН — и, главное, менялась ли она год к
  году: выросшая доля означает, что часть «падения» произошла в джойне;
* заполнены ли `holding_name` и `industry_name` в справочнике ЕПК (по профилю
  пром-витрины холдинг пуст) — без них разрезы «по холдингам» строить не из чего;
* разрешены ли временные таблицы: на них построена вся арифметика разбора, и
  запасной путь через CTE надо выбрать заранее, а не посреди прогона.

Всё, что не сошлось, попадает в `warnings` и печатается. Молча посчитать раздел
по нулям нельзя: получится «разбор, который ничего не показал», а выглядеть он
будет прилично.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from uzp_dash import config, db, progress

from . import queries as LQ

TABLE = "uzp_data_payroll_m"

# Имена, под которыми может лежать код зачисления. Порядок — предпочтение:
# в профиле пром-витрины колонка называется `enrollment_type`.
CODE_COLUMNS = ("enrollment_type", "enrollment_type_id")

# Ниже этой доли пригодных ИНН джойн перестаёт быть «почти полным», и разрезы по
# организациям надо читать с оговоркой.
INN_OK_MIN = 0.95

# Ниже этой доли опознанных подразделений территориальный разрез по регионам не
# строится вовсе: половина строк в «Регион неизвестен» — это не разрез.
GOSB_MATCH_MIN = 0.60


def _df(engine, sql: str, params: dict | None = None, conn=None) -> pd.DataFrame:
    """Запрос разведки. Отсутствие таблицы — не повод падать: раздел отключится."""
    try:
        return db.read_sql(engine, sql, params or {}, conn=conn)
    except Exception as ex:
        progress.warn(f"запрос разведки недоступен: {type(ex).__name__}: {str(ex)[:200]}")
        return pd.DataFrame()


def _one(df: pd.DataFrame, col: str, default=0):
    if df.empty or col not in df:
        return default
    v = df[col].iloc[0]
    return default if pd.isna(v) else v


def code_column(engine, conn=None) -> tuple[str, list[str]]:
    """Имя колонки кода зачисления — из information_schema.

    Останавливаемся, если не нашлось ни одного: продолжать бессмысленно, весь
    разбор фильтрует именно по этой колонке, и каждый следующий запрос упадёт с
    «column does not exist» — ошибкой, по которой причина не читается.
    """
    df = _df(engine, LQ.PROBE_COLUMNS,
             {"schema": config.SCHEMA, "table": TABLE}, conn=conn)
    cols = [str(c) for c in df["column_name"]] if not df.empty else []
    for name in CODE_COLUMNS:
        if name in cols:
            return name, cols
    raise RuntimeError(
        f"в {config.SCHEMA}.{TABLE} нет ни одной из колонок кода зачисления "
        f"{CODE_COLUMNS}. Найдено {len(cols)} колонок. Разбор фильтрует именно по "
        f"коду зачисления — без неё считать нечего. Проверьте имя витрины и права.")


def temp_tables_allowed(engine, conn) -> bool:
    """Можно ли создавать временные таблицы в этой сессии.

    Проверяется ЗАРАНЕЕ и на игрушечной таблице: узнать про запрет на середине
    прогона, после того как тяжёлая выборка уже отработала, — значит потерять её.
    """
    try:
        db.execute(engine, "CREATE TEMP TABLE t_probe_tmp (x integer)", conn=conn)
        db.execute(engine, "DROP TABLE t_probe_tmp", conn=conn)
        return True
    except Exception as ex:
        progress.warn(f"временные таблицы недоступны ({type(ex).__name__}: "
                      f"{str(ex)[:160]}) — разбор пойдёт запасным путём через CTE, "
                      f"это медленнее")
        return False


def run(engine, conn, months: list[str], d_base: str, d_cur: str, d_from: str,
        out_dir: Path) -> dict:
    """Разведка. Возвращает словарь; он же пишется в out_dir/probe.json."""
    res: dict = {"warnings": [], "months": [str(m) for m in months],
                 "d_base": str(d_base), "d_cur": str(d_cur),
                 "d_from": str(d_from), "seg": LQ.SEG_BIG,
                 "codes": list(LQ.CODES), "amt_min": LQ.AMT_MIN}

    progress.step("Разведка витрин")

    # --- имя колонки кода: без него дальше идти нельзя ---
    code_col, all_cols = code_column(engine, conn=conn)
    res["code_column"] = code_col
    res["n_columns"] = len(all_cols)
    if code_col != CODE_COLUMNS[0]:
        res["warnings"].append(
            f"колонка кода зачисления называется «{code_col}», а не "
            f"«{CODE_COLUMNS[0]}» — запросы построены под найденное имя")
    progress.done(f"витрина ведомостей: {len(all_cols)} колонок, "
                  f"код зачисления — «{code_col}»")

    res["temp_tables"] = temp_tables_allowed(engine, conn)

    # --- справочник ЕПК: сегмент, холдинг, отрасль, статус ---
    epk = _df(engine, LQ.PROBE_EPK, {"seg": LQ.SEG_BIG}, conn=conn)
    if epk.empty or int(_one(epk, "n_rows")) == 0:
        raise RuntimeError(
            "uzp_data_epk_consolidation пуста или недоступна — сегмент организации "
            "брать неоткуда, а весь разбор строится на нём")
    n_rows = int(_one(epk, "n_rows"))
    n_seg_inn = int(_one(epk, "n_seg_inn"))
    res["epk"] = {
        "n_rows": n_rows, "n_inn": int(_one(epk, "n_inn")),
        "n_epk": int(_one(epk, "n_epk")),
        "n_seg_rows": int(_one(epk, "n_seg_rows")), "n_seg_inn": n_seg_inn,
        "n_holding": int(_one(epk, "n_holding")),
        "n_industry": int(_one(epk, "n_industry")),
        "n_name": int(_one(epk, "n_name")),
        "n_active": int(_one(epk, "n_active")),
        "n_liquidated": int(_one(epk, "n_liquidated")),
    }
    if n_seg_inn == 0:
        raise RuntimeError(
            f"в uzp_data_epk_consolidation нет ни одного ИНН сегмента "
            f"'{LQ.SEG_BIG}'. В этой витрине сегмент записан БОЛЬШИМ именем; "
            f"короткий код ('{LQ.SEG_SHORT}') даёт ноль строк.")
    progress.done(f"справочник ЕПК: {n_rows:,} строк, {res['epk']['n_inn']:,} ИНН; "
                  f"{LQ.SEG_BIG}: {n_seg_inn:,} ИНН")

    res["holding_measurable"] = res["epk"]["n_holding"] > 0
    if not res["holding_measurable"]:
        res["warnings"].append(
            "holding_name в uzp_data_epk_consolidation пуст — разрез по холдингам "
            "не строится. Именно это заявлено в профиле пром-витрины: "
            "заполненность 0%")
    else:
        share = res["epk"]["n_holding"] / max(n_rows, 1)
        progress.done(f"холдинг известен у {share:.0%} записей ЕПК")
    res["industry_measurable"] = res["epk"]["n_industry"] > 0
    if not res["industry_measurable"]:
        res["warnings"].append(
            "industry_name в uzp_data_epk_consolidation пуст — разрез по отрасли "
            "справочника не строится, остаётся разбор по наименованию")
    if res["epk"]["n_name"] == 0:
        res["warnings"].append(
            "company_name в uzp_data_epk_consolidation пуст — ведомство и уровень "
            "подчинения выводить не из чего, оба разреза будут пустыми")

    # --- глубина ряда ведомостей ---
    # Имя НЕ `months`: параметр функции называется так же, и локальный кадр
    # затенил бы список опорных месяцев. Проверка ниже пошла бы по названиям
    # колонок кадра и объявила бы, что в витрине нет месяца «report_dt».
    have_df = _df(engine, LQ.PROBE_MONTHS, {"d_from": d_from, "d_to": d_cur},
                  conn=conn)
    if have_df.empty:
        raise RuntimeError(
            f"в {TABLE} нет ни одной строки за {d_from} … {d_cur}. Проверьте "
            f"отчётный месяц (params['report_month']) и права на витрину.")
    have = {str(pd.Timestamp(d).date()) for d in have_df["report_dt"]}
    res["months_in_mart"] = sorted(have)
    res["month_rows"] = {str(pd.Timestamp(d).date()): int(n)
                         for d, n in zip(have_df["report_dt"], have_df["n_rows"])}
    for d in months:
        if str(d) not in have:
            raise RuntimeError(
                f"в {TABLE} нет опорного месяца {d}. Есть месяцы: "
                f"{', '.join(sorted(have)[:5])}… Сравнивать не с чем.")
    progress.done(f"ведомости: {len(have)} мес. ({min(have)} … {max(have)}), "
                  f"строк за отчётный месяц {res['month_rows'][str(d_cur)]:,}")

    # --- недогруженные партиции ---
    # Порог — половина медианы: месяц, где строк вдвое меньше обычного, загружен
    # не полностью, и любой вывод по нему будет выводом про загрузку, а не про людей.
    vals = pd.Series(list(res["month_rows"].values()), dtype="float64")
    med = float(vals.median()) if len(vals) else 0.0
    thin = {m: n for m, n in res["month_rows"].items() if med and n < med * 0.5}
    res["thin_months"] = thin
    if thin:
        res["warnings"].append(
            f"месяцы с числом строк меньше половины медианы: "
            f"{', '.join(sorted(thin))} — партиция загружена не полностью, "
            f"падение в этих точках объясняется загрузкой, а не людьми")

    # --- пригодность ИНН к джойну ---
    mask = _df(engine, LQ.PROBE_INN_MASK, {"months": list(months)}, conn=conn)
    if not mask.empty:
        mask["share_ok"] = mask["n_castable"] / mask["n_rows"].clip(lower=1)
        res["inn_mask"] = [
            {"report_dt": str(pd.Timestamp(r.report_dt).date()),
             "n_rows": int(r.n_rows), "n_castable": int(r.n_castable),
             "share_ok": float(r.share_ok)} for r in mask.itertuples()]
        by = {r["report_dt"]: r["share_ok"] for r in res["inn_mask"]}
        s_base, s_cur = by.get(str(d_base)), by.get(str(d_cur))
        res["inn_ok_base"], res["inn_ok_cur"] = s_base, s_cur
        progress.done(f"ИНН приводится к числу: {s_base:.2%} в базовом месяце, "
                      f"{s_cur:.2%} в отчётном"
                      if s_base is not None and s_cur is not None
                      else "доля пригодных ИНН посчитана")
        if s_base is not None and s_cur is not None:
            # Просело покрытие — часть «падения» случилась в джойне, а не в жизни.
            # Это надо сказать ДО того, как объяснять падение людьми.
            drop = s_base - s_cur
            if drop > 0.005:
                res["warnings"].append(
                    f"доля ИНН, пригодных к джойну, упала с {s_base:.2%} до "
                    f"{s_cur:.2%} ({drop * 100:.2f} п.п.) — часть падения "
                    f"численности произошла в джойне, а не в данных о людях")
            if min(s_base, s_cur) < INN_OK_MIN:
                res["warnings"].append(
                    f"непригодных к джойну ИНН больше "
                    f"{(1 - min(s_base, s_cur)) * 100:.1f}% — разрезы по "
                    f"организациям считаются без этих строк")

    # --- два варианта порога: сверить с отчётностью до, а не после разбора ---
    sc = _df(engine, LQ.PROBE_AMT_SCOPE,
             {"months": list(months), "seg": LQ.SEG_BIG, "codes": list(LQ.CODES),
              "amt_min": LQ.AMT_MIN}, conn=conn)
    if not sc.empty:
        sc = sc.copy()
        sc["report_dt"] = pd.to_datetime(sc["report_dt"]).dt.date.astype(str)
        res["amt_scope"] = sc.to_dict("records")
        for r in sc.itertuples():
            progress.done(
                f"{r.report_dt}: получателей при пороге на организацию "
                f"{int(r.n_scope_inn):,}, при пороге на подразделение "
                f"{int(r.n_scope_triple):,}; людей {int(r.n_epk_inn):,}")
        cur_row = sc[sc["report_dt"] == str(d_cur)]
        if not cur_row.empty:
            a = int(cur_row.iloc[0]["n_scope_inn"])
            b = int(cur_row.iloc[0]["n_scope_triple"])
            if a != b:
                res["warnings"].append(
                    f"порог на организацию и порог на подразделение дают разные "
                    f"числа получателей ({a:,} против {b:,}). Разбор считает по "
                    f"первому — так задано постановкой. Если отчётность сходится "
                    f"со вторым, поменяйте params['amt_scope']")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "probe.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    progress.done(f"Разведка сохранена: {path}")
    for w in res["warnings"]:
        progress.warn(w)
    return res


def pick_gosb_key(match: pd.DataFrame, res: dict) -> str | None:
    """Каким ключом справочника опознаётся ГОСБ ведомостей — или ничем.

    Вызывается ПОСЛЕ сборки рабочего набора: сравниваются те ГОСБ, что реально
    встретились в разборе, а не все подряд.

    Угадывать ключ нельзя. На проме первый прогон подставил один из двух, не
    сошёлся ни одной строкой, и весь территориальный разрез схлопнулся в
    единственную строку-заглушку — которую отчёт вдобавок показал как настоящее
    подразделение. Ошибка не видна по цифрам: разрез из одной строки легко
    принять за свойство данных.
    """
    if match is None or match.empty:
        res["warnings"].append(
            "не удалось проверить, каким ключом справочника опознаётся "
            "подразделение — разрез по регионам не строится")
        return None
    r = match.iloc[0]
    n_used = int(r.get("n_used", 0) or 0)
    n_old = int(r.get("n_old", 0) or 0)
    n_new = int(r.get("n_new", 0) or 0)
    res["gosb_match"] = {"n_used": n_used, "n_old": n_old, "n_new": n_new}
    if not n_used:
        return None
    best, n_best = ("old_gosb_id", n_old) if n_old >= n_new else ("new_gosb_id", n_new)
    share = n_best / n_used
    if share < GOSB_MATCH_MIN:
        res["warnings"].append(
            f"подразделения ведомостей не сходятся со справочником: по "
            f"old_gosb_id опознано {n_old} из {n_used}, по new_gosb_id — {n_new}. "
            f"Разрез по регионам не строится — показывать заглушку под видом "
            f"подразделения хуже, чем не показывать разрез")
        return None
    progress.done(f"подразделения опознаются по «{best}»: {n_best} из {n_used} "
                  f"({share:.0%})")
    res["gosb_key"] = best
    return best
