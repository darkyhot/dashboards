"""Отчёт по последнему закрытому месяцу с проверкой на том горизонте, который уже виден."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import md5
from html.parser import HTMLParser
import json
from os import urandom
from pathlib import Path
import re

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from . import config, daily, db, months as M, progress, sql as Q

OFFSETS = (0, -1, -2, -12, -13, -14)
COMPARE = (0, 1, 3, 4)
CURRENCIES = ("actual", "prior", "year_actual", "year_prior")
COUNTS = tuple(f"n_{k}" for k in CURRENCIES)
STAYS = tuple(f"stay_{k}" for k in CURRENCIES)
KEY = ("pair_code", *Q.LATEST_FEATURES)
MIN_RUB_SYNC = 1.0  # только сверка равенства денежных сумм
LABELS = {
    "income": ("Новые в паре", "До 30 тыс. ₽", "30–60 тыс. ₽", "60–100 тыс. ₽", "100–200 тыс. ₽", "От 200 тыс. ₽"),
    "history": ("Новые в паре", "Были только в предыдущем месяце", "Были в двух предыдущих месяцах"),
    "jobs": ("Новые в паре", "Один работодатель", "Два работодателя", "Три работодателя", "От четырёх работодателей"),
}
MOVEMENTS = {
    "threshold": "Ниже порога",
    "code": "Другие коды",
    "same_org": "Та же организация в другом ГОСБ",
    "new_place": "Новая точка выплат в банке",
    "other_job": "Есть прежняя работа в банке",
    "missing": "Нет портфельных выплат в банке",
}


def _execute(conn, sql, params=None):
    sql = db.render(sql)
    progress.sql(sql, params)
    try:
        return conn.execute(text(sql), params or {}).rowcount
    except DBAPIError as exc:
        db.raise_if_kerberos(exc)
        raise


def _temp(conn, name, query, gp, distribution, params=None):
    clause = f"\nDISTRIBUTED BY ({distribution})" if gp else ""
    sql = f"CREATE TEMP TABLE {name} ON COMMIT DROP AS\n{query}{clause}"
    _execute(conn, sql, params)
    _execute(conn, f"ANALYZE {name}")


def _load_triples(conn, name, gp, query, slots):
    for i, params in enumerate(slots):
        if i == 0:
            _temp(conn, name, query, gp, "epk_id", params)
        else:
            _execute(conn, f"INSERT INTO {name}\n{query}", params)
        label = {"ptf_latest_tr": "месячные тройки", "ptf_latest_days": "дневные окна для проверки"}
        if name == "ptf_latest_bank_days":
            progress.done(f"{params['m']}: выплаты для проверки переноса")
        else:
            progress.done(f"{M.label(params['m'])}: {label[name]}")
    _execute(conn, f"ANALYZE {name}")


def _choose_month(journal, month):
    have = {M.iso(d) for d in journal["report_dt"]}
    if not have:
        raise ValueError("В журнале нет закрытых месяцев успешной сборки.")
    latest = max(have)
    selected = M.iso(month) if month else latest
    slots = [M.iso(M.shift(selected, offset)) for offset in OFFSETS]
    if min(slots) < Q.CMP_MONTH or not set(slots) <= have:
        raise ValueError(f"Для {M.label(selected)} нужна сопоставимая сводка за "
                         f"{', '.join(M.label(m) for m in sorted(slots))}. "
                         f"Свежий месяц успешной сборки: {M.label(latest)}.")
    return selected, latest, slots


def _monthly_audit(source, saved, journal, dates):
    if not (source.index.is_unique and saved.index.is_unique and journal.index.is_unique):
        raise ValueError("Месяцы в контрольных итогах дублируются.")

    def difference(left, right):
        return None if pd.isna(left) or pd.isna(right) else left - right

    audit = []
    for index, month in enumerate(dates):
        if index not in source.index or pd.isna(source.loc[index, "n"]) or source.loc[index, "n"] <= 0:
            raise ValueError(f"Нет портфельных получателей в ведомостях за {M.label(month)}.")
        a = source.loc[index]
        b = saved.loc[month] if month in saved.index else {}
        j = journal.loc[month]
        rub = difference(a.amount, b.get("amount"))
        fot = difference(a.fot, b.get("fot"))
        journal_fot = difference(b.get("fot"), j.fot_amt)
        version = j.def_ver
        audit.append({
            "month": month, "live_n": a.n, "saved_n": b.get("n"), "journal_n": j.pf_recip,
            "live_amount": a.amount, "live_fot": a.fot,
            "amount_delta": rub, "fot_delta": fot, "journal_fot_delta": journal_fot, "version": version,
            "ok": (a.n == b.get("n") == j.pf_recip and version == Q.DEF_VER
                   and all(v is not None and abs(v) <= MIN_RUB_SYNC for v in (rub, fot, journal_fot))),
        })
    return audit


def audit_table(audit):
    """Одна строка на месяц, только банковские итоги для сверки."""
    def signed(n):
        if n is None or pd.isna(n):
            return "—"
        return f"{n:+,.2f}".replace(",", " ")

    return pd.DataFrame([{
        "Месяц": M.label(r["month"]), "Ведомости, чел.": _number(r["live_n"]),
        "Сводка, чел.": _number(r["saved_n"]), "Журнал, чел.": _number(r["journal_n"]),
        "Зачисления В−С, ₽": signed(r["amount_delta"]), "ФОТ В−С, ₽": signed(r["fot_delta"]),
        "ФОТ С−Ж, ₽": signed(r["journal_fot_delta"]),
        "Версия Ж/отчёт": f"{_number(r['version'])}/{Q.DEF_VER}",
    } for r in audit if not r["ok"]])


def previous_monthly_audit(traceback):
    """Получить банковские итоги из старой проверки в том же ядре Jupyter."""
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_globals.get("__name__") == __name__ and frame.f_code.co_name == "_check_monthly":
            values = frame.f_locals
            if {"source", "saved", "journal", "dates"} <= values.keys():
                return _monthly_audit(values["source"], values["saved"], values["journal"], values["dates"])
        traceback = traceback.tb_next
    return []


def _check_monthly(conn, dates, journal):
    source = db.read_sql(conn, Q.LATEST_TOTALS).set_index("mi")
    saved = db.read_sql(conn, Q.LATEST_MONTHLY_TOTALS, {f"m{i}": d for i, d in enumerate(dates)})
    saved = saved.assign(month=saved["report_dt"].map(M.iso)).set_index("month")
    journal = journal.assign(month=journal["report_dt"].map(M.iso)).set_index("month")
    audit = _monthly_audit(source, saved, journal, dates)
    differences = audit_table(audit)
    if differences.empty:
        progress.done("Сверка ведомостей, сводки и журнала: все месяцы сошлись")
    else:
        progress.warn("Сверка ведомостей, сводки и журнала: найдены расхождения")
        print(differences.to_string(index=False), flush=True)
    return audit


def _coverage(conn, dates):
    frame = db.read_sql(conn, Q.LATEST_TB_COVERAGE)
    frame["month"] = frame["mi"].map(dict(enumerate(dates)))
    table = frame.pivot(index="tb_id", columns="month", values="n").reindex(
        columns=[dates[i] for i in COMPARE]).fillna(0)
    actual, prev, year_actual, year_prev = (table[dates[i]] for i in COMPARE)
    bank_growth = actual.sum() / prev.sum() if prev.sum() else 1.0
    rate = actual.div(prev.where(prev > 0))
    year_rate = year_actual.div(year_prev.where(year_prev > 0))
    # Сигнал качества, когда территория одновременно отстаёт от банка и от своей
    # сезонной динамики. Он не устанавливает причину снижения.
    bad = (prev >= 1000) & (rate < bank_growth - 0.10) & (rate < year_rate - 0.10)
    return {int(t) for t in table.index[bad] if pd.notna(t)}


def _monthly(conn, gp, dates, journal, params):
    progress.step("Месячные ведомости: получатели, группы и сопоставление с прошлым годом")
    _temp(conn, "t_tik", Q.T_TIK, gp, "inn")
    _temp(conn, "t_exc", Q.T_EXC, gp, "inn_exc", params)
    _load_triples(conn, "ptf_latest_tr", gp, Q.LATEST_TR_MONTH, [
        {**params, "mi": i, "m": m} for i, m in enumerate(dates)
    ])
    audit = _check_monthly(conn, dates, journal)
    doubtful = _coverage(conn, dates)
    for name, sql in (("jobs", Q.LATEST_JOBS), ("locations", Q.LATEST_LOCATIONS),
                      ("org_locations", Q.LATEST_ORG_LOCATIONS)):
        _temp(conn, f"ptf_latest_{name}", sql, gp, "epk_id")
    _temp(conn, "ptf_latest_events", Q.LATEST_EVENTS, gp, "epk_id",
          {"period": 0, "curr": 0, "prev": 1, "earlier": 2})
    _execute(conn, "INSERT INTO ptf_latest_events\n" + Q.LATEST_EVENTS,
             {"period": 1, "curr": 3, "prev": 4, "earlier": 5})
    _execute(conn, "ANALYZE ptf_latest_events")
    progress.step("Поиск по всем парам и сочетаниям признаков")
    _temp(conn, "ptf_latest_groups", Q.latest_groups(), gp, "org_id, gosb_id")
    _temp(conn, "ptf_latest_scored", Q.LATEST_SCORED, gp, "org_id, gosb_id", params)
    totals = db.read_sql(conn, Q.LATEST_SUMMARY, params).iloc[0].to_dict()
    totals.update(db.read_sql(conn, Q.LATEST_SIGNAL_COUNTS, params).iloc[0].to_dict())
    _temp(conn, "ptf_latest_shortlist", Q.latest_shortlist(), gp, "org_id, gosb_id", params)
    cards = db.read_sql(conn, Q.latest_cards(), params)
    totals["checked_cards"] = len(cards)
    return totals, cards, doubtful, audit


def _portfolio_context(conn, gp, audit):
    progress.step("Итоги зарплатного портфеля и структура зачислений")
    bands = db.read_sql(conn, Q.LATEST_PORTFOLIO_BANDS).to_dict("records")
    counts = {i: sum(int(row["people"]) for row in bands if row["mi"] == i) for i in (0, 1)}
    _temp(conn, "ptf_latest_bank_leavers", Q.LATEST_BANK_LEAVERS, gp, "epk_id")
    left = int(db.scalar(conn, Q.LATEST_BANK_LEAVER_COUNT))
    return {
        "periods": [{"month": audit[i]["month"], "amount": audit[i]["live_amount"], "fot": audit[i]["live_fot"]}
                    for i in COMPARE],
        "bands": bands,
        "gosb": db.read_sql(conn, Q.LATEST_PORTFOLIO_GOSB).to_dict("records"),
        "changes": {"previous": counts[1], "current": counts[0],
                    "net": counts[0] - counts[1], "left": left, "entered": counts[0] - counts[1] + left,
                    "early": None, "early_status": "not_checked"},
    }


def _bank_early_dates(month):
    first = M.parse(month).replace(day=1)
    references = [M.shift(first, offset).replace(day=1) for offset in (-1, -2)]
    if first.weekday() < 5:
        return "weekday", []
    if any(d.weekday() >= 5 for d in references):
        return "no_reference", []
    early = first - timedelta(days=first.weekday() - 4)
    return "scheduled", [d.isoformat() for d in (early, *references)]


def _check_bank_early(conn, gp, month, params, changes):
    status, days = _bank_early_dates(month)
    changes["early_status"] = status
    if not days or not changes["left"]:
        return
    progress.step("Проверка переноса первого числа по всему банку")
    changes["early_status"] = "not_checked"
    _load_triples(conn, "ptf_latest_bank_days", gp, Q.LATEST_BANK_EARLY_DAY, [
        {**params, "mi": i, "m": day, "s_from": day, "c_to": day} for i, day in enumerate(days)
    ])
    early = int(db.scalar(conn, Q.LATEST_BANK_EARLY_COUNT))
    changes.update(early_status="checked", early=early, early_date=days[0])


def _check_pairs(conn, dates, audit, params):
    # По четырём базовым месяцам сравнение не требует сканировать таблицу троек.
    slots = tuple(i for i, row in enumerate(audit) if i in COMPARE or not row["ok"])
    query = Q.latest_changed_pairs(slots)
    pair_params = {**params, "sync_rub": MIN_RUB_SYNC, **{f"m{i}": dates[i] for i in slots}}
    progress.step("Сверка отобранных пар с готовой сводкой")
    stale = set(db.read_sql(conn, query, pair_params)["pair_code"])
    progress.done(f"Расхождение со сводкой подтвердилось у {len(stale)} отобранных пар")
    return stale


def _check_stopped_organizations(conn, params):
    progress.step("Остановившиеся пары: выплаты организации во всех ГОСБ")
    frame = db.read_sql(conn, Q.LATEST_STOPPED_ORGANIZATIONS, params)
    return {key: part.drop(columns="pair_code").to_dict("records") for key, part in frame.groupby("pair_code")}


def _calendar(conn, gp, dates, params):
    progress.step("Дневные ведомости: проверка границы месяца для отобранных пар")
    _temp(conn, "ptf_latest_pairs", Q.LATEST_CHOSEN_PAIRS, gp, "org_id, gosb_id")
    _temp(conn, "ptf_latest_moves", Q.LATEST_MATCHED_MOVES, gp, "epk_id")
    _load_triples(conn, "ptf_latest_days", gp, Q.latest_daily_triples(), [
        {**params, **daily.windows(dates[i]), "mi": j} for j, i in enumerate(COMPARE)
    ])
    groups = db.read_sql(conn, Q.latest_daily_groups(), params)
    routes = db.read_sql(conn, Q.LATEST_CURRENT_ROUTES, params).to_dict("records")
    return groups, routes


def _delta(values):
    current, prev, year, year_prev = values
    return (current - prev) - (year - year_prev)


def _calendar_values(row, windows, doubtful, min_change, calendar_expected, min_base_ratio):
    comparable_rub = pd.notna(row["rub_change"])
    monthly_delta = _delta([row[f] for f in CURRENCIES]) if comparable_rub else None
    result = {"raw": monthly_delta, "boundary": None, "moved": None, "confirmed": None,
              "residual": monthly_delta, "state": "Дневная проверка недоступна" if calendar_expected else "Дневная проверка отключена"}
    if row["tb_id"] in doubtful:
        result["state"] = "Нужна сверка полноты данных ТБ"
        return result
    if windows is None:
        return result
    whole = windows.loc[[0, 2, 4, 6]]
    if (any(whole.n.to_numpy() != [row[f] for f in COUNTS])
            or any(abs(whole.amount.to_numpy() - [row[f] for f in CURRENCIES]) > MIN_RUB_SYNC)
            or any(abs(whole.stay.to_numpy() - [row[f] for f in STAYS]) > MIN_RUB_SYNC)):
        result["state"] = "Дневная и месячная витрины расходятся"
        return result
    shifted = windows.loc[[1, 3, 5, 7]]
    after = _delta(shifted.amount) if comparable_rub else None
    boundary = monthly_delta - after if comparable_rub else None
    moved = _delta(shifted.moved)
    curr, prev, year, year_prev = shifted.stay
    confirmed = None
    if pd.notna(row["stay_change"]) and abs(row["stay_change"]) >= min_change:
        expected = prev + (year - year_prev) * row["stay_n"] / row["stay_year_n"]
        if curr > 0 and prev > 0 and prev * min_base_ratio <= expected <= prev / min_base_ratio:
            change = (curr - expected) / max(expected, prev)
            if abs(change) >= min_change and change * row["stay_change"] > 0:
                confirmed = curr - expected
    if confirmed is not None:
        state = "Снижение выплат у тех же получателей" if confirmed < 0 else "Рост выплат у тех же получателей"
    elif monthly_delta and abs(after) < abs(monthly_delta) and abs(boundary) > MIN_RUB_SYNC:
        state = ("Отклонение объясняется границей окна выплат" if abs(after) <= MIN_RUB_SYNC
                 else "Часть отклонения объясняет граница окна выплат")
    elif comparable_rub:
        state = "Отклонение осталось после проверки границы окна"
    elif moved:
        state = "Учтена поправка на перераспределение выплат между ГОСБ"
    else:
        state = "Нет сопоставимой сезонной базы зачислений"
    if moved and comparable_rub and confirmed is None and abs(after - moved) < abs(after):
        state = "Учтена поправка на перераспределение выплат между ГОСБ"
    return {"raw": monthly_delta, "boundary": boundary, "moved": moved, "confirmed": confirmed,
            "residual": after - moved - (confirmed or 0) if comparable_rub else None, "state": state}


def _group(row):
    parts = [LABELS[f][int(row[f])] for f in Q.LATEST_FEATURES if row[f] >= 0]
    return " · ".join(dict.fromkeys(parts)) if parts else "Вся пара"


def _make_cards(rows, calendar, doubtful, min_change, calendar_expected, stale_pairs, min_base_ratio):
    daily_rows = {} if calendar.empty else {
        key: frame.set_index("mi") for key, frame in calendar.groupby(list(KEY))
    }
    cards = []
    for row in rows.to_dict("records"):
        key = tuple(row[k] for k in KEY)
        stale = stale_pairs is not None and row["pair_code"] in stale_pairs
        windows = None if stale or stale_pairs is None else daily_rows.get(key)
        result = _calendar_values(row, windows, doubtful, min_change, calendar_expected, min_base_ratio)
        if stale_pairs is None:
            result["state"] = "Сверка со сводкой по паре не закончилась"
        elif stale:
            result["state"] = "Ведомости расходятся со сводкой по паре"
        group = _group(row)
        lifecycle = ""
        if row["n_prior"] == 0 and row["n_actual"] > 0:
            lifecycle = "Появились выплаты в паре"
        elif row["n_prior"] > 0 and row["n_actual"] == 0:
            lifecycle = ("В этом месяце выплат в паре нет" if group == "Вся пара"
                         else "В этом месяце в группе выплат нет")
        cards.append({**result, "pair": row["pair_code"], "org": row["org_code"],
                      "gosb": row["gosb_id"], "group": group,
                      "amount": row["actual"], "expected": row["expected"] if pd.notna(row["rub_change"]) else None,
                      "people": row["n_actual"], "expected_people": row["expected_n"] if pd.notna(row["n_change"]) else None,
                      "stay_people": row["stay_n"], "lifecycle": lifecycle,
                      "flows": {label: {"people": int(row[f]), "previous": row[f"{f}_rub"]}
                                for f, label in MOVEMENTS.items() if row[f] > 0}})
    return sorted(cards, key=lambda c: (c["confirmed"] is None, (c["confirmed"] or 0) > 0,
                                        -(abs(c["confirmed"]) if c["confirmed"] is not None
                                          else max(abs(c["residual"] or 0), abs(c["boundary"] or 0),
                                                   abs(c["moved"] or 0))),
                                        c["pair"], c["group"]))


def run(connection, month=None, *, min_people=10, min_change=0.15, max_pairs=12, max_groups=3,
        amt_min=Q.AMT_MIN, exc_holding=Q.EXC_HOLDING, check_calendar=True, today=None, alias_salt=None,
        min_base_ratio=0.5):
    """Один месяц без наблюдений после конца отчёта, даже при историческом повторе."""
    if (min_people < 1 or not 0 < min_change <= 1 or not 0 < min_base_ratio < 1
            or not 1 <= max_pairs <= 100 or not 1 <= max_groups <= 10):
        raise ValueError("Проверьте минимальный размер, относительное отклонение и лимиты карточек.")
    gp = db.is_greenplum(connection)
    today = today or datetime.now(timezone(timedelta(hours=3))).date()
    closed = M.iso(M.shift(today, -1))
    params = {"min_people": min_people, "min_change": min_change, "max_pairs": max_pairs,
              "max_groups": max_groups, "min_base_ratio": min_base_ratio,
              "amt_min": amt_min, "exc_holding": exc_holding,
              "salt": alias_salt or urandom(16).hex()}
    calendar = pd.DataFrame()
    checks = {}
    portfolio = {}
    routes = []
    warnings = {}
    stale_pairs = set()
    stage = None
    monthly = None
    try:
        with connection.begin():
            journal = db.read_sql(connection, Q.LATEST_MONTHS, {"closed": closed})
            selected, latest, dates = _choose_month(journal, month)
            progress.done(f"Отчёт: {M.long(selected)}. Последний месяц успешной сводки: {M.long(latest)}")
            monthly = _monthly(connection, gp, dates, journal, params)
            card_rows = monthly[1]
            if not card_rows.empty and not all(r["ok"] for r in monthly[3]):
                stale_pairs = None
            stage = "portfolio_warning"
            portfolio = _portfolio_context(connection, gp, monthly[3])
            stopped = card_rows["n_prior"].gt(0) & card_rows["n_actual"].eq(0)
            whole = card_rows[list(Q.LATEST_FEATURES)].eq(-1).all(axis=1)
            if (stopped & whole).any():
                stage = "organization_warning"
                checks = _check_stopped_organizations(connection, params)
            if stale_pairs is None:
                stage = "pair_audit_warning"
                stale_pairs = _check_pairs(connection, dates, monthly[3], params)
            if check_calendar and not card_rows.empty:
                stage = "calendar_warning"
                calendar, routes = _calendar(connection, gp, dates, params)
            if check_calendar:
                stage = "bank_timing"
                _check_bank_early(connection, gp, selected, params, portfolio["changes"])
    except DBAPIError as exc:
        if monthly is None:
            raise
        code = getattr(exc.orig, "pgcode", None) or "без SQLSTATE"
        messages = {"portfolio_warning": "Сбор структуры портфеля",
                    "organization_warning": "Проверка выплат по всем ГОСБ",
                    "pair_audit_warning": "Сверка отобранных пар",
                    "calendar_warning": "Дневная проверка",
                    "bank_timing": "Проверка переноса выплат по банку"}
        key = stage or "pair_audit_warning"
        warning = f"{messages[key]} завершилась ошибкой ({code})."
        warnings["calendar_warning" if key == "bank_timing" else key] = warning
        progress.warn(warning)
    report = _assemble_report((*monthly, portfolio), journal, dates, closed, params, calendar, stale_pairs,
                              check_calendar, checks, warnings)
    report["routes"] = [r for r in routes if stale_pairs is not None and r["pair_code"] not in stale_pairs]
    return report


def _assemble_report(monthly, journal, dates, closed, params, calendar, stale_pairs,
                     check_calendar, organization_checks, warnings):
    totals, rows, doubtful, audit = monthly[:4]
    portfolio = monthly[4] if len(monthly) > 4 else {}
    organization_ids = (dict(zip(rows["pair_code"], rows["org_original"])) if "org_original" in rows else {})
    cards = _make_cards(rows, calendar, doubtful, params["min_change"], check_calendar,
                        stale_pairs, params["min_base_ratio"])
    return {"month": dates[0], "latest": max(M.iso(d) for d in journal["report_dt"]),
            "closed": closed, "dates": dates, "run_id": int(journal["run_id"].iloc[0]),
            "totals": totals, "cards": cards, "portfolio": portfolio, "organization_ids": organization_ids,
            "doubtful_tb": sorted(doubtful),
            "calendar_warning": warnings.get("calendar_warning"),
            "portfolio_warning": warnings.get("portfolio_warning"),
            "organization_checks": organization_checks, "organization_warning": warnings.get("organization_warning"),
            "pair_audit_warning": warnings.get("pair_audit_warning"),
            "monthly_audit": audit, "stale_pairs": None if stale_pairs is None else len(stale_pairs),
            "min_people": params["min_people"], "min_change": params["min_change"],
            "min_base_ratio": params["min_base_ratio"], "max_pairs": params["max_pairs"], "max_groups": params["max_groups"]}


def recover_pair_audit_report(traceback, month=None):
    """В старом запуске карточки уже могли доехать в Python до ошибки сверки."""
    pending = None
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_globals.get("__name__") == __name__ and frame.f_code.co_name == "_monthly":
            values = frame.f_locals
            required = {"totals", "cards", "doubtful", "audit", "params", "dates", "journal"}
            if required <= values.keys():
                pending = values
        query = frame.f_locals.get("sql") if frame.f_code.co_name == "read_sql" else None
        if pending and isinstance(query, str) and "FROM ptf_latest_tr" in query and "FROM changed" in query:
            dates = pending["dates"]
            if month and M.iso(month) != dates[0]:
                return None
            current = datetime.now(timezone(timedelta(hours=3))).date()
            closed = M.iso(M.shift(current, -1))
            monthly = tuple(pending[k] for k in ("totals", "cards", "doubtful", "audit"))
            return _assemble_report(monthly, pending["journal"], dates, closed, pending["params"],
                                    pd.DataFrame(), None, True, {},
                                    {"pair_audit_warning": "Сверка отобранных пар не закончилась. "
                                                           "Восстановлены месячные карточки предыдущего запуска."})
        traceback = traceback.tb_next
    return None


def pair_alias(org, gosb, salt):
    """Сверка кода карточки средствами DataLab с известной парой."""
    return md5(f"{int(org)}:{int(gosb)}:{salt}".encode("utf-8")).hexdigest()[:12]


def _number(n):
    return "—" if n is None or pd.isna(n) else f"{n:,.0f}".replace(",", " ")


def overview(report):
    totals = report["totals"]
    pair_share = totals["comparable"] / totals["pairs"] if totals["pairs"] else 0
    amount_share = totals["comparable_actual"] / totals["actual"] if totals["actual"] else 0
    text = [
        f"{M.long(report['month']).capitalize()} · сводка {report['run_id']}",
        f"Пары с выплатой в текущем или предыдущем месяце: {_number(totals['pairs'])}. "
        f"Получатели в парах: {_number(totals['n_actual'])}. Зачисления портфеля: {_number(totals['actual'])} ₽.",
        f"Сопоставимая денежная база: {_number(totals['comparable'])} пар "
        f"({pair_share:.1%} пар и {amount_share:.1%} зачислений). "
        f"Зачисления {_number(totals['comparable_actual'])} ₽ при ожидании "
        f"{_number(totals['comparable_expected'])} ₽. "
        f"Сезонная денежная база не прошла проверку у {_number(totals.get('weak_rub_base'))} пар.",
        f"В сопоставимых парах просадки {_number(totals['gross_down'])} ₽, "
        f"рост {_number(totals['gross_up'])} ₽ до проверки причин. "
        f"Пар с появлением выплат {_number(totals['added'])}, с прекращением выплат {_number(totals['stopped'])}. "
        f"Снижение к прошлому месяцу укладывается в сезонный ориентир в {_number(totals['seasonal'])} парах.",
        f"Сопоставимых пар и групп {_number(totals['tested_groups'])}, кандидатов {_number(totals['signals'])}, "
        f"отобрано карточек {_number(totals['checked_cards'])}. "
        f"Отбор: от {report['min_people']} получателей в базах, относительное отклонение от "
        f"{report['min_change']:.0%}. Появление и прекращение выплат учитываются отдельно. "
        "Рублёвая отсечка задаётся при просмотре HTML.",
    ]
    if report["latest"] < report["closed"]:
        text.append(f"Сводка пока заканчивается на {M.label(report['latest'])}; "
                    f"последний календарно закрытый месяц — {M.label(report['closed'])}.")
    drift = [M.label(r["month"]) for r in report["monthly_audit"] if not r["ok"]]
    if drift:
        text.append(f"Ведомости, сводка или журнал расходятся за {', '.join(drift)}. "
                    "Суммы отчёта рассчитаны по ведомостям.")
        if report["stale_pairs"] is None:
            text.append("Сверка по отобранным парам осталась открытой. Отчёт предварительный.")
        else:
            text.append(f"У {report['stale_pairs']} отобранных пар расходятся портфельные показатели. "
                        "Причины по ним остаются открытыми.")
    if report["doubtful_tb"]:
        text.append(f"Требуется сверка полноты по ТБ: {', '.join(map(str, report['doubtful_tb']))}.")
    if report["calendar_warning"]:
        text.append(report["calendar_warning"])
    if report.get("organization_warning"):
        text.append(report["organization_warning"])
    if report.get("portfolio_warning"):
        text.append(report["portfolio_warning"])
    if report.get("pair_audit_warning"):
        text.append(report["pair_audit_warning"])
    text.append("Возвраты в следующем месяце пока неизвестны. Группы пересекаются, суммы карточек не складываются.")
    return text


def _card_table(cards):
    return pd.DataFrame([{
        "Пара": c["pair"], "ГОСБ": c["gosb"], "Группа": c["group"],
        "Факт, ₽": _number(c["amount"]), "Ожидание, ₽": _number(c["expected"]),
        "Исходное, ₽": _number(c["raw"]), "Граница окна, ₽": _number(c["boundary"]),
        "Между ГОСБ, ₽": _number(c["moved"]), "Те же получатели, ₽": _number(c["confirmed"]), "Остаток, ₽": _number(c["residual"]),
        "Получатели": _number(c["people"]),
        "Статус": ". ".join(filter(None, (c["lifecycle"], c["state"]))),
    } for c in cards])


def card_table(report, limit=None):
    return _card_table(report["cards"][:limit])


class _SavedReportParser(HTMLParser):
    """Строки таблиц и текст раскрывающихся карточек из HTML отчёта."""

    def __init__(self):
        super().__init__()
        self.tables = []
        self.details = []
        self._table = self._row = self._detail = None
        self._tag = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag == "details":
            self._detail = []
        elif ((tag in ("td", "th") and self._row is not None)
              or (tag in ("summary", "p", "li") and self._detail is not None)):
            self._tag, self._parts = tag, []

    def handle_data(self, data):
        if self._tag is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == self._tag:
            value = "".join(self._parts).strip()
            if tag in ("td", "th"):
                self._row.append(value)
            else:
                self._detail.append((tag, value))
            self._tag = None
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag == "details" and self._detail is not None:
            self.details.append(self._detail)
            self._detail = None


def read_saved_review(path):
    """Восстановить просмотр из старого или нового latest.html без БД."""
    html = Path(path).read_text(encoding="utf-8")
    payload = re.search(r'<script id="ptf-review" type="application/json">(.*?)</script>', html, re.S)
    if payload:
        return json.loads(payload[1])
    parser = _SavedReportParser()
    parser.feed(html)
    parser.close()
    heading = re.search(r"<p>([А-Яа-яЁё]+) (\d{4}) · сводка \d+</p>", html)
    month = None
    if heading and heading[1].lower() in M.RU_FULL:
        month = M.month_end(int(heading[2]), M.RU_FULL.index(heading[1].lower()) + 1).isoformat()
    numbers = {
        "amount": "Факт, ₽", "expected": "Ожидание, ₽", "raw": "Исходное, ₽",
        "boundary": "Граница окна, ₽", "moved": "Между ГОСБ, ₽",
        "confirmed": "Те же получатели, ₽", "residual": "Остаток, ₽", "people": "Получатели",
    }
    required = {"Пара", "ГОСБ", "Группа", "Статус", *numbers.values()}
    table = next((t for t in parser.tables if t and required <= set(t[0])), None)
    if table is None:
        if "<h1>Последний месяц</h1>" in html and "Карточек по условиям отбора нет." in html:
            return {"cards": []}
        raise ValueError("В HTML нет полной таблицы карточек отчёта «Последний месяц».")

    def number(value):
        return None if value == "—" else int("".join(value.split()))

    cards = {}
    for cells in table[1:]:
        if len(cells) != len(table[0]):
            raise ValueError("В сохранённой таблице есть неполная строка.")
        record = dict(zip(table[0], cells))
        card = {name: number(record[label]) for name, label in numbers.items()}
        card.update(pair=record["Пара"], gosb=record["ГОСБ"], group=record["Группа"],
                    state=record["Статус"], lifecycle="", expected_people=None, flows={})
        cards[(card["pair"], card["group"])] = card
    for detail in parser.details:
        title = next((value for tag, value in detail if tag == "summary"), "")
        parts = title.split(" · ", 3)
        if len(parts) != 4 or not parts[2].startswith("Пара "):
            continue
        card = cards.get((parts[2][len("Пара "):], parts[3]))
        if card is None:
            continue
        for tag, value in detail:
            if tag == "p" and (matched := re.match(r"Получатели: [\d\s—]+, ожидание ([\d\s—]+)\.", value)):
                card["expected_people"] = number(matched[1])
            if tag == "li" and (matched := re.fullmatch(
                    r"(.+) — ([\d\s]+) чел\., ([\d\s—]+) ₽ в прошлом месяце", value)):
                card["flows"][matched[1]] = {"people": number(matched[2]), "previous": number(matched[3])}
    stored = re.search(r'<script id="ptf-organization-checks" type="application/json">(.*?)</script>', html, re.S)
    checks = json.loads(stored[1]) if stored else {}
    return {"month": month, "cards": list(cards.values()), "organization_checks": checks}


def review_tables(report, limit=6):
    """Короткие списки с раздельным ранжированием, в том числе для старого отчёта в памяти."""
    selections = (
        ("Подтверждённое снижение выплат у тех же получателей", "confirmed", -1),
        ("Отрицательный остаток после доступных проверок", "residual", -1),
        ("Подтверждённый рост выплат у тех же получателей", "confirmed", 1),
    )
    pages = []
    for title, field, sign in selections:
        ordered = sorted(
            (c for c in report["cards"] if c[field] is not None and c[field] * sign > 0),
            key=lambda c: (-c[field] * sign, c["group"] != "Вся пара", c["pair"], c["group"]),
        )
        best_by_pair = {}
        for card in ordered:
            best_by_pair.setdefault(card["pair"], card)
        table = _card_table(list(best_by_pair.values())[:limit])
        if not table.empty:
            table = table[["Пара", "ГОСБ", "Группа", "Исходное, ₽", "Те же получатели, ₽", "Остаток, ₽"]]
        pages.append((title, table))
    return pages


def _worst_residual(report, whole):
    return min((c for c in report["cards"] if (c["group"] == "Вся пара") == whole
                and c["residual"] is not None and c["residual"] < 0),
               key=lambda c: (c["residual"], c["pair"]), default=None)


def _movement_table(card):
    flows = card["flows"]
    previous = [item["previous"] for item in flows.values()]
    total = sum(previous) if all(n is not None and pd.notna(n) for n in previous) else None
    return pd.DataFrame([
        {"Наблюдение по выбывшим": label, "Получатели": item["people"],
         "Выплаты в прошлом месяце, ₽": _number(item["previous"]),
         "Доля выплат выбывшим": f"{item['previous'] / total:.1%}" if total else "—"}
        for label, item in flows.items()
    ])


def _review_cases(report):
    """Разбирает крупнейшее подтверждённое снижение и два крупнейших остатка."""
    largest_drop = min((c for c in report["cards"] if c["confirmed"] is not None and c["confirmed"] < 0),
                       key=lambda c: (c["confirmed"], c["pair"], c["group"]), default=None)
    seen = set()
    cases = (
        ("Наибольшее подтверждённое снижение", largest_drop),
        ("Крупнейшее необъяснённое снижение по организации в ГОСБ", _worst_residual(report, whole=True)),
        ("Крупнейшее необъяснённое снижение по группе", _worst_residual(report, whole=False)),
    )
    for caption, card in cases:
        if card is None or (card["pair"], card["group"]) in seen:
            continue
        seen.add((card["pair"], card["group"]))
        money = {
            "Факт": card["amount"], "Ожидание": card["expected"], "Исходное отклонение": card["raw"],
            "Граница окна": card["boundary"], "Между ГОСБ": card["moved"],
            "Выплаты тем же получателям": card["confirmed"], "Пока без объяснения": card["residual"],
        }
        title = (f"{caption}. Пара {card['pair']} · ГОСБ {card['gosb']} · {card['group']}. "
                 f"Получателей {_number(card['people'])}, ожидали {_number(card['expected_people'])}. "
                 f"{card['state']}")
        breakdown = pd.DataFrame({"Показатель": list(money), "Сумма, ₽": [_number(n) for n in money.values()]})
        yield title, breakdown, _movement_table(card)


def _organization_summary(report, limit):
    rows = []
    for pair, branches in report.get("organization_checks", {}).items():
        origin = next(branch for branch in branches if branch["gosb"] == branch["stopped_gosb"])
        prior = sum(branch["previous_amount"] for branch in branches)
        actual = sum(branch["current_amount"] for branch in branches)
        rows.append((origin["previous_amount"], {
            "Пара": pair, "ГОСБ": origin["gosb"], "Было в паре, ₽": _number(origin["previous_amount"]),
            "Было по организации, ₽": _number(prior), "Стало по организации, ₽": _number(actual),
            "Изменение по организации, ₽": _number(actual - prior),
            "Статус выплат": ("Продолжились в других ГОСБ" if actual > 0 else "Зачислений по организации нет"),
        }))
    return pd.DataFrame([row for _, row in sorted(rows, key=lambda item: (-item[0], item[1]["Пара"]))[:limit]])


def _organization_table(branches, month):
    months = M.label(M.shift(month, -1)), M.label(month)
    rows = []
    money = {name: sum(b[name] for b in branches) for name in ("previous_amount", "current_amount")}
    for b in [*branches, {"gosb": "Итого по выплатам", "previous_n": None, "current_n": None, **money}]:
        rows.append({"ГОСБ": b["gosb"], f"Получатели {months[0]}": _number(b["previous_n"]),
                     f"Получатели {months[1]}": _number(b["current_n"]),
                     f"Зачисления {months[0]}, ₽": _number(b["previous_amount"]),
                     f"Зачисления {months[1]}, ₽": _number(b["current_amount"])})
    return pd.DataFrame(rows)


def notebook_tables(report):
    """Один набор итогов для расчётной ячейки и открытия сохранённого отчёта."""
    changes = report.get("portfolio", {}).get("changes")
    if changes:
        rows = [("Людей в прошлом месяце", changes["previous"]),
                ("Людей в текущем месяце", changes["current"]),
                ("Изменение за месяц", changes["net"]),
                ("Появились портфельные выплаты", changes["entered"]),
                ("В этом месяце без портфельных выплат", changes["left"])]
        if changes.get("early") is not None:
            rows.append(("Из них найден перенос выплаты", changes["early"]))
            rows.append(("Изменение с учётом переноса", changes["net"] + changes["early"]))
        yield "Почему изменилось число людей в банке", pd.DataFrame(
            [(label, _number(value)) for label, value in rows], columns=["Что произошло", "Людей"])
    labels = ("Снизились выплаты тем же людям", "Снижение, причины которого уточняются", "Выросли выплаты тем же людям")
    for name, (_, table) in zip(labels, review_tables(report)):
        yield name, table.rename(columns={"Исходное, ₽": "Отклонение, ₽", "Остаток, ₽": "Пока без объяснения, ₽"})
    summary = _organization_summary(report, limit=6)
    if not summary.empty:
        yield "Выплаты организаций по всем ГОСБ при остановке пары", summary
        first = summary.iloc[0]
        yield (f"ГОСБ организации по паре {first['Пара']} · выплаты и получатели по каждому ГОСБ",
               _organization_table(report["organization_checks"][first["Пара"]], report["month"]))
    for title, breakdown, flows in _review_cases(report):
        yield title, breakdown
        if not flows.empty:
            yield "Движения выбывших получателей", flows


def write_html(report, path):
    from .latest_html import render

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report), encoding="utf-8")
    return path
