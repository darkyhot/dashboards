"""Сборка справочников территорий `bank_yoy/ref/*.csv` из классификаторов Росстата.

Запускается ВРУЧНУЮ на машине с интернетом; на платформе не нужен — отчёт читает
готовые CSV из своей папки (в БД расшифровки кодов нет).

Источники (открытые данные Росстата, CSV в UTF-8, разделитель «;»):
  ОКТМО — https://rosstat.gov.ru/opendata/7708234640-oktmo  (свежий срез + срез
          27.09.2022: коды, упразднённые после него, тоже получают название);
  ОКАТО — https://rosstat.gov.ru/opendata/7708234640-okato.

Запуск из папки отчёта:
  python -m bank_yoy.geo_build --oktmo oktmo.csv --oktmo-old oktmo_2022.csv --okato okato.csv

Что берётся. Код ОКТМО и ОКАТО — 11 знаков: 2 — субъект РФ, 3 — район / округ /
город, дальше поселения и пункты. Разрез отчёта — до РАЙОНА: первые 5 знаков.
* subjects.csv        — код субъекта (2) → название (из ОКАТО, в именительном);
* oktmo_districts.csv — код района ОКТМО (5) → название, признак «упразднён»;
* okato_districts.csv — код района ОКАТО (5) → название.
Строки-заголовки групп («Муниципальные районы …», «Районы …/») пропускаются.
Слово-блокатор внутри названий (есть и такие районы) записывается с экранированной
первой буквой — `geo.escape`, читается обратно `geo.unescape`.
"""
from __future__ import annotations

import argparse
import csv

from . import geo

REF_DIR = geo.REF_DIR

# Заголовки групп ОКТМО, а не муниципальные образования.
_OKTMO_HEAD = ("Муниципальные районы", "Муниципальные округа", "Городские округа",
               "Внутригородские", "Населенные пункты", "Межселенные", "Городские поселения",
               "Сельские поселения", "Муниципальные образования")


def _rows(path: str):
    with open(path, encoding="utf-8-sig", newline="") as f:
        yield from csv.reader(f, delimiter=";")


def oktmo_districts(path: str) -> dict[str, str]:
    """Код района (5) → название. Колонки: КОД1..КОД4, КЧ, РАЗДЕЛ, НАЗВАНИЕ, …"""
    out = {}
    for r in _rows(path):
        k1, k2, k3, k4, razdel, name = r[0], r[1], r[2], r[3], r[5], r[6].strip()
        if razdel != "1" or k1 == "00" or k2 == "000" or k3 != "000" or k4 != "000":
            continue
        if name.startswith(_OKTMO_HEAD):
            continue
        out[k1 + k2] = name
    return out


def okato(path: str) -> tuple[dict[str, str], dict[str, str]]:
    """(субъекты, районы). Колонки: КОД1..КОД4, РАЗДЕЛ, НАЗВАНИЕ, …"""
    subj, dist = {}, {}
    for r in _rows(path):
        k1, k2, k3, k4, razdel, name = r[0], r[1], r[2], r[3], r[4], r[5].strip()
        if razdel != "1" or k1 == "00" or k3 != "000" or k4 != "000":
            continue
        if k2 == "000":
            subj[k1] = name
        elif not name.endswith("/"):
            dist[k1 + k2] = name
    return subj, dist


def _write(name: str, header: list[str], rows) -> int:
    REF_DIR.mkdir(exist_ok=True)
    rows = sorted(tuple(geo.escape(v) if isinstance(v, str) else v for v in r) for r in rows)
    with open(REF_DIR / name, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def build(oktmo_path: str, oktmo_old_path: str | None, okato_path: str) -> dict:
    subj, okd = okato(okato_path)
    cur = oktmo_districts(oktmo_path)
    old = oktmo_districts(oktmo_old_path) if oktmo_old_path else {}
    okt = [(k, v, 0) for k, v in cur.items()] + [(k, v, 1) for k, v in old.items() if k not in cur]
    return {"subjects": _write("subjects.csv", ["code", "name"], subj.items()),
            "oktmo": _write("oktmo_districts.csv", ["code", "name", "abolished"], okt),
            "okato": _write("okato_districts.csv", ["code", "name"], okd.items())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oktmo", required=True)
    ap.add_argument("--oktmo-old", default=None)
    ap.add_argument("--okato", required=True)
    a = ap.parse_args()
    print(build(a.oktmo, a.oktmo_old, a.okato))


if __name__ == "__main__":
    main()
