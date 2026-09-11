"""Шаг 5: сборка HTML. Самодостаточный файл, открывается без сети.

Компоненты, тема и каркас страницы берутся из `uzp_dash.render`: второй набор
стилей означал бы, что отчёт и дэши выглядят по-разному без единой причины.

Графики рисуются ИНЛАЙНОВЫМ SVG. Не потому, что так красивее, а потому, что
внутри контура нет интернета: любая библиотека графиков с CDN дала бы пустое
место в файле, и заметили бы это только на проме. Картинок тоже нет — SVG
масштабируется и печатается.

Слово-идентификатор организации вычищается из ГОТОВОГО документа целиком
(`render.html.page` → `sanitize`), последним шагом: тогда под замену попадает
и то, что пришло из витрины, и то, что сочинила модель.
"""
from __future__ import annotations

import pandas as pd

from uzp_dash.render import components as C
from uzp_dash.render import html as H

# Порог, ниже которого доля оттока считается нормальной. Не «правильное» значение,
# а порог ВНИМАНИЯ: он задаёт цвет, но никогда не единственный носитель смысла —
# рядом всегда стоит число.
RATE_WARN = 0.03
RATE_BAD = 0.06


def _kind_by_rate(rate: float | None) -> str:
    if rate is None or pd.isna(rate):
        return "warn"
    if rate >= RATE_BAD:
        return "bad"
    if rate >= RATE_WARN:
        return "warn"
    return "good"


def _pct(v, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{float(v) * 100:.{digits}f}%".replace(".", ",")


def _n(v, digits: int = 0) -> str:
    return C.fmt_num(v, digits=digits)


# --------------------------------------------------------------------------- #
# Мини-графики: инлайновый SVG, без единой внешней зависимости
# --------------------------------------------------------------------------- #

def sparkline(points: list[tuple[str, float]], height: int = 120,
              caption: str = "") -> str:
    """Линия ряда с подписью краёв. Пустой ряд рисуется как честная заглушка."""
    if len(points) < 2:
        return '<div class="muted">ряда нет — рисовать нечего</div>'
    vals = [float(v) for _, v in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    w, pad = 100.0, 6.0
    step = w / (len(vals) - 1)
    coords = [(i * step,
               height - pad - (v - lo) / span * (height - 2 * pad))
              for i, v in enumerate(vals)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.2f},{y:.2f}"
                    for i, (x, y) in enumerate(coords))
    area = path + f" L{coords[-1][0]:.2f},{height} L0,{height} Z"
    dots = "".join(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="1.1" fill="var(--accent)"/>'
                   for x, y in coords)
    cap = f'<div class="muted" style="margin-top:4px">{C.esc(caption)}</div>' if caption else ""
    return (
        f'<svg viewBox="0 0 100 {height}" preserveAspectRatio="none" '
        f'style="width:100%;height:{height}px;display:block">'
        f'<path d="{area}" fill="color-mix(in srgb, var(--accent) 12%, transparent)"/>'
        f'<path d="{path}" fill="none" stroke="var(--accent)" stroke-width="0.8" '
        f'vector-effect="non-scaling-stroke"/>{dots}</svg>'
        f'<div class="spark-ax"><span>{C.esc(points[0][0])}</span>'
        f'<span>{C.esc(points[-1][0])}</span></div>{cap}')


def stacked_bar(parts: list[tuple[str, float, str]]) -> str:
    """Стековая полоса: доли частей одного целого + легенда с числами."""
    total = sum(max(v, 0) for _, v, _ in parts)
    if total <= 0:
        return '<div class="muted">делить нечего</div>'
    seg = "".join(
        f'<span style="width:{max(v, 0) / total * 100:.2f}%;background:{color}" '
        f'title="{C.esc(name)}"></span>' for name, v, color in parts)
    legend = "".join(
        f'<div class="lg"><i style="background:{color}"></i>{C.esc(name)} — '
        f'<b>{_n(v)}</b> <span class="muted">({v / total:.0%})</span></div>'
        for name, v, color in parts)
    return f'<div class="sbar">{seg}</div><div class="lgs">{legend}</div>'


def scenario_chart(fc: pd.DataFrame, base_month, base_value: float) -> str:
    """Три сценария на одной оси. Общая шкала — иначе их не сравнить глазами."""
    if fc is None or fc.empty:
        return ""
    colors = {"Сохранение": "var(--accent)", "Восстановление": "var(--good)",
              "Риск": "var(--bad)"}
    series = {}
    for sc, g in fc.groupby("scenario"):
        t = g.groupby("ym", as_index=False)["fl_end"].sum().sort_values("ym")
        series[sc] = ([pd.Timestamp(base_month)] + list(t["ym"]),
                      [base_value] + list(t["fl_end"]))
    all_v = [v for _, ys in series.values() for v in ys]
    lo, hi = min(all_v), max(all_v)
    span = (hi - lo) or 1.0
    h, pad = 160.0, 8.0
    n = max(len(xs) for xs, _ in series.values())
    step = 100.0 / max(n - 1, 1)
    paths = []
    for sc, (xs, ys) in series.items():
        pts = [(i * step, h - pad - (v - lo) / span * (h - 2 * pad))
               for i, v in enumerate(ys)]
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.2f},{y:.2f}"
                     for i, (x, y) in enumerate(pts))
        paths.append(f'<path d="{d}" fill="none" stroke="{colors.get(sc, "var(--text-2)")}" '
                     f'stroke-width="1" vector-effect="non-scaling-stroke"/>')
    legend = "".join(f'<div class="lg"><i style="background:{colors.get(sc)}"></i>'
                     f'{C.esc(sc)}</div>' for sc in series)
    first = pd.Timestamp(base_month).strftime("%m.%Y")
    last = max(max(xs) for xs, _ in series.values()).strftime("%m.%Y")
    return (f'<svg viewBox="0 0 100 {h:.0f}" preserveAspectRatio="none" '
            f'style="width:100%;height:{h:.0f}px;display:block">{"".join(paths)}</svg>'
            f'<div class="spark-ax"><span>{first}</span><span>{last}</span></div>'
            f'<div class="lgs">{legend}</div>')


# --------------------------------------------------------------------------- #
# Дополнительный CSS: только то, чего нет в общей теме
# --------------------------------------------------------------------------- #

EXTRA_CSS = """
<style>
.spark-ax{display:flex;justify-content:space-between;font-size:12px;color:var(--text-2);margin-top:2px}
.sbar{display:flex;height:16px;border-radius:8px;overflow:hidden;margin:10px 0 8px}
.sbar span{display:block;height:100%}
.lgs{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--text-2)}
.lg{display:flex;align-items:center;gap:6px}
.lg i{width:10px;height:10px;border-radius:3px;display:inline-block}
.muted{color:var(--text-2);font-size:13px}
.note{border-left:3px solid var(--warn);padding:8px 12px;margin:12px 0;
      color:var(--text-2);font-size:13px;background:color-mix(in srgb,var(--warn) 7%,transparent)}
.src{font-size:12px;color:var(--text-2);margin-top:8px}
.fb{font-size:12px;color:var(--warn);margin-bottom:6px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media (max-width:820px){.grid2{grid-template-columns:1fr}}
</style>
"""


def _fallback_mark(used: bool) -> str:
    """Пометка «текст посчитан правилами». Читатель обязан это видеть."""
    return ('<div class="fb">Вывод собран расчётом по правилам: модель не ответила '
            'или её ответ не разобрался.</div>') if used else ""


def _note(text: str) -> str:
    return f'<div class="note">{C.esc(text)}</div>'


def _src(text: str) -> str:
    return f'<div class="src">{C.esc(text)}</div>'


# --------------------------------------------------------------------------- #
# Разделы
# --------------------------------------------------------------------------- #

def _stats(items: list[dict]) -> str:
    """Равнозначные числа в ряд: {value, caption, sub, kind}.

    Общие `stat_row` / `big_stat` и их CSS удалены из компонентов рефакторингом
    отрисовки (679b22e). Ряд собирается из уцелевших `kpi` в сетке темы: подпись
    числа уходит в `delta`, цвет статуса — в `delta_kind`, так что слово рядом с
    цветом остаётся и смысл карточек не меняется.
    """
    cols = min(max(len(items), 1), 4)
    cards = "".join(
        C.kpi(i.get("caption", ""), C.esc(i["value"]), i.get("sub", ""),
              i.get("kind", "") or "text-2")
        for i in items)
    return f'<div class="grid cols-{cols}">{cards}</div>'


def head_kpi(month, base_fl, out_qty, ret_qty, out_kept, n_org) -> str:
    rate = out_kept / max(base_fl, 1)
    return _stats([
        {"value": _n(base_fl), "caption": "получателей в бюджетной сфере",
         "sub": f"{_n(n_org)} организаций"},
        {"value": _n(out_qty), "caption": "ушло за месяц", "kind": "bad"},
        {"value": _n(ret_qty), "caption": "вернулось", "kind": "good"},
        {"value": _n(out_kept), "caption": "ушло и не вернулось",
         "kind": _kind_by_rate(rate), "sub": f"{_pct(rate)} численности"},
    ])


def agencies_block(ag: pd.DataFrame, cut: dict, text: str, fb: bool) -> str:
    if ag.empty:
        return C.section("Ведомства", '<div class="muted">данных нет</div>')
    rows = []
    for r in ag.itertuples():
        rows.append([
            C.esc(r.agency),
            _n(r.out_kept),
            C.meter(min(r.out_rate / RATE_BAD, 1.2)) + f' <span>{_pct(r.out_rate)}</span>',
            _pct(r.share, 0),
            _n(r.n_org),
            _pct(r.ret_rate, 0),
        ])
    tbl = C.table(["Ведомство", "Ушло и не вернулось", "Доля от численности",
                   "Вклад в отток", "Организаций", "Возвращено"],
                  rows, num_cols=[1, 3, 4, 5])
    hidden = ""
    if cut.get("hidden"):
        hidden = _src(f"Свёрнуто в «прочие»: {cut['hidden']} ведомств "
                      f"({_n(cut['hidden_value'])} человек)")
    return C.section(
        "Где основной отток: ведомства",
        _fallback_mark(fb) + C.narrative_html(text) + C.card(tbl) + hidden,
        eyebrow="Отчётный месяц")


def trend_block(tr: dict, ag_tr: pd.DataFrame, source: str, text: str = "",
                fb: bool = False) -> str:
    total = tr["total"]
    pts = [(pd.Timestamp(r.ym).strftime("%m.%y"), float(r.out_kept))
           for r in total.itertuples()]
    d = tr["direction"]
    word = d.get("word", "—")
    head = (f"За последние {d['window']} мес. невозвращённый отток {word} на "
            f"{abs(d['rel']):.0%} относительно предыдущих {d['window']}."
            if d.get("measurable") else
            "Динамику измерить не по чему: в ряду меньше двух точек.")
    rows = [[C.esc(r.agency), _n(r.out_kept), C.esc(r.direction),
             _pct(r.rel, 0) if r.measurable and r.rel is not None else "—"]
            for r in ag_tr.itertuples()]
    tbl = C.table(["Ведомство", "Ушло и не вернулось", "Направление", "Изменение"],
                  rows, num_cols=[1, 3])
    src = {"fact_outflow": "витрина фактического оттока",
           "company_holding_metric": "витрина организаций (отток заполнен реже)",
           }.get(source, source)
    return C.section(
        "Тенденции",
        _fallback_mark(fb) + (C.narrative_html(text) if text else "") +
        C.card(f"<p>{C.esc(head)}</p>" +
               sparkline(pts, caption="невозвращённый отток по месяцам")) +
        C.card(tbl) + _src(f"Источник ряда: {src}"),
        eyebrow=f"{len(pts)} мес. истории")


def territory_block(tb: pd.DataFrame, reg: pd.DataFrame, subj: pd.DataFrame,
                    text: str, fb: bool, oktmo_note: str = "") -> str:
    def mini(df: pd.DataFrame, key: str, title: str) -> str:
        if df.empty:
            return C.card(f"<h3>{C.esc(title)}</h3>"
                          f'<div class="muted">данных нет</div>')
        rows = [[C.esc(getattr(r, key)), _n(r.out_kept), _pct(r.out_rate), _n(r.n_org)]
                for r in df.head(10).itertuples()]
        return C.card(f"<h3>{C.esc(title)}</h3>" +
                      C.table(["", "Ушло", "Доля", "Орг."], rows, num_cols=[1, 2, 3]))

    body = (_fallback_mark(fb) + C.narrative_html(text) +
            '<div class="grid2">' +
            mini(tb, "tb_short_name", "Территориальные банки") +
            mini(reg, "region_name", "Регионы") + "</div>" +
            mini(subj, "subject_code", "Субъекты РФ по ОКТМО"))
    if oktmo_note:
        body += _note(oktmo_note)
    return C.section("Территории и регионы", body, eyebrow="Отчётный месяц")


def causes_block(drop: float, staff: float, comp: float, window: int,
                 by_ag: pd.DataFrame, by_reg: pd.DataFrame, text: str,
                 fb: bool) -> str:
    if drop <= 0:
        return C.section("Причины оттока",
                         '<div class="muted">за окно разбора численность не падала</div>')
    bar = stacked_bar([("Сокращение штата", staff, "var(--warn)"),
                       ("Уход к конкуренту", comp, "var(--bad)")])
    rows = [[C.esc(r.agency), _n(r.drop), _n(r.staff_cut), _n(r.competitor),
             _pct(r.staff_share, 0)] for r in by_ag.itertuples()]
    t1 = C.table(["Ведомство", "Падение", "Сокращение штата", "Уход к конкуренту",
                  "Доля сокращения"], rows, num_cols=[1, 2, 3, 4])
    rows2 = [[C.esc(getattr(r, "region_name")), _n(r.drop), _n(r.staff_cut),
              _n(r.competitor), _pct(r.staff_share, 0)]
             for r in by_reg.head(12).itertuples()]
    t2 = C.table(["Регион", "Падение", "Сокращение штата", "Уход к конкуренту",
                  "Доля сокращения"], rows2, num_cols=[1, 2, 3, 4])
    method = (
        "Падение численности получателей за окно раскладывается на две части. "
        "Если вместе с получателями уменьшился и штат организации — людей стало "
        "меньше, и банк на это не влияет. Если штат на месте, а получателей стало "
        "меньше — те же люди получают зарплату в другом банке. Сумма частей равна "
        "падению по построению, поэтому разрезы сходятся с итогом.")
    return C.section(
        "Причины: сокращение штата или уход к конкуренту",
        _fallback_mark(fb) + C.narrative_html(text) +
        C.card(f"<p>За {window} мес. численность упала на <b>{_n(drop)}</b> "
               f"человек.</p>" + bar) +
        C.card(t1) +
        # Общий `disclosure` удалён из компонентов; нативный <details> работает
        # без JS и без CSS и раскрывается с клавиатуры.
        C.card(f"<details><summary>Разрез по регионам — показать</summary>"
               f"{t2}</details>") +
        _src(method),
        eyebrow=f"окно {window} мес.")


def macro_block(mc: pd.DataFrame) -> str:
    if mc.empty:
        return C.section("Зарплатные проекты в регионах",
                         '<div class="muted">данных нет</div>')
    rows = []
    for r in mc.head(15).itertuples():
        rows.append([
            C.esc(r.region_name), _n(r.fl_now), _n(r.emp_now), _pct(r.zp_now),
            _n(r.salary_now), _n(r.fl_y1_diff),
            _pct(r.plan_exec) if r.plan_exec == r.plan_exec else "—",
        ])
    tbl = C.table(["Регион", "Получателей", "Штат", "Проникновение",
                   "Средняя ЗП", "ФЛ год к году", "Исполнение плана"],
                  rows, num_cols=[1, 2, 3, 4, 5, 6])
    return C.section(
        "Зарплатные проекты в регионах",
        C.card(tbl) +
        _note("Это показатели витрин банка, а не государственная статистика. "
              "«Штат» — численность сотрудников организаций-клиентов по данным "
              "банка; «проникновение» — доля из них, получающая зарплату у нас. "
              "Занятость и сокращения по региону в целом здесь не измеряются."),
        eyebrow="внутренние прокси")


def competitors_block(cp: dict, text: str, fb: bool) -> str:
    banks = cp.get("banks", pd.DataFrame())
    cov = cp.get("coverage", 0.0)
    if banks is None or banks.empty:
        body = '<div class="muted">банк-конкурент не назван ни у одной организации</div>'
    else:
        rows = [[C.esc(r.bank), _n(r.out_kept), _n(r.n_org)]
                for r in banks.itertuples()]
        body = C.card(C.table(["Банк", "Ушло и не вернулось", "Организаций"],
                              rows, num_cols=[1, 2]))
    strat = cp.get("strategies", pd.DataFrame())
    if strat is not None and not strat.empty:
        rows = [[C.esc(r.strategy), _n(r.out_kept), _n(r.n_org)]
                for r in strat.itertuples()]
        body += C.card("<h3>Стратегия по клиенту</h3>" +
                       C.table(["Стратегия", "Ушло", "Организаций"], rows,
                               num_cols=[1, 2]))
    return C.section(
        "Активность банков-конкурентов",
        _fallback_mark(fb) + C.narrative_html(text) + body +
        _note(f"Банк-конкурент известен только по ключевым клиентам — это "
              f"{cov:.0%} организаций сегмента ({cp.get('known', 0)} из "
              f"{cp.get('total', 0)} с оттоком). Отсутствие банка в списке не "
              f"означает, что конкурента нет: про остальных не спрашивали."),
        eyebrow="ключевые клиенты")


def outlook_block(fc: pd.DataFrame, end: pd.DataFrame, diag: dict, month,
                  base: float, text: str, fb: bool) -> str:
    if end is None or end.empty:
        return C.section("Прогноз численности",
                         _fallback_mark(fb) + C.narrative_html(text) +
                         '<div class="muted">прогноз не построен</div>')
    stats = []
    kinds = {"Сохранение": "warn", "Восстановление": "good", "Риск": "bad"}
    for r in end.itertuples():
        stats.append({"value": _n(r.fl_end), "kind": kinds.get(r.scenario, ""),
                      "caption": r.scenario,
                      "sub": f"{r.delta:+,.0f} чел. · {r.delta_perc:+.1%}"
                             .replace(",", " ")})
    chart = scenario_chart(fc, pd.Timestamp(month), base)
    note = ""
    if not diag.get("season_measurable"):
        note = _note("Сезонность не учитывалась: для её оценки нужно два полных "
                     "года истории. Сентябрьский набор в образовании в прогнозе "
                     "не отражён.")
    method = ("«Сохранение» — отток и приход по среднему за окно; "
              "«Восстановление» — отток по среднему, приход по лучшему месяцу окна; "
              "«Риск» — отток по худшему месяцу окна, приход по среднему. "
              "Приход выведен из динамики численности, а не взят колонкой витрины.")
    return C.section(
        "Прогноз численности до конца года",
        _fallback_mark(fb) + C.narrative_html(text) + _stats(stats) +
        C.card(chart) + note + _src(method),
        eyebrow=f"горизонт {len(diag.get('horizon', []))} мес.")


def limits_block(warnings: list[str], checks: list[dict], meta: dict) -> str:
    items = []
    for w in warnings:
        items.append(f"<li>{C.esc(w)}</li>")
    if not items:
        items.append("<li>ограничений, влияющих на выводы, не зафиксировано</li>")
    bad = [c for c in checks if not c.get("ok", True)]
    check_html = ("<p>Все проверки сходимости пройдены: суммы частей совпадают "
                  "с итогами.</p>" if not bad else
                  "<p>НЕ СОШЛОСЬ: " + C.esc("; ".join(
                      f"{c['name']} — невязка {c['residual']:+.2f}" for c in bad)) +
                  "</p>")
    cov = meta.get("agency_share")
    cov_html = ""
    if cov is not None:
        cov_html = (f"<p>Ведомство определено у {meta.get('agency_known', 0)} из "
                    f"{meta.get('agency_total', 0)} организаций ({cov:.0%}). "
                    f"Остальные показаны отдельной строкой «Не классифицировано» "
                    f"и по ведомствам не распределены.</p>")
    return C.section(
        "Ограничения и проверки",
        C.card(f"<ul>{''.join(items)}</ul>" + cov_html + check_html),
        eyebrow="читать до выводов")


def page(title: str, subtitle: str, blocks: list[str], footer: str) -> str:
    """Готовая страница. Слово-идентификатор вычищается внутри `H.page`."""
    return H.page(title, subtitle, EXTRA_CSS + "".join(blocks), footer)
