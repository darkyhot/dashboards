"""Привлечение получателей ЗП год к году: ячейки «организация + ГОСБ» и НФЛ по
каналам продаж (МЗП, ВСП, Digital, Остальное).

Пакет АВТОНОМЕН: не импортирует ничего из проекта, в котором лежит. Нужные
механизмы (подключение к БД, графики, маска названий) — своя копия; папка
`payroll_attraction` переносится как есть (самопроверка `check_self_contained`).
"""
from __future__ import annotations

from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
REPORT_DIR = PKG_DIR.parent
