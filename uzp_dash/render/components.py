"""Переиспользуемые apple-design компоненты. Каждый возвращает HTML-строку.

Дэши собирают страницу из этих компонентов, не дублируя вёрстку.
"""
from __future__ import annotations

import html as _html
from typing import Iterable, Sequence


def esc(v) -> str:
    return _html.escape("" if v is None else str(v))


def fmt_num(v, unit: str = "", digits: int = 0) -> str:
    """Число с разрядами (тонкий пробел) и опциональной единицей."""
    if v is None:
        return "—"
    try:
        s = f"{float(v):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return esc(v)
    return f"{s} {esc(unit)}".strip()


def status_of(exec_pct: float | None) -> str:
    """good/warn/bad по проценту выполнения плана (доля, 1.0 = 100%)."""
    if exec_pct is None:
        return "warn"
    if exec_pct >= 1.0:
        return "good"
    if exec_pct >= 0.95:
        return "warn"
    return "bad"


def badge(text: str, kind: str = "good") -> str:
    return f'<span class="badge {esc(kind)}">{esc(text)}</span>'


def kpi(label: str, value: str, delta: str = "", delta_kind: str = "text-2") -> str:
    color = {
        "good": "var(--good)", "warn": "var(--warn)", "bad": "var(--bad)",
    }.get(delta_kind, "var(--text-2)")
    delta_html = f'<div class="delta" style="color:{color}">{esc(delta)}</div>' if delta else ""
    return (
        '<div class="card kpi">'
        f'<div class="label">{esc(label)}</div>'
        f'<div class="value">{value}</div>'
        f"{delta_html}"
        "</div>"
    )


def meter(exec_pct: float | None) -> str:
    kind = status_of(exec_pct)
    color = {"good": "var(--good)", "warn": "var(--warn)", "bad": "var(--bad)"}[kind]
    pct = 0 if exec_pct is None else max(0, min(exec_pct, 1.2)) * 100
    return f'<div class="meter"><span style="width:{pct:.1f}%;background:{color}"></span></div>'


def table(headers: Sequence[str], rows: Iterable[Sequence[str]], num_cols: Sequence[int] = ()) -> str:
    num = set(num_cols)
    head = "".join(
        f'<th class="{"num" if i in num else ""}">{esc(h)}</th>' for i, h in enumerate(headers)
    )
    body = []
    for r in rows:
        cells = "".join(
            f'<td class="{"num" if i in num else ""}">{c}</td>' for i, c in enumerate(r)
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def card(inner: str, cls: str = "") -> str:
    return f'<div class="card {esc(cls)}">{inner}</div>'


def section(title: str, inner: str, eyebrow: str = "") -> str:
    eb = f'<div class="eyebrow">{esc(eyebrow)}</div>' if eyebrow else ""
    return f'<section>{eb}<h2>{esc(title)}</h2>{inner}</section>'


def heat_bg(exec_pct: float | None) -> str:
    """Фон ячейки тепловой карты по выполнению плана (доля)."""
    if exec_pct is None:
        return "transparent"
    # 0.75 и ниже — насыщенный красный; 1.0+ — зелёный; между — плавно
    x = max(0.0, min((exec_pct - 0.75) / 0.30, 1.0))
    if exec_pct >= 1.0:
        return "color-mix(in srgb, var(--good) 22%, transparent)"
    r = int(60 * (1 - x)) + 20  # прозрачность краснее при меньшем x
    return f"color-mix(in srgb, var(--bad) {int(46*(1-x))+8}%, transparent)"


def heat_matrix(rows_id_label: list[tuple], seg_names: list[str], cells: dict) -> str:
    """Тепловая карта ГОСБ×сегмент.
    rows_id_label — [(row_key, label), ...]; cells[(row_key, seg)] = (exec, nedobor)."""
    head = '<th>ГОСБ</th>' + "".join(f'<th class="num">{esc(s)}</th>' for s in seg_names)
    rows = []
    for key, label in rows_id_label:
        tds = [f'<td>{esc(label)}</td>']
        for s in seg_names:
            cell = cells.get((key, s))
            if not cell or cell[0] is None:
                tds.append('<td class="num" style="color:var(--text-2)">—</td>')
            else:
                ex, ned = cell
                bg = heat_bg(ex)
                # 99.5–99.9% печатаем с десятой долей: иначе ячейка «100%» выглядит
                # выполненной, хотя план недобран (и в карточке ГОСБ она красная)
                pct = f"{ex*100:.1f}%" if 0.995 <= ex < 1 else f"{ex*100:.0f}%"
                tds.append(
                    f'<td class="num heat" style="background:{bg}" title="недобор {ned:.0f}">'
                    f'{pct}</td>'
                )
        rows.append(f'<tr>{"".join(tds)}</tr>')
    return (f'<div style="overflow-x:auto"><table class="matrix">'
            f'<thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def projection_bar(attract: float, retention: float, gap: float) -> str:
    """Как закрывается НЕДОБОР (масштаб = разрыв до плана, не весь план).
    Подписи — в легенде под полосой (сегменты бывают узкими)."""
    attract = max(0.0, attract); retention = max(0.0, retention); gap = max(gap, 1.0)
    covered = attract + retention
    scale = max(gap, covered)
    def w(x): return f"{x / scale * 100:.2f}%"

    segs = (f'<div class="proj-seg attract" style="width:{w(attract)}" title="Привлечь"></div>'
            f'<div class="proj-seg retention" style="width:{w(retention)}" title="Вернуть"></div>')
    legend = [("attract", "Привлечь", attract), ("retention", "Вернуть", retention)]
    if covered < gap:                    # разрыв закрыт не полностью
        segs += f'<div class="proj-seg rest" style="width:{w(gap-covered)}"></div>'
        legend.append(("rest", "не хватает", gap - covered))
        tail = ""
    else:                                # закрыт с запасом
        tail = f' · с запасом +{fmt_num(covered - gap)}'
    plan_pos = f"{gap / scale * 100:.2f}%"
    leg_html = "".join(
        f'<span class="lg"><i class="{cls}"></i>{esc(name)} <b>+{fmt_num(val)}</b></span>'
        if cls != "rest" else
        f'<span class="lg"><i class="{cls}"></i>{esc(name)} <b>{fmt_num(val)}</b></span>'
        for cls, name, val in legend
    )
    return (f'<div class="proj">{segs}'
            f'<div class="proj-plan" style="left:{plan_pos}"><span>план</span></div></div>'
            f'<div class="proj-legend">{leg_html}<span class="lg-note">масштаб = недобор {fmt_num(gap)} чел{tail}</span></div>')


def hbars(items: list[tuple[str, float]], unit: str = "") -> str:
    """Мини горизонтальные полоски (напр. активности по типу/статусу)."""
    if not items:
        return '<div class="sub" style="font-size:14px">нет данных</div>'
    mx = max((v for _, v in items), default=1) or 1
    rows = []
    for label, val in items:
        rows.append(
            '<div class="hbar">'
            f'<div class="hbar-l">{esc(label)}</div>'
            f'<div class="hbar-track"><span style="width:{val/mx*100:.1f}%"></span></div>'
            f'<div class="hbar-v">{fmt_num(val, unit)}</div>'
            '</div>'
        )
    return "".join(rows)


def narrative_html(text: str) -> str:
    """Лёгкий markdown → HTML: **жирный**, абзацы, подзаголовки **Х.**."""
    text = esc(text)
    text = _bold(text)
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    out = []
    for p in parts:
        if p.startswith("<b>") and p.endswith("</b>"):
            out.append(f'<h3 style="margin:18px 0 8px">{p[3:-4]}</h3>')
        else:
            out.append(f'<p style="margin:0 0 10px;color:var(--text-2);font-size:16px">{p}</p>')
    return "".join(out)


def _bold(t: str) -> str:
    import re
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)


def orgs_explorer(table_id: str, rows: list[dict], gosb_options: list[str],
                  seg_options: list[str] | None = None, page_size: int = 15) -> str:
    """Интерактивная таблица организаций: цель по плану + поиск + фильтры (ГОСБ,
    сегмент, рычаг) + пагинация. Самодостаточный инлайн-JS (работает офлайн).

    rows: [{inn, lever, gosb, seg, fl, fot, reason, action, needk}, ...]
    Фильтр «Цель»: needk — минимальная цель (1.0/1.2/1.5), при которой организация
    нужна для закрытия разрыва её сегмента; 0 — не нужна ни при какой (видна только
    при выборе «Все организации»).
    """
    import json
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    gopts = "".join(f'<option value="{esc(g)}">{esc(g)}</option>' for g in gosb_options)
    sopts = "".join(f'<option value="{esc(s)}">{esc(s)}</option>' for s in (seg_options or []))
    tid = esc(table_id)
    return f"""
<div class="filters">
  <select id="{tid}-k">
    <option value="1">Выполнить план</option>
    <option value="1.2">Перевыполнить на 20%</option>
    <option value="1.5">Перевыполнить на 50%</option>
    <option value="0">Все организации</option>
  </select>
  <input id="{tid}-q" placeholder="Поиск: номер, название, ГОСБ, сегмент, причина…">
  <select id="{tid}-g"><option value="">Все ГОСБ</option>{gopts}</select>
  <select id="{tid}-s"><option value="">Все сегменты</option>{sopts}</select>
  <select id="{tid}-l"><option value="">Все рычаги</option>
    <option value="Привлечь">Привлечь</option><option value="Вернуть">Вернуть</option></select>
</div>
<div class="tbl-scroll"><table id="{tid}-t">
  <thead><tr>
    <th>Организация</th><th>Рычаг</th><th>ГОСБ</th><th>Сегмент</th>
    <th class="num">Эффект, чел</th><th class="num">ФОТ, млн</th><th>Причина / действие</th>
  </tr></thead><tbody></tbody>
</table></div>
<div class="pager">
  <div class="info" id="{tid}-i"></div>
  <div class="btns"><button id="{tid}-p">← Назад</button><button id="{tid}-n">Вперёд →</button></div>
</div>
<script>
(function(){{
  const DATA={data};
  const PS={page_size};
  let page=0;
  const $=id=>document.getElementById("{tid}-"+id);
  const fmt=n=>Number(n).toLocaleString('ru-RU',{{maximumFractionDigits:0}});
  function filtered(){{
    const q=($('q').value||'').toLowerCase(), g=$('g').value, s=$('s').value, l=$('l').value;
    const k=parseFloat($('k').value);
    return DATA.filter(r=>{{
      // цель по плану: организация нужна, если её needk не больше выбранной цели
      if(k>0&&!(Number(r.needk)>0&&Number(r.needk)<=k))return false;
      if(g&&r.gosb!==g)return false;
      if(s&&r.seg!==s)return false;
      if(l&&r.lever!==l)return false;
      if(q){{const hay=(r.inn+' '+(r.company||'')+' '+r.gosb+' '+r.seg+' '+r.reason).toLowerCase();
        if(!hay.includes(q))return false;}}
      return true;
    }});
  }}
  function render(){{
    const rows=filtered();
    const pages=Math.max(1,Math.ceil(rows.length/PS));
    if(page>=pages)page=pages-1; if(page<0)page=0;
    const slice=rows.slice(page*PS,page*PS+PS);
    const tb=$('t').querySelector('tbody');
    tb.innerHTML=slice.map(r=>{{
      const cls=r.lever==='Привлечь'?'good':'bad';
      const act=r.action?' · <span style="color:var(--text-2)">'+esc(r.action)+'</span>':'';
      return '<tr><td><div>'+esc(r.company||('Орг. '+r.inn))+'</div>'
        +'<div style="font-size:12px;color:var(--text-2)">Орг. '+r.inn+'</div></td>'
        +'<td><span class="badge '+cls+'">'+r.lever+'</span></td>'
        +'<td>'+esc(r.gosb)+'</td><td>'+esc(r.seg)+'</td>'
        +'<td class="num">'+fmt(r.fl)+'</td><td class="num">'+fmt(r.fot)+'</td>'
        +'<td>'+esc(r.reason)+act+'</td></tr>';
    }}).join('')||'<tr><td colspan="7" style="color:var(--text-2)">Ничего не найдено</td></tr>';
    const sumFl=rows.reduce((a,r)=>a+Number(r.fl||0),0);
    $('i').textContent='Показано '+slice.length+' из '+rows.length+' орг · суммарный эффект +'
      +fmt(sumFl)+' чел · стр. '+(page+1)+'/'+pages;
    $('p').disabled=page<=0; $('n').disabled=page>=pages-1;
  }}
  function esc(s){{return String(s==null?'':s).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
  ['k','q','g','s','l'].forEach(k=>$(k).addEventListener('input',()=>{{page=0;render();}}));
  $('p').addEventListener('click',()=>{{page--;render();}});
  $('n').addEventListener('click',()=>{{page++;render();}});
  render();
}})();
</script>
"""
