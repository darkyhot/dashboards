"""forecast_lab — подбор формулы прогноза портфеля перебором на истории.

Отдельный инструмент, НЕ часть дэша tb_health. Дэш он не меняет и не импортирует
из себя: наоборот, это lab импортирует `uzp_dash` — слой БД, прогресс и саму
рабочую формулу (`tb_health.forecast`), чтобы «текущий вариант» в переборе был
ровно тем кодом, который стоит на проме, а не его копией.

Порядок работы (см. README.md):
    probe   — что вообще есть в данных;
    fetch   — выгрузка панели чанками в кэш на диске (один раз, долго);
    backtest — перебор вариантов по кэшу (сколько угодно раз, быстро);
    report  — обезличенные результаты в output/forecast_lab/.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Где лежит uzp_dash. Папку forecast_lab переносят с машины на машину, и класть
# её всегда в одно и то же место относительно репозитория не выходит: снаружи
# это uzp_scripts/forecast_lab/, на проме — просто dashboards/forecast_lab/.
# Поэтому корень не вычисляется по числу уровней вверх (такой путь молча
# указывает не туда), а ИЩЕТСЯ по наличию самого пакета.
_HERE = Path(__file__).resolve().parent


def _find_root() -> Path:
    """Каталог, в котором лежит пакет uzp_dash."""
    env = os.environ.get("UZP_DASH_ROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if (p / "uzp_dash" / "__init__.py").exists():
            return p
        raise RuntimeError(
            f"UZP_DASH_ROOT={p} — в этом каталоге нет пакета uzp_dash")
    for cand in _HERE.parents:
        if (cand / "uzp_dash" / "__init__.py").exists():
            return cand
    raise RuntimeError(
        "не найден пакет uzp_dash: forecast_lab должен лежать ВНУТРИ каталога "
        "проекта (рядом с uzp_dash или глубже). Если он лежит отдельно, укажите "
        "путь переменной окружения UZP_DASH_ROOT=/путь/к/dashboards")


ROOT = _find_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Корень самой лаборатории — от него считаются пути к кэшу и результатам, если
# они не заданы явно.
LAB_DIR = _HERE.parent

__all__ = ["ROOT", "LAB_DIR"]
