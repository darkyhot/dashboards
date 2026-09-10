"""Шаг 3: сборка HTML. Самодостаточный файл, открывается без сети.

Компоненты, тема и каркас страницы берутся из `uzp_dash.render`: второй набор
стилей означал бы, что разбор и дэши выглядят по-разному без единой причины.

Графики — ИНЛАЙНОВЫЙ SVG. Не потому, что так красивее, а потому, что внутри
контура нет интернета: библиотека графиков с CDN дала бы пустое место в файле, и
заметили бы это только на проме.

У каждого блока — раскрывающийся запрос, которым посчитаны его цифры. Не пересказ
методики, а SQL, который можно скопировать и выполнить: показывается СЕБЯ-
ДОСТАТОЧНАЯ форма с подставленными выборками, даже если исполнялась форма по
временным таблицам (их читателю выполнить негде — они жили в чужой сессии).

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

from . import analyze as A

# Цвета видов движения. Настоящее — тёплые тона, счёт — серо-синие, перерыв —
# отдельный: читатель обязан отличать «людей стало меньше» от «людей столько же,
# считаем иначе» и от «месяц пропущен» до того, как начнёт читать числа.
KIND_COLOR = {A.REAL: "#c2410c", A.METHOD: "#0369a1", A.GAP: "#a16207"}
KIND_TAG = {A.REAL: "реальное движение", A.METHOD: "счёт, а не люди",
            A.GAP: "перерыв"}

EXTRA_CSS = """
<style>
.spark-ax{display:flex;justify-content:space-between;font-size:12px;color:var(--text-2);margin-top:2px}
.muted{color:var(--text-2);font-size:13px}
.note{border-left:3px solid var(--warn);padding:8px 12px;margin:12px 0;
      color:var(--text-2);font-size:13px;background:color-mix(in srgb,var(--warn) 7%,transparent)}
.src{font-size:12px;color:var(--text-2);margin-top:8px}
.fb{font-size:12px;color:var(--warn);margin-bottom:6px}
.wf{display:flex;flex-direction:column;gap:6px;margin:14px 0}
.wf-row{display:grid;grid-template-columns:250px 1fr 96px 62px;gap:10px;align-items:center;font-size:13px}
.wf-bar{height:15px;border-radius:7px;min-width:2px}
.wf-val{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.wf-pct{text-align:right;color:var(--text-2);font-variant-numeric:tabular-nums}
.wf-desc{grid-column:1/-1;color:var(--text-2);font-size:12px;margin:-2px 0 6px 0}
@media (max-width:820px){.wf-row{grid-template-columns:1fr 70px 52px}.wf-bar{display:none}}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;
     background:color-mix(in srgb,var(--text-2) 14%,transparent);color:var(--text-2)}
.tag.real{background:color-mix(in srgb,#c2410c 16%,transparent);color:#c2410c}
.tag.gap{background:color-mix(in srgb,#a16207 18%,transparent);color:#a16207}
.qbox{margin:12px 0 2px}
.qbox > summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;
  gap:7px;font-size:12px;color:var(--text-2);padding:3px 9px;border-radius:12px;
  background:color-mix(in srgb,var(--text-2) 10%,transparent)}
.qbox > summary::-webkit-details-marker{display:none}
.qbox > summary:hover{background:color-mix(in srgb,var(--text-2) 18%,transparent)}
.qbox-i{display:inline-flex;align-items:center;justify-content:center;width:15px;
  height:15px;border-radius:50%;border:1px solid currentColor;font-size:10px;
  font-weight:700;font-style:italic;line-height:1}
.qbox pre{margin:8px 0 0;padding:12px;border-radius:8px;overflow-x:auto;
  font-size:11.5px;line-height:1.45;white-space:pre;
  background:color-mix(in srgb,var(--text-2) 8%,transparent)}
.qbox h4{margin:12px 0 0;font-size:12px;color:var(--text-2);font-weight:600}
</style>
"""


def _pct(v, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{float(v) * 100:.{digits}f}%".replace(".", ",")


def _n(v, digits: int = 0) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
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
# «i»: как проверить цифру
# --------------------------------------------------------------------------- #
def params_comment(params: dict) -> str:
    """Значения параметров шапкой запроса.

    Без них запрос не воспроизводит цифру, и «проверка» превращается в чтение:
    читатель видит `:d_base`, но не знает, какая это дата.
    """
    if not params:
        return ""
    lines = ["-- Параметры:"]
    for key in sorted(params):
        val = params[key]
        if isinstance(val, (list, tuple)):
            val = ", ".join(str(v) for v in val)
        lines.append(f"--   :{key} = {val}")
    return "\n".join(lines) + "\n"


def sql_info(shown: dict, *names: str) -> str:
    """Значок «i» с запросами, которыми посчитан блок.

    Запросы берутся из ФАКТИЧЕСКОГО исполнения (`ws.shown`), а не собираются
    заново: вторая сборка молча разъедется с исполнением, и отчёт начнёт
    предъявлять запрос, которым цифра не считалась.

    Блок, собранный из нескольких запросов, показывает все — иначе значок обещает
    полную проверку, давая половину.
    """
    have = [(n, shown[n]) for n in names if n in (shown or {})]
    if not have:
        return ""
    parts = []
    for name, (text, params) in have:
        head = f"<h4>{C.esc(name)}</h4>" if len(have) > 1 else ""
        parts.append(f"{head}<pre>{C.esc(params_comment(params) + text.strip())}</pre>")
    label = ("Как проверить цифру" if len(have) == 1
             else f"Как проверить цифры: запросов {len(have)}")
    return (f'<details class="qbox"><summary><span class="qbox-i">i</span>'
            f'{C.esc(label)}</summary>{"".join(parts)}</details>')


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
    мелких как раз те, что отличают счёт от оттока.
    """
    if not rows:
        return '<div class="muted">раскладывать нечего</div>'
    mx = max(abs(float(r["value"])) for r in rows) or 1.0
    out = []
    for r in rows:
        v = float(r["value"])
        kind = r.get("kind", A.REAL)
        cls = {A.REAL: "tag real", A.GAP: "tag gap"}.get(kind, "tag")
        out.append(
            f'<div class="wf-row">'
            f'<div>{C.esc(r["name"])} <span class="{cls}">'
            f'{C.esc(KIND_TAG.get(kind, ""))}</span></div>'
            f'<div><div class="wf-bar" style="width:{abs(v) / mx * 100:.1f}%;'
            f'background:{KIND_COLOR.get(kind, "var(--accent)")}"></div></div>'
            f'<div class="wf-val">{_n(v)}</div>'
            f'<div class="wf-pct">{_pct(v / (total or 1), 0)}</div></div>')
        if r.get("descr"):
            out.append(f'<div class="wf-desc">{C.esc(r["descr"])}</div>')
    return f'<div class="wf">{"".join(out)}</div>'


def _ladder_rows(df: pd.DataFrame, value_col: str) -> list[dict]:
    return [{"name": r.title, "value": float(getattr(r, value_col)),
             "kind": r.kind, "descr": r.descr} for r in df.itertuples()]


# --------------------------------------------------------------------------- #
# Разделы
# --------------------------------------------------------------------------- #
def head_kpi(t: dict) -> str:
    """Первый экран: падение метрики, изменение по людям и чистое движение.

    Три разных числа рядом, потому что они отвечают на разные вопросы и
    расходятся: метрика может падать, когда людей столько же, — ради этого разбор
    и затеян.
    """
    d, de, nr = t["d_triples"], t["d_epk"], t["net_real"]
    return C.stat_row([
        {"value": _n(t["triples_cur"]),
         "caption": f"получателей в {t['report_month']:%m.%Y}",
         "sub": f"было {_n(t['triples_base'])} в {t['base_month']:%m.%Y}"},
        {"value": _signed(d), "caption": "изменение получателей год к году",
         "kind": "bad" if d < 0 else "good",
         "sub": _pct(d / (t["triples_base"] or 1))},
        {"value": _signed(de), "caption": "изменение по ЛЮДЯМ",
         "kind": "bad" if de < 0 else "good",
         "sub": f"{_n(t['epk_cur'])} человек, {_pct(de / (t['epk_base'] or 1))}"},
        {"value": _signed(nr), "caption": "чистое движение без счёта и перерывов",
         "kind": "bad" if nr < 0 else "good",
         "sub": "приход минус уход по настоящим причинам"},
    ])


def metric_block(t: dict, shown: dict) -> str:
    """Из чего сложено падение: людей меньше или получателей на человека меньше.

    Это первое, что надо развести. Метрика считает тройки (человек, организация,
    подразделение), поэтому падение доли совместителей уменьшает её, не тронув ни
    одного человека, — и без этой раскладки весь дальнейший разбор идёт не про то.
    """
    parts = [
        {"name": "Людей стало меньше", "value": t["d_by_people"], "kind": A.REAL,
         "descr": "вклад изменения числа людей при прежнем совместительстве"},
        {"name": "Совместительство схлопнулось", "value": t["d_by_multi"],
         "kind": A.METHOD,
         "descr": "вклад изменения среднего числа получателей на человека — "
                  "людей это не убавляет"},
    ]
    tbl = C.table(
        ["Показатель", f"{t['base_month']:%m.%Y}", f"{t['report_month']:%m.%Y}",
         "Изменение"],
        [["Получателей (человек × организация × подразделение)",
          _n(t["triples_base"]), _n(t["triples_cur"]), _signed(t["d_triples"])],
         ["Людей", _n(t["epk_base"]), _n(t["epk_cur"]), _signed(t["d_epk"])],
         ["Организаций", _n(t["inn_base"]), _n(t["inn_cur"]),
          _signed(t["inn_cur"] - t["inn_base"])],
         ["Получателей на человека", f"{t['multi_base']:.4f}".replace(".", ","),
          f"{t['multi_cur']:.4f}".replace(".", ","),
          f"{t['multi_cur'] - t['multi_base']:+.4f}".replace(".", ",")],
         ["Доля совместителей", _pct(t["multi_share_base"], 2),
          _pct(t["multi_share_cur"], 2),
          _pct(t["multi_share_cur"] - t["multi_share_base"], 2)]],
        num_cols=[1, 2, 3])
    return C.section(
        "Падение метрики: люди или счёт",
        C.card(waterfall(parts, t["d_triples"]) + tbl
               + _src("Получатель — тройка (человек, организация, подразделение) "
                      "с суммой зачислений в организацию за месяц выше порога. "
                      "Совместитель весит нескольких получателей.")
               + sql_info(shown, "month_totals")),
        eyebrow="с этого начинается разбор")


def net_block(t: dict, gains: pd.DataFrame, gains_epk: pd.DataFrame,
              shown: dict, text: str = "", fb: bool = False) -> str:
    """Выросли мы или нет, если не брать методологию счёта.

    Раздел существует потому, что без раскладки ПРИХОДА ответить на этот вопрос
    нельзя. Вычитать из полного прихода только настоящие потери — арифметика, не
    значащая ничего: она завышает рост ровно на ту величину, которую мы вычитаем
    со стороны потерь.
    """
    rows = [
        {"name": "Реальное движение", "value": t["net_real"], "kind": A.REAL,
         "descr": "пришли новые люди и организации минус ушедшие из банка и из "
                  "сегмента"},
        {"name": "Особенности счёта", "value": t["net_method"], "kind": A.METHOD,
         "descr": "порог, коды, переводы между подразделениями, совместительство "
                  "и переходы внутри сегмента — людей не прибавляют и не убавляют"},
        {"name": "Перерывы в выплатах", "value": t["net_gap"], "kind": A.GAP,
         "descr": "человека нет в отчётном месяце, но он был в предыдущие"},
    ]
    epk_rows = [
        ["Реальное движение", _signed(t["net_real"]), _signed(t["net_real_epk"])],
        ["Особенности счёта", _signed(t["net_method"]), _signed(t["net_method_epk"])],
        ["Перерывы", _signed(t["net_gap"]), _signed(t["net_gap_epk"])],
        ["Итого изменение", _signed(t["d_triples"]), _signed(t["d_epk"])],
    ]
    gain_tbl = ""
    if not gains.empty:
        rows_g = [[C.esc(r.title), _n(r.n_triples), _pct(r.share, 0),
                   C.esc(KIND_TAG.get(r.kind, ""))] for r in gains.itertuples()]
        gain_tbl = ("<h3>Из чего состоит приход</h3>"
                    + C.table(["Откуда", "Получателей", "Доля", "Вид"], rows_g,
                              num_cols=[1, 2]))
    return C.section(
        "Выросли или нет",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + waterfall(rows, t["d_triples"])
               + "<h3>То же самое по получателям и по людям</h3>"
               + C.table(["Вид движения", "Получателей", "Людей"], epk_rows,
                         num_cols=[1, 2])
               + gain_tbl
               + _src("Приход разложен теми же видами, что и потери. Иначе «рост» "
                      "завышается ровно на ту величину, которую мы вычитаем со "
                      "стороны потерь.")
               + sql_info(shown, "gained_totals", "gained_epk", "lost_totals",
                          "lost_epk")),
        eyebrow="главный ответ")


def causes_block(causes: pd.DataFrame, both: pd.DataFrame, t: dict,
                 shown: dict, text: str = "", fb: bool = False) -> str:
    """Лестница причин потерь и она же — по людям, рядом."""
    if causes.empty:
        return _empty("Из чего состоит потеря", "раскладка не посчиталась")
    side = ""
    if not both.empty:
        rows = [[C.esc(r.title), C.esc(KIND_TAG.get(r.kind, "")), _n(r.n_triples),
                 _n(r.n_epk) if r.epk_applies else "—"]
                for r in both.itertuples()]
        side = ("<h3>Получатели и люди рядом</h3>"
                + C.table(["Причина", "Вид", "Получателей", "Людей"], rows,
                          num_cols=[2, 3])
                + _note("Прочерк значит, что на уровне человека такой ветки нет "
                        "вовсе: перевод, совместительство и переход внутри "
                        "сегмента получателя убавляют, а человека — нет. Разница "
                        "между колонками и есть цена методологии счёта."))
    lk = t["lost_kinds"]
    summary = (
        f'<p>Потеряно <b>{_n(t["lost"])}</b> получателей, пришло '
        f'<b>{_n(t["gained"])}</b>. Из потерянных настоящей потерей является '
        f'<b>{_n(lk[A.REAL])}</b> ({_pct(lk[A.REAL] / (t["lost"] or 1), 0)}); '
        f'<b>{_n(lk[A.METHOD])}</b> — особенности счёта, '
        f'<b>{_n(lk[A.GAP])}</b> — перерывы в выплатах.</p>')
    return C.section(
        "Из чего состоит потеря",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + summary
               + waterfall(_ladder_rows(causes, "n_triples"), t["lost"])
               + side
               + _src("Каждый потерянный получатель получает ровно одну причину — "
                      "первую сработавшую по лестнице приоритетов, поэтому части "
                      "складываются в целое.")
               + sql_info(shown, "lost_totals", "lost_epk")),
        eyebrow="ответ на «сколько на самом деле»")


def when_block(tr: pd.DataFrame, st: pd.DataFrame, measured: str,
               cmp_months: pd.DataFrame, surv: pd.DataFrame, load: pd.DataFrame,
               t_prev: dict | None, causes_prev: pd.DataFrame, shown: dict,
               text: str = "", fb: bool = False) -> str:
    """Когда именно произошло падение — и не сезон ли это."""
    if tr.empty:
        return _empty("Когда это произошло", "помесячный ряд не построился")
    pts = [(f"{pd.Timestamp(r.report_dt):%m.%y}", float(r.n_triples))
           for r in tr.itertuples()]
    marks = ({f"{pd.Timestamp(r.report_dt):%m.%y}" for r in st.itertuples()}
             if not st.empty else set())

    # Сравнение с соседями — первым делом: если отчётный месяц сезонная яма, всё
    # остальное в разделе читается иначе.
    cmp_html = ""
    if not cmp_months.empty:
        rows = []
        for r in cmp_months.itertuples():
            rows.append([
                f"{pd.Timestamp(r.report_dt):%m.%Y}"
                + (" — отчётный" if r.is_report else ""),
                _n(r.n_triples), _n(r.n_epk),
                f"{r.multi:.4f}".replace(".", ",") if pd.notna(r.multi) else "—",
                "—" if r.is_report else _signed(-r.vs_report),
                "—" if r.is_report else _signed(-r.vs_report_epk)])
        cmp_html = (
            "<h3>Отчётный месяц против соседних и против года назад</h3>"
            + C.table(["Месяц", "Получателей", "Людей", "Получ. на человека",
                       "Получателей к отчётному", "Людей к отчётному"], rows,
                      num_cols=[1, 2, 3, 4, 5])
            + _note("Если отчётный месяц ниже соседних так же, как год назад, "
                    "годовое падение повторяет обычный сезонный провал, и "
                    "выводить из него тренд нельзя."))

    prev_html = ""
    if t_prev is not None and not causes_prev.empty:
        rows = [[C.esc(r.title), C.esc(KIND_TAG.get(r.kind, "")), _n(r.n_triples),
                 _pct(r.share, 0)] for r in causes_prev.itertuples()]
        prev_html = (
            f"<h3>Что произошло за один месяц: "
            f"{t_prev['base_month']:%m.%Y} → {t_prev['report_month']:%m.%Y}</h3>"
            + C.table(["Причина", "Вид", "Получателей", "Доля"], rows,
                      num_cols=[2, 3])
            + _src(f"Изменение за месяц {_signed(t_prev['d_triples'])} получателей "
                   f"и {_signed(t_prev['d_epk'])} людей. Та же лестница, другая "
                   f"база: видно, что из годового падения пришлось на последний "
                   f"месяц."))

    if not st.empty:
        rows = [[f"{pd.Timestamp(r.report_dt):%m.%Y}", _signed(r.delta),
                 f"{r.score:.1f}".replace(".", ",")] for r in st.itertuples()]
        step_html = (
            "<h3>Месяцы-обрывы</h3>"
            + C.table(["Месяц", f"Изменение ({measured})",
                       "Во сколько раз резче обычного"], rows, num_cols=[1, 2])
            + _note(f"Мерилось: {measured}. Обрыв в одном месяце — это событие: "
                    f"смена кодировки выплат, переклассификация или загрузка. "
                    f"Регулярный сезонный провал обрывом не является и здесь не "
                    f"показан."))
    else:
        step_html = _note(f"Ни одного месяца-обрыва не найдено ({measured}): "
                          f"изменение идёт плавно. Это текучесть, а не событие.")

    surv_html = ""
    if not surv.empty:
        last = surv.iloc[-1]
        spts = [(f"{pd.Timestamp(r.report_dt):%m.%y}", float(r.share))
                for r in surv.itertuples()]
        surv_html = (
            "<h3>Дожитие когорты базового месяца</h3>"
            + sparkline(spts, height=110,
                        caption=f"осталось {_pct(last['share'])} получателей "
                                f"базового месяца")
            + _src("Доля получателей базового месяца, доживших до каждого "
                   "следующего. Считается по тем же самым получателям, а не по "
                   "численности сегмента."))

    load_html = ""
    if not load.empty and bool(load["is_thin"].any()):
        thin = load[load["is_thin"]]
        load_html = _note(
            "Недогруженные месяцы по всему банку: "
            + ", ".join(f"{pd.Timestamp(r.report_dt):%m.%Y}" for r in thin.itertuples())
            + ". Изменение в этих точках объясняется загрузкой партиции, а не людьми.")

    return C.section(
        "Когда это произошло",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + cmp_html
               + sparkline(pts, caption="получателей по месяцам", marks=marks)
               + step_html + prev_html + surv_html + load_html
               + sql_info(shown, "monthly", "survival", "lost_totals_prev",
                          "monthly_all")),
        eyebrow="ответ на «когда»")


def why_block(thr: pd.DataFrame, split: pd.DataFrame, gone: pd.DataFrame,
              mig: pd.DataFrame, shown: dict, text: str = "",
              fb: bool = False) -> str:
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

    if not split.empty:
        rows = [[C.esc(r.title), _n(r.epk_base), _n(r.epk_cur), _signed(r.d_epk),
                 _pct(r.d_epk_pct), _pct(r.d_amt_pct)] for r in split.itertuples()]
        parts.append(
            "<h3>Зарплатные коды против всех остальных</h3>"
            + C.table(["Группа кодов", "Людей в базе", "Людей в отчёте",
                       "Изменение", "% по людям", "% по объёму"], rows,
                      num_cols=[1, 2, 3, 4, 5])
            + _note("Метрика считает ТОЛЬКО зарплатные коды. Что бы ни произошло "
                    "с остальными, на неё это не влияет по построению — смотреть "
                    "надо на первую строку."))

    if not gone.empty:
        rows = [[_n(r.code), C.esc(str(r.code_name or "")), _n(r.base),
                 "да" if r.in_list else "нет"] for r in gone.itertuples()]
        parts.append(
            "<h3>Коды, исчезнувшие из витрины целиком</h3>"
            + C.table(["Код", "Вид зачисления", "Людей было",
                       "Влияет на метрику?"], rows, num_cols=[0, 2])
            + _note("Справка о переменах в данных, а не объяснение падения. Код, "
                    "которого нет в списке зарплатных, метрику не задевает ни при "
                    "каком своём поведении."))

    if not mig.empty:
        rows = []
        for r in mig.head(12).itertuples():
            rows.append([
                C.esc(str(getattr(r, "name_from", None) or r.inn_from)),
                C.esc(str(getattr(r, "name_to", None) or r.inn_to)),
                _n(r.n_epk), _pct(r.share, 0),
                "да" if getattr(r, "to_in_segment", False) else "нет"])
        parts.append(
            "<h3>Похоже на переоформление, а не на отток</h3>"
            + C.table(["Откуда", "Куда", "Человек", "Доля потерь организации",
                       "Приёмник в сегменте"], rows, num_cols=[2, 3])
            + _note("Люди этих организаций дружно оказались в одном и том же "
                    "новом номере. Для банка они никуда не уходили. Если приёмник "
                    "ещё не размечен как бюджетный, метрика теряет их дважды — и "
                    "как отток, и как непопадание в сегмент."))

    if not parts:
        return _empty("Почему это произошло",
                      "ни одна из проверок причины не дала результата")
    return C.section(
        "Почему это произошло",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + "".join(parts)
               + sql_info(shown, "threshold_sens", "code_split", "code_mix",
                          "inn_migration")),
        eyebrow="ответ на «почему»")


def where_block(cuts: dict, orgs: pd.DataFrame, tenure: pd.DataFrame,
                meta: dict, shown: dict, text: str = "",
                fb: bool = False) -> str:
    """Разрезы потерь. В каждом — не только объём, но и состав по видам."""
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
                C.esc(str(getattr(r, dim))), _n(r.n_triples), _pct(r.share, 0),
                _n(r.n_inn), _n(getattr(r, A.REAL)), _n(getattr(r, A.METHOD)),
                _n(getattr(r, A.GAP))])
        parts.append(f"<h3>{C.esc(titles.get(dim, dim))}</h3>"
                     + C.table([titles.get(dim, dim), "Потеряно", "Доля потерь",
                                "Организаций", "Реальная потеря", "Счёт",
                                "Перерыв"], rows, num_cols=[1, 2, 3, 4, 5, 6]))

    if not tenure.empty:
        head = list(tenure.columns)
        rows = [[C.esc(str(row[0]))]
                + [_pct(v, 0) if head[i + 1] == "Доля" else _n(v)
                   for i, v in enumerate(row[1:])]
                for row in tenure.itertuples(index=False)]
        parts.append(
            "<h3>Стаж ушедших: сколько месяцев были в сегменте</h3>"
            + C.table(["Месяцев в сегменте"] + [str(c) for c in head[1:]], rows,
                      num_cols=list(range(1, len(head))))
            + _note("Уходят недавно пришедшие — это ротация и сезонники. Уходят "
                    "старожилы — это потеря ядра. По числу «ушло N человек» эти "
                    "два случая одинаковы, а решения по ним разные."))

    if not orgs.empty:
        rows = [[C.esc(str(r.company_name or r.inn)), _signed(r.net), _n(r.lost),
                 _n(r.gained), C.esc(str(r.cause_title or "")),
                 C.esc(str(r.agency or "")), C.esc(str(r.tb_short_name or ""))]
                for r in orgs.itertuples()]
        parts.append(
            "<h3>Организации с наибольшим чистым изменением</h3>"
            + C.table(["Организация", "Нетто", "Потеряно", "Пришло",
                       "Преобладающая причина потерь", "Ведомство", "ТБ"], rows,
                      num_cols=[1, 2, 3])
            + _note("Сортировка по НЕТТО. Организация, потерявшая двести тысяч "
                    "получателей и набравшая столько же, по одним потерям "
                    "выглядела бы катастрофой, не потеряв ничего."))

    if not parts:
        return _empty("Где это произошло", "разрезы не построились")

    cov = _src(
        f"Ведомство и уровень подчинения выводятся из наименования: отдельных "
        f"полей для них нет ни в одном разрешённом источнике. Имя известно у "
        f"{_pct(meta.get('named_share', 0), 0)} организаций; неразобранные имена "
        f"показаны отдельной строкой и по группам не раскидываются."
        + ("" if meta.get("gosb_key") else
           " Подразделения ведомостей со справочником не сошлись — разрез по "
           "регионам не строился, территория показана по номеру ТБ из самих "
           "ведомостей."))
    return C.section(
        "Где это произошло",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + "".join(parts) + cov
               + sql_info(shown, "lost_by_inn", "gained_by_inn", "tenure")),
        eyebrow="ответ на «где»")


def limits_block(warnings: list[str], checks: list[dict], probe: dict,
                 meta: dict) -> str:
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
        "<li>Ветка «перерыв» не доказывает, что человек вернётся: она означает "
        "только то, что в отчётном месяце его нет, а в предыдущие он был. "
        "Подтвердить возврат можно лишь следующим месяцем, которого ещё нет.</li>",
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
    if not meta.get("gosb_key"):
        m = probe.get("gosb_match") or {}
        items.append(
            f"<li>Подразделения ведомостей не опознаются справочником "
            f"(по old_gosb_id {m.get('n_old', 0)} из {m.get('n_used', 0)}, по "
            f"new_gosb_id {m.get('n_new', 0)}) — разрез по регионам не строился. "
            f"Территория показана по номеру ТБ из самих ведомостей.</li>")
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
