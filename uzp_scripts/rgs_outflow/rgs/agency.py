"""Ведомство бюджетной организации — ре-экспорт общего классификатора.

Правила переехали в `uzp_dash/agency.py`: тем же классификатором пользуется
разбор численности РГС (`uzp_scripts/payroll_rgs_cohort`), а две копии правил
разъехались бы молча — и два отчёта про один и тот же сегмент начали бы
раскладывать организации по разным ведомствам.

Модуль оставлен, чтобы `from . import agency as AG` в остальном коде отчёта
продолжало работать без правок.
"""
from __future__ import annotations

from uzp_dash.agency import (  # noqa: F401
    AGENCIES,
    INDUSTRY_HINTS,
    ORDER,
    RULES,
    UNKNOWN,
    classify,
    classify_frame,
    coverage,
    normalize,
)
