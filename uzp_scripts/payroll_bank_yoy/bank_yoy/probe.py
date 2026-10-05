"""Разведка: что лежит в витринах, ДО первого тяжёлого запроса.

Каждый пункт — ошибка, которая иначе обнаружилась бы через полчаса прогона или
не обнаружилась бы вовсе:
* имя колонки кода (`enrollment_type` против `enrollment_type_id`) — неверное
  роняет КАЖДЫЙ запрос;
* какие месяцы загружены — в том числе следующий за отчётным (проверка возврата);
* разрешены ли временные таблицы — без них разбор банка не выполним;
* срез справочника ЕПК — ключ кэша ряда.
"""
from __future__ import annotations

from . import config, db, progress
from . import months as M
from . import queries as Q


class ProbeError(RuntimeError):
    pass


def run(conn, d_from, d_to) -> dict:
    progress.step("Разведка витрин")
    out: dict = {"schema": config.SCHEMA}

    cols = db.read_sql(conn, Q.PROBE_COLUMNS,
                       {"schema": config.SCHEMA, "table": "uzp_data_payroll_m"})
    names = set(cols["column_name"])
    if not names:
        raise ProbeError(f"таблица {config.SCHEMA}.uzp_data_payroll_m не найдена")
    code_col = next((c for c in ("enrollment_type", "enrollment_type_id") if c in names), None)
    if code_col is None:
        raise ProbeError("в uzp_data_payroll_m нет колонки кода зачисления "
                         "(ни enrollment_type, ни enrollment_type_id)")
    for need in ("sys_gosb_id", "sys_tb_id", "epk_id", "inn", "amt", "report_dt"):
        if need not in names:
            raise ProbeError(f"в uzp_data_payroll_m нет колонки {need}")
    db.CODE_COL = code_col
    out["code_col"] = code_col
    progress.done(f"колонка кода зачисления: {code_col}")

    mon = db.read_sql(conn, Q.PROBE_MONTHS, {"d_from": M.iso(d_from), "d_to": M.iso(d_to)})
    mon["report_dt"] = mon["report_dt"].astype(str)
    out["months"] = {r.report_dt: int(r.n_rows) for r in mon.itertuples()}
    not_end = [d for d in out["months"] if M.iso(d) != d]
    if not_end:
        raise ProbeError(f"report_dt не конец месяца: {not_end[:3]} — разбор рассчитан "
                         f"на месячные партиции с датой последнего дня")
    progress.done(f"месяцев в витрине за {M.label(d_from)}…{M.label(d_to)}: {len(mon)}")

    epk = db.read_sql(conn, Q.PROBE_EPK).iloc[0].to_dict()
    out["epk"] = {k: (str(v) if v is not None else None) for k, v in epk.items()}
    progress.done(f"справочник ЕПК: {int(epk['n_inn']):,} ИНН, "
                  f"без сегмента {int(epk['n_no_seg']):,} строк")

    try:
        db.execute(conn, Q.PROBE_TEMP)
        db.execute(conn, Q.DROP_PROBE_TEMP)
        out["temp_ok"] = True
    except Exception as ex:                                      # noqa: BLE001
        db.rollback(conn)
        raise ProbeError(
            "временные таблицы запрещены в этой сессии "
            f"({type(ex).__name__}: {str(ex)[:160]}). Разбор всего банка без них "
            "невыполним: каждый запрос пересчитывал бы тройки за все месяцы.") from ex
    progress.done("временные таблицы разрешены")
    return out
