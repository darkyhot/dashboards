"""Конфигурация инструмента. Единственное место, зависящее от контура.

Контур переключается переменной окружения UZP_CONTOUR (open|closed).
Всё остальное в проекте контур-агностично.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Загрузка .env (без внешних зависимостей) ---
ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv(ROOT / ".env")

# --- Схема пром-БД. Локальная синтетика повторяет её ТОЧЬ-В-ТОЧЬ, ---
# поэтому SQL дэшей идентичен в обоих контурах.
SCHEMA = "s_grnplm_ld_salesntwrk_pcap_sn_uzp"

# --- Контур --- (управляется из ноутбука; env — лишь фолбэк по умолчанию)
CONTOUR = os.environ.get("UZP_CONTOUR", "open").strip().lower()


def set_contour(name: str | None) -> str:
    """Переключить контур из тетрадки: 'open' (DeepSeek) | 'closed' (GLM/Qwen)."""
    global CONTOUR
    if name:
        CONTOUR = name.strip().lower()
    return CONTOUR

# --- Пути ---
DATA_DIR = ROOT / "data"                 # исходные данные прод (только открытый контур)
PROFILES_DIR = DATA_DIR / "profiles"     # профили прод-таблиц (reference)
OUTPUT_DIR = ROOT / "output"
SYNTH_DIR = ROOT / "synth"

# Справочники-CSV (грузятся как есть при сборке синтетики)
CSV_GOSB = DATA_DIR / f"{SCHEMA}.uzp_dim_gosb.csv"
CSV_METRIC = DATA_DIR / f"{SCHEMA}.uzp_dim_metric.csv"


def db_url(override: str | None = None) -> str:
    """SQLAlchemy URL. В ноутбуке можно передать явно; иначе берётся из env."""
    url = override or os.environ.get("UZP_DB_URL")
    if not url:
        raise RuntimeError(
            "Не задан URL БД. Передайте conn=... в generate_dashboard() "
            "или заполните UZP_DB_URL в .env"
        )
    return url
