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


def fb_overview(base_m: str, cur_m: str, t: dict, causes, causes_epk) -> str:
    lk = t["lost_kinds"]
    top = ""
    if causes is not None and not causes.empty:
        first = causes.sort_values("n_triples", ascending=False).iloc[0]
        top = (f" Самая крупная ветка — «{first['title']}»: "
               f"{_n(first['n_triples'])} получателей, "
               f"{_pct(first['share'], 0)} всех потерь.")
    epk_note = ""
    if causes_epk is not None and not causes_epk.empty:
        epk_note = (f" По людям та же лестница короче и даёт "
                    f"{_n(t['lost_epk'])} потерянных человек против "
                    f"{_n(t['lost'])} получателей — разница и есть цена счёта.")
    return (
        f"С {base_m} по {cur_m} получателей стало меньше на "
        f"{_n(abs(t['d_triples']))}, а людей — на {_n(abs(t['d_epk']))}. "
        f"Доля совместителей изменилась с {_pct(t['multi_share_base'], 2)} до "
        f"{_pct(t['multi_share_cur'], 2)}, что дало {_n(t['d_by_multi'])} "
        f"получателей без потери единого человека. Из {_n(t['lost'])} потерянных "
        f"получателей настоящей потерей является {_n(lk['real'])}, "
        f"{_n(lk['method'])} приходится на особенности счёта и {_n(lk['gap'])} "
        f"на перерывы в выплатах.{top}{epk_note}")


def fb_net(base_m: str, cur_m: str, t: dict) -> str:
    """Выросли или нет. Главный вывод отчёта — он обязан быть и без модели."""
    verdict = ("вырос" if t["net_real"] > 0 else
               "сократился" if t["net_real"] < 0 else "не изменился")
    verdict_epk = ("выросло" if t["net_real_epk"] > 0 else
                   "сократилось" if t["net_real_epk"] < 0 else "не изменилось")
    return (
        f"Если считать только реальное движение — приход новых людей и "
        f"организаций минус уход из банка и из сегмента, — сегмент за год "
        f"{verdict} на {_n(abs(t['net_real']))} получателей, а число людей "
        f"{verdict_epk} на {_n(abs(t['net_real_epk']))}. Особенности счёта дали "
        f"{_n(t['net_method'])} получателей, перерывы в выплатах — "
        f"{_n(t['net_gap'])}; ни то, ни другое людей не прибавляет и не убавляет. "
        f"В сумме три вида дают общее изменение метрики "
        f"{_n(t['d_triples'])} получателей.")


def fb_when(tr, st, measured: str, cmp_months, surv) -> str:
    if tr is None or tr.empty:
        return "Помесячный ряд не построился — сказать, когда произошло падение, нечем."
    parts = []
    # Сезонность — первым делом: если отчётный месяц яма, всё остальное читается
    # иначе, и сказать об этом надо до, а не после.
    if cmp_months is not None and not cmp_months.empty and "is_report" in cmp_months:
        rep = cmp_months[cmp_months["is_report"]]
        others = cmp_months[~cmp_months["is_report"]]
        if not rep.empty and not others.empty:
            gap = float(others["n_triples"].max()) - float(rep.iloc[0]["n_triples"])
            if gap > 0:
                parts.append(
                    f"Отчётный месяц ниже соседних на {_n(gap)} получателей — это "
                    f"сезонная яма, и годовое падение надо читать с поправкой на неё.")
    if st is not None and not st.empty:
        months = ", ".join(f"{pd.Timestamp(r.report_dt):%m.%Y}"
                           for r in st.head(3).itertuples())
        worst = st.iloc[0]
        parts.append(
            f"Изменение неравномерно: выделяются месяцы {months}. Сильнее всех "
            f"{pd.Timestamp(worst['report_dt']):%m.%Y} — {_n(worst['delta'])} "
            f"получателей, в {worst['score']:.1f} раза резче обычного. Мерилось "
            f"{measured}, поэтому регулярный сезонный провал сюда не попал.")
    else:
        parts.append(f"Ни одного месяца-обрыва не найдено ({measured}): изменение "
                     f"идёт плавно. Это текучесть, а не разовое событие.")
    if surv is not None and not surv.empty:
        last = surv.iloc[-1]
        parts.append(f"Из получателей базового месяца дожили до конца периода "
                     f"{_n(last['n_alive'])} — {_pct(last['share'])}.")
    return " ".join(parts)


def fb_why(thr, split, gone, mig) -> str:
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
                    f"численность меняется на {_n(d0)} против {_n(d1)} при "
                    f"пороге 2500 ₽.")
            else:
                parts.append(
                    f"Порог получателя объясняет заметную часть падения: без "
                    f"порога изменение составляет {_n(d0)} против {_n(d1)} при "
                    f"пороге 2500 ₽.")
    if split is not None and not split.empty:
        row = split[split["grp"] == "in"]
        if not row.empty:
            r = row.iloc[0]
            parts.append(
                f"По зарплатным кодам — тем, которые метрика только и считает, — "
                f"людей стало {_n(r['d_epk'])} ({_pct(r['d_epk_pct'])}), объём "
                f"изменился на {_pct(r['d_amt_pct'])}.")
    if gone is not None and not gone.empty:
        n_out = int((~gone["in_list"]).sum())
        n_in = int(gone["in_list"].sum())
        if n_out:
            parts.append(
                f"Из витрины целиком исчезло {n_out} кодов вне списка — на метрику "
                f"они не влияют по построению, это перемена в данных, а не причина "
                f"падения.")
        if n_in:
            parts.append(f"ВНИМАНИЕ: исчезло {n_in} кодов ИЗ СПИСКА — вот они на "
                         f"метрику влияют напрямую.")
    if mig is not None and not mig.empty:
        n = int(mig["n_epk"].sum())
        parts.append(
            f"Найдено {len(mig)} организаций, чьи люди ({_n(n)} человек) дружно "
            f"перешли в один и тот же новый номер: это переоформление, а не отток.")
    return " ".join(parts) or ("Ни порог, ни смена кодов, ни переоформление "
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
        tot = float(top.get("n_triples", 0)) or 1.0
        real = float(top.get("real", 0))
        parts.append(
            f"По {titles.get(dim, dim)} больше всего потеряла группа "
            f"«{top[dim]}» — {_n(tot)} получателей ({_pct(top.get('share', 0), 0)} "
            f"всех потерь), из них настоящей потерей является {_pct(real / tot, 0)}.")
        if len(parts) >= 3:
            break
    return " ".join(parts) or "Разрезы не построились."
