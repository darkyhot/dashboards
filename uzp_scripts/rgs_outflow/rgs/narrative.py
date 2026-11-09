"""Тексты выводов отчёта об оттоке: фолбэки на правила.

Механизм вызова LLM (счёт вызовов, защёлка недоступного шлюза, разбор ответа,
обезличивание) переехал в `uzp_dash/narrative.py`: им пользуется и разбор
численности РГС, а две копии защиты от утечки имён разъехались бы молча.

Ниже остались только фолбэки — они про предмет ИМЕННО этого отчёта.
"""
from __future__ import annotations

import pandas as pd

from uzp_dash.narrative import (  # noqa: F401
    PROMPT_CHAR_LIMIT,
    ZERO_STREAK_ABORT,
    Aliases,
    Narrator,
    check_masked,
    mask_frame,
)


# --------------------------------------------------------------------------- #
# Фолбэки: те же выводы, посчитанные правилами
# --------------------------------------------------------------------------- #

def fb_overview(month: str, kept: float, ret: float, base_fl: float,
                agencies: pd.DataFrame, direction: dict) -> str:
    if agencies is None or agencies.empty:
        return f"За {month} данных по ведомствам нет."
    top = agencies.iloc[0]
    rate = agencies.sort_values("out_rate", ascending=False).iloc[0]
    parts = [
        f"За {month} из бюджетной сферы ушло и не вернулось {kept:,.0f} человек "
        f"при численности {base_fl:,.0f} ({kept / max(base_fl, 1):.1%}); "
        f"вернулось {ret:,.0f}.",
        f"Больше всего потерь в ведомстве «{top['agency']}» — {top['out_kept']:,.0f} "
        f"человек, это {top['share']:.0%} всего оттока сегмента.",
    ]
    if rate["agency"] != top["agency"]:
        parts.append(f"Самая высокая доля оттока — в ведомстве «{rate['agency']}»: "
                     f"{rate['out_rate']:.1%} численности.")
    if direction.get("measurable"):
        parts.append(f"За последние {direction['window']} мес. отток "
                     f"{direction['word']} на {abs(direction['rel']):.0%} "
                     f"относительно предыдущих {direction['window']}.")
    return " ".join(parts)


def fb_territory(tb: pd.DataFrame, regions: pd.DataFrame) -> str:
    parts = []
    if tb is not None and not tb.empty:
        t = tb.iloc[0]
        parts.append(f"Основной объём оттока даёт {t['tb_short_name']} — "
                     f"{t['out_kept']:,.0f} человек ({t['share']:.0%} сегмента) "
                     f"при доле оттока {t['out_rate']:.1%}.")
    if regions is not None and not regions.empty:
        r = regions.sort_values("out_rate", ascending=False).iloc[0]
        parts.append(f"Самая высокая доля оттока по регионам — {r['region_name']}: "
                     f"{r['out_rate']:.1%} численности при {r['n_org']:.0f} "
                     f"организациях.")
    return " ".join(parts) or "Территориальный разрез построить не из чего."


def fb_causes(window: int, drop: float, staff: float, comp: float,
              by_region: pd.DataFrame) -> str:
    if drop <= 0:
        return "За выбранное окно численность получателей не падала."
    share = staff / drop
    lead = ("сокращение штата в самих организациях" if share >= 0.5
            else "уход получателей к другим банкам")
    parts = [f"За {window} мес. численность упала на {drop:,.0f} человек: "
             f"{staff:,.0f} объясняется сокращением штата, {comp:,.0f} — уходом "
             f"к конкуренту; преобладает {lead}."]
    if by_region is not None and not by_region.empty:
        w = by_region.sort_values("competitor", ascending=False).iloc[0]
        if w["competitor"] > 0:
            parts.append(f"Больше всего уходов к конкурентам в регионе "
                         f"{w['region_name']} — {w['competitor']:,.0f} человек "
                         f"из {w['drop']:,.0f} падения.")
    return " ".join(parts)


def fb_competitors(coverage: float, banks: pd.DataFrame) -> str:
    if banks is None or banks.empty:
        return (f"Банк-конкурент не назван ни у одной организации с оттоком. "
                f"Данные есть только по ключевым клиентам — {coverage:.0%} сегмента.")
    b = banks.iloc[0]
    return (f"Чаще других конкурентом назван {b['bank']}: {b['n_org']:.0f} "
            f"организаций, {b['out_kept']:,.0f} человек оттока. "
            f"Оценка неполная: банк-конкурент известен только по ключевым "
            f"клиентам, это {coverage:.0%} организаций сегмента.")


def fb_outlook(horizon: str, months: int, base: float, scenarios: pd.DataFrame,
               season_ok: bool) -> str:
    if scenarios is None or scenarios.empty:
        return "Прогноз не построен: не хватило истории."
    row = {r["scenario"]: r for _, r in scenarios.iterrows()}
    hold = row.get("Сохранение")
    rec = row.get("Восстановление")
    risk = row.get("Риск")
    parts = [f"При сохранении текущих темпов численность к {horizon} составит "
             f"{hold['fl_end']:,.0f} человек против {base:,.0f} сейчас "
             f"({hold['delta_perc']:+.1%} за {months} мес.)."]
    if rec is not None:
        parts.append(f"Восстановление до {rec['fl_end']:,.0f} возможно, если приход "
                     f"выйдет на уровень лучшего месяца окна.")
    if risk is not None:
        parts.append(f"При повторении худшего месяца по оттоку численность падает "
                     f"до {risk['fl_end']:,.0f} ({risk['delta_perc']:+.1%}).")
    if not season_ok:
        parts.append("Сезонность не учтена: истории для её оценки не хватило.")
    return " ".join(parts)
