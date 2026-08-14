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

import sys
from pathlib import Path

# Корень репозитория — чтобы `import uzp_dash` работал при запуске тетрадки из
# uzp_scripts/forecast_lab/. Тетрадка лежит не рядом с пакетом uzp_dash, а путь
# запуска на проме заранее не известен.
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

__all__ = ["ROOT"]
