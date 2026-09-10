"""Шаг 3: сборка HTML. Самодостаточный файл, открывается без сети.

Компоненты, тема и каркас страницы берутся из `uzp_dash.render`: второй набор
стилей означал бы, что разбор и дэши выглядят по-разному без единой причины.

Графики — ИНЛАЙНОВЫЙ SVG. Не потому, что так красивее, а потому, что внутри
контура нет интернета: библиотека графиков с CDN дала бы пустое место в файле, и
заметили бы это только на проме.

В отчёт попадают ТОЛЬКО те расчёты, которые что-то показали. Раздел, посчитанный
по нулям, выглядит прилично и не значит ничего — а читатель принимает его за
ответ. Поэтому пустой раздел либо не строится вовсе, либо честно говорит, чего не
хватило.

Слово-идентификатор организации вычищается из ГОТОВОГО документа целиком
(`render.html.page` → `sanitize`), последним шагом.
"""
from __future__ import annotations

import pandas as pd

from uzp_dash.render import components as C
from uzp_dash.render import html as H


# Цвета веток раскладки. Настоящая потеря — тёплые тона, методология — серо-синие:
# читатель обязан отличать «людей стало меньше» от «людей столько же, считаем
# иначе» до того, как начнёт читать числа.
COLORS = {
    "person_left_bank": "#c2410c",
    "left_rgs":         "#ea580c",
    "moved_within_rgs": "#94a3b8",
    "inn_gone":         "#b91c1c",
    "liquidated":       "#7f1d1d",
    "below_threshold":  "#0369a1",
    "code_out_of_list": "#0891b2",
    "multi_collapsed":  "#64748b",
}

EXTRA_CSS = """
<style>
.spark-ax{display:flex;justify-content:space-between;font-size:12px;color:var(--text-2);margin-top:2px}
.sbar{display:flex;height:18px;border-radius:9px;overflow:hidden;margin:10px 0 8px}
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
.wf{display:flex;flex-direction:column;gap:6px;margin:14px 0}
.wf-row{display:grid;grid-template-columns:230px 1fr 96px 62px;gap:10px;align-items:center;font-size:13px}
.wf-bar{height:15px;border-radius:7px;min-width:2px}
.wf-name{color:var(--text-1)}
.wf-val{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.wf-pct{text-align:right;color:var(--text-2);font-variant-numeric:tabular-nums}
.wf-desc{grid-column:1/-1;color:var(--text-2);font-size:12px;margin:-2px 0 6px 0}
@media (max-width:820px){.wf-row{grid-template-columns:1fr 70px 52px}.wf-bar{display:none}}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;
     background:color-mix(in srgb,var(--text-2) 14%,transparent);color:var(--text-2)}
.tag.real{background:color-mix(in srgb,#c2410c 16%,transparent);color:#c2410c}
</style>
"""


def _pct(v, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{float(v) * 100:.{digits}f}%".replace(".", ",")


def _n(v, digits: int = 0) -> str:
    return C.fmt_num(v, digits=digits)


def _signed(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return ("+" if float(v) > 0 else "") + _n(v)


def _note(text: str) -> str:
    return f'<div class="note">{C.esc(text)}</div>'


def _src(text: str) -> str:
    return f'<div class="src">{C.esc(text)}</div>'


def _fallback_mark(used: bool) -> str:
    return ('<div class="fb">Вывод собран расчётом по правилам: модель не ответила '
            'или её ответ не разобрался.</div>') if used else ""


def _empty(title: str, why: str) -> str:
    """Честная заглушка. Пустая таблица выглядела бы как посчитанный ответ."""
    return C.section(title, f'<div class="muted">{C.esc(why)}</div>')


# --------------------------------------------------------------------------- #
# Мини-графики
# --------------------------------------------------------------------------- #
def sparkline(points: list[tuple[str, float]], height: int = 130,
              caption: str = "", marks: set[str] | None = None) -> str:
    """Линия ряда. `marks` — месяцы, которые надо выделить (обрывы).

    Обрывы помечаются НА САМОМ ряду, а не отдельным списком под ним: месяц-ступень
    и есть главный ответ на «когда», и искать его глазами по таблице читатель не
    должен.
    """
    if len(points) < 2:
        return '<div class="muted">ряда нет — рисовать нечего</div>'
    marks = marks or set()
    vals = [float(v) for _, v in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    w, pad = 100.0, 8.0
    step = w / (len(vals) - 1)
    coords = [(i * step, height - pad - (v - lo) / span * (height - 2 * pad))
              for i, v in enumerate(vals)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.2f},{y:.2f}"
                    for i, (x, y) in enumerate(coords))
    area = path + f" L{coords[-1][0]:.2f},{height} L0,{height} Z"
    dots = []
    for (label, _), (x, y) in zip(points, coords):
        if label in marks:
            dots.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.6" fill="#c2410c"/>')
        else:
            dots.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="1.1" '
                        f'fill="var(--accent)"/>')
    cap = (f'<div class="muted" style="margin-top:4px">{C.esc(caption)}</div>'
           if caption else "")
    return (
        f'<svg viewBox="0 0 100 {height}" preserveAspectRatio="none" '
        f'style="width:100%;height:{height}px;display:block">'
        f'<path d="{area}" fill="color-mix(in srgb, var(--accent) 12%, transparent)"/>'
        f'<path d="{path}" fill="none" stroke="var(--accent)" stroke-width="0.8" '
        f'vector-effect="non-scaling-stroke"/>{"".join(dots)}</svg>'
        f'<div class="spark-ax"><span>{C.esc(points[0][0])}</span>'
        f'<span>{C.esc(points[-1][0])}</span></div>{cap}')


def waterfall(rows: list[dict], total: float) -> str:
    """Раскладка одного целого на части: название, полоса, число, доля.

    Полоса рисуется от МАКСИМАЛЬНОЙ части, а не от целого: иначе мелкие ветки
    вырождаются в невидимую чёрточку и читатель их просто не замечает — а среди
    мелких как раз те, что отличают методологию от оттока.
    """
    if not rows:
        return '<div class="muted">раскладывать нечего</div>'
    mx = max(abs(float(r["value"])) for r in rows) or 1.0
    out = []
    for r in rows:
        v = float(r["value"])
        width = abs(v) / mx * 100
        tag = ('<span class="tag real">реальная потеря</span>' if r.get("real")
               else '<span class="tag">счёт, а не люди</span>')
        out.append(
            f'<div class="wf-row">'
            f'<div class="wf-name">{C.esc(r["name"])} {tag}</div>'
            f'<div><div class="wf-bar" style="width:{width:.1f}%;'
            f'background:{r.get("color", "var(--accent)")}"></div></div>'
            f'<div class="wf-val">{_n(v)}</div>'
            f'<div class="wf-pct">{_pct(v / (total or 1), 0)}</div>'
            f'</div>')
        if r.get("descr"):
            out.append(f'<div class="wf-desc">{C.esc(r["descr"])}</div>')
    return f'<div class="wf">{"".join(out)}</div>'


# --------------------------------------------------------------------------- #
# Разделы
# --------------------------------------------------------------------------- #
def head_kpi(t: dict) -> str:
    """Первый экран: то самое падение и то, из чего оно на самом деле состоит."""
    d = t["d_pairs"]
    real = -t["lost_real"] + t["gained"]
    return C.stat_row([
        {"value": _n(t["pairs_base"]),
         "caption": f"получателей в {t['base_month']:%m.%Y}",
         "sub": f"{_n(t['epk_base'])} человек, {_n(t['inn_base'])} организаций"},
        {"value": _n(t["pairs_cur"]),
         "caption": f"получателей в {t['report_month']:%m.%Y}",
         "sub": f"{_n(t['epk_cur'])} человек, {_n(t['inn_cur'])} организаций"},
        {"value": _signed(d), "caption": "изменение год к году",
         "kind": "bad" if d < 0 else "good",
         "sub": _pct(d / (t["pairs_base"] or 1))},
        {"value": _signed(real),
         "caption": "из них реальная потеря получателей",
         "kind": "bad" if real < 0 else "good",
         "sub": f"остальное — методология счёта"},
    ])


def metric_block(t: dict) -> str:
    """Из чего сложено падение: людей меньше или организаций на человека меньше.

    Это первое, что надо развести. Метрика считается парами, поэтому падение
    доли совместителей уменьшает её, не тронув ни одного человека, — и без этой
    раскладки весь дальнейший разбор идёт не про то.
    """
    parts = [
        {"name": "Людей стало меньше", "value": t["d_by_people"],
         "color": "#c2410c", "real": True,
         "descr": "вклад изменения числа людей при прежнем совместительстве"},
        {"name": "Совместительство схлопнулось", "value": t["d_by_multi"],
         "color": "#64748b", "real": False,
         "descr": "вклад изменения среднего числа организаций на человека — "
                  "людей это не убавляет"},
    ]
    tbl = C.table(
        ["Показатель", f"{t['base_month']:%m.%Y}", f"{t['report_month']:%m.%Y}",
         "Изменение"],
        [["Пар (человек × организация)", _n(t["pairs_base"]), _n(t["pairs_cur"]),
          _signed(t["d_pairs"])],
         ["Людей", _n(t["epk_base"]), _n(t["epk_cur"]), _signed(t["d_epk"])],
         ["Организаций на человека", f"{t['multi_base']:.4f}".replace(".", ","),
          f"{t['multi_cur']:.4f}".replace(".", ","),
          f"{t['multi_cur'] - t['multi_base']:+.4f}".replace(".", ",")],
         ["Доля совместителей", _pct(t["multi_share_base"], 2),
          _pct(t["multi_share_cur"], 2),
          _pct(t["multi_share_cur"] - t["multi_share_base"], 2)]],
        num_cols=[1, 2, 3])
    return C.section(
        "Падение метрики: люди или счёт",
        C.card(waterfall(parts, t["d_pairs"]) + tbl
               + _src("Метрика — пары (человек, организация) с суммой зачислений "
                      "за месяц выше порога. Совместитель весит два получателя.")),
        eyebrow="с этого начинается разбор")


def causes_block(causes: pd.DataFrame, gains: pd.DataFrame, t: dict,
                 text: str = "", fb: bool = False) -> str:
    """Лестница причин — ядро отчёта."""
    if causes.empty:
        return _empty("Из чего состоит потеря", "раскладка не посчиталась")
    rows = [{"name": r.title, "value": float(r.n_pairs),
             "color": COLORS.get(r.cause, "var(--accent)"),
             "real": bool(r.is_real_loss), "descr": r.descr}
            for r in causes.itertuples()]
    gain_html = ""
    if not gains.empty:
        grows = [[C.esc(r.title), _n(r.n_pairs), _pct(r.share, 0), _n(r.n_epk)]
                 for r in gains.itertuples()]
        gain_html = ("<h3>Пришло за тот же год</h3>"
                     + C.table(["Откуда", "Пар", "Доля", "Человек"], grows,
                               num_cols=[1, 2, 3]))
    summary = (
        f'<p>Потеряно <b>{_n(t["lost"])}</b> пар, пришло <b>{_n(t["gained"])}</b>. '
        f'Из потерянных <b>{_n(t["lost_not_real"])}</b> '
        f'({_pct(t["lost_not_real"] / (t["lost"] or 1), 0)}) — это не ушедшие люди, '
        f'а особенности счёта: сумма ниже порога, код зачисления вне списка, '
        f'схлопнувшееся совместительство и переходы внутри сегмента.</p>')
    return C.section(
        "Из чего состоит потеря",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + summary + waterfall(rows, t["lost"]) + gain_html
               + _src("Каждая потерянная пара получает ровно одну причину — "
                      "первую сработавшую по лестнице приоритетов, поэтому части "
                      "складываются в целое.")),
        eyebrow="ответ на «сколько на самом деле»")


def when_block(tr: pd.DataFrame, st: pd.DataFrame, surv: pd.DataFrame,
               load: pd.DataFrame, text: str = "", fb: bool = False) -> str:
    """Когда именно произошло падение."""
    if tr.empty:
        return _empty("Когда это произошло", "помесячный ряд не построился")
    pts = [(f"{pd.Timestamp(r.report_dt):%m.%y}", float(r.n_pairs))
           for r in tr.itertuples()]
    marks = {f"{pd.Timestamp(r.report_dt):%m.%y}" for r in st.itertuples()} \
        if not st.empty else set()

    step_html = ""
    if not st.empty:
        rows = [[f"{pd.Timestamp(r.report_dt):%m.%Y}", _signed(r.d_pairs),
                 _pct(r.d_pairs_pct), f"{r.score:.1f}".replace(".", ",")]
                for r in st.itertuples()]
        step_html = (
            "<h3>Месяцы-обрывы</h3>"
            + C.table(["Месяц", "Изменение пар", "К предыдущему", "Во сколько раз "
                       "резче обычного шага"], rows, num_cols=[1, 2, 3])
            + _note("Обрыв в одном месяце — это событие: смена кодировки выплат, "
                    "переклассификация или загрузка. Плавное снижение — текучесть. "
                    "По двум точкам года эти случаи неразличимы."))
    else:
        step_html = _note("Ни одного месяца-обрыва не найдено: падение идёт "
                          "плавно, месяц за месяцем. Это текучесть, а не событие.")

    surv_html = ""
    if not surv.empty:
        last = surv.iloc[-1]
        spts = [(f"{pd.Timestamp(r.report_dt):%m.%y}", float(r.share))
                for r in surv.itertuples()]
        surv_html = (
            "<h3>Дожитие когорты базового месяца</h3>"
            + sparkline(spts, height=110,
                        caption=f"осталось {_pct(last['share'])} пар базового месяца")
            + _src("Доля пар базового месяца, доживших до каждого следующего. "
                   "Считается по тем же самым парам, а не по численности сегмента."))

    load_html = ""
    if not load.empty and bool(load["is_thin"].any()):
        thin = load[load["is_thin"]]
        load_html = _note(
            "Недогруженные месяцы по всему банку: "
            + ", ".join(f"{pd.Timestamp(r.report_dt):%m.%Y}"
                        for r in thin.itertuples())
            + ". Падение в этих точках объясняется загрузкой партиции, а не людьми.")

    return C.section(
        "Когда это произошло",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + sparkline(pts, caption="получателей (пар) по месяцам", marks=marks)
               + step_html + surv_html + load_html),
        eyebrow="ответ на «когда»")


def why_block(thr: pd.DataFrame, codes: pd.DataFrame, mig: pd.DataFrame,
              text: str = "", fb: bool = False) -> str:
    """Три проверки, каждая из которых может объяснить падение целиком."""
    parts = []

    if not thr.empty:
        rows = [[_n(r.threshold) + " ₽", _n(r.base), _n(r.cur), _signed(r.delta),
                 _pct(r.delta_pct)] for r in thr.itertuples()]
        parts.append(
            "<h3>Чувствительность к порогу получателя</h3>"
            + C.table(["Порог", "База", "Отчёт", "Изменение", "%"], rows,
                      num_cols=[1, 2, 3, 4])
            + _src("Порог фиксирован, а зарплаты индексируются — сам по себе он "
                   "должен год к году добавлять получателей. Если падение "
                   "сохраняется при пороге 0, порог ни при чём."))

    if not codes.empty:
        moved = codes[codes["delta"].abs() > 0].head(12)
        rows = [[_n(r.code), C.esc(str(r.code_name or "")),
                 "в списке" if r.in_list else "вне списка",
                 _n(r.base), _n(r.cur), _signed(r.delta)]
                for r in moved.itertuples()]
        parts.append(
            "<h3>Куда переехали коды зачисления</h3>"
            + C.table(["Код", "Вид зачисления", "Статус", "База", "Отчёт",
                       "Изменение"], rows, num_cols=[0, 3, 4, 5])
            + _src("Исчезнувший из метрики код обычно не исчезает из витрины — "
                   "он переезжает в соседний, которого нет в списке. Поэтому "
                   "показаны и те и другие."))

    if not mig.empty:
        rows = []
        for r in mig.head(12).itertuples():
            rows.append([
                C.esc(str(getattr(r, "name_from", None) or r.inn_from)),
                C.esc(str(getattr(r, "name_to", None) or r.inn_to)),
                _n(r.n_epk), _pct(r.share, 0),
                "да" if getattr(r, "to_in_segment", False) else "нет"])
        parts.append(
            "<h3>Похоже на реорганизацию, а не на отток</h3>"
            + C.table(["Откуда", "Куда", "Человек", "Доля потерь организации",
                       "Приёмник в сегменте"], rows, num_cols=[2, 3])
            + _note("Люди этих организаций дружно оказались в одном и том же новом "
                    "ИНН. Это переоформление: для банка они никуда не уходили. "
                    "Если приёмник ещё не размечен как бюджетный, метрика теряет "
                    "их дважды — и как отток, и как непопадание в сегмент."))

    if not parts:
        return _empty("Почему это произошло",
                      "ни одна из проверок причины не дала результата")
    return C.section(
        "Почему это произошло",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + "".join(parts)),
        eyebrow="ответ на «почему»")


def where_block(cuts: dict, orgs: pd.DataFrame, meta: dict,
                text: str = "", fb: bool = False) -> str:
    """Разрезы потерь. В каждом — не только объём, но и состав причин."""
    parts = []
    titles = {
        "holding_name": "Холдинги",
        "agency": "Ведомства (по наименованию)",
        "level": "Уровень подчинения",
        "industry_name": "Отрасль справочника",
        "tb_short_name": "Территориальные банки",
        "region_name": "Регионы",
    }
    for dim, df in cuts.items():
        if df is None or df.empty:
            continue
        rows = []
        for r in df.itertuples():
            rows.append([
                C.esc(str(getattr(r, dim))),
                _n(r.n_pairs), _pct(r.share, 0), _n(r.n_inn),
                _n(r.real_loss), _n(r.not_real),
            ])
        parts.append(f"<h3>{C.esc(titles.get(dim, dim))}</h3>"
                     + C.table([titles.get(dim, dim), "Потеряно пар", "Доля потерь",
                                "Организаций", "Из них отток", "Методология"],
                               rows, num_cols=[1, 2, 3, 4, 5]))

    if not orgs.empty:
        rows = [[C.esc(str(r.company_name or r.inn)), _n(r.n_pairs),
                 C.esc(str(r.cause_title or "")), C.esc(str(r.agency or "")),
                 C.esc(str(r.level or "")), C.esc(str(r.tb_short_name or ""))]
                for r in orgs.itertuples()]
        parts.append("<h3>Организации с наибольшей потерей</h3>"
                     + C.table(["Организация", "Потеряно пар",
                                "Преобладающая причина", "Ведомство", "Уровень",
                                "ТБ"], rows, num_cols=[1]))

    if not parts:
        return _empty("Где это произошло", "разрезы не построились")

    cov = ""
    if meta:
        cov = _src(
            f"Ведомство и уровень подчинения выводятся из наименования: "
            f"отдельных полей для них нет ни в одном разрешённом источнике. "
            f"Имя известно у {_pct(meta.get('named_share', 0), 0)} организаций; "
            f"неразобранные имена показаны отдельной строкой и по группам не "
            f"раскидываются.")
    return C.section(
        "Где это произошло",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + "".join(parts) + cov),
        eyebrow="ответ на «где»")


def limits_block(warnings: list[str], checks: list[dict], probe: dict) -> str:
    """Ограничения и проверки. Читается ДО выводов, а не после."""
    items = [
        "<li>Сегмент организации берётся текущим срезом справочника ЕПК: "
        "отчётной даты в этой витрине нет. Один и тот же список организаций "
        "применён к обоим годам, поэтому перекоса год к году это не создаёт — "
        "но переклассификация организации между годами в разбор не попадёт.</li>",
        "<li>Отчёт не скажет, в какой банк ушли люди: в разрешённых источниках "
        "такого поля нет.</li>",
        "<li>Увольнение и смену работодателя вне клиентской базы банка отчёт не "
        "различает: человек, которого нет в ведомостях, для банка в обоих "
        "случаях выглядит одинаково.</li>",
    ]
    if probe.get("inn_ok_base") is not None:
        items.append(
            f"<li>Доля организаций, чей номер приводится к числу и участвует в "
            f"сопоставлении: {_pct(probe['inn_ok_base'], 2)} в базовом месяце, "
            f"{_pct(probe.get('inn_ok_cur') or 0, 2)} в отчётном. Строки вне этой "
            f"доли в разбор не входят — ни в одном из годов.</li>")
    if not probe.get("holding_measurable", True):
        items.append("<li>Холдинг в справочнике ЕПК не заполнен — разрез по "
                     "холдингам не строился.</li>")
    if probe.get("temp_tables") is False:
        items.append("<li>Временные таблицы в сессии недоступны: разбор шёл "
                     "запасным путём через CTE. На числа это не влияет.</li>")

    warn_html = ""
    if warnings:
        warn_html = ("<h3>Предупреждения прогона</h3><ul>"
                     + "".join(f"<li>{C.esc(w)}</li>" for w in warnings) + "</ul>")

    check_html = ""
    if checks:
        rows = [[C.esc(c["name"]), _n(c["left"]), _n(c["right"]),
                 "сошлось" if c["ok"] else "НЕ СОШЛОСЬ"] for c in checks]
        check_html = ("<h3>Проверки сходимости</h3>"
                      + C.table(["Проверка", "Посчитано", "Ожидалось", "Итог"],
                                rows, num_cols=[1, 2]))

    return C.section("Что этот отчёт не говорит",
                     C.card(f"<ul>{''.join(items)}</ul>" + warn_html + check_html),
                     eyebrow="читать до выводов")


def page(title: str, subtitle: str, blocks: list[str], footer: str) -> str:
    """Готовая страница. Слово-идентификатор вычищается внутри `H.page`."""
    return H.page(title, subtitle, EXTRA_CSS + "".join(blocks), footer)
