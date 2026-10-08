"""Конфигурация: строка подключения, схемы, каталог вывода.

Контур (открытый — синтетика, закрытый — пром) различается ТОЛЬКО строкой
подключения и именами схем. Витрины лежат в двух схемах: основной (`SCHEMA`) и
технической (`SCHEMA_T`: архив НФЛ до апреля 2026, клики СБОЛ).
"""
from __future__ import annotations

import os

from . import REPORT_DIR

PROM_SCHEMA = "s_grnplm_ld_salesntwrk_pcap_sn_uzp"
PROM_SCHEMA_T = "s_grnplm_ld_salesntwrk_pcap_sn_t_uzp"
SYNTH_SCHEMA = "synth_attraction"
SYNTH_SCHEMA_T = "synth_attraction_t"
SCHEMA = PROM_SCHEMA
SCHEMA_T = PROM_SCHEMA_T

OUTPUT_DIR = REPORT_DIR / "output"

# Правило платформы 13: сервер сам снимает запрос дольше лимита. Большие витрины
# копируются помесячно, чтобы каждый оператор в лимит укладывался.
SQL_TIMEOUT_MIN = 10


def _load_dotenv() -> None:
    """`.env` — в папке отчёта, затем вверх по дереву; ближайший выигрывает."""
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


def set_schemas(schema: str | None, schema_t: str | None) -> None:
    global SCHEMA, SCHEMA_T
    if schema:
        SCHEMA = schema.strip()
    if schema_t:
        SCHEMA_T = schema_t.strip()


def db_url(override: str | None = None) -> str:
    """URL БД: явный из тетрадки, иначе ATTR_DB_URL, иначе UZP_DB_URL."""
    url = override or os.environ.get("ATTR_DB_URL") or os.environ.get("UZP_DB_URL")
    if not url:
        raise RuntimeError("Не задан URL БД. Передайте CONN в тетрадке или заполните "
                           "ATTR_DB_URL (или UZP_DB_URL) в .env")
    return url


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
