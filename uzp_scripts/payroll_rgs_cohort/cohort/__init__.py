"""Численность РГС год к году: разбор до физлица.

Пакет лежит внутри каталога проекта и пользуется общим ядром `uzp_dash`
(подключение к БД, прогресс, обезличивание, вызов LLM, сборка HTML). Второй
копии этих механизмов быть не должно: они разъедутся молча.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _find_root() -> Path:
    """Каталог, в котором лежит пакет uzp_dash.

    Ищем ВВЕРХ по дереву, а не отсчитываем фиксированное число уровней: такой
    путь молча указывает не туда, если папку переложат на уровень глубже.
    """
    env = os.environ.get("UZP_DASH_ROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if (p / "uzp_dash" / "__init__.py").exists():
            return p
        raise RuntimeError(f"UZP_DASH_ROOT={p} — в этом каталоге нет пакета uzp_dash")
    for cand in _HERE.parents:
        if (cand / "uzp_dash" / "__init__.py").exists():
            return cand
    raise RuntimeError(
        "не найден пакет uzp_dash: payroll_rgs_cohort должен лежать ВНУТРИ каталога "
        "проекта (рядом с uzp_dash или глубже). Если он лежит отдельно, укажите "
        "путь переменной окружения UZP_DASH_ROOT=/путь/к/dashboards")


ROOT = _find_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT_DIR = _HERE.parent
