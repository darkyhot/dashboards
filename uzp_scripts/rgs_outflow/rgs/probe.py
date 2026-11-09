"""Шаг 0: разведка витрин. Что вообще есть в данных — до того, как считать.

Разведка отвечает на вопросы, от которых зависит СОСТАВ отчёта, а не только его
цифры:

* сколько месяцев истории у витрины оттока — в профиле пром-витрины отчётная дата
  РОВНО ОДНА, и тогда тенденции по ней построить нельзя вовсе;
* есть ли история глубже в других витринах (возвраты, витрина организаций);
* заполнены ли `other_inn_emp_perc` и `prev_m_overflow_qty` — на проме оба пусты,
  и признаки «куда ушли» считать не из чего;
* годен ли `oktmo_subject_code` (на проме — нет) и хватает ли полных ОКТМО;
* какую долю бюджетной сферы покрывает витрина ключевых клиентов — без этой доли
  блок конкурентов читается как «конкурентов нет».

Результат — словарь и файл `probe.json`. Всё, что не сошлось, попадает в
`warnings` и печатается в прогресс: молча посчитать раздел по нулям нельзя —
получится «анализ, который ничего не показал», и выглядеть он будет прилично.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from uzp_dash import db, progress

from . import queries as LQ

# Меньше этого числа месяцев — ряд не ряд: тренд по двум точкам не отличить от шума.
MIN_TREND_MONTHS = 4


def _df(engine, sql: str, params: dict | None = None) -> pd.DataFrame:
    """Запрос разведки. Отсутствие таблицы — не повод падать: раздел отключится."""
    try:
        return db.read_sql(engine, sql, params or {})
    except Exception as ex:
        progress.warn(f"запрос разведки недоступен: {type(ex).__name__}: {str(ex)[:200]}")
        return pd.DataFrame()


def _one(df: pd.DataFrame, col: str, default=0):
    if df.empty or col not in df:
        return default
    v = df[col].iloc[0]
    return default if pd.isna(v) else v


def run(engine, d_from: str, d_to: str, out_dir: Path) -> dict:
    """Разведка. Возвращает словарь; он же пишется в out_dir/probe.json."""
    res: dict = {"warnings": [], "d_from": str(d_from), "d_to": str(d_to)}
    seg = {"seg": LQ.SEG_SHORT}

    progress.step("Разведка витрин")

    # --- витрина оттока: единственный обязательный источник ---
    fact = _df(engine, LQ.PROBE_FACT, seg)
    if fact.empty or int(_one(fact, "n_rows")) == 0:
        raise RuntimeError(
            "uzp_dwh_fact_outflow пуста или недоступна — отчёту по оттоку "
            "бюджетной сферы не на чем работать")

    n_rows = int(_one(fact, "n_rows"))
    n_seg_rows = int(_one(fact, "n_seg_rows"))
    n_seg_inn = int(_one(fact, "n_seg_inn"))
    res["fact"] = {
        "n_rows": n_rows, "n_inn": int(_one(fact, "n_inn")),
        "n_months": int(_one(fact, "n_months")),
        "d_min": str(_one(fact, "d_min", "")), "d_max": str(_one(fact, "d_max", "")),
        "n_seg_rows": n_seg_rows, "n_seg_inn": n_seg_inn,
    }
    progress.done(f"витрина оттока: {n_rows:,} строк, "
                  f"{res['fact']['n_months']} мес. "
                  f"({res['fact']['d_min']} … {res['fact']['d_max']}); "
                  f"{LQ.SEG_SHORT}: {n_seg_rows:,} строк, {n_seg_inn:,} организаций")

    if n_seg_rows == 0:
        raise RuntimeError(
            f"в uzp_dwh_fact_outflow нет строк сегмента '{LQ.SEG_SHORT}'. "
            f"Проверьте написание сегмента: в этой витрине он записан КОРОТКИМ "
            f"кодом, а в справочнике компаний — большим именем "
            f"('{LQ.SEG_BIG}'). Фильтр большим именем даёт ноль строк.")

    # --- признаки «куда ушли»: на проме оба пусты ---
    n_other = int(_one(fact, "n_other_inn"))
    n_overflow = int(_one(fact, "n_overflow"))
    res["other_inn_measurable"] = n_other > 0
    res["overflow_measurable"] = n_overflow > 0
    if not res["other_inn_measurable"]:
        res["warnings"].append(
            "other_inn_emp_perc пуст во всей витрине — доля сотрудников, ушедших "
            "в другой ИНН, не измеряется; признак в отчёте не показывается")
    if not res["overflow_measurable"]:
        res["warnings"].append(
            "prev_m_overflow_qty пуст во всей витрине — переток внутри Сбера "
            "(смена работодателя без ухода из банка) не измеряется")

    # --- территория по ОКТМО ---
    n_ok = int(_one(fact, "n_oktmo_full"))
    n_subj_ok = int(_one(fact, "n_subject_code_ok"))
    res["oktmo"] = {"n_full": n_ok, "share": n_ok / n_rows if n_rows else 0.0,
                    "n_subject_code_ok": n_subj_ok}
    progress.done(f"ОКТМО: полных кодов {n_ok:,} из {n_rows:,} "
                  f"({n_ok / max(n_rows, 1):.0%}); годных oktmo_subject_code "
                  f"{n_subj_ok:,} — субъект берём из substr(oktmo, 1, 2)")
    if n_ok == 0:
        res["warnings"].append(
            "ни одного полного ОКТМО (11 знаков) — разрез по субъектам РФ "
            "построить не из чего, территория останется только по ГОСБ и ТБ")

    # --- глубина ряда: три источника, берём самый глубокий ---
    months = _df(engine, LQ.PROBE_FACT_MONTHS, seg)
    res["fact_months"] = [str(d) for d in months["report_dt"]] if not months.empty else []

    ret = _df(engine, LQ.PROBE_RETURN, seg)
    res["returns"] = {
        "n_rows": int(_one(ret, "n_rows")), "n_months": int(_one(ret, "n_months")),
        "d_min": str(_one(ret, "d_min", "")), "d_max": str(_one(ret, "d_max", "")),
        "out_qty": float(_one(ret, "out_qty")), "ret_qty": float(_one(ret, "ret_qty")),
    }
    if res["returns"]["n_rows"] == 0:
        res["warnings"].append(
            "витрина возвратов пуста — весь отток считается невозвращённым")

    hold = _df(engine, LQ.PROBE_HOLDING)
    res["holding"] = {"n_months": int(_one(hold, "n_months")),
                      "d_min": str(_one(hold, "d_min", "")),
                      "d_max": str(_one(hold, "d_max", ""))}

    n_fact_m = len(res["fact_months"])
    n_hold_m = res["holding"]["n_months"]
    # Источник ряда выбирается по ГЛУБИНЕ, а не по предпочтению: витрина оттока
    # точнее (в ней отток и возвраты), но на проме в ней может быть один месяц.
    if n_fact_m >= MIN_TREND_MONTHS:
        res["trend_source"] = "fact_outflow"
    elif n_hold_m >= MIN_TREND_MONTHS:
        res["trend_source"] = "company_holding_metric"
        res["warnings"].append(
            f"в витрине оттока всего {n_fact_m} мес. — тенденции строятся по "
            f"uzp_dwh_company_holding_metric ({n_hold_m} мес.); там отток "
            f"заполнен реже, и ряд ниже фактического")
    else:
        res["trend_source"] = "none"
        res["warnings"].append(
            f"истории нет ни в одной витрине (оттока {n_fact_m} мес., "
            f"организаций {n_hold_m} мес.) — блок тенденций и прогноз "
            f"не строятся")
    progress.done(f"история: витрина оттока {n_fact_m} мес., витрина организаций "
                  f"{n_hold_m} мес. → ряд по «{res['trend_source']}»")

    # --- покрытие витрины ключевых клиентов ---
    kc = _df(engine, LQ.PROBE_KEY_CLIENT,
             {"d_to": d_to, "d_from": d_from, "seg": LQ.SEG_SHORT})
    kc_inn = int(_one(kc, "n_inn"))
    res["key_client"] = {
        "n_rows": int(_one(kc, "n_rows")), "n_inn": kc_inn,
        "d_max": str(_one(kc, "d_max", "")),
        "n_competitor": int(_one(kc, "n_competitor")),
        "n_captive": int(_one(kc, "n_captive")),
        # покрытие считается от ЧИСЛА ОРГАНИЗАЦИЙ СЕГМЕНТА, а не от всей витрины:
        # знаменатель «все клиенты банка» дал бы обнадёживающе маленькую цифру
        "coverage_of_segment": kc_inn / n_seg_inn if n_seg_inn else 0.0,
    }
    if res["key_client"]["n_rows"] == 0:
        res["warnings"].append(
            "uzp_data_key_client_info_add_attr пуста или недоступна — "
            "блок банков-конкурентов не строится")
    else:
        progress.done(f"ключевые клиенты: {kc_inn:,} организаций, конкурент назван "
                      f"в {res['key_client']['n_competitor']:,} строках")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "probe.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    progress.done(f"Разведка сохранена: {path}")
    for w in res["warnings"]:
        progress.warn(w)
    return res
