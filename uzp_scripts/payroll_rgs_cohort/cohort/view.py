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

# Цвета видов. Перестал/начал получать — тёплый тон, «продолжает получать в РГС»
# — серый: это различие читатель должен видеть до того, как начнёт читать числа.
KIND_COLOR = {A.LOSS: "#c2410c", A.INSIDE: "#94a3b8"}


def _tag(side: str, kind: str) -> str:
    """Подпись вида. Зависит от стороны: одна и та же ситуация на стороне ушедших
    и новых строк читается по-разному."""
    return A.KIND_TAG.get((side, kind), "")


def _tag_html(side: str, kind: str) -> str:
    # Метка нужна только строке, где ФЛ продолжает получать зарплату в РГС: у
    # остальных заголовок раздела уже говорит, что они перестали или начали.
    if kind == A.LOSS:
        return ""
    return f'<span class="tag">{C.esc(_tag(side, kind))}</span>'


EXTRA_CSS = """
<style>
.tw{overflow-x:auto;max-width:100%}
.tw table{min-width:100%;font-size:13.5px}
.tw th,.tw td{padding:9px 10px}
.tw th{vertical-align:bottom}
.card{min-width:0}
.sorter > input{position:absolute;opacity:0;pointer-events:none}
.sorter > label{display:inline-block;cursor:pointer;font-size:12.5px;padding:4px 11px;
  margin:0 6px 8px 0;border-radius:12px;color:var(--text-2);
  background:color-mix(in srgb,var(--text-2) 10%,transparent)}
.sorter > input:checked + label{color:#fff;background:var(--accent)}
.sorter > input:focus-visible + label{outline:2px solid var(--accent)}
.sorter .sv{display:none}
.sorter .r-loss:checked ~ .sv-loss,.sorter .r-delta:checked ~ .sv-delta{display:block}
.lc-wrap{margin:10px 0 14px;overflow-x:auto}
.lc-grid{stroke:color-mix(in srgb,var(--text-2) 22%,transparent);stroke-width:1}
.lc-t{font-size:12px;fill:var(--text-2)}
.lc-hl{fill:var(--accent);font-weight:700}
.lc-line{fill:none;stroke:var(--accent);stroke-width:2}
.lc-area{fill:color-mix(in srgb,var(--accent) 10%,transparent)}
.lc-dot{fill:var(--accent)}
.lc-dot-hl{fill:#1d4ed8;background:#1d4ed8}
.lc-dot-mk{fill:#c2410c;background:#c2410c}
.lc-legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:4px}
.lc-k{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
.orgs{margin:14px 0 2px}
.orgs > summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;
  gap:7px;font-size:13px;font-weight:600;padding:4px 11px;border-radius:12px;
  background:color-mix(in srgb,var(--accent) 12%,transparent)}
.orgs > summary::-webkit-details-marker{display:none}
.orgs > summary::before{content:"+";font-weight:700}
.orgs[open] > summary::before{content:"−"}
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


def _disclosure(label: str, inner: str) -> str:
    """Длинный список организаций — под «+», закрытым по умолчанию."""
    return (f'<details class="orgs"><summary>{C.esc(label)}</summary>{inner}'
            f'</details>')


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
def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    """3–5 «круглых» делений оси, покрывающих [lo, hi]."""
    import math
    span = hi - lo
    if span <= 0:
        span = abs(hi) or 1.0
        lo, hi = lo - span * 0.05, hi + span * 0.05
        span = hi - lo
    raw = span / max(n - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step) * step
    ticks, v = [], first
    while v <= hi + step * 1e-9:
        ticks.append(v)
        v += step
    if ticks[-1] < hi:
        ticks.append(ticks[-1] + step)
    return ticks


def _axis_label(v: float, as_pct: bool) -> str:
    if as_pct:
        return _pct(v, 0)
    av = abs(v)
    if av >= 1e6:
        return f"{v / 1e6:.2f} млн".replace(".", ",")
    if av >= 1e4:
        return f"{v / 1e3:.0f} тыс"
    return _n(v)


def line_chart(points: list[tuple[pd.Timestamp, float]], caption: str = "",
               marks: set | None = None, highlight: dict | None = None,
               as_pct: bool = False, height: int = 240) -> str:
    """Линейный график с подписью месяцев по оси X и делениями по оси Y.

    Раньше ряд рисовался растянутой полоской с подписью только крайних дат, и
    понять, в каком месяце провал, было нельзя. Здесь `viewBox` в пикселях без
    растяжения (текст не искажается), каждый месяц подписан (при длинном ряде —
    через один, но базовый и отчётный — всегда), у каждой точки подсказка
    «08.2026 — 11 196 382».

    `marks` — месяцы-обрывы, `highlight` — {месяц: подпись} (базовый, отчётный).
    """
    if len(points) < 2:
        return '<div class="muted">ряда нет — рисовать нечего</div>'
    marks = {pd.Timestamp(m) for m in (marks or set())}
    highlight = {pd.Timestamp(k): v for k, v in (highlight or {}).items()}
    months = [pd.Timestamp(m) for m, _ in points]
    vals = [float(v) for _, v in points]
    ticks = _nice_ticks(min(vals), max(vals))
    lo, hi = ticks[0], ticks[-1]
    span = (hi - lo) or 1.0
    width, left, right, top, bottom = 900, 78, 16, 16, 58
    ph, pw = height - top - bottom, width - left - right
    step = pw / (len(vals) - 1)

    def y(v: float) -> float:
        return top + ph - (v - lo) / span * ph

    xy = [(left + i * step, y(v)) for i, v in enumerate(vals)]
    out = [f'<svg class="lc" viewBox="0 0 {width} {height}" role="img" '
           f'style="width:100%;height:auto;display:block">']
    for tv in ticks:
        ty = y(tv)
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{ty:.1f}" '
                   f'y2="{ty:.1f}" class="lc-grid"/>'
                   f'<text x="{left - 8}" y="{ty + 4:.1f}" text-anchor="end" '
                   f'class="lc-t">{C.esc(_axis_label(tv, as_pct))}</text>')
    every = 1 if len(vals) <= 14 else 2
    for i, (m, (x, _)) in enumerate(zip(months, xy)):
        if i % every and m not in highlight and m not in marks and i != len(vals) - 1:
            continue
        cls = "lc-t lc-hl" if m in highlight else "lc-t"
        out.append(f'<text x="{x:.1f}" y="{top + ph + 16}" text-anchor="end" '
                   f'transform="rotate(-45 {x:.1f} {top + ph + 16})" '
                   f'class="{cls}">{m:%m.%Y}</text>')
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{yy:.1f}"
                    for i, (x, yy) in enumerate(xy))
    out.append(f'<path d="{path} L{xy[-1][0]:.1f},{top + ph} L{left},{top + ph} Z" '
               f'class="lc-area"/><path d="{path}" class="lc-line"/>')
    for m, v, (x, yy) in zip(months, vals, xy):
        tip = f"{m:%m.%Y} — {_pct(v) if as_pct else _n(v)}"
        if m in highlight:
            tip += f" ({highlight[m]})"
            cls, r = "lc-dot lc-dot-hl", 5
        elif m in marks:
            tip += " (обрыв)"
            cls, r = "lc-dot lc-dot-mk", 5
        else:
            cls, r = "lc-dot", 3
        out.append(f'<circle cx="{x:.1f}" cy="{yy:.1f}" r="{r}" class="{cls}">'
                   f'<title>{C.esc(tip)}</title></circle>')
    out.append("</svg>")
    legend = []
    if highlight:
        legend.append('<span><i class="lc-k lc-dot-hl"></i>'
                      + C.esc(", ".join(f"{k:%m.%Y} — {t}" for k, t in highlight.items()))
                      + "</span>")
    if marks:
        legend.append('<span><i class="lc-k lc-dot-mk"></i>месяц-обрыв</span>')
    cap = C.esc(caption) + (" · " if caption and legend else "")
    return ('<div class="lc-wrap">' + "".join(out)
            + f'<div class="muted lc-legend">{cap}{"".join(legend)}</div></div>')


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
def _decomp_table(t: dict) -> str:
    """Разложение «было − перестали + начали ± совместители = стало»."""
    rows = []
    for r in A.decomposition(t):
        strong = r["key"] in ("base", "cur", "delta")
        fmt = _signed if r["key"] in ("lost", "gained", "inside", "delta") else _n
        cells = [C.esc(r["title"]),
                 fmt(r["triples"]), fmt(r["epk"])]
        if strong:
            cells = [f"<b>{x}</b>" for x in cells]
        rows.append(cells)
    return C.table(["", "Получателей", "ФЛ"], rows, num_cols=[1, 2])


def _definitions() -> str:
    return _src(f"{A.DEF_GETS} {A.DEF_ROW}")


def head_kpi(t: dict) -> str:
    """Первый экран: было, стало, изменение — и из чего оно сложилось.

    Одно разложение на весь отчёт (`A.decomposition`). Карточки «чистого
    изменения» больше нет: это была неполная сумма без третьего слагаемого, и она
    не сходилась с таблицей под ней.
    """
    d = t["d_triples"]
    cards = [
        C.kpi(f"Получателей в {t['base_month']:%m.%Y}", C.esc(_n(t["triples_base"])),
              f"{_n(t['epk_base'])} ФЛ"),
        C.kpi(f"Получателей в {t['report_month']:%m.%Y}", C.esc(_n(t["triples_cur"])),
              f"{_n(t['epk_cur'])} ФЛ"),
        C.kpi("Изменение получателей", C.esc(_signed(d)),
              f"{_pct(d / (t['triples_base'] or 1))}; ФЛ {_signed(t['d_epk'])}",
              "bad" if d < 0 else "good"),
    ]
    return (f'<div class="grid cols-3">{"".join(cards)}</div>'
            + C.card("<h3>Из чего сложилось изменение</h3>" + _decomp_table(t)
                     + _definitions()))


def metric_block(t: dict, thr: pd.DataFrame, shown: dict) -> str:
    """Показатели месяца и проверка порога.

    Разложение изменения здесь НЕ повторяется другой формулой: оно одно и стоит в
    шапке. Доля совместителей оставлена справкой — это то самое «7% → 5%».
    """
    tbl = C.table(
        ["Показатель", f"{t['base_month']:%m.%Y}", f"{t['report_month']:%m.%Y}",
         "Изменение"],
        [["Получателей (ФЛ × организация × ГОСБ)",
          _n(t["triples_base"]), _n(t["triples_cur"]), _signed(t["d_triples"])],
         ["ФЛ", _n(t["epk_base"]), _n(t["epk_cur"]), _signed(t["d_epk"])],
         ["Организаций", _n(t["inn_base"]), _n(t["inn_cur"]),
          _signed(t["inn_cur"] - t["inn_base"])],
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
        "Показатели месяца",
        C.card(tbl
               + _src("Доля совместителей — (получателей − ФЛ) / ФЛ: на сколько "
                      "получателей больше, чем ФЛ, из-за работы в нескольких "
                      "организациях. Её снижение и есть «" + A.T_INSIDE
                      + "» в разложении выше.")
               + thr_html
               + sql_info(shown, "month_totals", "threshold_sens")),
        eyebrow="справка")


def net_block(t: dict, causes: pd.DataFrame, gains: pd.DataFrame,
              causes_epk: pd.DataFrame, gains_epk: pd.DataFrame, shown: dict,
              text: str = "", fb: bool = False) -> str:
    """Кто перестал и кто начал получать зарплату в РГС — главный ответ отчёта.

    Обе стороны раскладываются по одним и тем же ситуациям: без раскладки новых
    строк ответить «выросли или нет» нельзя.
    """
    lost_html = ""
    if not causes_epk.empty:
        lost_html = (f"<h3>{A.T_LOST}: почему (ФЛ)</h3>"
                     + waterfall(_ladder_rows(causes_epk, "n_epk"),
                                 t["real_lost_epk"], side="lost"))
    gain_html = ""
    if not gains_epk.empty:
        gain_html = (f"<h3>{A.T_GAINED}: откуда (ФЛ)</h3>"
                     + waterfall(_ladder_rows(gains_epk, "n_epk"),
                                 t["real_gained_epk"], side="gained"))
    return C.section(
        "Кто перестал и кто начал получать зарплату в РГС",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + lost_html + gain_html
               + _note("ФЛ перестало получать зарплату в РГС, только если в "
                       "отчётном месяце у него нет ни одной бюджетной организации "
                       "с зарплатой больше 2 500 ₽. Сменило школу, ГОСБ или "
                       "осталось в одной организации из двух — продолжает получать "
                       "зарплату в РГС, и уходом это не считается.")
               + _definitions()
               + sql_info(shown, "lost_totals", "gained_totals", "lost_epk",
                          "gained_epk")),
        eyebrow="главный ответ")


def multi_block(multi: dict, t: dict, shown: dict, text: str = "",
                fb: bool = False) -> str:
    """Почему стало меньше совместителей: сколько, как устроено, почему."""
    if not multi:
        return ""
    parts = []
    parts.append(
        "<h3>Сколько</h3>"
        + C.table(["", "Получателей"], [
            ["<b>Изменение получателей</b>", f"<b>{_signed(multi['d_triples'])}</b>"],
            ["из них изменение ФЛ", _signed(multi["d_epk"])],
            ["<b>из них изменение совместительства</b>",
             f"<b>{_signed(multi['d_multi'])}</b>"],
            ["  совместители перестали получать зарплату в РГС",
             _signed(multi["lost"])],
            ["  совместители среди начавших получать зарплату в РГС",
             _signed(multi["gained"])],
            ["  " + A.inside_title(multi["inside"]), _signed(multi["inside"])],
        ], num_cols=[1])
        + _note("Совместитель в трёх организациях — один ФЛ и три получателя, то "
                "есть два «лишних». Изменение совместительства — изменение числа "
                "таких лишних получателей. Три его части — те же числа, что в "
                "шапке: перестали и начали получать по получателям минус по ФЛ, "
                "плюс третья строка разложения."))
    lbc = multi.get("lost_by_cause")
    if lbc is not None and not lbc.empty:
        rows = [[C.esc(r.title), _n(r.n_triples), _n(r.n_epk), _n(r.extra)]
                for r in lbc.itertuples()]
        parts.append(
            "<h3>Совместители, переставшие получать: почему</h3>"
            + C.table(["Ситуация", "Получателей", "ФЛ", "Лишних получателей"], rows,
                      num_cols=[1, 2, 3]))
    st = multi.get("structure")
    if st is not None and not st.empty:
        rows = [[C.esc(r.title), _n(r.base), _n(r.cur), _signed(r.delta)]
                for r in st.itertuples()]
        parts.append(
            "<h3>Как устроено совместительство</h3>"
            + C.table(["Показатель", f"{t['base_month']:%m.%Y}",
                       f"{t['report_month']:%m.%Y}", "Изменение"], rows,
                      num_cols=[1, 2, 3])
            + _note("«Одна организация через несколько ГОСБ» — ФЛ получает от одной "
                    "организации, но зарплата идёт через два и более ГОСБ, и "
                    "метрика считает его несколькими получателями. Если эта строка "
                    "дала заметный минус, это не уход людей, а сведение выплат "
                    "организации в один ГОСБ."))
    mr = multi.get("rows")
    if mr is not None and not mr.empty:
        rows = [[C.esc(r.title), _n(r.lost), _n(r.gained), _signed(r.net)]
                for r in mr.itertuples()]
        not_exit = mr[~mr["is_exit"]]["net"].sum()
        exit_ = mr[mr["is_exit"]]["net"].sum()
        rows += [["<b>Не уход человека (ГОСБ, закрытие и слияние организаций)</b>",
                  "", "", f"<b>{_signed(not_exit)}</b>"],
                 ["<b>Уход со второй работы или её зарплаты</b>", "", "",
                  f"<b>{_signed(exit_)}</b>"],
                 ["<b>Итого</b>", f"<b>{_n(mr['lost'].sum())}</b>",
                  f"<b>{_n(mr['gained'].sum())}</b>",
                  f"<b>{_signed(mr['net'].sum())}</b>"]]
        parts.append(
            f"<h3>{A.inside_title(multi['inside'])}: почему</h3>"
            + C.table(["Что случилось с местом работы", "Было и не стало",
                       "Не было и стало", "Итог"], rows, num_cols=[1, 2, 3])
            + _note("Только ФЛ, которые продолжают получать зарплату в РГС. «Было и "
                    "не стало» — места работы год назад, которых нет сейчас; «не "
                    "было и стало» — новые. Итог по всем ситуациям равен третьей "
                    "строке разложения в шапке. " + " ".join(
                        f"«{v[0]}» — {v[1]}" for v in A.MULTI_SITUATIONS.values())))
    orgs = multi.get("orgs") or {}
    for key, head in (("org_stopped", "Организации, которые больше не платят "
                                      "зарплату в РГС никому"),
                      ("no_pay", "Организации, откуда совместители ушли с места "
                                 "работы")):
        df = orgs.get(key)
        if df is None or df.empty:
            continue
        if key == "org_stopped":
            rows = [[C.esc(str(r.company_name or "нет в справочнике")),
                     C.esc("" if pd.isna(r.inn) else str(int(r.inn))),
                     _n(r.n_triples),
                     C.esc(str(r.company_to or ("—" if pd.isna(r.inn_to)
                                                else "нет в справочнике"))),
                     _n(r.n_epk_to)]
                    for r in df.itertuples()]
            tbl = _disclosure(f"Показать организации: {len(rows)}", C.table(
                ["Организация", "Номер", "Мест работы исчезло",
                 "Куда перешло больше всего этих ФЛ", "ФЛ"], rows, num_cols=[2, 4]))
        else:
            rows = [[C.esc(str(r.company_name or "нет в справочнике")),
                     C.esc("" if pd.isna(r.inn) else str(int(r.inn))),
                     _n(r.n_triples)] for r in df.itertuples()]
            tbl = _disclosure(f"Показать организации: {len(rows)}", C.table(
                ["Организация", "Номер", "Мест работы исчезло"], rows, num_cols=[2]))
        parts.append(f"<h3>{head}</h3>" + tbl)
    return C.section(
        A.T_MULTI,
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + "".join(parts)
               + sql_info(shown, "multi_structure", "multi_rows", "multi_orgs")),
        eyebrow="разница между получателями и ФЛ")


def where_gone_block(to_seg: pd.DataFrame, to_codes: pd.DataFrame,
                     to_codes_inn: pd.DataFrame,
                     mig: pd.DataFrame, tenure: pd.DataFrame, shown: dict,
                     text: str = "", fb: bool = False,
                     extra: dict | None = None) -> str:
    """Куда именно делись люди: сегмент, вид выплат, переоформление, стаж.

    Строка «ушёл» без адреса — половина ответа. Забрал ли человека коммерческий
    клиент, вышел ли он на пенсию, или организацию просто переоформили — разные
    истории, и решения по ним разные вплоть до противоположных.
    """
    parts = []
    if not to_seg.empty:
        rows = [[C.esc(str(r.segment_name)), _n(r.n_epk), _pct(r.share, 0)]
                for r in to_seg.itertuples()]
        parts.append("<h3>В каком сегменте теперь получают зарплату</h3>"
                     + C.table(["Сегмент", "ФЛ", "Доля"], rows,
                               num_cols=[1, 2]))
    if not to_codes.empty:
        rows = [[_n(r.code), C.esc(str(r.code_name or "")), _n(r.n_epk),
                 _pct(r.share, 0)] for r in to_codes.itertuples()]
        parts.append(
            "<h3>Чем заменились зарплатные зачисления</h3>"
            + C.table(["Код", "Вид зачисления", "ФЛ", "Доля"], rows,
                      num_cols=[0, 2, 3])
            + _note("Это ФЛ, которым от бюджетной организации приходят только "
                    "незарплатные выплаты. Зарплату в РГС они получать перестали — "
                    "это уже учтено в разложении. Таблица говорит, что с ними "
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
            "<h3>От каких организаций приходят только незарплатные выплаты</h3>"
            # Колонка названа «Номер», а не тремя буквами, которых в готовом
            # HTML всё равно не останется: `html.sanitize` вычищает это слово
            # как отдельное (оно блокирует пересылку отчёта), и заголовок
            # превратился бы в «Орг.» рядом с колонкой «Организация».
            + _disclosure(f"Показать организации: {len(rows)}",
                          C.table(["Вид зачисления", "Организация", "Номер", "ФЛ",
                                   "Доля вида"], rows, num_cols=[3, 4]))
            + _note("Доля считается ВНУТРИ вида зачисления: вопрос здесь — какую "
                    "часть перешедших на пенсию или на пособие даёт одна "
                    "организация. Если верх занимает одна-две — это адрес, куда "
                    "идти; если список ровный — это фон по всему сегменту, и "
                    "идти некуда."))
    extra = extra or {}
    lso, lm = extra.get("lso", pd.DataFrame()), extra.get("lso_meta", {}) or {}
    if lso is not None and not lso.empty:
        rows = [[C.esc(str(getattr(r, "company_name", None) or "нет в справочнике")),
                 C.esc("" if pd.isna(r.inn) else str(int(r.inn))),
                 C.esc(str(r.segment_name or "")),
                 C.esc(str(getattr(r, "industry_name", None) or "")),
                 _n(r.n_epk), _pct(r.share, 0), _pct(r.same_gosb_share, 0)]
                for r in lso.itertuples()]
        parts.append(
            "<h3>От каких небюджетных организаций теперь получают зарплату</h3>"
            + f'<p>Всего {_n(lm.get("n_orgs", 0))} организаций. Десять крупнейших '
              f'дают {_pct(lm.get("top10_share", 0), 0)} таких ФЛ; '
              f'в том же ГОСБ осталось '
              f'{_pct(lm.get("same_gosb_share", 0), 0)}.</p>'
            + _disclosure(f"Показать организации: {len(rows)}",
                          C.table(["Организация", "Номер", "Сегмент", "Отрасль",
                                   "ФЛ", "Доля", "В том же ГОСБ"], rows,
                                  num_cols=[4, 5, 6]))
            + _note("Приёмник у человека один — тот, кто платит больше всех. "
                    "Высокая доля крупнейших — людей забирают конкретные "
                    "организации, с ними и надо работать; низкая — обычная смена "
                    "работы. «Тот же ГОСБ» — человек остался в своём "
                    "городе: переехал работодатель или функцию вывели на "
                    "аутсорсинг, а не переехал человек."))
    below = extra.get("below", pd.DataFrame())
    if below is not None and not below.empty:
        rows = [[C.esc(r.axis), C.esc(r.bucket), _n(r.n_epk), _pct(r.share, 0)]
                for r in below.itertuples()]
        parts.append(
            "<h3>Ниже порога — насколько</h3>"
            + C.table(["Что меряем", "Диапазон", "ФЛ", "Доля"], rows,
                      num_cols=[2, 3])
            + _note("Уровень — лучшая сумма по зарплатным кодам в одной организации "
                    "против порога. Изменение — к сумме базового месяца. «80–100% "
                    "порога» вместе с «почти не изменилась» — артефакт порога: "
                    "зарплата была чуть выше и стала чуть ниже, человек никуда не "
                    "делся. «Упала больше чем вдвое» — неполная ставка, простой, "
                    "частичная выплата."))
    if not mig.empty:
        rows = []
        for r in mig.head(12).itertuples():
            rows.append([
                C.esc(str(getattr(r, "name_from", None) or r.inn_from)),
                C.esc(str(getattr(r, "name_to", None) or r.inn_to)),
                _n(r.n_epk), _pct(r.share, 0),
                "да" if getattr(r, "to_in_segment", False) else "нет"])
        parts.append(
            "<h3>Похоже на переоформление организации</h3>"
            + _disclosure(f"Показать организации: {len(rows)}", C.table(
                ["Откуда", "Куда", "ФЛ", "Доля от ушедших получателей организации",
                       "Новая организация в РГС"], rows, num_cols=[2, 3]))
            + _note("ФЛ этих организаций дружно оказались в одной и той же новой "
                    "организации. Если она в РГС — ФЛ продолжают получать зарплату "
                    "в РГС, и уходом это не считается. Если нет — они учтены в "
                    "ситуации «получает зарплату в банке, но от небюджетной "
                    "организации»: стоит проверить, не должна ли новая организация "
                    "быть бюджетной."))
    if not tenure.empty:
        head = list(tenure.columns)
        rows = [[C.esc(str(row[0]))]
                + [_pct(v, 0) if head[i + 1] == "Доля" else _n(v)
                   for i, v in enumerate(row[1:])]
                for row in tenure.itertuples(index=False)]
        parts.append(
            "<h3>Сколько месяцев переставшие получали зарплату в РГС</h3>"
            + C.table(["Месяцев в РГС"] + [str(c) for c in head[1:]], rows,
                      num_cols=list(range(1, len(head))))
            + _note("Перестают получать недавно начавшие — это ротация. "
                    "Перестают давние получатели — уходит ядро. По числу «N ФЛ» "
                    "эти два случая "
                    "одинаковы, а решения по ним разные."))
    if not parts:
        return _empty("Куда делись люди", "ни один разрез не дал результата")
    return C.section(
        "Куда делись люди",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + "".join(parts)
               + sql_info(shown, "left_segment", "left_segment_orgs", "left_codes",
                          "below_depth", "inn_migration", "tenure")),
        eyebrow="ответ на «куда»")


def why_block(why: dict, shown: dict, text: str = "", fb: bool = False) -> str:
    """Почему ушли: организация или человек, договор, как уходили, кто уходил.

    Отвечает не на «куда», а на «почему» — насколько это возможно по трём
    разрешённым витринам. Мотивы людей (цена, бонусы конкурента, увольнение
    против смены работы) из данных не видны, и блок их не выдумывает: он
    отделяет решения организаций от решений людей и показывает, можно ли было
    уход заметить заранее.
    """
    why = why or {}
    parts = []
    summ, cross = why.get("org_sum", pd.DataFrame()), why.get("org_agr", pd.DataFrame())
    top, meta = why.get("org_top", pd.DataFrame()), why.get("org_meta", {}) or {}
    if summ is not None and not summ.empty:
        rows = [[C.esc(r.org_class), _n(r.n_org), _n(r.n_base), _n(r.left_bank),
                 _pct(r.share_lb, 0), _n(r.loss), _pct(r.share_loss, 0)]
                for r in summ.itertuples()]
        parts.append(
            "<h3>Ушла организация или уходят люди</h3>"
            + f'<p>На организации, которые увели зарплатный проект целиком или '
              f'массово, приходится <b>{_pct(meta.get("share_lb_org", 0), 0)}</b> '
              f'всех ФЛ, больше не получающих зачислений в банке.</p>'
            + C.table(["Организации", "Сколько", "Получателей в базовом месяце",
                       "Нет зачислений в банке", "Доля от всех без зачислений",
                       A.T_LOST, "Доля от всех переставших"], rows,
                      num_cols=[1, 2, 3, 4, 5, 6])
            + _note(f"«Ушла целиком» — в отчётном месяце ни одного получателя и ни "
                    f"одного зачисления бывшим получателям: зарплатный проект уведён, "
                    f"это потеря в B2B и вопрос к менеджеру организации. «Массовый "
                    f"уход» — организация платит, но из банка ушло не меньше "
                    f"{_pct(meta.get('mass_share', 0.5), 0)} её получателей. «Перестала "
                    f"платить, люди остались в банке» — организации в ведомостях нет, но "
                    f"её люди получают деньги в банке: реорганизация или новый ИНН, а не "
                    f"уход клиента; куда именно — в таблице переоформлений. «Точечные» "
                    f"— люди уходят сами: переводят зарплату по заявлению или "
                    f"увольняются. Организации меньше {meta.get('min_base', 10)} "
                    f"получателей в классы не делятся: у маленькой «ушли все» — "
                    f"это двое."))
    if cross is not None and not cross.empty:
        rows = [[C.esc(r.org_class), C.esc(r.agr), _n(r.n_org), _n(r.loss)]
                for r in cross.itertuples()]
        parts.append(
            "<h3>Договор зарплатного проекта</h3>"
            + C.table(["Организации", "Договор", "Сколько", A.T_LOST], rows,
                      num_cols=[2, 3])
            + _note(f"Номер договора известен у {_pct(meta.get('agr_known', 0), 0)} "
                    f"организаций. «Договор сменился» — ни один договор базового "
                    f"месяца не жив в отчётном, но есть новый: переоформление у нас. "
                    f"Переставшие получать у таких организаций — повод проверить, не ушла ли часть "
                    f"людей при переоформлении."))
    if top is not None and not top.empty:
        rows = [[C.esc(str(getattr(r, "company_name", None) or "")),
                 C.esc("" if pd.isna(r.inn) else str(int(r.inn))),
                 C.esc(r.org_class), C.esc(r.agr), _n(r.n_base), _n(r.n_cur),
                 _n(r.left_bank), _n(r.loss), _pct(r.lb_share, 0)]
                for r in top.itertuples()]
        parts.append(
            "<h3>Организации, которые увели проект целиком или массово</h3>"
            + _disclosure(f"Показать организации: {len(rows)}", C.table(
                ["Организация", "Номер", "Что произошло", "Договор", "Было",
                 "Стало", "Нет зачислений в банке", A.T_LOST,
                 "Доля без зачислений в банке"], rows, num_cols=[4, 5, 6, 7, 8]))
            + _note("Это адресный список: по каждой организации есть конкретный "
                    "вопрос к её менеджеру. Сортировка по числу переставших "
                    "получать зарплату в РГС."))
    pat, gone_m = why.get("pat", pd.DataFrame()), why.get("gone_m", pd.DataFrame())
    if pat is not None and not pat.empty:
        rows = [[C.esc(r.pattern), _n(r.n_epk), _pct(r.share, 0), _pct(r.ratio, 0)]
                for r in pat.itertuples()]
        parts.append(
            "<h3>Как уходили из банка: обрывом или постепенно</h3>"
            + C.table(["Как", "ФЛ", "Доля", "Последние месяцы к прежним (в среднем)"],
                      rows, num_cols=[1, 2, 3])
            + _note("Два последних месяца с зачислениями против трёх до них. "
                    "Постепенный уход — человек сначала уводил часть зарплаты (аванс "
                    "в одном банке, зарплата в другом) или ему сокращали ставку: "
                    "такого клиента можно было заметить и удержать заранее. Обрыв — "
                    "увольнение или разовый перевод зарплаты целиком."))
    if gone_m is not None and not gone_m.empty:
        rows = [[C.esc(f"{pd.Timestamp(r.gone_month):%m.%Y}"), _n(r.n_epk),
                 _pct(r.share, 0)] for r in gone_m.itertuples()]
        parts.append(
            "<h3>В каком месяце уходили</h3>"
            + C.table(["Месяц ухода", "ФЛ", "Доля"], rows, num_cols=[1, 2])
            + _note("Месяц ухода — следующий за последним месяцем с зачислениями. "
                    "Всплеск в одном месяце — событие: организация увела проект. "
                    "Ровный фон — текучесть людей."))
    pay = why.get("pay", pd.DataFrame())
    if pay is not None and not pay.empty:
        heads = [A.PAY_BUCKETS[k] for k in A.PAY_BUCKETS]
        rows = [[C.esc(str(r.fate_title)), _n(r.n_triples)]
                + [_pct(getattr(r, f"b{k}"), 0) for k in A.PAY_BUCKETS]
                for r in pay.itertuples()]
        parts.append(
            "<h3>Сколько получали ушедшие по сравнению с коллегами</h3>"
            + C.table(["Группа", "Получателей"] + heads, rows,
                      num_cols=list(range(1, len(heads) + 2)))
            + _note("Зарплата получателя против средней по ЕГО организации в базовом "
                    "месяце. Сравнивать со строкой «Остались на месте»: сдвиг влево — "
                    "уходят низкооплачиваемые (текучка, сокращения), вправо — "
                    "высокооплачиваемые (их переманивают — это дороже всего)."))
    if not parts:
        return _empty("Почему ушли", "ни один разбор не дал результата")
    return C.section(
        "Почему ушли",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + "".join(parts)
               + _src("Мотивы людей — цена обслуживания, бонусы конкурента, "
                      "увольнение против смены работы — в разрешённых источниках не "
                      "видны. Блок отделяет решения организаций от решений людей и "
                      "показывает, можно ли было уход заметить заранее.")
               + sql_info(shown, "org_status", "exit_pattern", "pay_level")),
        eyebrow="ответ на «почему»")


def when_block(tr: pd.DataFrame, st: pd.DataFrame, measured: str,
               cmp_months: pd.DataFrame, surv: pd.DataFrame, load: pd.DataFrame,
               seas: dict, codes: pd.DataFrame, t_prev: dict | None,
               causes_prev: pd.DataFrame, shown: dict, text: str = "",
               fb: bool = False, t: dict | None = None) -> str:
    """Когда именно произошло падение — и не сезон ли это."""
    if tr.empty:
        return _empty("Когда это произошло", "помесячный ряд не построился")
    pts = [(pd.Timestamp(r.report_dt), float(r.n_triples)) for r in tr.itertuples()]
    marks = ({pd.Timestamp(r.report_dt) for r in st.itertuples()}
             if not st.empty else set())
    hl = ({t["base_month"]: "год назад", t["report_month"]: "отчётный"}
          if t else {})

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
            + C.table(["Месяц", "Получателей", "ФЛ", "Получателей на ФЛ",
                       "Получателей к отчётному", "ФЛ к отчётному"], rows,
                      num_cols=[1, 2, 3, 4, 5])
            + _note("Если отчётный месяц ниже соседних так же, как год назад, "
                    "годовое падение повторяет обычный сезонный провал, и "
                    "выводить из него тренд нельзя."))

    # Сезонность — сразу после сравнения месяцев: она объясняет провал отчётного
    # месяца, и без неё этот провал читается как уход.
    seas_html = ""
    if seas and seas.get("n_prev_both"):
        seas_html = (
            "<h3>Сезонность: повторяется ли провал год к году</h3>"
            + C.table(
                ["Показатель", "ФЛ"],
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
        real = causes_prev[causes_prev["kind"] == A.LOSS]
        tot = float(real["n_triples"].sum()) or 1.0
        rows = [[C.esc(r.title), _n(r.n_triples), _pct(r.n_triples / tot, 0)]
                for r in real.itertuples()]
        prev_html = (
            f"<h3>Что произошло за один месяц: "
            f"{t_prev['base_month']:%m.%Y} → {t_prev['report_month']:%m.%Y}</h3>"
            + _decomp_table(t_prev)
            + C.table([f"{A.T_LOST}: почему", "Получателей", "Доля"], rows,
                      num_cols=[1, 2])
            + _src("То же разложение, что в шапке, но к предыдущему месяцу: видно, "
                   "что из годового изменения случилось за последний месяц."))

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
        spts = [(pd.Timestamp(r.report_dt), float(r.share))
                for r in surv.itertuples()]
        surv_html = (
            "<h3>Сколько получателей базового месяца получают до сих пор</h3>"
            + line_chart(spts, as_pct=True, height=200,
                         highlight={k: v for k, v in hl.items()
                                    if k in {m for m, _ in spts}},
                         caption=f"в отчётном месяце получают "
                                 f"{_pct(last['share'])} получателей базового месяца")
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
               + line_chart(pts, caption="получателей по месяцам; наведите на "
                                         "точку, чтобы увидеть число",
                            marks=marks, highlight=hl)
               + seas_html + codes_html + step_html + prev_html
               + surv_html + load_html
               + sql_info(shown, "monthly", "seasonal", "code_months",
                          "survival", "lost_totals_prev", "monthly_all")),
        eyebrow="ответ на «когда»")


def _cut_table(dim: str, df: pd.DataFrame, title: str, total: dict) -> str:
    spec = A.cut_columns(dim, df)
    rows = [[C.esc(str(getattr(r, dim)))]
            + [_pct(getattr(r, c), 0) if c == "share"
               else _signed(getattr(r, c)) if c in (A.MOVES, A.DELTA)
               else _n(getattr(r, c)) for c, _ in spec]
            for r in df.itertuples()]
    tot = ["<b>Итого по всем группам</b>"] + [
        f"<b>{_pct(total[c], 0) if c == 'share' else _signed(total[c]) if c in (A.MOVES, A.DELTA) else _n(total[c])}</b>"
        if c in total else "" for c, _ in spec]
    return C.table([title] + [h for _, h in spec], rows + [tot],
                   num_cols=list(range(1, len(spec) + 1)))


def cut_sorter(dim: str, df: pd.DataFrame, title: str, top_n: int) -> str:
    """Таблица разреза с переключателем сортировки — без JS.

    Верх по «перестали» и верх по изменению — РАЗНЫЕ наборы групп (группа с
    большим оттоком могла столько же набрать), поэтому это две таблицы, а не
    одна пересортированная: сортировка на странице по top-N не нашла бы группу,
    которая в top-N по оттоку не попала. Переключение — радиокнопки и CSS
    `:checked`, отчёт открывается без сети.
    """
    total = A.cut_total(df)
    loss = _cut_table(dim, A.top_cut(df, A.LOSS, top_n), title, total)
    if A.DELTA not in df:
        return loss
    delta = _cut_table(dim, A.top_cut(df, A.DELTA, top_n), title, total)
    key = "s_" + "".join(ch for ch in dim if ch.isalnum())
    return (f'<div class="sorter">'
            f'<input type="radio" class="r-loss" name="{key}" id="{key}_l" checked>'
            f'<label for="{key}_l">Сортировать по переставшим</label>'
            f'<input type="radio" class="r-delta" name="{key}" id="{key}_d">'
            f'<label for="{key}_d">по изменению</label>'
            f'<div class="sv sv-loss">{loss}</div>'
            f'<div class="sv sv-delta">{delta}</div></div>')


def where_block(cuts: dict, orgs: pd.DataFrame, meta: dict, shown: dict,
                text: str = "", fb: bool = False) -> str:
    """Где перестали получать зарплату в РГС: разрезы и организации."""
    parts = []
    titles = CUT_TITLES
    top_n = int(meta.get("top_n", 12))
    for dim, df in cuts.items():
        if df is None or df.empty:
            continue
        parts.append(f"<h3>{C.esc(titles.get(dim, dim))}</h3>"
                     + cut_sorter(dim, df, titles.get(dim, dim), top_n))
    if parts:
        parts.append(_note(
            f"«{A.T_LOST}» = нет зачислений в банке + зарплата от небюджетной "
            f"организации + только незарплатные выплаты + зарплата до 2 500 ₽. "
            f"«Изменение получателей» = начали − перестали ± переходы внутри РГС и "
            f"совместители; сумма изменения по всем группам (строка «Итого») равна "
            f"изменению в шапке. «Переходы» — ФЛ продолжают получать зарплату в "
            f"РГС, но сменили организацию или ГОСБ либо стали получать в меньшем "
            f"числе организаций: для сегмента это не уход, а для группы — минус "
            f"или плюс. Переключатель над таблицей меняет сортировку: по числу "
            f"переставших или по изменению (сначала самые большие минусы). Счёт — "
            f"в получателях."))

    if not orgs.empty:
        rows = [[C.esc(str(r.company_name or r.inn)), _signed(r.net), _n(r.lost),
                 _n(r.gained), C.esc(str(r.cause_title or "")),
                 C.esc(str(r.agency or "")), C.esc(str(r.tb_short_name or ""))]
                for r in orgs.itertuples()]
        parts.append(
            f'<details class="orgs"><summary>Показать организации с наибольшим '
            f'изменением: {len(orgs)}</summary>'
            + C.table(["Организация", "Изменение получателей", "Было и не стало",
                       "Не было и стало", "Чаще всего", "Ведомство", "ТБ"], rows,
                      num_cols=[1, 2, 3])
            + _note("Считается по организации: «было и не стало» — получатели этой "
                    "организации год назад, которых в ней нет сейчас, в том числе "
                    "перешедшие в другую организацию РГС. Поэтому сумма по "
                    "организациям больше, чем «перестали получать зарплату в РГС» "
                    "в шапке: переход из школы в школу — минус у одной и плюс у "
                    "другой. Сортировка по изменению.")
            + "</details>")

    if not parts:
        return _empty("Где это произошло", "разрезы не построились")

    cov = _src(
        f"Ведомство и уровень подчинения выводятся из наименования: отдельных "
        f"полей для них нет ни в одном разрешённом источнике. Имя известно у "
        f"{_pct(meta.get('named_share', 0), 0)} организаций; неразобранные имена "
        f"показаны отдельной группой и никуда не раскидываются. ГОСБ — номер "
        f"из ведомостей; где справочник название не дал, ГОСБ подписан "
        f"номером.")
    return C.section(
        "Где это произошло",
        C.card(_fallback_mark(fb) + (C.narrative_html(text) if text else "")
               + "".join(parts) + cov
               + sql_info(shown, "lost_by_inn", "gained_by_inn", "stayed_dest")),
        eyebrow="ответ на «где»")


# Заголовки разрезов — одни на HTML, документ и промпты.
CUT_TITLES = {
    "holding_name": "Холдинги",
    "agency": "Ведомства (по наименованию)",
    "level": "Уровень подчинения",
    "industry_name": "Отрасль справочника",
    "tb_short_name": "Территориальные банки",
    "gosb_name": "ГОСБы",
}


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
        "переставшим получать.</li>",
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
            f"{m.get('n_used_legacy', 0)}) — ГОСБы в разрезе подписаны "
            f"номерами, а не названиями.</li>")
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
                f"они попадают в строку «ТБ неизвестен».</li>")
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
    """Готовая страница. Слово-идентификатор вычищается внутри `H.page`.

    Любая таблица со списком организаций обязана лежать под «+» (`_disclosure`):
    это требование заказчика, и `selfcheck.check_org_lists_hidden` его сверяет.

    Каждая таблица — в обёртке с горизонтальной прокруткой: широкие разрезы
    (десять колонок) иначе вылезают за карточку.
    """
    body = "".join(blocks).replace("<table>", '<div class="tw"><table>') \
                          .replace("</table>", "</table></div>")
    return H.page(title, subtitle, EXTRA_CSS + body, footer)
