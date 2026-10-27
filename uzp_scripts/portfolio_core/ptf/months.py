"""Месяцы. В витрине `report_dt` — ПОСЛЕДНИЙ день месяца, поэтому месяц везде
представлен датой конца месяца, а строкой — 'ГГГГ-ММ-ДД'."""
from __future__ import annotations

import calendar
from datetime import date

RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
RU_FULL = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
           "сентябрь", "октябрь", "ноябрь", "декабрь"]
RU_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
RU_PREP = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе",
           "сентябре", "октябре", "ноябре", "декабре"]


def month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def parse(s) -> date:
    """'2026-08', '2026-08-31', date — всё к концу месяца."""
    if isinstance(s, date):
        return month_end(s.year, s.month)
    s = str(s).strip()
    y, m = int(s[:4]), int(s[5:7])
    return month_end(y, m)


def shift(d, k: int) -> date:
    d = parse(d)
    idx = d.year * 12 + (d.month - 1) + k
    return month_end(idx // 12, idx % 12 + 1)


def iso(d) -> str:
    return parse(d).isoformat()


def label(d) -> str:
    """'авг-26' — короткая подпись оси."""
    d = parse(d)
    return f"{RU[d.month - 1]}-{d.year % 100:02d}"


def long(d) -> str:
    """'август 2026'."""
    d = parse(d)
    return f"{RU_FULL[d.month - 1]} {d.year}"


def name(d) -> str:
    return RU_FULL[parse(d).month - 1]


def prep(d) -> str:
    return RU_PREP[parse(d).month - 1]


def gen(d) -> str:
    return RU_GEN[parse(d).month - 1]


def span(d_from, d_to) -> list[date]:
    a, b = parse(d_from), parse(d_to)
    out = []
    while a <= b:
        out.append(a)
        a = shift(a, 1)
    return out
