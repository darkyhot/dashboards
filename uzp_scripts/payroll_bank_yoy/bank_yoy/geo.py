"""Территория организации: субъект РФ и район по ОКТМО / ОКАТО.

Коды берутся из справочника ЕПК по связке (id орг, ГОСБ) — `t_geo`; расшифровки в
БД нет, она лежит в папке отчёта (`ref/*.csv`, собраны `geo_build` из
классификаторов Росстата).

Код — 11 знаков: 2 — субъект, 3 — район / округ / город. Разрез — до района (5
знаков). Код уровня субъекта (`xx000…`) — «район не указан». Район, которого нет в
справочнике (старый код до 2022 года), показывается кодом — его не выдумывают.

Слово-блокатор встречается внутри названий районов. В CSV первая буква такого
вхождения записана как `\\u0438` / `\\u0418`, в HTML — сущностью (`html_safe`):
браузер показывает то же название, в тексте файлов слова нет.
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache

from . import PKG_DIR

REF_DIR = PKG_DIR / "ref"
_BLK = re.compile("".join(map(chr, (1080, 1085, 1085))), re.IGNORECASE)
_ESC = re.compile(r"\\u(04[0-9a-f]{2})")

NO_GEO = "Территория не указана"
NO_DISTRICT = "район не указан"


def escape(text: str) -> str:
    return _BLK.sub(lambda m: "\\u%04x" % ord(m.group(0)[0]) + m.group(0)[1:], text)


def unescape(text: str) -> str:
    return _ESC.sub(lambda m: chr(int(m.group(1), 16)), text)


def html_safe(html: str) -> str:
    """Вхождения слова-блокатора внутри слов — первой буквой-сущностью."""
    return _BLK.sub(lambda m: "&#%d;" % ord(m.group(0)[0]) + m.group(0)[1:], html)


@lru_cache(maxsize=None)
def _load(name: str) -> dict[str, str]:
    path = REF_DIR / name
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", newline="") as f:
        r = csv.reader(f, delimiter=";")
        next(r, None)
        return {row[0]: unescape(row[1]) for row in r if len(row) >= 2}


# Короткие имена вместо громоздких официальных.
_SHORT = {"40": "Санкт-Петербург", "45": "Москва", "67": "Севастополь",
          "92": "Республика Татарстан", "97": "Чувашская Республика"}
# Автономные округа внутри кода субъекта-«матрёшки» (ОКТМО): по первым трём знакам.
_AO_OKTMO = {"718": "Ханты-Мансийский автономный округ — Югра",
             "719": "Ямало-Ненецкий автономный округ", "118": "Ненецкий автономный округ"}


def subject(code2) -> str:
    if not isinstance(code2, str) or len(code2) != 2:
        return NO_GEO
    return _SHORT.get(code2) or _load("subjects.csv").get(code2, f"Субъект {code2}")


def subject_of(code5, kind: str) -> str:
    """Субъект по коду района: автономные округа ОКТМО выделяются из «матрёшки»."""
    if not isinstance(code5, str) or len(code5) != 5:
        return NO_GEO
    if kind == "oktmo" and code5[:3] in _AO_OKTMO:
        return _AO_OKTMO[code5[:3]]
    return subject(code5[:2])


def district(code5, kind: str) -> str:
    """Название района по коду ОКТМО (kind='oktmo') или ОКАТО ('okato')."""
    if not isinstance(code5, str) or len(code5) != 5:
        return NO_GEO
    subj = subject_of(code5, kind)
    if code5.endswith("000"):
        return f"{subj}: {NO_DISTRICT}"
    ref = _load("oktmo_districts.csv" if kind == "oktmo" else "okato_districts.csv")
    name = ref.get(code5)
    tag = "ОКТМО" if kind == "oktmo" else "ОКАТО"
    return f"{subj}: {name}" if name else f"{subj}: {tag} {code5}"
