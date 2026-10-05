"""Графики — серверный SVG без библиотек и без сети (правило платформы 16).

Подсказки: у каждой метки атрибут `data-tip`; один скрипт в странице показывает
его у курсора и при фокусе с клавиатуры. Подсказка дополняет, а не заменяет:
каждое число графика есть и в таблице рядом.

Цвета — ролями из CSS-переменных страницы (светлая и тёмная темы). Знак
(рост/падение) — расходящаяся пара синий ↔ красный с серой серединой; категории
(сегменты) — фиксированный порядок слотов, никогда не по рангу.
"""
from __future__ import annotations

import html
import math

import numpy as np

# Фиксированный порядок слотов сегментов: цвет следует за сегментом, а не за его
# местом в таблице. Заглушки и «вне получателей» — нейтральные серые.
SEG_SLOT = {"КСБ": 1, "РГС": 2, "ММБ": 3, "СКМ": 4, "КФИ": 5, "БМО": 6}


def seg_color(seg: str) -> str:
    k = SEG_SLOT.get(seg)
    return f"var(--series-{k})" if k else "var(--neutral)"


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def fnum(v, signed: bool = False, digits: int = 0) -> str:
    """1 234 567 · −1 234 · +1 234. Неразрывный пробел между разрядами."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    s = f"{abs(float(v)):,.{digits}f}".replace(",", " ").replace(".", ",")
    if float(v) < 0 and round(abs(float(v)), digits) != 0:
        return "−" + s
    return ("+" + s) if signed and round(float(v), digits) != 0 else s


def fpct(v, signed: bool = False, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return fnum(100 * float(v), signed, digits) + "%"


def _nice(vmax: float) -> float:
    if vmax <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(vmax))
    for m in (1, 2, 2.5, 5, 10):
        if vmax <= m * e:
            return m * e
    return 10 * e


def ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    span = hi - lo
    if span <= 0:
        return [lo]
    step = _nice(span / n)
    a = math.floor(lo / step) * step
    out = []
    while a <= hi + 1e-9:
        out.append(a)
        a += step
    return out


# --------------------------------------------------------------------------- #
# Линии: два года по календарному месяцу (наложение)
# --------------------------------------------------------------------------- #
def lines(xlabels: list[str], series: list[dict], height: int = 260,
          fmt=fnum, y_from_zero: bool = False) -> str:
    """series: [{name, values (None — пропуск), color, emphasis}]. Перекрестие —
    колонками-мишенями по X: подсказка перечисляет все серии в этой точке."""
    W, H = 720, height
    L, R, T, B = 64, 80, 14, 30
    vals = [v for s in series for v in s["values"] if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if y_from_zero:
        lo = min(0, lo)
    pad = (hi - lo) * 0.08 or abs(hi) * 0.05 or 1
    lo, hi = lo - pad, hi + pad
    tk = ticks(lo, hi)
    lo, hi = min(lo, tk[0]), max(hi, tk[-1])
    n = len(xlabels)
    xs = [L + (W - L - R) * (i / max(1, n - 1)) for i in range(n)]

    def y(v):
        return T + (H - T - B) * (1 - (v - lo) / (hi - lo))

    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="{esc(", ".join(s["name"] for s in series))}">']
    for t in tk:
        out.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                   f'<text class="tick" x="{L - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{esc(fmt(t))}</text>')
    step = 1 if n <= 13 else 2
    for i, lab in enumerate(xlabels):
        if i % step == 0 or i == n - 1:
            out.append(f'<text class="tick" x="{xs[i]:.1f}" y="{H - 10}" text-anchor="middle">{esc(lab)}</text>')
    for s in series:
        pts, path = [], []
        for i, v in enumerate(s["values"]):
            if v is None:
                if path:
                    pts.append(path)
                path = []
                continue
            path.append(f"{xs[i]:.1f},{y(v):.1f}")
        if path:
            pts.append(path)
        cls = "line" + (" em" if s.get("emphasis") else "")
        for p in pts:
            out.append(f'<polyline class="{cls}" style="stroke:{s["color"]}" points="{" ".join(p)}"/>')
        last = max((i for i, v in enumerate(s["values"]) if v is not None), default=None)
        if last is not None:
            v = s["values"][last]
            out.append(f'<circle class="dot" cx="{xs[last]:.1f}" cy="{y(v):.1f}" r="4" style="fill:{s["color"]}"/>'
                       f'<text class="dlabel" x="{xs[last] + 8:.1f}" y="{y(v) + 4:.1f}">{esc(s["name"])}</text>')
    # Колонки-мишени: полоса шириной в шаг X, внутри — волосяная вертикаль.
    half = (W - L - R) / max(1, n - 1) / 2
    for i, lab in enumerate(xlabels):
        rows = [f'{esc(s["name"])}: {esc(fmt(s["values"][i]))}' for s in series
                if s["values"][i] is not None]
        if not rows:
            continue
        tip = esc(lab) + "|" + "|".join(rows)
        out.append(f'<g class="hit" tabindex="0" data-tip="{tip}">'
                   f'<rect x="{xs[i] - half:.1f}" y="{T}" width="{2 * half:.1f}" height="{H - T - B}"/>'
                   f'<line class="cross" x1="{xs[i]:.1f}" x2="{xs[i]:.1f}" y1="{T}" y2="{H - B}"/></g>')
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Столбцы со знаком (рост — синий, падение — красный)
# --------------------------------------------------------------------------- #
def columns(xlabels: list[str], values: list, height: int = 200, fmt=fnum,
            highlight: set[int] | None = None, tips: list[str] | None = None) -> str:
    W, H = 720, height
    L, R, T, B = 64, 16, 18, 30
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    lo, hi = min(0, min(vals)), max(0, max(vals))
    tk = ticks(lo, hi)
    lo, hi = min(lo, tk[0]), max(hi, tk[-1])
    n = len(values)
    band = (W - L - R) / n
    bw = min(24, band * 0.6)

    def y(v):
        return T + (H - T - B) * (1 - (v - lo) / (hi - lo))

    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img">']
    for t in tk:
        out.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                   f'<text class="tick" x="{L - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{esc(fmt(t))}</text>')
    out.append(f'<line class="base" x1="{L}" x2="{W - R}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    step = 1 if n <= 13 else 2
    for i, v in enumerate(values):
        cx = L + band * (i + 0.5)
        if i % step == 0 or i == n - 1:
            out.append(f'<text class="tick" x="{cx:.1f}" y="{H - 10}" text-anchor="middle">{esc(xlabels[i])}</text>')
        if v is None:
            continue
        y0, y1 = y(0), y(v)
        top, h = min(y0, y1), abs(y1 - y0)
        cls = "pos" if v >= 0 else "neg"
        if highlight is not None and i not in highlight:
            cls += " dim"
        tip = tips[i] if tips else f"{xlabels[i]}|{fmt(v)}"
        r = min(4, h / 2, bw / 2)
        out.append(f'<g class="hit" tabindex="0" data-tip="{esc(tip)}">'
                   f'<rect class="hitbox" x="{cx - band / 2:.1f}" y="{T}" width="{band:.1f}" height="{H - T - B}"/>'
                   f'{_bar_path(cx - bw / 2, top, bw, h, r, up=v >= 0, cls=cls)}</g>')
        if highlight is not None and i in highlight:
            ly = (top - 5) if v >= 0 else (top + h + 13)
            out.append(f'<text class="vlabel" x="{cx:.1f}" y="{ly:.1f}" text-anchor="middle">{esc(fmt(v))}</text>')
    out.append("</svg>")
    return "".join(out)


def _bar_path(x, y, w, h, r, up: bool, cls: str) -> str:
    """Столбик со скруглённым концом данных (4px) и прямым основанием."""
    if h <= 0.5:
        return f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="0.5"/>'
    if up:
        d = (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
             f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")
    else:
        d = (f"M{x:.1f},{y:.1f} V{y + h - r:.1f} Q{x:.1f},{y + h:.1f} {x + r:.1f},{y + h:.1f} "
             f"H{x + w - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w:.1f},{y + h - r:.1f} V{y:.1f} Z")
    return f'<path class="{cls}" d="{d}"/>'


# --------------------------------------------------------------------------- #
# Горизонтальные столбцы со знаком (слагаемые разницы разниц)
# --------------------------------------------------------------------------- #
def hbars(labels: list[str], values: list[float], fmt=fnum, tips: list[str] | None = None,
          strong: list[bool] | None = None) -> str:
    W = 720
    LW, R = 300, 70
    row = 26
    H = row * len(values) + 10
    vmax = max([abs(v) for v in values if v is not None] + [1])
    pad = 64                      # место под подпись значения с каждой стороны
    x0 = LW + (W - LW) / 2
    scale = ((W - LW) / 2 - pad) / vmax
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img">',
           f'<line class="base" x1="{x0:.1f}" x2="{x0:.1f}" y1="0" y2="{H}"/>']
    for i, (lab, v) in enumerate(zip(labels, values)):
        yc = 5 + row * i + row / 2
        cls_t = "rlabel strong" if strong and strong[i] else "rlabel"
        out.append(f'<text class="{cls_t}" x="{LW - 10}" y="{yc + 4:.1f}" text-anchor="end">{esc(lab)}</text>')
        if v is None:
            continue
        w = abs(v) * scale
        x = x0 if v >= 0 else x0 - w
        cls = "pos" if v >= 0 else "neg"
        tip = tips[i] if tips else f"{lab}|{fmt(v)}"
        bh = 14 if not (strong and strong[i]) else 18
        out.append(f'<g class="hit" tabindex="0" data-tip="{esc(tip)}">'
                   f'<rect class="hitbox" x="{LW}" y="{yc - row / 2:.1f}" width="{W - LW:.1f}" height="{row}"/>'
                   f'<rect class="{cls}" x="{x:.1f}" y="{yc - bh / 2:.1f}" width="{max(w, 0.5):.1f}" '
                   f'height="{bh}" rx="3"/></g>')
        lx = x0 + w + 6 if v >= 0 else x0 - w - 6
        anchor = "start" if v >= 0 else "end"
        out.append(f'<text class="vlabel" x="{lx:.1f}" y="{yc + 4:.1f}" text-anchor="{anchor}">{esc(fmt(v, True))}</text>')
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Сгруппированные горизонтальные столбцы: этот год против прошлого
# --------------------------------------------------------------------------- #
def paired(labels: list[str], cur: list, prev: list, names: tuple[str, str], fmt=fpct,
           vmax: float | None = None) -> str:
    W, LW, R = 720, 220, 70
    row = 44
    H = row * len(labels) + 34
    vmax = vmax or max([v for v in cur + prev if v is not None] + [1e-9])
    scale = (W - LW - R) / vmax
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img">',
           f'<rect class="key-cur" x="{LW}" y="4" width="12" height="10" rx="2"/>'
           f'<text class="tick" x="{LW + 18}" y="13">{esc(names[0])}</text>'
           f'<rect class="key-prev" x="{LW + 150}" y="4" width="12" height="10" rx="2"/>'
           f'<text class="tick" x="{LW + 168}" y="13">{esc(names[1])}</text>']
    for i, lab in enumerate(labels):
        y0 = 26 + row * i
        out.append(f'<text class="rlabel" x="{LW - 10}" y="{y0 + 22:.1f}" text-anchor="end">{esc(lab)}</text>')
        for j, (v, cls) in enumerate(((cur[i], "key-cur"), (prev[i], "key-prev"))):
            if v is None:
                continue
            yy = y0 + 4 + j * 18
            w = max(0.5, v * scale)
            out.append(f'<g class="hit" tabindex="0" data-tip="{esc(lab)}|{esc(names[j])}: {esc(fmt(v))}">'
                       f'<rect class="hitbox" x="{LW}" y="{yy - 2}" width="{W - LW:.0f}" height="18"/>'
                       f'<rect class="{cls}" x="{LW}" y="{yy}" width="{w:.1f}" height="14" rx="3"/></g>'
                       f'<text class="vlabel" x="{LW + w + 6:.1f}" y="{yy + 11}">{esc(fmt(v))}</text>')
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Тепловая карта со знаком: синий — рост, красный — падение, серый — ноль
# --------------------------------------------------------------------------- #
def div_scale(values) -> float:
    a = np.array([abs(v) for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))])
    if a.size == 0:
        return 1.0
    return float(np.quantile(a, 0.95)) or float(a.max()) or 1.0


def heat_style(v, vmax: float) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    t = max(-1.0, min(1.0, float(v) / vmax)) if vmax else 0.0
    pct = round(100 * abs(t) ** 0.8)
    pole = "var(--div-pos)" if t >= 0 else "var(--div-neg)"
    ink = " color:var(--on-strong);" if pct >= 60 else ""
    return f"background:color-mix(in oklab, {pole} {pct}%, var(--div-mid));{ink}"


def heatmap(rows: list[str], cols: list[str], cell, fmt, tipf=None, vmax: float | None = None,
            row_head: str = "", total_col: str | None = None) -> str:
    """cell(r, c) → число или None. Таблица: ячейки — цвет + число (число не
    отдаётся одному цвету)."""
    vals = [cell(r, c) for r in rows for c in cols]
    vmax = vmax or div_scale(vals)
    out = ['<div class="scroll"><table class="heat"><thead><tr>',
           f'<th class="rh">{esc(row_head)}</th>']
    out += [f"<th>{esc(c)}</th>" for c in cols]
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append(f'<tr><th class="rh">{esc(r)}</th>')
        for c in cols:
            v = cell(r, c)
            tip = tipf(r, c, v) if tipf else f"{r} · {c}|{fmt(v)}"
            cls = ' class="tot"' if total_col and c == total_col else ""
            out.append(f'<td{cls} style="{heat_style(v, vmax)}" tabindex="0" data-tip="{esc(tip)}">'
                       f"{esc(fmt(v))}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Sankey: перетоки ФЛ между основными сегментами (без оставшихся на месте)
# --------------------------------------------------------------------------- #
def sankey(flows: list[tuple[str, str, float]], left: list[str], right: list[str],
           left_title: str, right_title: str) -> str:
    W, H = 720, 380
    NW, gap, T = 12, 10, 24
    xl, xr = 170, W - 170 - NW
    tot_l = {n: sum(v for a, _, v in flows if a == n) for n in left}
    tot_r = {n: sum(v for _, b, v in flows if b == n) for n in right}
    left = [n for n in left if tot_l[n] > 0]
    right = [n for n in right if tot_r[n] > 0]
    total = sum(v for _, _, v in flows)
    if total <= 0:
        return ""
    avail = H - T - 10 - gap * (max(len(left), len(right)) - 1)
    k = avail / total

    def place(nodes, tot):
        pos, y = {}, T
        for n in nodes:
            pos[n] = [y, y]          # верх, текущее заполнение
            y += tot[n] * k + gap
        return pos

    pl, pr = place(left, tot_l), place(right, tot_r)
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img">',
           f'<text class="tick" x="{xl + NW / 2}" y="14" text-anchor="middle">{esc(left_title)}</text>',
           f'<text class="tick" x="{xr + NW / 2}" y="14" text-anchor="middle">{esc(right_title)}</text>']
    order = sorted(flows, key=lambda f: (left.index(f[0]) if f[0] in left else 99,
                                         right.index(f[1]) if f[1] in right else 99))
    for a, b, v in order:
        if v <= 0 or a not in pl or b not in pr:
            continue
        h = v * k
        y0, y1 = pl[a][1], pr[b][1]
        pl[a][1] += h
        pr[b][1] += h
        mx = (xl + NW + xr) / 2
        d = (f"M{xl + NW},{y0:.1f} C{mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {xr},{y1:.1f} "
             f"L{xr},{y1 + h:.1f} C{mx:.1f},{y1 + h:.1f} {mx:.1f},{y0 + h:.1f} {xl + NW},{y0 + h:.1f} Z")
        out.append(f'<path class="band" tabindex="0" style="fill:{seg_color(a if a in SEG_SLOT else b)}" d="{d}" '
                   f'data-tip="{esc(a)} → {esc(b)}|{esc(fnum(v))} ФЛ"/>')
    for nodes, pos, tot, x, side in ((left, pl, tot_l, xl, "l"), (right, pr, tot_r, xr, "r")):
        for n in nodes:
            y0, h = pos[n][0], tot[n] * k
            out.append(f'<rect x="{x}" y="{y0:.1f}" width="{NW}" height="{max(h, 1):.1f}" rx="2" '
                       f'style="fill:{seg_color(n)}"/>')
            tx = x - 8 if side == "l" else x + NW + 8
            anchor = "end" if side == "l" else "start"
            out.append(f'<text class="rlabel" x="{tx}" y="{y0 + h / 2 + 4:.1f}" text-anchor="{anchor}">'
                       f'{esc(n)} · {esc(fnum(tot[n]))}</text>')
    out.append("</svg>")
    return "".join(out)
