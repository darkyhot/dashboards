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

# Цвета видов. Потеря и приход — тёплые тона, переход внутри сегмента — серый:
# читатель обязан отличать «получателя не стало» от «получатель остался в сегменте» до того, как начнёт читать числа.
KIND_COLOR = {A.LOSS: "#c2410c", A.INSIDE: "#94a3b8"}


def _tag(side: str, kind: str) -> str:
    """Подпись вида. Зависит от стороны: «остался в сегменте» на потерях и
    «перешёл внутри сегмента» на приходе — про одно и то же, но читаются иначе."""
    return A.KIND_TAG.get((side, kind), "")


def _tag_html(side: str, kind: str) -> str:
    cls = "tag real" if kind == A.LOSS else "tag"
    return f'<span class="{cls}">{C.esc(_tag(side, kind))}</span>'


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


def waterfall(rows: list[dict], total: float, side: str = "lost") -> str:
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
        kind = r.get("kind", A.LOSS)
        out.append(
            f'<div class="wf-row">'
            f'<div>{C.esc(r["name"])} {_tag_html(side, kind)}</div>'
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
    """Первый экран: реальные потери, реальный приход и что вышло в итоге.

    Четыре числа рядом, потому что они отвечают на разные вопросы и расходятся:
    метрика может падать, когда людей столько же, — ради этого разбор и затеян.
    """
    net, de = t["net_real"], t["d_epk"]
    # Общий `stat_row` удалён из компонентов рефакторингом отрисовки (679b22e).
    # Ряд собирается из уцелевших `kpi` в сетке темы: подпись числа уходит в
    # `delta`, её цвет — в `delta_kind`, так что смысл карточек не меняется.
    cards = [
        C.kpi("реально потеряно получателей", C.esc(_n(t["real_lost"])),
              f"{_n(t['real_lost_epk'])} человек", "bad"),
        C.kpi("реально пришло получателей", C.esc(_n(t["real_gained"])),
              f"{_n(t['real_gained_epk'])} человек", "good"),
        C.kpi("чистое изменение по получателям", C.esc(_signed(net)),
              f"из {_n(t['triples_base'])} в {t['base_month']:%m.%Y}",
              "bad" if net < 0 else "good"),
        C.kpi("чистое изменение по ЛЮДЯМ", C.esc(_signed(de)),
              f"{_n(t['epk_cur'])} человек, {_pct(de / (t['epk_base'] or 1))}",
              "bad" if de < 0 else "good"),
    ]
    return f'<div class="grid cols-4">{"".join(cards)}</div>'


def metric_block(t: dict, thr: pd.DataFrame, shown: dict) -> str:
    """Метрика падает из-за людей или из-за того, чем она их считает.

    Это первое, что надо развести. Метрика считает тройки (человек, организация,
    подразделение), поэтому падение доли совместителей уменьшает её, не тронув ни
    одного человека, — и без этой раскладки весь дальнейший разбор идёт не про то.

    Рядом — проверка порога. Порог фиксирован, а зарплаты индексируются: сам по
    себе он должен год к году ДОБАВЛЯТЬ получателей. Если падение сохраняется при
    пороге 0, порог ни при чём, и обсуждать его больше не нужно.
    """
    parts = [
        {"name": "Людей стало меньше", "value": t["d_by_people"], "kind": A.LOSS,
         "descr": "вклад изменения числа людей при прежнем совместительстве"},
        {"name": "Совместительство схлопнулось", "value": t["d_by_multi"],
         "kind": A.INSIDE,
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
    thr_html = ""
    if not thr.empty:
        rows = [[_n(r.threshold) + " ₽", _n(r.base), _n(r.cur), _signed(r.delta),
                 _pct(r.delta_pct)] for r in thr.itertuples()]
        thr_html = ("<h3>А не в пороге ли дело</h3>"
                    + C.table(["Порог", f"{t['base_month']:%m.%Y}",
                               f"{t['report_month']:%m.%Y}", "Изменение", "%"],
                              rows, num_cols=[1, 2, 3, 4])
                    + _src("Порог фиксирован, а зарплаты индексируются — сам по "
                           "себе он должен год к году добавлять получателей. Если "
                           "падение сохраняется при пороге 0, порог ни при чём."))
    return C.section(
        "Метрика: люди или то, чем их считают",
        C.card(waterfall(parts, t["d_triples"]) + tbl + thr_html
               + _src("Получатель — тройка (человек, организация, подразделение) "
                      "с суммой зачислений в организацию за месяц выше порога. "
                      "Совместитель весит нескольких получателей.")
               + sql_info(shown, "month_totals", "threshold_sens")),
        eyebrow="с этого начинается разбор")


def net_block(t: dict, causes: pd.DataFrame, gains: pd.DataFrame,
              causes_epk: pd.DataFrame, gains_epk: pd.DataFrame, shown: dict,
              text: str = "", fb: bool = False) -> str:
    """Реальные потери и реальный приход — главный ответ отчёта.

    Раздел существует потому, что без раскладки ПРИХОДА ответить «выросли или нет»
    нельзя. Вычитать из полного прихода только настоящие потери — арифметика, не
    значащая ничего: она завышает рост ровно на ту величину, которую мы вычитаем
    со стороны потерь.
    """
    lk, gk = t["lost_kinds"], t["gained_kinds"]
    summary = C.table(
        ["", "Получателей", "Людей"],
        [["Реально потеряно", _n(t["real_lost"]), _n(t["real_lost_epk"])],
         ["Реально пришло", _n(t["real_gained"]), _n(t["real_gained_epk"])],
         ["Чистое изменение", _signed(t["net_real"]), _signed(t["net_real_epk"])],
         ["Остались получателями сегмента (потери нет)",
          f'−{_n(lk[A.INSIDE])} / +{_n(gk[A.INSIDE])}', "—"],
         ["Итого изменение метрики", _signed(t["d_triples"]), _signed(t["d_epk"])]],
        num_cols=[1, 2])

    lost_html = ""
    if not causes.empty:
        lost_html = ("<h3>Из чего состоит потеря</h3>"
                     + waterfall(_ladder_rows(causes, "n_triples"), t["lost"],
                                 side="lost"))
    gain_html = ""
    if not gains.empty:
        gain_html = ("<h3>Из чего состоит приход</h3>"
                     + waterfall(_ladder_rows(gains, "n_triples"), t["gained"],
                                 side="gained"))
    return C.section(
        "Реальные потери и реальный приход",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + summary + lost_html + gain_html
               + _src("Получатель потерян, если он больше не получает зарплату по "
                      "заданным кодам выше порога — неважно, ушёл он из банка, в "
                      "другой сегмент или перешёл на другие коды. Если он остался "
                      "получателем бюджетного сегмента, потери нет: как именно он "
                      "внутри переместился, для сегмента неважно.")
               + sql_info(shown, "lost_totals", "gained_totals", "lost_epk",
                          "gained_epk")),
        eyebrow="главный ответ")


def both_block(both: pd.DataFrame, t: dict, shown: dict) -> str:
    """Потери по получателям и по людям рядом — цена того, что считаем не людей."""
    if both.empty:
        return ""
    rows = [[C.esc(r.title), C.esc(_tag("lost", r.kind)), _n(r.n_triples),
             _n(r.n_epk) if r.epk_applies else "—"] for r in both.itertuples()]
    return C.section(
        "Получатели и люди рядом",
        C.card(C.table(["Причина", "Вид", "Получателей", "Людей"], rows,
                       num_cols=[2, 3])
               + _note("Прочерк значит, что на уровне человека такой ветки нет "
                       "вовсе: переход внутри сегмента получателя убавляет, а "
                       "человека — нет. Разница "
                       f"между колонками ({_n(t['lost'])} получателей против "
                       f"{_n(t['lost_epk'])} людей) и есть цена того, что метрика "
                       f"считает не людей.")
               + sql_info(shown, "lost_totals", "lost_epk")),
        eyebrow="цена счёта")


def where_gone_block(to_seg: pd.DataFrame, to_codes: pd.DataFrame,
                     to_codes_inn: pd.DataFrame,
                     mig: pd.DataFrame, tenure: pd.DataFrame, shown: dict,
                     text: str = "", fb: bool = False) -> str:
    """Куда именно делись люди: сегмент, вид выплат, переоформление, стаж.

    Строка «ушёл» без адреса — половина ответа. Забрал ли человека коммерческий
    клиент, вышел ли он на пенсию, или организацию просто переоформили — разные
    истории, и решения по ним разные вплоть до противоположных.
    """
    parts = []
    if not to_seg.empty:
        rows = [[C.esc(str(r.segment_name)), _n(r.n_epk), _pct(r.share, 0)]
                for r in to_seg.itertuples()]
        parts.append("<h3>В какой сегмент ушли</h3>"
                     + C.table(["Сегмент", "Человек", "Доля"], rows,
                               num_cols=[1, 2]))
    if not to_codes.empty:
        rows = [[_n(r.code), C.esc(str(r.code_name or "")), _n(r.n_epk),
                 _pct(r.share, 0)] for r in to_codes.itertuples()]
        parts.append(
            "<h3>Чем заменились зарплатные зачисления</h3>"
            + C.table(["Код", "Вид зачисления", "Человек", "Доля"], rows,
                      num_cols=[0, 2, 3])
            + _note("Это те, кто продолжает получать от бюджетной организации, но "
                    "не по зарплатным кодам. Получателями они быть перестали — "
                    "это уже посчитано потерей. Таблица говорит, что с ними "
                    "случилось: пенсия означает выход на пенсию, пособие на "
                    "детей — декрет, расчёт при увольнении — увольнение."))
    if to_codes_inn is not None and not to_codes_inn.empty:
        rows = [[C.esc(str(r.code_name or r.code)),
                 C.esc(str(getattr(r, "company_name", None) or r.inn)),
                 # ИНН — идентификатор, а не число: разряды пробелами здесь
                 # мешают его сверить и скопировать.
                 C.esc("" if pd.isna(r.inn) else str(int(r.inn))),
                 _n(r.n_epk), _pct(r.share, 0)]
                for r in to_codes_inn.itertuples()]
        parts.append(
            "<h3>Кто именно перешёл на каждый вид зачисления</h3>"
            # Колонка названа «Номер», а не тремя буквами, которых в готовом
            # HTML всё равно не останется: `html.sanitize` вычищает это слово
            # как отдельное (оно блокирует пересылку отчёта), и заголовок
            # превратился бы в «Орг.» рядом с колонкой «Организация».
            + C.table(["Вид зачисления", "Организация", "Номер", "Человек",
                       "Доля вида"], rows, num_cols=[3, 4])
            + _note("Доля считается ВНУТРИ вида зачисления: вопрос здесь — какую "
                    "часть перешедших на пенсию или на пособие даёт одна "
                    "организация. Если верх занимает одна-две — это адрес, куда "
                    "идти; если список ровный — это фон по всему сегменту, и "
                    "идти некуда."))
    if not mig.empty:
        rows = []
        for r in mig.head(12).itertuples():
            rows.append([
                C.esc(str(getattr(r, "name_from", None) or r.inn_from)),
                C.esc(str(getattr(r, "name_to", None) or r.inn_to)),
                _n(r.n_epk), _pct(r.share, 0),
                "да" if getattr(r, "to_in_segment", False) else "нет"])
        parts.append(
            "<h3>Похоже на переоформление, а не на уход</h3>"
            + C.table(["Откуда", "Куда", "Человек", "Доля потерь организации",
                       "Приёмник в сегменте"], rows, num_cols=[2, 3])
            + _note("Люди этих организаций дружно оказались в одном и том же "
                    "новом номере. Для банка они никуда не уходили. Если приёмник "
                    "ещё не размечен как бюджетный, метрика теряет их дважды."))
    if not tenure.empty:
        head = list(tenure.columns)
        rows = [[C.esc(str(row[0]))]
                + [_pct(v, 0) if head[i + 1] == "Доля" else _n(v)
                   for i, v in enumerate(row[1:])]
                for row in tenure.itertuples(index=False)]
        parts.append(
            "<h3>Сколько месяцев ушедшие были в сегменте</h3>"
            + C.table(["Месяцев в сегменте"] + [str(c) for c in head[1:]], rows,
                      num_cols=list(range(1, len(head))))
            + _note("Уходят недавно пришедшие — это ротация. Уходят старожилы — "
                    "это потеря ядра. По числу «ушло N человек» эти два случая "
                    "одинаковы, а решения по ним разные."))
    if not parts:
        return _empty("Куда делись люди", "ни один разрез не дал результата")
    return C.section(
        "Куда делись люди",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + "".join(parts)
               + sql_info(shown, "left_segment", "left_codes", "inn_migration",
                          "tenure")),
        eyebrow="ответ на «куда»")


def when_block(tr: pd.DataFrame, st: pd.DataFrame, measured: str,
               cmp_months: pd.DataFrame, surv: pd.DataFrame, load: pd.DataFrame,
               seas: dict, codes: pd.DataFrame, t_prev: dict | None,
               causes_prev: pd.DataFrame, shown: dict, text: str = "",
               fb: bool = False) -> str:
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

    # Сезонность — сразу после сравнения месяцев: она объясняет провал отчётного
    # месяца, и без неё этот провал читается как потеря.
    seas_html = ""
    if seas and seas.get("n_prev_both"):
        seas_html = (
            "<h3>Сезонность: повторяется ли провал год к году</h3>"
            + C.table(
                ["Показатель", "Человек"],
                [[f"Получали зарплату в {seas['prev_month']:%m.%Y} и в "
                  f"{seas['prev_month'] - pd.DateOffset(months=12):%m.%Y}",
                  _n(seas["n_prev_both"])],
                 [f"Из них пропали в {seas['report_month']:%m.%Y}",
                  _n(seas["n_gone_cur"])],
                 [f"Из них пропадали и в "
                  f"{seas['report_month'] - pd.DateOffset(months=12):%m.%Y} — "
                  f"это сезонность", _n(seas["n_seasonal"])]],
                num_cols=[1])
            + _note(
                f"Сезонными считаются только те, у кого провал ПОВТОРИЛСЯ: "
                f"получал в предыдущем месяце оба года и не получал в отчётном оба "
                f"года. Таких {_pct(seas['share_of_gone'], 0)} от всех пропавших. "
                f"Просто «был в прошлом месяце, нет сейчас» сезонностью не "
                f"является: доказать, что человек вернётся, нечем — следующего "
                f"месяца в данных нет. Эти люди не участвуют в сравнении год к "
                f"году вовсе, но именно они объясняют, почему отчётный месяц ниже "
                f"предыдущего."))

    # Какой ВИД ВЫПЛАТЫ просел — сразу после сезонности: чаще всего именно он и
    # объясняет провал отчётного месяца к предыдущему.
    codes_html = ""
    if not codes.empty:
        rows = [[C.esc(str(r.name)), _n(r.base), _n(r.prev), _n(r.cur),
                 _signed(r.d_month), _pct(r.d_month_pct),
                 _signed(r.d_year), _pct(r.d_year_pct)]
                for r in codes.itertuples()]
        codes_html = (
            "<h3>Какой вид выплаты просел</h3>"
            + C.table(["Вид зачисления", "Год назад", "Пред. месяц", "Отчётный",
                       "К пред. месяцу", "%", "Год к году", "%"], rows,
                      num_cols=[1, 2, 3, 4, 5, 6, 7])
            + _note("Только зарплатные коды — те, что входят в метрику. Метрика "
                    "складывается из них, и провал одного вида выплаты (стипендия "
                    "в каникулы, премия в конце квартала) выглядит падением "
                    "численности, хотя человек никуда не ушёл: у него просто нет "
                    "выплаты этого вида в этом месяце. Сортировка по изменению к "
                    "предыдущему месяцу."))

    prev_html = ""
    if t_prev is not None and not causes_prev.empty:
        rows = [[C.esc(r.title), C.esc(_tag("lost", r.kind)), _n(r.n_triples),
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
               + seas_html + codes_html + step_html + prev_html
               + surv_html + load_html
               + sql_info(shown, "monthly", "seasonal", "code_months",
                          "survival", "lost_totals_prev", "monthly_all")),
        eyebrow="ответ на «когда»")


def where_block(cuts: dict, orgs: pd.DataFrame, meta: dict, shown: dict,
                text: str = "", fb: bool = False) -> str:
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
                _n(r.n_inn), _n(getattr(r, A.LOSS)), _n(getattr(r, A.INSIDE))])
        parts.append(f"<h3>{C.esc(titles.get(dim, dim))}</h3>"
                     + C.table([titles.get(dim, dim), "Потеряно", "Доля потерь",
                                "Организаций", "Реальная потеря",
                                "Остались в сегменте"], rows,
                               num_cols=[1, 2, 3, 4, 5]))

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
               + sql_info(shown, "lost_by_inn", "gained_by_inn")),
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
        "<li>Сезонность подтверждается только повтором год к году. Про человека, "
        "пропавшего впервые, отчёт не говорит, что он вернётся: доказать это "
        "нечем — следующего месяца в данных нет, и такой человек считается "
        "потерянным.</li>",
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
            f"new_gosb_id {m.get('n_new', 0)}; старый номер gosb_id — "
            f"{m.get('n_old_legacy', 0)} и {m.get('n_new_legacy', 0)} из "
            f"{m.get('n_used_legacy', 0)}) — разрез по регионам не строился. "
            f"Территория показана по номеру ТБ из самих ведомостей.</li>")
    tbm = probe.get("tb_match") or {}
    if tbm and tbm.get("n_used") and not tbm.get("n_matched"):
        items.append(
            f"<li>Номера ТБ ведомостей не опознаются справочником "
            f"({tbm['n_matched']} из {tbm['n_used']}) — территориальный разрез "
            f"состоит из заглушки и подразделением не является.</li>")
    elif tbm.get("n_rows") and tbm.get("n_null_rows"):
        share = tbm["n_null_rows"] / tbm["n_rows"]
        if share > 0.01:
            items.append(
                f"<li>У {_pct(share, 0)} строк рабочего набора номера ТБ нет — "
                f"эти потери попадают в строку «ТБ неизвестен».</li>")
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
