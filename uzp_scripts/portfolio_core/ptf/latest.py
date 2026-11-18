"""Отчёт по последнему закрытому месяцу с проверкой на том горизонте, который уже виден."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import md5
from html import escape
from os import urandom
from pathlib import Path

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
        progress.done(f"{M.label(params['m'])}: месячные тройки" if name == "ptf_latest_tr"
                      else f"{M.label(params['m'])}: дневные окна для проверки")
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
    _load_triples(conn, "ptf_latest_tr", gp, Q.T_TR_MONTH, [
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
    stale_pairs = set()
    if not cards.empty and not all(r["ok"] for r in audit):
        pair_params = {**params, "sync_rub": MIN_RUB_SYNC, **{f"m{i}": d for i, d in enumerate(dates)}}
        stale = db.read_sql(conn, Q.LATEST_CHANGED_PAIRS, pair_params)
        stale_pairs = set(stale["pair_code"])
        progress.done(f"Расхождение со сводкой подтвердилось у {len(stale_pairs)} отобранных пар")
    return totals, cards, doubtful, audit, stale_pairs


def _calendar(conn, gp, dates, params):
    progress.step("Дневные ведомости: проверка границы месяца для отобранных пар")
    _temp(conn, "ptf_latest_pairs", Q.LATEST_CHOSEN_PAIRS, gp, "org_id, gosb_id")
    _temp(conn, "ptf_latest_moves", Q.LATEST_MATCHED_MOVES, gp, "epk_id")
    _load_triples(conn, "ptf_latest_days", gp, Q.latest_daily_triples(), [
        {**params, **daily.windows(dates[i]), "mi": j} for j, i in enumerate(COMPARE)
    ])
    return db.read_sql(conn, Q.latest_daily_groups(), params)


def _delta(values):
    current, prev, year, year_prev = values
    return (current - prev) - (year - year_prev)


def _calendar_values(row, windows, doubtful, min_change, calendar_expected):
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
        if expected > 0 and curr > 0 and prev > 0:
            change = (curr - expected) / expected
            if abs(change) >= min_change and change * row["stay_change"] > 0:
                confirmed = curr - expected
    if confirmed is not None:
        state = "Подтверждено изменение выплат у тех же получателей"
    elif monthly_delta and abs(after) < abs(monthly_delta) and abs(boundary) > MIN_RUB_SYNC:
        state = ("Отклонение объясняется границей окна выплат" if abs(after) <= MIN_RUB_SYNC
                 else "Часть отклонения объясняет граница окна выплат")
    elif comparable_rub:
        state = "Отклонение осталось после проверки границы окна"
    elif moved:
        state = "Найден переход выплат между ГОСБ одной организации"
    else:
        state = "Нет сопоставимой сезонной базы зачислений"
    if moved and comparable_rub and confirmed is None and abs(after - moved) < abs(after):
        state = ("Отклонение объясняется переходом между ГОСБ" if abs(after - moved) <= MIN_RUB_SYNC
                 else "Часть отклонения объясняет переход между ГОСБ")
    return {"raw": monthly_delta, "boundary": boundary, "moved": moved, "confirmed": confirmed,
            "residual": after - moved - (confirmed or 0) if comparable_rub else None, "state": state}


def _group(row):
    parts = [LABELS[f][int(row[f])] for f in Q.LATEST_FEATURES if row[f] >= 0]
    return " · ".join(dict.fromkeys(parts)) if parts else "Вся пара"


def _make_cards(rows, calendar, doubtful, min_change, calendar_expected, stale_pairs):
    daily_rows = {} if calendar.empty else {
        key: frame.set_index("mi") for key, frame in calendar.groupby(list(KEY))
    }
    cards = []
    for row in rows.to_dict("records"):
        key = tuple(row[k] for k in KEY)
        stale = row["pair_code"] in stale_pairs
        result = _calendar_values(row, None if stale else daily_rows.get(key),
                                  doubtful, min_change, calendar_expected)
        if stale:
            result["state"] = "Ведомости расходятся со сводкой по паре"
        lifecycle = ""
        if row["n_prior"] == 0 and row["n_actual"] > 0:
            lifecycle = "Появились выплаты в паре"
        elif row["n_prior"] > 0 and row["n_actual"] == 0:
            lifecycle = "В этом месяце выплат в паре нет"
        cards.append({**result, "pair": row["pair_code"], "org": row["org_code"],
                      "gosb": row["gosb_id"], "group": _group(row),
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
        amt_min=Q.AMT_MIN, exc_holding=Q.EXC_HOLDING, check_calendar=True, today=None, alias_salt=None):
    """Один месяц без наблюдений после конца отчёта, даже при историческом повторе."""
    if min_people < 1 or not 0 < min_change <= 1 or not 1 <= max_pairs <= 100 or not 1 <= max_groups <= 10:
        raise ValueError("Проверьте минимальный размер, относительное отклонение и лимиты карточек.")
    gp = db.is_greenplum(connection)
    today = today or datetime.now(timezone(timedelta(hours=3))).date()
    closed = M.iso(M.shift(today, -1))
    params = {"min_people": min_people, "min_change": min_change, "max_pairs": max_pairs,
              "max_groups": max_groups, "amt_min": amt_min, "exc_holding": exc_holding,
              "salt": alias_salt or urandom(16).hex()}
    calendar = pd.DataFrame()
    calendar_warning = None
    monthly = None
    try:
        with connection.begin():
            journal = db.read_sql(connection, Q.LATEST_MONTHS, {"closed": closed})
            selected, latest, dates = _choose_month(journal, month)
            progress.done(f"Отчёт: {M.long(selected)}. Последний месяц успешной сводки: {M.long(latest)}")
            monthly = _monthly(connection, gp, dates, journal, params)
            if check_calendar and not monthly[1].empty:
                calendar = _calendar(connection, gp, dates, params)
    except DBAPIError as exc:
        if monthly is None:
            raise
        code = getattr(exc.orig, "pgcode", None) or "без SQLSTATE"
        # Greenplum может отменить всю транзакцию при ошибке дневного чтения.
        # Месячные агрегаты уже в памяти, поэтому их можно показать с явным статусом.
        calendar = pd.DataFrame()
        calendar_warning = f"Дневная проверка завершилась ошибкой ({code})."
        progress.warn(calendar_warning)
    totals, rows, doubtful, audit, stale_pairs = monthly
    cards = _make_cards(rows, calendar, doubtful, min_change, check_calendar, stale_pairs)
    return {"month": selected, "latest": latest, "closed": closed, "dates": dates,
            "run_id": int(journal["run_id"].iloc[0]), "totals": totals, "cards": cards,
            "doubtful_tb": sorted(doubtful), "calendar_warning": calendar_warning,
            "monthly_audit": audit, "stale_pairs": len(stale_pairs),
            "min_people": min_people, "min_change": min_change, "max_pairs": max_pairs, "max_groups": max_groups}


def pair_alias(org, gosb, salt):
    """Сверка кода карточки средствами DataLab с известной парой."""
    return md5(f"{int(org)}:{int(gosb)}:{salt}".encode("utf-8")).hexdigest()[:12]


def _number(n):
    return "—" if n is None or pd.isna(n) else f"{n:,.0f}".replace(",", " ")


def overview(report):
    totals = report["totals"]
    text = [
        f"{M.long(report['month']).capitalize()} · сводка {report['run_id']}",
        f"Пары с выплатой в текущем или предыдущем месяце: {_number(totals['pairs'])}. "
        f"Получатели в парах: {_number(totals['n_actual'])}. Зачисления портфеля: {_number(totals['actual'])} ₽.",
        f"Сопоставимая денежная база: {_number(totals['comparable'])} пар. "
        f"Зачисления {_number(totals['comparable_actual'])} ₽ при ожидании "
        f"{_number(totals['comparable_expected'])} ₽.",
        f"В сопоставимых парах просадки {_number(totals['gross_down'])} ₽, "
        f"рост {_number(totals['gross_up'])} ₽ до проверки причин. "
        f"Пар с появлением выплат {_number(totals['added'])}, с прекращением выплат {_number(totals['stopped'])}. "
        f"Снижение к прошлому месяцу укладывается в сезонный ориентир в {_number(totals['seasonal'])} парах.",
        f"Сопоставимых пар и групп {_number(totals['tested_groups'])}, кандидатов {_number(totals['signals'])}, "
        f"отобрано карточек {_number(totals['checked_cards'])}. "
        f"Отбор: от {report['min_people']} получателей в базах, относительное отклонение от "
        f"{report['min_change']:.0%}. Появление и прекращение выплат учитываются отдельно. "
        "Отсечки в рублях нет.",
    ]
    if report["latest"] < report["closed"]:
        text.append(f"Сводка пока заканчивается на {M.label(report['latest'])}; "
                    f"последний календарно закрытый месяц — {M.label(report['closed'])}.")
    drift = [M.label(r["month"]) for r in report["monthly_audit"] if not r["ok"]]
    if drift:
        text.append(f"Ведомости, сводка или журнал расходятся за {', '.join(drift)}. "
                    f"Суммы отчёта рассчитаны по ведомостям. У {report['stale_pairs']} отобранных пар "
                    "расходятся портфельные показатели, причины по ним остаются открытыми.")
    if report["doubtful_tb"]:
        text.append(f"Требуется сверка полноты по ТБ: {', '.join(map(str, report['doubtful_tb']))}.")
    if report["calendar_warning"]:
        text.append(report["calendar_warning"])
    text.append("Возвраты в следующем месяце пока неизвестны. Группы пересекаются, суммы карточек не складываются.")
    return text


def card_table(report, limit=None):
    return pd.DataFrame([{
        "Пара": c["pair"], "ГОСБ": c["gosb"], "Группа": c["group"],
        "Факт, ₽": _number(c["amount"]), "Ожидание, ₽": _number(c["expected"]),
        "Исходное, ₽": _number(c["raw"]), "Граница окна, ₽": _number(c["boundary"]),
        "Между ГОСБ, ₽": _number(c["moved"]), "Те же получатели, ₽": _number(c["confirmed"]), "Остаток, ₽": _number(c["residual"]),
        "Получатели": _number(c["people"]),
        "Статус": ". ".join(filter(None, (c["lifecycle"], c["state"]))),
    } for c in report["cards"][:limit]])


def write_html(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    paragraphs = "".join(f"<p>{escape(line)}</p>" for line in overview(report))
    table = card_table(report)
    audit = audit_table(report["monthly_audit"])
    detail = []
    for c in report["cards"]:
        title = escape(f"Орг. {c['org']} · ГОСБ {c['gosb']} · Пара {c['pair']} · {c['group']}")
        flows = "".join(f"<li>{escape(label)} — {_number(v['people'])} чел., "
                        f"{_number(v['previous'])} ₽ в прошлом месяце</li>" for label, v in c["flows"].items())
        detail.append(f"<details><summary>{title}</summary><p>Получатели: {_number(c['people'])}, "
                      f"ожидание {_number(c['expected_people'])}. Выплаты в обоих месяцах получили "
                      f"{_number(c['stay_people'])}. {escape(c['state'])}.</p>"
                      + (f"<ul>{flows}</ul>" if flows else "<p>Выбывших из пары в группе нет.</p>")
                      + "</details>")
    path.write_text(
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Последний месяц</title>'
        '<style>body{font:15px system-ui;margin:2rem;color:#19252e}table{border-collapse:collapse}'
        'td,th{padding:.45rem;border-bottom:1px solid #d4dde1;text-align:left}'
        'th{background:#eef3f5}details{margin:.6rem 0}</style><h1>Последний месяц</h1>'
        + paragraphs + (audit.to_html(index=False) if len(audit) else "")
        + (table.to_html(index=False) if len(table) else "<p>Карточек по условиям отбора нет.</p>")
        + "".join(detail) + "</html>", encoding="utf-8")
    return path
