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


def heat_matrix(rows_id_label: list[tuple], seg_names: list[str], cells: dict,
                first_col: str = "Отделение") -> str:
    """Тепловая карта «единица × сегмент».

    rows_id_label — [(row_key, label), ...]; cells[(row_key, seg)] = (exec, nedobor).
    Заголовок первой колонки задаётся: на экране банка строки — это банки, на экране
    банка — отделения, и подпись «ГОСБ» на верхнем уровне была бы неправдой.
    """
    head = (f'<th>{esc(first_col)}</th>'
            + "".join(f'<th class="num">{esc(s)}</th>' for s in seg_names))
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
                    f'<td class="num heat" style="background:{bg}" title="не хватает {ned:.0f} чел">'
                    f'{pct}</td>'
                )
        rows.append(f'<tr>{"".join(tds)}</tr>')
    return (f'<div style="overflow-x:auto"><table class="matrix">'
            f'<thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def crumbs(items: list[tuple[str, str | None]]) -> str:
    """Путь «Банк › ЮЗБ»: где читатель сейчас и как вернуться выше.

    Элемент с ссылкой (второй элемент кортежа — JS-переход) кликабелен, последний
    без ссылки — текущее место. Заменяет прежний ряд вкладок: вкладки показывали
    список уровней, но не показывали, что уровни вложены друг в друга.
    """
    out = []
    for i, (label, go) in enumerate(items):
        if i:
            out.append('<span class="crumb-sep">›</span>')
        if go:
            out.append(f'<button class="crumb" onclick="{go}">{esc(label)}</button>')
        else:
            out.append(f'<span class="crumb on">{esc(label)}</span>')
    return f'<nav class="crumbs">{"".join(out)}</nav>'


def slide(sid: str, question: str, body: str, hint: str = "",
          foot: str = "", cls: str = "") -> str:
    """Слайд — РОВНО один экран: шапка, прокручиваемое тело, подвал с кнопками.

    Высоту задаёт CSS (100vh + scroll-snap), поэтому здесь важна только структура:
    заголовок-вопрос и подвал закреплены, прокручивается только `.slide-body`.
    Иначе длинный разбор отделения растянул бы слайд и сломал листание.

    `sid` — якорь: по нему кнопки рейтинга листают к нужному слайду.
    """
    h = f'<p class="slide-hint">{hint}</p>' if hint else ""
    f = f'<div class="slide-foot">{foot}</div>' if foot else ""
    return (f'<section class="slide {esc(cls)}" id="{esc(sid)}">'
            f'<div class="slide-head"><h2 class="slide-q">{esc(question)}</h2>{h}</div>'
            f'<div class="slide-body">{body}</div>{f}</section>')


def big_stat(value: str, kind: str, caption: str, sub: str = "") -> str:
    """Одно главное число слайда: крупно, цветом статуса, с фразой под ним.

    Цвет никогда не единственный носитель смысла — рядом всегда стоит слово
    («выполняется» / «не выполняется»), иначе на печати и при дальтонизме
    показатель нечитаем.
    """
    color = {"good": "var(--good)", "warn": "var(--warn)", "bad": "var(--bad)"}.get(
        kind, "var(--text)")
    sub_html = f'<div class="stat-sub">{sub}</div>' if sub else ""
    return (f'<div class="stat"><div class="stat-v" style="color:{color}">{value}</div>'
            f'<div class="stat-c">{caption}</div>{sub_html}</div>')


def hero_pair(left: dict, right: dict) -> str:
    """Два ГЛАВНЫХ числа экрана одного кегля: процент плана и сколько не хватает.

    Раньше «не хватает» стояло мелкой подписью под процентом, и руководитель видел
    только процент. Это два ответа на один вопрос: «насколько» и «сколько людей», и
    по важности они равны — значит и по размеру тоже.

    Каждый элемент: {value, kind, caption, sub}.
    """
    def one(d):
        color = {"good": "var(--good)", "warn": "var(--warn)",
                 "bad": "var(--bad)"}.get(d.get("kind", ""), "var(--text)")
        sub = f'<div class="hero-sub">{d["sub"]}</div>' if d.get("sub") else ""
        return (f'<div class="hero-one">'
                f'<div class="hero-v" style="color:{color}">{d["value"]}</div>'
                f'<div class="hero-c">{d["caption"]}</div>{sub}</div>')
    return f'<div class="hero2">{one(left)}{one(right)}</div>'


def dots(items: list[tuple[str, str]]) -> str:
    """Точки-индикатор справа: сколько всего слайдов и где читатель сейчас.

    Заодно оглавление: клик листает к слайду, подпись всплывает при наведении.
    Без индикатора при жёстком листании непонятно, кончилась ли колода.
    """
    li = "".join(f'<button class="dot" data-for="{esc(sid)}" title="{esc(title)}" '
                 f'onclick="slideGo(\'{esc(sid)}\')"><i></i></button>'
                 for sid, title in items)
    return f'<nav class="dots">{li}</nav>'


def stat_row(items: list[dict]) -> str:
    """Несколько равнозначных чисел в ряд: {value, caption, sub, kind}.

    Ряд, а не таблица: числа независимы друг от друга, и таблица навязала бы им
    несуществующую арифметику (сложить их нельзя — см. блок портфеля).
    """
    cells = "".join(
        big_stat(i["value"], i.get("kind", ""), i.get("caption", ""), i.get("sub", ""))
        for i in items)
    return f'<div class="stats">{cells}</div>'


def rank_row(name: str, sub: str, pct: float | None, kind: str, right: str,
             action: str = "") -> str:
    """Строка рейтинга единиц: имя · полоса · процент · чего не хватает · действие.

    Рейтинг, а не сетка карточек: руководителю нужен порядок «кто хуже», а сетка
    заставляет сравнивать карточки глазами. Порядок строк задаёт вызывающий.
    """
    color = {"good": "var(--good)", "warn": "var(--warn)", "bad": "var(--bad)"}.get(
        kind, "var(--text-2)")
    w = 0 if pct is None else max(0.0, min(pct, 1.2)) * 100 / 1.2
    p = "—" if pct is None else f"{pct * 100:.0f}%"
    return (
        f'<div class="rk-name">{esc(name)}'
        + (f'<span class="rk-sub">{esc(sub)}</span>' if sub else "") + '</div>'
        f'<div class="rk-bar"><span style="width:{w:.1f}%;background:{color}"></span></div>'
        f'<div class="rk-pct" style="color:{color}">{p}</div>'
        f'<div class="rk-right">{right}</div>'
        f'<div class="rk-act">{action}</div>'
    )


def disclosure(summary: str, body: str, label: str = "Показать разбор",
               cls: str = "") -> str:
    """Раскрывающийся блок, о котором видно, что он раскрывается.

    Нативный `<details>`: работает без JS, раскрывается при печати, доступен с
    клавиатуры. Подпись со стрелкой обязательна и меняется на «Скрыть» —
    треугольник сам по себе замечают не все.
    """
    return (f'<details class="disc {esc(cls)}"><summary>{summary}'
            f'<span class="disc-tog" data-open="Скрыть разбор">{esc(label)}</span>'
            f'</summary><div class="disc-body">{body}</div></details>')


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


# поля, которые повторяются из строки в строку: их выносим в словари
# (первые буквы имён различны — они же ключи словаря в payload)
_PACK_DICT = ("lever", "gosb", "seg", "reason", "action", "emp")


def _pack(rows: list[dict]) -> str:
    """Строки таблицы → JS-выражение, которое разворачивается в те же объекты.

    Повторяющиеся строковые поля выносятся в словари, а сама строка становится
    массивом индексов и чисел: имена ключей перестают повторяться в каждой записи.
    `company` в словарь не идёт — уникальных названий почти столько же, сколько
    строк, и словарь там только добавил бы индексы.

    `needk` кодируется индексом в списке значений: их всего три-четыре, зато
    исчезает риск, что 1.2 приедет в браузер с другим хвостом float.

    ФИО закреплённого сотрудника (`emp`) идёт именно СЛОВАРЁМ: сотрудников тысячи,
    а строк на проме десятки тысяч — повтор имени в каждой строке раздул бы файл.
    """
    import json

    def js(v):
        return json.dumps(v, ensure_ascii=False).replace("</", "<\\/")

    if not rows:
        return "[]"
    dicts = {c: [] for c in _PACK_DICT}
    idx = {c: {} for c in _PACK_DICT}
    for c in _PACK_DICT:
        for r in rows:
            v = r.get(c) or ""
            if v not in idx[c]:
                idx[c][v] = len(dicts[c])
                dicts[c].append(v)
    ks = sorted({float(r.get("needk") or 0.0) for r in rows})
    kidx = {v: i for i, v in enumerate(ks)}
    packed = [[r["inn"], r.get("company") or "", idx["lever"][r.get("lever") or ""],
               idx["gosb"][r.get("gosb") or ""], idx["seg"][r.get("seg") or ""],
               r["fl"], r["fot"], idx["reason"][r.get("reason") or ""],
               idx["action"][r.get("action") or ""],
               kidx[float(r.get("needk") or 0.0)],
               idx["emp"][r.get("emp") or ""]] for r in rows]
    d = ",".join(f'{c[0]}:{js(dicts[c])}' for c in _PACK_DICT)
    return (f'(function(){{var D={{{d}}},K={js(ks)},R={js(packed)};'
            f'return R.map(function(x){{return {{inn:x[0],company:x[1],'
            f'lever:D.l[x[2]],gosb:D.g[x[3]],seg:D.s[x[4]],fl:x[5],fot:x[6],'
            f'reason:D.r[x[7]],action:D.a[x[8]],needk:K[x[9]],'
            f'emp:D.e[x[10]]}};}});}})()')


def orgs_explorer(table_id: str, rows: list[dict], gosb_options: list[str],
                  seg_options: list[str] | None = None, page_size: int = 15,
                  all_label: str = "Все организации") -> str:
    """Интерактивная таблица организаций: цель по плану + поиск + фильтры (ГОСБ,
    сегмент, рычаг) + пагинация. Самодостаточный инлайн-JS (работает офлайн).

    rows: [{inn, lever, gosb, seg, emp, fl, fot, reason, action, needk}, ...]
    emp — ФИО закреплённого за организацией сотрудника (пусто → прочерк).
    Фильтр «Цель»: needk — минимальная цель (1.0/1.2/1.5), при которой организация
    нужна для закрытия разрыва её сегмента; 0 — не нужна ни при какой (видна только
    при выборе «все с эффектом от порога»).

    Строки уезжают в страницу СЛОВАРНОЙ УПАКОВКОЙ, а не списком JSON-объектов: на
    проме их десятки тысяч, и объектная запись раздувала файл до 20 МБ. В каждой
    строке повторялись имена ключей (треть объёма) и одни и те же значения —
    название ГОСБ переписывалось целиком, хотя ГОСБ полтора десятка. Замер: −60%.
    JS распаковывает payload обратно в ТЕ ЖЕ объекты, поэтому фильтрация, поиск и
    отрисовка ниже не знают об упаковке вовсе.
    """
    data = _pack(rows)
    gopts = "".join(f'<option value="{esc(g)}">{esc(g)}</option>' for g in gosb_options)
    sopts = "".join(f'<option value="{esc(s)}">{esc(s)}</option>' for s in (seg_options or []))
    tid = esc(table_id)
    return f"""
<div class="filters">
  <select id="{tid}-k">
    <option value="1">Выполнить план</option>
    <option value="1.2">Перевыполнить на 20%</option>
    <option value="1.5">Перевыполнить на 50%</option>
    <option value="0">{esc(all_label)}</option>
  </select>
  <input id="{tid}-q" placeholder="Поиск: номер, название, ГОСБ, сотрудник, сегмент, причина…">
  <select id="{tid}-g"><option value="">Все ГОСБ</option>{gopts}</select>
  <select id="{tid}-s"><option value="">Все сегменты</option>{sopts}</select>
  <select id="{tid}-l"><option value="">Все рычаги</option>
    <option value="Привлечь">Привлечь</option><option value="Вернуть">Вернуть</option></select>
</div>
<div class="tbl-scroll"><table id="{tid}-t">
  <thead><tr>
    <th>Организация</th><th>Рычаг</th><th>ГОСБ</th><th>Сотрудник</th><th>Сегмент</th>
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
      if(q){{const hay=(r.inn+' '+(r.company||'')+' '+r.gosb+' '+(r.emp||'')+' '+r.seg+' '+r.reason).toLowerCase();
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
        +'<div style="font-size:15px;color:var(--text-2)">Орг. '+r.inn+'</div></td>'
        +'<td><span class="badge '+cls+'">'+r.lever+'</span></td>'
        +'<td>'+esc(r.gosb)+'</td><td>'+esc(r.emp||'—')+'</td><td>'+esc(r.seg)+'</td>'
        +'<td class="num">'+fmt(r.fl)+'</td><td class="num">'+fmt(r.fot)+'</td>'
        +'<td>'+esc(r.reason)+act+'</td></tr>';
    }}).join('')||'<tr><td colspan="8" style="color:var(--text-2)">Ничего не найдено</td></tr>';
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
