"""Сегменты. Два словаря, потому что источники пишут сегмент по-разному.

* История (`uzp_data_payroll_segment_month_2021`) и ведомости — «ведомостные» имена
  («Средний бизнес», «Государственный сектор»); сведение — дословно как в запросе
  заказчика.
* ЕПК — «большие» имена («Средние», «Рег. госсектор»); нужен только как запасной
  сегмент для пар, которых нет в истории.

Неизвестное имя не прячется в «прочие» — выводится как есть.
"""
from __future__ import annotations

PAY_TO_SHORT = [
    ("КСБ", ["СКБ-Средние", "СКБ-Прочее", "СКБ-Крупные", "Средний бизнес", "Крупный бизнес",
             "ККСБ - прочие", "ККСБ - Прочие"]),
    ("СКМ", ["ОПК", "Клиенты машиностроения", "СКМ", "Прочие", "УРКМ"]),
    ("ММБ", ["Малый бизнес", "Микробизнес", "БМО", "УМБ-Малые", "УМБ-Микро", "малый/микро"]),
    ("КФИ", ["Крупнейший бизнес", "CIB"]),
    ("РГС", ["Государственный сектор"]),
]

EPK_TO_SHORT = {
    "Средние": "КСБ", "Крупные": "КСБ", "Крупнейшие": "КСБ",
    "Рег. госсектор": "РГС",
    "Клиенты машиностроения": "СКМ",
    "Фин.институты": "КФИ",
    "SBI": "БМО",
    "Микро": "ММБ", "Малые": "ММБ",
}

NO_SEG = "Сегмент не указан"
NOT_FOUND = "Не найден"
ORDER = ["КСБ", "РГС", "ММБ", "СКМ", "КФИ", "БМО"]


def _lit(xs) -> str:
    return ", ".join("'" + x.replace("'", "''") + "'" for x in xs)


def seg_case_pay(col: str) -> str:
    whens = "\n".join(f"           WHEN {col} IN ({_lit(names)}) THEN '{short}'"
                      for short, names in PAY_TO_SHORT)
    return f"CASE\n{whens}\n           ELSE NULLIF(btrim({col}), '') END"


def seg_case_epk(col: str) -> str:
    whens = "\n".join(f"      WHEN '{big}' THEN '{short}'" for big, short in EPK_TO_SHORT.items())
    return (f"CASE {col}\n{whens}\n"
            f"      ELSE COALESCE(NULLIF(btrim({col}), ''), '{NO_SEG}')\n    END")


def pay_to_short(name):
    """То же сведение в Python — для эталона на синтетике."""
    if name is None:
        return None
    for short, names in PAY_TO_SHORT:
        if name in names:
            return short
    s = str(name).strip()
    return s or None


def epk_to_short(name):
    if name is None or not str(name).strip():
        return NO_SEG
    return EPK_TO_SHORT.get(name, name)
