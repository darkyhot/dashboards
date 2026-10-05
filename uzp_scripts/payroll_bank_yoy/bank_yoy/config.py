"""Конфигурация: строка подключения, схема, каталог вывода.

Контур (открытый — синтетика, закрытый — пром) различается ТОЛЬКО строкой
подключения и именем схемы. В запросах и расчётах ветвлений по контуру нет.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import REPORT_DIR

# Пром-схема витрин. Синтетика открытого контура повторяет таблицы ТОЧЬ-В-ТОЧЬ,
# но лежит в своей схеме (`synth_bank_yoy`), чтобы не задеть синтетику соседних
# отчётов. Схема задаётся параметром тетрадки.
PROM_SCHEMA = "s_grnplm_ld_salesntwrk_pcap_sn_uzp"
SYNTH_SCHEMA = "synth_bank_yoy"
SCHEMA = PROM_SCHEMA

# Всё, что пишет отчёт, лежит ВНУТРИ его папки: отчёт переносится одной папкой.
OUTPUT_DIR = REPORT_DIR / "output"

# Правило платформы 13: сервер сам снимает запрос, висящий дольше лимита.
# Копия ведомостей t_raw заполняется ПОМЕСЯЧНО именно затем, чтобы каждый
# оператор в лимит укладывался даже на объёме всего банка.
SQL_TIMEOUT_MIN = 10


def _load_dotenv() -> None:
    """Прочитать `.env`: сначала в папке отчёта, затем вверх по дереву.

    Читается только ФАЙЛ с переменными, а не чужой код. Ближайший к отчёту файл
    выигрывает: `setdefault` не перетирает уже заданное значение.
    """
    for d in [REPORT_DIR, *REPORT_DIR.parents]:
        path = d / ".env"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()


def set_schema(name: str | None) -> str:
    global SCHEMA
    if name:
        SCHEMA = name.strip()
    return SCHEMA


def db_url(override: str | None = None) -> str:
    """URL БД: явный из тетрадки, иначе BANK_YOY_DB_URL, иначе UZP_DB_URL."""
    url = override or os.environ.get("BANK_YOY_DB_URL") or os.environ.get("UZP_DB_URL")
    if not url:
        raise RuntimeError(
            "Не задан URL БД. Передайте CONN в тетрадке или заполните "
            "BANK_YOY_DB_URL (или UZP_DB_URL) в .env")
    return url


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
