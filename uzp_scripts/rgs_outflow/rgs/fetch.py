"""Шаг 1: выгрузка. Тяжёлое агрегируется в БД, в pandas едет только результат.

Два правила платформы, из-за которых модуль выглядит именно так:

* **больше миллиона строк в память не тянем** — и не обрезаем молча: тихо
  усечённая выборка даёт неверные итоги, которые выглядят правдоподобно;
* **сколько строк придёт, спрашиваем ЗАРАНЕЕ** отдельным счётным запросом —
  иначе размер части приходится угадывать, и выборка упирается в лимит уже
  после того, как тяжёлый запрос отработал.
"""
from __future__ import annotations

import pandas as pd

from uzp_dash import db, progress

from . import queries as LQ

MAX_ROWS = 1_000_000
CHUNK_TARGET = int(MAX_ROWS * 0.75)


class RowLimitError(RuntimeError):
    pass


def guard_rows(df: pd.DataFrame, name: str, limit: int | None = None) -> pd.DataFrame:
    """Не дать выборке съесть память ядра.

    Лимит разрешается ВНУТРИ функции, а не в значении по умолчанию: иначе он
    зафиксируется в момент импорта, и правка MAX_ROWS перед прогоном молча ни на
    что не повлияет — защита окажется выключенной ровно тогда, когда её решили
    ужесточить.
    """
    limit = MAX_ROWS if limit is None else limit
    if len(df) > limit:
        raise RowLimitError(
            f"{name}: {len(df):,} строк — больше лимита {limit:,}. "
            f"Сузьте период (params['history_months']) или агрегируйте в SQL.")
    return df


def _opt(engine, sql: str, params: dict, name: str) -> pd.DataFrame:
    """Необязательный источник: недоступен — раздел отключается, прогон идёт."""
    try:
        return guard_rows(db.read_sql(engine, sql, params), name)
    except (Exception, RowLimitError) as ex:
        progress.warn(f"{name} не читается ({type(ex).__name__}: {str(ex)[:200]}) — "
                      f"раздел, который его использует, будет пропущен")
        return pd.DataFrame()


def gosb_dim(engine) -> pd.DataFrame:
    """Справочник территории: ГОСБ → ТБ → регион."""
    df = guard_rows(db.read_sql(engine, LQ.GOSB_DIM), "gosb_dim")
    n_reg = int(df["region_name"].notna().sum()) if "region_name" in df else 0
    progress.done(f"справочник территории: {len(df)} ГОСБ, регион известен у {n_reg}")
    return df


def rgs_fact(engine, d_from: str, d_to: str) -> pd.DataFrame:
    """Месячный отток бюджетной сферы на грейне (месяц, ГОСБ, организация)."""
    progress.step(f"Выгрузка оттока {LQ.SEG_SHORT}: {d_from} … {d_to}")
    df = guard_rows(db.read_sql(engine, LQ.RGS_FACT,
                                {"seg": LQ.SEG_SHORT, "d_from": d_from, "d_to": d_to}),
                    "rgs_fact")
    if df.empty:
        raise RuntimeError(
            f"за период {d_from} … {d_to} в витрине оттока нет строк сегмента "
            f"'{LQ.SEG_SHORT}' — проверьте отчётный месяц (params['report_month'])")
    progress.done(f"отток: {len(df):,} строк, {df['inn'].nunique():,} организаций, "
                  f"{df['report_dt'].nunique()} мес., "
                  f"ушло {int(df['out_qty'].sum()):,} ФЛ, "
                  f"вернулось {int(df['ret_qty'].sum()):,}")
    return df


def rgs_names(engine, d_from: str, d_to: str) -> pd.DataFrame:
    """Наименования организаций — сырьё классификатора ведомств."""
    df = _opt(engine, LQ.RGS_NAMES,
              {"seg": LQ.SEG_SHORT, "d_from": d_from, "d_to": d_to}, "rgs_names")
    progress.done(f"наименования: {len(df):,} организаций")
    return df


def rgs_competitors(engine, d_from: str, d_to: str) -> pd.DataFrame:
    """Банки-конкуренты и стратегия — только по ключевым клиентам."""
    df = _opt(engine, LQ.RGS_COMPETITORS,
              {"seg": LQ.SEG_SHORT, "d_from": d_from, "d_to": d_to}, "rgs_competitors")
    if not df.empty:
        named = int(df["bank_competitor"].notna().sum())
        progress.done(f"конкуренты: {len(df):,} организаций в витрине, "
                      f"банк назван у {named:,}")
    return df


def seg_metrics(engine, p_from: str, d_to: str) -> pd.DataFrame:
    """План и факт по сегменту в разрезе ГОСБ — прокси «как идут ЗП-проекты»."""
    df = _opt(engine, LQ.SEG_METRICS,
              {"p_from": p_from, "d_to": d_to, "seg_code": LQ.SEG_CODE,
               "m_rcp": LQ.METRIC_RECIPIENTS, "m_fot": LQ.METRIC_FOT}, "seg_metrics")
    progress.done(f"план/факт сегмента: {len(df):,} строк")
    return df


def rgs_panel(engine, d_from: str, d_to: str, p_from: str) -> pd.DataFrame:
    """Помесячная панель численности и ШТАТА по бюджетным организациям.

    Читается частями по остатку от деления ИНН. Число частей выбирается по
    счётному запросу; если выборка всё равно упёрлась в лимит, частей становится
    вдвое больше и выгрузка начинается заново — угадывать размер части нельзя.
    """
    base = {"seg": LQ.SEG_SHORT, "d_from": d_from, "d_to": d_to, "p_from": p_from}
    cnt = _opt(engine, LQ.RGS_PANEL_COUNT, base, "rgs_panel_count")
    total = int(cnt["n_rows"].iloc[0]) if not cnt.empty else 0
    n_parts = max(1, -(-total // CHUNK_TARGET))     # деление вверх
    progress.step(f"Выгрузка панели: ожидается {total:,} строк, частей {n_parts}")

    while True:
        try:
            frames = []
            for part in range(n_parts):
                df = db.read_sql(engine, LQ.RGS_PANEL,
                                 {**base, "n_parts": n_parts, "part": part})
                guard_rows(df, f"rgs_panel[{part + 1}/{n_parts}]")
                frames.append(df)
                if n_parts > 1:
                    progress.done(f"  часть {part + 1}/{n_parts}: {len(df):,} строк")
            break
        except RowLimitError as ex:
            n_parts *= 2
            progress.warn(f"{ex} → делю на {n_parts} частей и повторяю")
        except Exception as ex:
            progress.warn(f"панель не читается ({type(ex).__name__}: {str(ex)[:200]}) — "
                          f"разбор причин оттока и прогноз будут пропущены")
            return pd.DataFrame()

    panel = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not panel.empty:
        progress.done(f"панель: {len(panel):,} строк, {panel['inn'].nunique():,} "
                      f"организаций, {panel['ym'].nunique()} мес.")
    return panel
