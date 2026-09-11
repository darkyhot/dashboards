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
    top = ""
    if causes is not None and not causes.empty:
        first = causes.sort_values("n_triples", ascending=False).iloc[0]
        top = (f" Самая крупная ветка — «{first['title']}»: "
               f"{_n(first['n_triples'])} получателей, "
               f"{_pct(first['share'], 0)} всех потерь.")
    return (
        f"С {base_m} по {cur_m} получателей стало меньше на "
        f"{_n(abs(t['d_triples']))}, а людей — на {_n(abs(t['d_epk']))}. "
        f"Доля совместителей изменилась с {_pct(t['multi_share_base'], 2)} до "
        f"{_pct(t['multi_share_cur'], 2)}, что дало {_n(t['d_by_multi'])} "
        f"получателей без потери единого человека. Всего потеряно "
        f"{_n(t['lost'])} получателей, из них {_n(t['real_lost'])} — настоящая "
        f"потеря, а {_n(t['inside_lost'])} остались получателями бюджетного "
        f"сегмента. По людям потеряно "
        f"{_n(t['lost_epk'])}.{top}")


def fb_net(base_m: str, cur_m: str, t: dict, causes) -> str:
    """Реальные потери и приход. Главный вывод — он обязан быть и без модели."""
    verdict = ("вырос" if t["net_real"] > 0 else
               "сократился" if t["net_real"] < 0 else "не изменился")
    biggest = ""
    if causes is not None and not causes.empty:
        real = causes[causes["kind"] == "loss"]
        if not real.empty:
            r = real.sort_values("n_triples", ascending=False).iloc[0]
            biggest = (f" Крупнейшая причина потери — «{r['title']}»: "
                       f"{_n(r['n_triples'])} получателей.")
    return (
        f"С {base_m} по {cur_m} реально потеряно {_n(t['real_lost'])} получателей "
        f"({_n(t['real_lost_epk'])} человек) и реально пришло "
        f"{_n(t['real_gained'])} ({_n(t['real_gained_epk'])} человек): сегмент "
        f"{verdict} на {_n(abs(t['net_real']))} получателей. Ещё "
        f"{_n(t['inside_lost'])} получателей остались в сегменте, перейдя внутри "
        f"него, и {_n(t['inside_gained'])} пришли таким же переходом — сегмент их "
        f"не потерял и не привёл. В сумме "
        f"метрика изменилась на {_n(t['d_triples'])} получателей.{biggest}")


def fb_gone(to_seg, to_codes, mig) -> str:
    """Куда делись люди."""
    parts = []
    if to_seg is not None and not to_seg.empty:
        top = to_seg.iloc[0]
        parts.append(
            f"Из ушедших в другие сегменты больше всего забрал «{top['segment_name']}» "
            f"— {_n(top['n_epk'])} человек, {_pct(top['share'], 0)} всех ушедших "
            f"из бюджетной сферы.")
    if to_codes is not None and not to_codes.empty:
        top = to_codes.iloc[0]
        names = ", ".join(f"«{r.code_name or r.code}» ({_n(r.n_epk)})"
                          for r in to_codes.head(3).itertuples())
        parts.append(
            f"Те, кто перестал получать по зарплатным кодам, теперь получают по "
            f"другим: {names}. Чаще всего это «{top['code_name'] or top['code']}» "
            f"— {_pct(top['share'], 0)} таких людей.")
    if mig is not None and not mig.empty:
        n = int(mig["n_epk"].sum())
        parts.append(
            f"Ещё {len(mig)} организаций переоформлены: их люди ({_n(n)} человек) "
            f"дружно перешли в один и тот же новый номер, никуда фактически не "
            f"уходя.")
    return " ".join(parts) or "Куда делись люди, разрезы не показали."


def fb_when(tr, st, measured: str, cmp_months, surv, seas, codes_m) -> str:
    if tr is None or tr.empty:
        return "Помесячный ряд не построился — сказать, когда произошло падение, нечем."
    parts = []
    # Сезонность — первым делом: она объясняет провал отчётного месяца, и без неё
    # этот провал читается как потеря.
    if seas and seas.get("n_gone_cur"):
        parts.append(
            f"Из {_n(seas['n_prev_both'])} человек, получавших в "
            f"{seas['prev_month']:%m.%Y} оба года, в {seas['report_month']:%m.%Y} "
            f"пропали {_n(seas['n_gone_cur'])}, и {_n(seas['n_seasonal'])} из них "
            f"({_pct(seas['share_of_gone'], 0)}) пропадали в этом месяце и год "
            f"назад — это сезонность. Остальные пропали впервые, и сезонностью их "
            f"объяснить нельзя.")
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
    # Какой вид выплаты просел — часто это и есть весь ответ про провал месяца.
    if codes_m is not None and not codes_m.empty:
        worst = codes_m.iloc[0]
        if float(worst["d_month"]) < 0:
            parts.append(
                f"Сильнее всех к предыдущему месяцу просела выплата "
                f"«{worst['name']}»: {_n(worst['d_month'])} человек "
                f"({_pct(worst['d_month_pct'])}), год к году "
                f"{_n(worst['d_year'])}. Это вид выплаты, а не ушедшие люди: у "
                f"человека может просто не быть выплаты этого вида в этом месяце.")
    if surv is not None and not surv.empty:
        last = surv.iloc[-1]
        parts.append(f"Из получателей базового месяца дожили до конца периода "
                     f"{_n(last['n_alive'])} — {_pct(last['share'])}.")
    return " ".join(parts)


def fb_where(cuts: dict) -> str:
    parts = []
    titles = {"holding_name": "холдингам", "agency": "ведомствам",
              "level": "уровню подчинения", "tb_short_name": "территориальным банкам",
              "region_name": "регионам"}
    for dim, df in cuts.items():
        if df is None or df.empty:
            continue
        top = df.iloc[0]
        loss = float(top.get("loss", 0))
        causes = ", ".join(
            f"{name} {_n(top.get(col, 0))}" for col, name in (
                ("left_bank", "ушли из банка"), ("left_segment", "в другой сегмент"),
                ("other_codes", "не зарплатными кодами"),
                ("below_threshold", "ниже порога")))
        text = (f"По {titles.get(dim, dim)} больше всего реально потеряла группа "
                f"«{top[dim]}» — {_n(loss)} получателей "
                f"({_pct(top.get('share', 0), 0)} всех реальных потерь): {causes}.")
        other = float(top.get("inside_other", 0) or 0)
        if other > 0:
            text += (f" Ещё {_n(other)} остались в сегменте, но перешли в другую "
                     f"строку — для неё это тоже убыль.")
        parts.append(text)
        if len(parts) >= 3:
            break
    return " ".join(parts) or "Разрезы не построились."


def fb_why(w: dict) -> str:
    """Почему ушли — правилами."""
    w = w or {}
    parts = []
    m = w.get("org_meta", {}) or {}
    if m:
        parts.append(
            f"Из организаций, которые увели зарплатный проект целиком "
            f"({m.get('n_gone', 0)}) или массово ({m.get('n_mass', 0)}), пришло "
            f"{_pct(m.get('share_lb_org', 0), 0)} всех ушедших из банка; остальные "
            f"уходили по одному.")
        if m.get("n_reorg"):
            parts.append(f"Ещё {m['n_reorg']} организаций перестали платить, но их "
                         f"люди остались в банке — это реорганизация или новый ИНН, "
                         f"а не уход клиента.")
        if m.get("n_agr_new"):
            parts.append(f"Договор зарплатного проекта сменился у организаций: "
                         f"{m['n_agr_new']}.")
    pat = w.get("pat")
    if pat is not None and not pat.empty:
        grad = float(pat[pat["pattern"].str.startswith("Постепенно")]["share"].sum())
        parts.append(f"Постепенно — с падением сумм или числа зачислений перед "
                     f"уходом — ушли {_pct(grad, 0)} ушедших из банка: их можно было "
                     f"заметить заранее.")
    pay = w.get("pay")
    if pay is not None and not pay.empty and "retained" in set(pay["fate"]):
        base = pay.set_index("fate")
        if "left_bank" in base.index:
            lo_r, lo_l = float(base.at["retained", "b1"]), float(base.at["left_bank", "b1"])
            hi_r, hi_l = float(base.at["retained", "b5"]), float(base.at["left_bank", "b5"])
            parts.append(
                f"Среди ушедших из банка зарплату меньше половины средней по "
                f"организации получали {_pct(lo_l, 0)} (у оставшихся — {_pct(lo_r, 0)}), "
                f"больше двух средних — {_pct(hi_l, 0)} (у оставшихся — {_pct(hi_r, 0)}).")
    return " ".join(parts) or "Почему ушли, разборы не показали."
