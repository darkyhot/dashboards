"""Тексты выводов разбора: фолбэки на правила.

Механизм вызова LLM (счёт вызовов, защёлка недоступного шлюза, разбор ответа,
обезличивание) берётся из `uzp_dash.narrative` — он общий для всех отчётов
проекта. Здесь только фолбэки: те же выводы, посчитанные правилами.

Фолбэк — не заглушка «данных нет». Это полноценный текст раздела, собранный из
тех же чисел, что ушли бы в модель. Отчёт обязан быть осмысленным при полностью
недоступном шлюзе: на проме это не редкий случай, а рабочий режим.
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


def _n(v) -> str:
    return f"{float(v):,.0f}".replace(",", " ")


def _pct(v, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{float(v) * 100:.{digits}f}%".replace(".", ",")


def fb_overview(base_m: str, cur_m: str, t: dict, causes: pd.DataFrame) -> str:
    real = t["lost_real"] - t["gained"]
    share_not = t["lost_not_real"] / (t["lost"] or 1)
    top = ""
    if causes is not None and not causes.empty:
        first = causes.sort_values("n_pairs", ascending=False).iloc[0]
        top = (f" Самая крупная ветка — «{first['title']}»: "
               f"{_n(first['n_pairs'])} пар, {_pct(first['share'], 0)} всех потерь.")
    return (
        f"С {base_m} по {cur_m} численность сегмента изменилась на "
        f"{_n(t['d_pairs'])} пар: было {_n(t['pairs_base'])}, стало "
        f"{_n(t['pairs_cur'])}. Людей при этом стало меньше на "
        f"{_n(abs(t['d_epk']))}, а доля совместителей изменилась с "
        f"{_pct(t['multi_share_base'], 2)} до {_pct(t['multi_share_cur'], 2)}, что "
        f"дало ещё {_n(t['d_by_multi'])} пар без потери единого человека. "
        f"Всего потеряно {_n(t['lost'])} пар и пришло {_n(t['gained'])}; из "
        f"потерянных {_n(t['lost_not_real'])} ({_pct(share_not, 0)}) оттоком не "
        f"являются — это порог суммы, коды вне списка, схлопнувшееся "
        f"совместительство и переходы внутри сегмента. Чистая потеря людей "
        f"составляет {_n(real)} пар.{top}")


def fb_when(tr: pd.DataFrame, st: pd.DataFrame, surv: pd.DataFrame) -> str:
    if tr is None or tr.empty:
        return "Помесячный ряд не построился — сказать, когда произошло падение, нечем."
    if st is not None and not st.empty:
        months = ", ".join(f"{pd.Timestamp(r.report_dt):%m.%Y}"
                           for r in st.head(3).itertuples())
        worst = st.iloc[0]
        body = (
            f"Падение не равномерно: выделяются месяцы {months}. Сильнее всех "
            f"{pd.Timestamp(worst['report_dt']):%m.%Y} — минус "
            f"{_n(abs(worst['d_pairs']))} пар за месяц, это в "
            f"{worst['score']:.1f} раза резче обычного шага ряда. Ступень в одном "
            f"месяце обычно означает событие — смену кодировки выплат, "
            f"переклассификацию или неполную загрузку партиции, — а не "
            f"постепенный уход людей; какое именно, отвечает раздел «Почему».")
    else:
        body = ("Ни одного месяца-обрыва не найдено: численность снижается "
                "равномерно, месяц за месяцем. Это текучесть, а не разовое событие.")
    if surv is not None and not surv.empty:
        last = surv.iloc[-1]
        body += (f" Из пар базового месяца дожили до конца периода "
                 f"{_n(last['n_alive'])} — {_pct(last['share'])}.")
    return body


def fb_why(thr: pd.DataFrame, codes: pd.DataFrame, mig: pd.DataFrame) -> str:
    parts = []
    if thr is not None and not thr.empty:
        zero = thr[thr["threshold"] == 0]
        base_row = thr[thr["threshold"] == 2500]
        if not zero.empty and not base_row.empty:
            d0 = float(zero.iloc[0]["delta"])
            d1 = float(base_row.iloc[0]["delta"])
            if d0 < 0 and abs(d0) > abs(d1) * 0.7:
                parts.append(
                    f"Порог получателя падение не объясняет: без порога вовсе "
                    f"численность падает на {_n(abs(d0))} пар против "
                    f"{_n(abs(d1))} при пороге 2500 ₽.")
            else:
                parts.append(
                    f"Порог получателя объясняет заметную часть падения: без "
                    f"порога изменение составляет {_n(d0)} пар против {_n(d1)} "
                    f"при пороге 2500 ₽.")
    if codes is not None and not codes.empty:
        lost_codes = codes[codes["delta"] < 0].head(2)
        gain_out = codes[(codes["delta"] > 0) & (~codes["in_list"])].head(2)
        if not lost_codes.empty:
            names = ", ".join(f"«{r.code_name or r.code}» ({_n(r.delta)})"
                              for r in lost_codes.itertuples())
            parts.append(f"Сильнее всего просели коды зачисления: {names}.")
        if not gain_out.empty:
            names = ", ".join(f"«{r.code_name or r.code}» (+{_n(r.delta)})"
                              for r in gain_out.itertuples())
            parts.append(
                f"При этом выросли коды ВНЕ списка: {names} — похоже на смену "
                f"кодировки выплат, а не на уход людей.")
    if mig is not None and not mig.empty:
        n = int(mig["n_epk"].sum())
        parts.append(
            f"Найдено {len(mig)} организаций, чьи люди ({_n(n)} человек) дружно "
            f"перешли в один и тот же новый номер: это переоформление, а не отток.")
    return " ".join(parts) or ("Ни порог, ни смена кодов, ни реорганизация "
                               "падения не объясняют.")


def fb_where(cuts: dict) -> str:
    parts = []
    titles = {"holding_name": "холдингам", "agency": "ведомствам",
              "level": "уровню подчинения", "tb_short_name": "территориальным банкам",
              "region_name": "регионам"}
    for dim, df in cuts.items():
        if df is None or df.empty:
            continue
        top = df.iloc[0]
        real = float(top.get("real_loss", 0))
        tot = float(top.get("n_pairs", 0)) or 1.0
        parts.append(
            f"По {titles.get(dim, dim)} больше всего потеряла группа "
            f"«{top[dim]}» — {_n(tot)} пар ({_pct(top.get('share', 0), 0)} всех "
            f"потерь), из них оттоком людей является {_pct(real / tot, 0)}.")
        if len(parts) >= 3:
            break
    return " ".join(parts) or "Разрезы не построились."
