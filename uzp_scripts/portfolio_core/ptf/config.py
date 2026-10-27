"""Конфигурация. Контур (открытый — синтетика, закрытый — пром) различается только
строкой подключения и именами схем; в расчётах ветвлений по контуру нет."""
from __future__ import annotations

import os

from . import REPORT_DIR

PROM_SCHEMA = "s_grnplm_ld_salesntwrk_pcap_sn_uzp"       # витрины (только чтение)
PROM_SCHEMA_T = "s_grnplm_ld_salesntwrk_pcap_sn_t_uzp"   # ТИК/ЦИК и наши таблицы yva_*
SYNTH_SCHEMA = "synth_ptf"                                # открытый контур: всё в одной схеме

SCHEMA = PROM_SCHEMA
SCHEMA_T = PROM_SCHEMA_T
PAYROLL = "uzp_data_payroll_m"                            # ведомости
SEG_HIST = "uzp_data_payroll_segment_month_2021"          # исторический сегмент

OUTPUT_DIR = REPORT_DIR / "output"
SQL_TIMEOUT_MIN = 10


def _load_dotenv() -> None:
    """Прочитать `.env` (только переменные, не код): в папке отчёта, затем выше."""
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


def setup(schema: str | None = None, schema_t: str | None = None,
          payroll: str | None = None, seg_hist: str | None = None) -> None:
    global SCHEMA, SCHEMA_T, PAYROLL, SEG_HIST
    SCHEMA = (schema or SCHEMA).strip()
    SCHEMA_T = (schema_t or SCHEMA_T).strip()
    PAYROLL = (payroll or PAYROLL).strip()
    SEG_HIST = (seg_hist or SEG_HIST).strip()


def db_url(override: str | None = None) -> str:
    url = override or os.environ.get("PTF_DB_URL") or os.environ.get("UZP_DB_URL")
    if not url:
        raise RuntimeError("Не задан URL БД: CONN в тетрадке или PTF_DB_URL / UZP_DB_URL в .env")
    return url


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
