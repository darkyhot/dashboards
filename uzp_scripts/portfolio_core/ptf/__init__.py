"""Единая подложка портфеля ФЛ: сводка «месяц × id орг × ГОСБ» с метками движений.

Пакет АВТОНОМЕН: не импортирует ничего из проекта, в котором лежит. Вся папка
`portfolio_core` переносится как есть; это проверяет `selfcheck.check_self_contained`.
"""
from __future__ import annotations

from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
REPORT_DIR = PKG_DIR.parent
