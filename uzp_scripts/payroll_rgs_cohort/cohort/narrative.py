"""Тексты выводов разбора: фолбэки на правила.

Механизм вызова LLM (счёт вызовов, защёлка недоступного шлюза, разбор ответа,
обезличивание) берётся из `uzp_dash.narrative` — он общий для всех отчётов
проекта. Здесь только фолбэки: те же выводы, посчитанные правилами.

Фолбэк — не заглушка «данных нет». Это полноценный текст раздела, собранный из
тех же чисел, что ушли бы в модель. Отчёт обязан быть осмысленным при полностью
недоступном шлюзе: на проме это не редкий случай, а рабочий режим.
"""
from __future__ import annotations

import re

import pandas as pd

from uzp_dash.narrative import (  # noqa: F401
    PROMPT_CHAR_LIMIT,
    ZERO_STREAK_ABORT,
    Aliases,
    Narrator,
    check_masked,
    mask_frame,
)


# Термины, убранные из отчёта: заказчик их не понимает. Ищутся в ГОТОВОМ тексте —
# и в выводах модели, и в самопроверке HTML и документа. «Приход» — только как
# отдельное слово: «приходят», «приходится» — обычная речь.
OLD_TERMS = re.compile(
    r"реальн\w*|(?<![А-Яа-яЁё])приход(?:а|у|ом|е|ы|ов)?(?![А-Яа-яЁё])"
    r"|остал\w* в сегменте|чист\w* изменени\w*",
    re.IGNORECASE)


def old_terms(text: str) -> list[str]:
    """Какие устаревшие термины встретились в тексте."""
    return sorted({m.group(0).lower() for m in OLD_TERMS.finditer(text or "")})


def _n(v) -> str:
    return f"{float(v):,.0f}".replace(",", " ")


def _pct(v, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{float(v) * 100:.{digits}f}%".replace(".", ",")


def _decomp_sentence(t: dict) -> str:
    """Разложение одной фразой — те же числа, что в шапке отчёта."""
    from .analyze import T_GAINED, T_LOST, inside_title
    return (f"{T_LOST} {_n(t['real_lost'])} получателей ({_n(t['real_lost_epk'])} "
            f"ФЛ), {T_GAINED[0].lower() + T_GAINED[1:]} {_n(t['real_gained'])} "
            f"({_n(t['real_gained_epk'])} ФЛ); "
            f"{inside_title(t['net_inside'])[0].lower()}"
            f"{inside_title(t['net_inside'])[1:]} — ещё {_n(abs(t['net_inside']))} "
            f"получателей {'меньше' if t['net_inside'] <= 0 else 'больше'}.")


def fb_overview(base_m: str, cur_m: str, t: dict, causes, causes_epk) -> str:
    verdict = "выросло" if t["d_triples"] > 0 else "сократилось"
    return (
        f"С {base_m} по {cur_m} число получателей {verdict} на "
        f"{_n(abs(t['d_triples']))} (ФЛ — на {_n(abs(t['d_epk']))}). "
        + _decomp_sentence(t)
        + f" Доля совместителей изменилась с {_pct(t['multi_share_base'], 2)} до "
          f"{_pct(t['multi_share_cur'], 2)}.")


def fb_net(base_m: str, cur_m: str, t: dict, causes_epk) -> str:
    """Кто перестал и кто начал получать. Главный вывод — обязан быть и без модели."""
    biggest = ""
    if causes_epk is not None and not causes_epk.empty:
        r = causes_epk.sort_values("n_epk", ascending=False).iloc[0]
        biggest = (f" Крупнейшая ситуация среди переставших — «{r['title']}»: "
                   f"{_n(r['n_epk'])} ФЛ.")
    verdict = ("выросло" if t["d_triples"] > 0 else
               "сократилось" if t["d_triples"] < 0 else "не изменилось")
    return (f"С {base_m} по {cur_m} число получателей {verdict} на "
            f"{_n(abs(t['d_triples']))}, ФЛ — на {_n(abs(t['d_epk']))}. "
            + _decomp_sentence(t) + biggest)


def fb_gone(to_seg, to_codes, mig) -> str:
    """Куда делись люди."""
    parts = []
    if to_seg is not None and not to_seg.empty:
        top = to_seg.iloc[0]
        parts.append(
            f"Из ФЛ, получающих теперь зарплату от небюджетной организации, больше "
            f"всего в сегменте «{top['segment_name']}» — {_n(top['n_epk'])} ФЛ, "
            f"{_pct(top['share'], 0)}.")
    if to_codes is not None and not to_codes.empty:
        top = to_codes.iloc[0]
        names = ", ".join(f"«{r.code_name or r.code}» ({_n(r.n_epk)})"
                          for r in to_codes.head(3).itertuples())
        parts.append(
            f"Тем, кому от бюджетной организации приходят только незарплатные "
            f"выплаты, приходит: {names}. Чаще всего это «{top['code_name'] or top['code']}» "
            f"— {_pct(top['share'], 0)} таких людей.")
    if mig is not None and not mig.empty:
        n = int(mig["n_epk"].sum())
        parts.append(
            f"Похоже на переоформление у {len(mig)} организаций: их ФЛ ({_n(n)}) "
            f"дружно перешли в одну и ту же новую организацию.")
    return " ".join(parts) or "Куда делись люди, разрезы не показали."


def fb_when(tr, st, measured: str, cmp_months, surv, seas, codes_m) -> str:
    if tr is None or tr.empty:
        return "Помесячный ряд не построился — сказать, когда произошло падение, нечем."
    parts = []
    # Сезонность — первым делом: она объясняет провал отчётного месяца, и без неё
    # этот провал читается как уход.
    if seas and seas.get("n_gone_cur"):
        parts.append(
            f"Из {_n(seas['n_prev_both'])} ФЛ, получавших в "
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
                f"«{worst['name']}»: {_n(worst['d_month'])} ФЛ "
                f"({_pct(worst['d_month_pct'])}), год к году "
                f"{_n(worst['d_year'])}. Это вид выплаты, а не ушедшие люди: у "
                f"человека может просто не быть выплаты этого вида в этом месяце.")
    if surv is not None and not surv.empty:
        last = surv.iloc[-1]
        parts.append(f"Из получателей базового месяца в отчётном получают "
                     f"{_n(last['n_alive'])} — {_pct(last['share'])}.")
    return " ".join(parts)


def fb_where(cuts: dict) -> str:
    parts = []
    titles = {"holding_name": "холдингам", "agency": "ведомствам",
              "level": "уровню подчинения", "tb_short_name": "территориальным банкам",
              "gosb_name": "ГОСБ"}
    for dim, df in cuts.items():
        if df is None or df.empty:
            continue
        top = df.iloc[0]
        loss = float(top.get("loss", 0))
        causes = ", ".join(
            f"{name} {_n(top.get(col, 0))}" for col, name in (
                ("left_bank", "нет зачислений в банке"),
                ("left_segment", "зарплата от небюджетной организации"),
                ("other_codes", "только незарплатные выплаты"),
                ("below_threshold", "зарплата до 2 500 ₽")))
        text = (f"По {titles.get(dim, dim)} больше всего перестали получать зарплату "
                f"в РГС в группе «{top[dim]}» — {_n(loss)} получателей "
                f"({_pct(top.get('share', 0), 0)} всех переставших): {causes}.")
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
            f"({m.get('n_gone', 0)}) или массово ({m.get('n_mass', 0)}), приходится "
            f"{_pct(m.get('share_lb_org', 0), 0)} всех ФЛ без зачислений в банке; "
            f"остальные уходили по одному.")
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


def fb_multi(m: dict) -> str:
    """Почему стало меньше совместителей — правилами."""
    if not m:
        return "Разбор совместительства не построился."
    from .analyze import inside_title
    d, dm = float(m["d_triples"]), float(m["d_multi"])
    share = f" ({_pct(dm / d, 0)} изменения)" if d else ""
    parts = [f"Изменение получателей {_n(d)} складывается из изменения ФЛ "
             f"{_n(m['d_epk'])} и изменения совместительства {_n(dm)}{share}. "
             f"Из совместительства: совместители, переставшие получать зарплату в "
             f"РГС, дали {_n(m['lost'])}, совместители среди начавших "
             f"{_n(m['gained'])}, а «{inside_title(m['inside'])}» — "
             f"{_n(m['inside'])}."]
    st = m.get("structure")
    if st is not None and not st.empty:
        g = st.set_index("key")
        if "extra_gosb" in g.index and "extra_inn" in g.index:
            parts.append(
                f"Лишние получатели из-за нескольких организаций изменились на "
                f"{_n(g.at['extra_inn', 'delta'])}, из-за одной организации через "
                f"несколько ГОСБ — на {_n(g.at['extra_gosb', 'delta'])}.")
    rows = m.get("rows")
    if rows is not None and not rows.empty:
        not_exit = float(rows[~rows["is_exit"]]["net"].sum())
        exit_ = float(rows[rows["is_exit"]]["net"].sum())
        top = rows.sort_values("net").iloc[0]
        parts.append(
            f"У продолжающих получать в РГС не уход человека (ГОСБ, закрытие и "
            f"слияние организаций) дал {_n(not_exit)}, уход со второй работы или её "
            f"зарплаты — {_n(exit_)}; крупнейшее — «{top['title']}»: "
            f"{_n(top['net'])}.")
    return " ".join(parts)
