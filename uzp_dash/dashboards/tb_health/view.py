"""Сборка страницы дэша tb_health — отчёт-презентация для руководителя.

Читатель — топ-менеджер, у которого один вопрос: почему не выполняется план по
портфелю и кто конкретно его не выполняет. Поэтому страница устроена не как
аналитическая панель, а как презентация: один экран на уровень, внутри —
пронумерованные слайды, на каждом ОДИН вопрос и ответ на него.

Путь сверху вниз: банк → территориальный банк → отделение → организация.
  * экран «Банк»: вердикт → портфель → рейтинг ТБ (кнопка ведёт на его экран) →
    сегменты;
  * экран ТБ: то же самое про свои отделения, каждое отделение раскрывается прямо
    в ленте, а последний слайд — поимённый список организаций.
Где читатель находится, показывают крошки вверху; ими же он возвращается наверх.
"""
from __future__ import annotations

import pandas as pd

from ...registry import Context, dashboard
from ...render import components as C
from ...render import page
from ... import progress
from . import analyze, bank, prompts, segments

SEG_ORDER = segments.ORDER   # короткие названия сегментов (КСБ, РГС, …)


@dashboard("tb_health")
def build(ctx: Context) -> str:
    """Отчёт «банк → территориальный банк → отделение → клиент» одним файлом.

    Порядок шагов задан ценой запросов, а не удобством чтения: сначала ОДИН проход в
    БД по всему банку (`bank.load`), затем все уровни считаются без БД (`prepare`),
    затем ОДИН запрос текстов активностей сразу по всем аудиторским пулам, и только
    потом идёт LLM. Раньше каждый из 13 уровней ходил в БД сам, и одни и те же
    таблицы читались по 12 раз.
    """
    prompts.reset_gateway()
    b = bank.load(ctx)

    levels_tb = [(int(r.tb_id), str(r.tb_short_name), str(r.tb_full_name))
                 for r in b.tbs.itertuples()]
    preps = []
    for i, (tb_id, short, full) in enumerate(levels_tb, start=1):
        progress.step(f"═══ ТБ {short} ({i} из {len(levels_tb)}) ═══")
        preps.append(analyze.prepare(b, tb_id, short, full))

    # Тексты активностей нужны ТОЛЬКО аудиту. При выключенном аудите этот запрос —
    # десятки секунд и миллионы строк впустую, поэтому его просто не делаем.
    if analyze.audit_enabled(ctx):
        text_df = bank.audit_texts(ctx.engine, b, analyze.audit_inns(preps))
    else:
        text_df = pd.DataFrame()
        progress.done("Разбор причин отключён (llm_max_calls=0): тексты активностей "
                      "не читаем, причины берутся из правил")

    for a in preps:
        progress.step(f"═══ ТБ {a.tb_short}: разбор работы с клиентами ═══")
        analyze.finish(ctx, b, a, text_df)
        _log_llm_stats(a)

    sb = analyze.build_sb(b, preps)
    # строка ТБ на экране банка знает номер экрана своего разбора — по ней и устроен
    # переход вниз
    lvl_of = {a.tb_id: i + 1 for i, a in enumerate(preps)}
    for c in sb.gosb_cards:
        c["lvl"] = lvl_of.get(c["gosb_id"])

    # Выводы по разделам — по вызову LLM на уровень, и это основное время отчёта.
    # ПОСЛЕДОВАТЕЛЬНО: параллельный вариант пробовали, корпоративный шлюз отвечает на
    # него 429 и после ретраев роняет запрос — из 12 уровней доходил один, остальные
    # получали фолбэк вместо выводов модели.
    levels = []
    for a in [sb] + preps:
        progress.step(f"LLM: выводы по разделам — {a.tb_short}")
        levels.append((a, prompts.section_narratives(ctx, a)))

    progress.step("Сборка HTML")
    names = [a.tb_short for a, _ in levels]
    bodies = "".join(
        f'<div class="lvl" id="lvl-{i}"{"" if i == 0 else " hidden"}>'
        f'{_level_body(a, story, i, names)}</div>'
        for i, (a, story) in enumerate(levels))
    d = sb.dates or {}
    return page(
        title=f"Портфель зарплатных клиентов — прогноз на "
              f"{C.esc(d.get('label', sb.ref_date))}",
        subtitle="",
        body=bodies + _LVL_JS,
        chrome=False,          # заголовок живёт в закреплённой полосе слайдов
    )


# --------------------------------------------------------------------------- #
def _level_body(a: analyze.Analysis, story: dict, idx: int, names: list) -> str:
    """Один экран = колода слайдов. Слайд занимает ровно окно, скролл защёлкивается.

    Колода: состояние (вердикт + портфель) → кто отстаёт и в каких сегментах →
    по слайду на каждое отделение → список организаций. На уровне банка последних
    двух нет: оттуда идут не в отделение, а на экран самого ТБ (кнопка в строке
    рейтинга), а имена — ещё шагом ниже.

    Внутри слайда прокручивается только тело: заголовок и кнопки закреплены, иначе
    длинный разбор отделения растянул бы слайд и листание сломалось бы.
    """
    is_sb = a.level == "sb"
    crumbs = C.crumbs([("Банк", None if is_sb else "lvlGo(0)")]
                      + ([] if is_sb else [(a.tb_short, None)]))
    p = f"l{idx}"                      # префикс якорей: экранов в файле двенадцать
    deck = [(f"{p}-state", "Выполнение плана"), (f"{p}-where", "Кто отстаёт")]
    units = [] if is_sb else [c for c in a.gosb_cards
                              if (a.gosb_detail or {}).get(c["gosb_id"])]
    deck += [(f"{p}-u{c['gosb_id']}", c["gosb_name"]) for c in units]
    if not is_sb:
        deck.append((f"{p}-orgs", "С кем работать"))

    parts = [_slide_state(a, f"{p}-state", story.get("forecast")),
             _slide_where(a, f"{p}-where", p, story.get("gosb"), story.get("matrix"))]
    parts += [_slide_unit(a, c, (a.gosb_detail or {})[c["gosb_id"]], p) for c in units]
    if not is_sb:
        parts.append(_slide_orgs(a, f"{p}-orgs", idx, story.get("orgs")))
    d = a.dates or {}
    where = (f'<span class="topbar-name">{C.esc(a.tb_full)}</span>'
             f'<span class="topbar-when">прогноз на конец '
             f'{C.esc(d.get("label", ""))}</span>')
    return (f'<div class="topbar">{crumbs}{where}{_picker(names, idx)}</div>'
            + C.dots(deck) + "".join(parts))


def _picker(names: list, idx: int) -> str:
    """Выбор другого банка — чтобы не возвращаться наверх ради соседнего ТБ."""
    opts = "".join(f'<option value="{i}"{" selected" if i == idx else ""}>'
                   f'{C.esc(n if i else "Весь банк")}</option>'
                   for i, n in enumerate(names))
    return (f'<div class="picker"><label for="pick{idx}">Смотреть:</label>'
            f'<select id="pick{idx}" onchange="lvlGo(Number(this.value))">{opts}'
            f'</select></div>')


# --------------------------------------------------------------------------- #
def _slide_state(a: analyze.Analysis, sid: str, ai: str | None = None) -> str:
    """Слайд 1. Выполним ли план и что происходит с портфелем — на одном экране.

    Два ГЛАВНЫХ числа одного кегля: процент плана и сколько людей не хватает. Это
    два ответа на один вопрос, и по важности они равны — руководителю нужны оба.
    Ниже — зарплатный фонд строкой и три числа портфеля: что защищаем, что уже
    потеряли, что придёт из сделок. Складывать их и сверять с прогнозом
    бессмысленно: прогноз приходит готовым из витрины.
    """
    r = a.verdict["rcp"]
    d = a.dates or {}
    st = C.status_of(r["exec"])
    word = {"good": "План выполняется", "warn": "План под угрозой",
            "bad": "План не выполняется"}[st]
    gap = a.gap_rcp
    hero = C.hero_pair(
        {"value": f'{(r["exec"] or 0) * 100:.0f}%', "kind": st,
         "caption": word,
         "sub": f'ожидаем {C.fmt_num(r["fact"])} человек при плане '
                f'{C.fmt_num(r["plan"])}'},
        {"value": ("−" + C.fmt_num(gap)) if gap >= 1 else "0",
         "kind": "bad" if gap >= 1 else "good",
         "caption": "человек не хватает до плана" if gap >= 1 else "план закрыт",
         "sub": (f'зарплатный фонд: {(a.verdict["fot"]["exec"] or 0) * 100:.0f}% плана, '
                 + (f'не хватает {C.fmt_num(a.gap_fot_mln)} млн ₽'
                    if a.gap_fot_mln >= 1 else 'план закрыт'))})

    pf = a.pf or {}
    out_lbl = d.get("out_label", "")
    stats = C.stat_row([
        {"value": C.fmt_num(pf.get("base", 0)), "kind": "",
         "caption": f'Было на конец {C.esc(d.get("closed_label", ""))}',
         "sub": "Портфель, который защищаем"},
        {"value": "−" + C.fmt_num(pf.get("out_kept", 0)), "kind": "bad",
         "caption": "Ушли за три месяца и не вернулись",
         "sub": f'{C.esc(out_lbl)}, уходы от {bank.OUT_MIN_QTY} человек'
                if out_lbl else ""},
        {"value": "+" + C.fmt_num(pf.get("pipe", 0)), "kind": "good",
         "caption": "Придёт из сделок в работе",
         "sub": f'обещано {C.fmt_num(pf.get("pipe_raw", 0))}'},
    ]) if pf else ""

    closed = a.closed or {}
    cl = closed.get("rcp", {})
    facts = []
    if cl.get("fact"):
        facts.append(f'{C.esc(d.get("closed_label", ""))} уже закрыт: '
                     f'{C.fmt_num(cl["fact"])} человек, '
                     f'{(cl.get("exec") or 0) * 100:.0f}% плана')
    y = (a.yoy or {}).get("rcp")
    if y:
        sign = "больше" if y["delta"] >= 0 else "меньше"
        col = "var(--good)" if y["delta"] >= 0 else "var(--bad)"
        facts.append(f'Год назад было {C.fmt_num(y["fact"])} — сейчас на '
                     f'<b style="color:{col}">{C.fmt_num(abs(y["delta"]))}</b> {sign}')
    if r["rank"]:
        facts.append(f'Место среди банков за закрытый месяц: '
                     f'<b>{r["rank"]} из {r["n_tb"]}</b>')
    if pf.get("pipe_upside", 0) >= 1:
        facts.append(f'Если сделки закроют как обещали, придёт ещё '
                     f'<b style="color:var(--good)">+{C.fmt_num(pf["pipe_upside"])}</b> '
                     f'человек')
    facts_html = ('<div class="facts">'
                  + "".join(f'<div class="fact">{f}</div>' for f in facts)
                  + '</div>') if facts else ""
    legend = ('<div class="legend">'
              '<span class="lg good">зелёное — план выполняется</span>'
              '<span class="lg warn">жёлтое — почти выполняется</span>'
              '<span class="lg bad">красное — не выполняется</span></div>')
    return C.slide(sid, "Выполним ли план в этом месяце?",
                   hero + stats + facts_html + legend + _ai(ai),
                   hint="Прогноз считает витрина банка — отчёт его не пересчитывает. "
                        "Три числа портфеля независимы и в прогноз не складываются")


def _slide_where(a: analyze.Analysis, sid: str, prefix: str,
                 ai_units: str | None = None, ai_matrix: str | None = None) -> str:
    """Слайд 2. Кто отстаёт и в каких сегментах — рейтинг и матрица на одном экране.

    Рейтинг сверху: сортировка та же, что и раньше, — сначала те, кому больше всех
    не хватает. На экране банка строка ведёт на экран своего ТБ, на экране ТБ —
    листает к слайду разбора этого отделения.

    Матрица оставлена в прежнем виде (две карточки рядом: тепловая карта и пять
    худших мест) — так её и просили вернуть.
    """
    is_sb = a.level == "sb"
    unit = a.unit_label
    rows = []
    for c in a.gosb_cards:
        st = ("good" if c["healthy"] else "warn" if c["seg_only"]
              else "bad" if c["exec"] < 0.9 else "warn")
        if c["healthy"]:
            right = '<span class="ok-txt">план выполняется</span>'
        elif c["seg_only"]:
            segs = ", ".join(s["seg"] for s in c["segs_bad"][:3])
            right = f'план в целом есть, отстают: {C.esc(segs)}'
        else:
            right = f'не хватает <b>{C.fmt_num(c["gap"])}</b> человек'
        if is_sb and c.get("lvl"):
            act = (f'<button class="go" onclick="lvlGo({c["lvl"]})">Разобрать →</button>')
        elif not is_sb and (a.gosb_detail or {}).get(c["gosb_id"]):
            act = (f'<button class="go" onclick="slideGo(\'{prefix}-u{c["gosb_id"]}\')">'
                   f'Разобрать →</button>')
        else:
            act = ""
        rows.append(f'<div class="rk-row {st}">'
                    + C.rank_row(c["gosb_name"], "", c["exec"], st, right, act)
                    + '</div>')
    rank = f'<div class="rk">{"".join(rows)}</div>'

    m = a.matrix
    matrix = ""
    if not m.empty:
        gg = a.gosb_gap.sort_values("nedobor", ascending=False)
        present = set(m.unit_id)
        rows_id_label = [(int(r.unit_id), (r.unit_name or "")[:26])
                         for r in gg.itertuples() if int(r.unit_id) in present]
        segs = [s for s in SEG_ORDER if s in set(m.seg_name)]
        cells = {(int(row.unit_id), row.seg_name):
                 (row.execution_percent if analyze._failing_seg(row.nedobor) else
                  max(float(row.execution_percent or 0), 1.0), row.nedobor)
                 for row in m.itertuples()}
        heat = C.card('<h3>Выполнение плана по портфелю, %</h3>'
                      '<p class="card-sub">красное — дальше всего до плана этого '
                      'месяца по прогнозу</p>'
                      + C.heat_matrix(rows_id_label, segs, cells, first_col=unit))
        top = [(f'{r.unit_name} · {r.seg_name}',
                C.badge(f'{r.execution_percent*100:.0f}%',
                        C.status_of(r.execution_percent)),
                C.fmt_num(r.nedobor), f'{r.share*100:.0f}%')
               for r in a.top_cells.itertuples()]
        top_tbl = C.card('<h3>С чего начинать: пять худших мест</h3>'
                         + C.table([f"{unit} и сегмент", "Выполнение",
                                    "Не хватает, чел", "Доля нехватки"],
                                   top, num_cols=[2, 3]))
        matrix = f'<div class="grid cols-2 matrix-pair">{heat}{top_tbl}</div>'

    hint = ("Нажмите «Разобрать», чтобы перейти к банку и увидеть его отделения"
            if is_sb else
            "Нажмите «Разобрать» — откроется экран этого отделения")
    foot = ("" if is_sb else
            f'<button class="go ghost" onclick="slideGo(\'{prefix}-orgs\')">'
            f'К списку организаций →</button>')
    return C.slide(sid,
                   "Кто не выполняет план?" if is_sb
                   else "Какие отделения отстают и в каких сегментах?",
                   rank + _ai(ai_units) + matrix + _ai(ai_matrix),
                   hint=hint, foot=foot)


def _slide_unit(a: analyze.Analysis, c: dict, det: dict, prefix: str) -> str:
    """Слайд разбора одного отделения. Содержание — как в прежнем окне разбора.

    Своим слайдом, а не раскрытием внутри списка: разбор длинный, и в чужом слайде
    он ломал листание. Здесь заголовок и кнопки закреплены, прокручивается только
    содержимое.
    """
    st = C.status_of(c["exec"])
    gap = (f'не хватает {C.fmt_num(c["gap"])} человек' if c["gap"] > 0
           else "план выполняется")
    hint = (f'{c["exec"]*100:.0f}% плана · {gap} · прогноз '
            f'{C.fmt_num(c["forecast"])} при плане {C.fmt_num(c["plan"])}')
    foot = (f'<button class="go ghost" onclick="slideGo(\'{prefix}-where\')">'
            f'← Ко всем отделениям</button>'
            f'<button class="go" onclick="slideGo(\'{prefix}-orgs\')">'
            f'К списку организаций →</button>')
    return C.slide(f'{prefix}-u{c["gosb_id"]}', c["gosb_name"],
                   _unit_detail(c, det, a.dates or {}, a.unit_label),
                   hint=hint, foot=foot, cls=f"unit {st}")


# --------------------------------------------------------------------------- #
def _unit_detail(c: dict, det: dict, d: dict, unit_label: str = "отделения") -> str:
    """Разбор одного отделения — то, что раскрывается под его строкой.

    Порядок: что с портфелем → кто ушёл и почему → что придёт из сделок → что
    изменилось за год → разбор по сегментам → что сделать. От числа к именам:
    именами число объяснить нельзя — на реальном отделении отток размазан по
    десяткам организаций, и первые пять дают лишь треть.
    """
    pf = det["pf"]
    groups = det.get("out_groups") or []
    out_html = ("".join(_out_group(g, i == 0) for i, g in enumerate(groups)) if groups
                else '<div class="gd-note">крупных уходов не было</div>')
    yoy = det["yoy_total"]
    yoy_groups = det.get("yoy_groups") or []
    yoy_html = ("".join(_yoy_group(g, i == 0) for i, g in enumerate(yoy_groups))
                if yoy_groups else
                '<div class="gd-note">за год ни одна организация не уменьшилась</div>')
    yoy_col = "var(--bad)" if yoy < 0 else "var(--good)"
    return (
        f'<div class="gd-block"><h4>Что с портфелем</h4>{_portfolio_lines(pf, d)}</div>'

        f'<div class="gd-block"><h4>Кто ушёл и почему — '
        f'{C.fmt_num(det["out_tot"])} человек из {C.fmt_num(det.get("out_n_all", 0))} '
        f'организаций</h4>{out_html}</div>'

        f'<div class="gd-block"><h4>Что придёт из сделок: обещано '
        f'{C.fmt_num(pf["pipe_raw"])}, в прогнозе {C.fmt_num(pf["pipe"])}</h4>'
        + _org_rows(det["top_pipe"], "pipe", det["pipe_tail_n"], det["pipe_tail_fl"],
                    "", 1, _why_pipe, cover=det.get("pipe_cov", 0.0),
                    n_all=det.get("pipe_n_all", 0))
        + '</div>'

        f'<div class="gd-block"><h4>Как изменился портфель за год: '
        f'<span style="color:{yoy_col}">{"−" if yoy < 0 else "+"}'
        f'{C.fmt_num(abs(yoy))} человек</span> · уменьшились '
        f'{C.fmt_num(det.get("yoy_n_all", 0))} организаций на '
        f'−{C.fmt_num(abs(det.get("yoy_down_tot", 0)))}</h4>{yoy_html}</div>'

        f'<div class="gd-block"><h4>Разбор по сегментам</h4>'
        f'{_seg_table(c, wide=True)}</div>'

        f'<div class="gd-block"><h4>Что сделать</h4>{_todo(c, unit_label)}</div>'
    )


def _portfolio_lines(pf: dict, d: dict) -> str:
    """Три строки портфеля внутри разбора отделения: было, потеряли, придёт."""
    rows = [
        (f'Было на конец {C.esc(d.get("closed_label", ""))}', pf["base"], 0, ""),
        ("Ушли за три месяца и не вернулись", pf.get("out_kept", 0), -1, ""),
        ("Придёт из сделок в работе", pf["pipe"], 1,
         f'обещано {C.fmt_num(pf.get("pipe_raw", 0))}'),
    ]
    body = "".join(
        f'<div class="g-seg"><b>{lbl}</b> '
        f'<span style="color:{"var(--bad)" if sign < 0 else "var(--good)" if sign > 0 else "var(--text)"}">'
        f'{"−" if sign < 0 else "+" if sign > 0 else ""}{C.fmt_num(val)}</span>'
        + (f' <span class="g-hint">· {hint}</span>' if hint else "") + '</div>'
        for lbl, val, sign, hint in rows)
    ex = pf.get("exec") or 0
    return body + (
        f'<div class="g-do">Прогноз на конец месяца: <b>{C.fmt_num(pf["forecast"])}</b> '
        f'при плане {C.fmt_num(pf["plan"])} — '
        + C.badge(f"{ex * 100:.0f}% плана", C.status_of(pf.get("exec"))) + '</div>')


def _todo(c: dict, unit_label: str) -> str:
    """«Что сделать» по отделению: сколько организаций и что с ними делать."""
    filler = (f' Ещё <b>{c["filler_n"]}</b> организаций берём из других сегментов '
              f'(+{C.fmt_num(c["filler_fl"])}), потому что своих не хватает.'
              if c["filler_n"] else "")
    cover = (c["fl_need"] / c["gap_seg"]) if c["gap_seg"] > 0 else None
    short = (f' Даже всех этих организаций хватит только на '
             f'<b>{cover*100:.0f}%</b> нехватки — больше в отделении взять негде.'
             if cover is not None and cover < 0.999 else "")
    act = c["act"]
    return (
        f'<p class="note">Чтобы закрыть план, нужно поработать с '
        f'<b>{c["n_need"]}</b> организациями: это +{C.fmt_num(c["fl_need"])} человек '
        f'и примерно {C.fmt_num(c["fot_need"])} млн ₽ зарплатного фонда. '
        f'Привлечь новых — {c["n_attract"]}, вернуть ушедших — {c["n_return"]}.'
        f'{filler}{short}</p>'
        f'<p class="note dim">За три месяца по отделению провели {act["act_n"]} '
        f'встреч и звонков по {act["worked_orgs"]} организациям, успешных — '
        f'{act["success"]*100:.0f}%. С <b>{c["not_worked"]}</b> нужными организациями '
        f'не работали вовсе.</p>')


def _seg_table(c: dict, wide: bool = False) -> str:
    """Таблица по сегментам: строка «Всего» и строки всех сегментов в одних колонках.

    Выполняющие сегменты идут ниже отстающих и приглушены: видно, за счёт чего
    отделение вытягивает план, но взгляд цепляется за проблемные. Строка «прочие»
    закрывает разницу до итога — таблица, которая не сходится, выглядит сломанной.
    """
    def row(label, dd, cls=""):
        pipe = dd.get("pipe_np", 0)
        extra = (f'<span>{"+" + C.fmt_num(pipe) if pipe >= 1 else "—"}</span>'
                 if wide else "")
        return (f'<div class="g-row {cls}"><span>{label}</span>'
                f'<span>{C.fmt_num(dd["forecast"])}</span>'
                f'<span>{C.fmt_num(dd["plan"])}</span>'
                f'<span>{_gap_cell(dd["nedobor"])}</span>'
                f'{extra}<span>{dd.get("n_need") or "—"}</span></div>')

    cols = ('<span>из сделок</span>' if wide else "")
    head = (f'<div class="g-row head"><span>сегмент</span><span>прогноз</span>'
            f'<span>план</span><span>не хватает</span>{cols}<span>орг.</span></div>')
    tot = {"forecast": c["forecast"], "plan": c["plan"], "nedobor": c["gap"],
           "n_need": c["n_need"],
           "pipe_np": sum(s.get("pipe_np", 0) for s in c["segs"])}
    rows = [row(_seg_badge(s), s, "" if s["failing"] else "ok") for s in c["segs"]]
    if not rows:
        rows.append('<div class="g-row"><span>по сегментам данных нет</span></div>')
    rest = row("прочие", c["rest"], "rest") if c.get("rest") else ""
    cls_w = " wide" if wide else ""
    return (f'<div class="g-tbl{cls_w}">{head}{row("Всего", tot, "total")}'
            f'{"".join(rows)}{rest}</div>')


def _gap_cell(v: float) -> str:
    """Ячейка нехватки. План выполнен — ставим «—», а не «−0»."""
    return "—" if v <= 0.5 else "−" + C.fmt_num(v)


def _seg_badge(s: dict) -> str:
    """Бейдж сегмента: имя и выполнение, цвет по статусу. При 99.5–99.9% показываем
    десятую долю, иначе рядом с нехваткой стояло бы «100%»."""
    fmt = "%s %.1f%%" if s["exec"] >= 0.995 else "%s %.0f%%"
    return C.badge(fmt % (s["seg"], s["exec"] * 100), C.status_of(s["exec"]))


# --------------------------------------------------------------------------- #
def _org_rows(rows: list, key: str, tail_n: int, tail_fl: float,
              tail_txt: str, sign: int = -1, why=None,
              cover: float = 0.0, n_all: int = 0, with_emp: bool = False) -> str:
    """Строки поимённой детализации: организация · сколько человек · что произошло.

    Названия компаний приходят из БД — обязательно через C.esc. Оттуда же ФИО
    закреплённого сотрудника (`emp`).

    `with_emp` — показывать ли ФИО. Флагом, а не «есть ли поле в строке»: строками
    одного и того же кадра живут два блока (уход людей и сделки), поле несут оба, а
    подпись нужна только первому.

    Хвост не прячем, и покрытие тоже: подпись всегда говорит, сколько организаций из
    общего числа показано и какую долю они объясняют. На проме в блоке бывает
    несколько тысяч организаций, и 8 названных могут объяснять лишь пятую часть —
    читатель обязан это видеть, иначе примет часть за целое.
    """
    if not rows:
        return '<div class="gd-note">крупных организаций здесь нет</div>'
    why_fn = why or (lambda r: " · ".join(x for x in (r.get("note"), r.get("action")) if x))
    out = []
    if cover and n_all > len(rows):
        out.append(f'<div class="gd-note">Показаны {len(rows)} организаций из '
                   f'{C.fmt_num(n_all)} — это {cover * 100:.0f}% всей цифры</div>')
    for r in rows:
        mark = "−" if sign < 0 else "+"
        emp = str(r.get("emp") or "").strip() if with_emp else ""
        # span, а не div: ячейка сетки — сам <span>, а блочный элемент внутри
        # фразового делает разметку невалидной. Перевод строки даёт CSS
        sub = f'<span class="gd-emp">Ведёт: {C.esc(emp)}</span>' if emp else ""
        out.append(
            f'<div class="gd-row"><span>{C.esc(r["name"])}{sub}</span>'
            f'<span>{mark}{C.fmt_num(abs(r[key]))}</span>'
            f'<span class="gd-why">{C.esc(why_fn(r)) or "—"}</span></div>')
    if tail_n:
        out.append(f'<div class="gd-note">И ещё {C.fmt_num(tail_n)} организаций — '
                   f'вместе {C.fmt_num(tail_fl)} человек{C.esc(tail_txt)}</div>')
    return "".join(out)


def _why_out(r: dict) -> str:
    """Что написано у строки ушедшей организации: вернулись ли, что делали, что дальше.

    Пометка о том, что работать нельзя, стоит первой: в списке крупнейших неизбежно
    окажутся организации не за нами или те, где уже всё сделали. Без пометки читатель
    начнёт раздавать поручения по чужим клиентам.
    """
    back = float(r.get("ret", 0) or 0)
    parts = []
    if back:
        parts.append(f'вернулись {C.fmt_num(back)} из {C.fmt_num(r.get("gone", 0))}')
    parts += [x for x in (r.get("reason"), r.get("action")) if x]
    zone = r.get("zone")
    if zone and zone != "можно работать":
        parts.insert(0, zone)
    return " · ".join(parts) if parts else "причина не записана"


def _why_pipe(r: dict) -> str:
    """Строка сделки: сколько из обещанного дошло до прогноза."""
    return f'в прогнозе {C.fmt_num(r["pipe_adj"], "чел")} — по опыту доходит не всё'


def _why_size(r: dict) -> str:
    """Строка годового изменения: когда уходили люди, работали ли тогда, что известно.

    Про работу с клиентом говорим ровно то, что знаем: «задач не заводили» — это факт
    из выборки, «данных нет» — месяц ухода не попал в окно. Подменять второе первым
    нельзя, это разные утверждения.
    """
    parts = [f'сейчас {C.fmt_num(r["cur"])} человек']
    months = r.get("out_months") or []
    if months:
        shown = ", ".join(months[:3])
        more = f" и ещё {len(months) - 3}" if len(months) > 3 else ""
        parts.append(f"люди уходили: {shown}{more}")
        if r.get("yoy_key") == "unknown":
            parts.append("что делали в те месяцы — в системе не осталось")
        elif r.get("yoy_tasks"):
            # задачи считаются и в самом месяце ухода, и в следующем: витрина
            # закрывает уход позже, чем он случился, и задачу заводят обоими
            parts.append(f'встреч и звонков тогда: {r["yoy_tasks"]}')
        else:
            parts.append("ни одной встречи и звонка тогда не было")
    else:
        parts.append("крупных уходов не было")
    parts.append(f'причина: {r["reason"]}' if r.get("reason") else "причина не записана")
    return " · ".join(parts)


def _out_group(g: dict, open_: bool = False) -> str:
    """Группа ушедших: шапка с причиной и итогом, внутри — список организаций."""
    work = (f'работать можно с {g["work_n"]} организациями (−{C.fmt_num(g["work_fl"])})'
            if g["work_n"] else "работать не с кем")
    return _group_html(g, open_, work, "out", _why_out, with_emp=True)


def _yoy_group(g: dict, open_: bool = False) -> str:
    """Группа годового изменения. Тот же вид, что у ушедших, — меняется только третья
    строка шапки: там сказано, у скольких организаций записана причина."""
    work = (f'причина записана у {g["work_n"]} организаций (−{C.fmt_num(g["work_fl"])})'
            if g["work_n"] else "причина не записана ни у одной")
    return _group_html(g, open_, work, "yoy", _why_size, with_emp=True)


def _group_html(g: dict, open_: bool, work: str, key: str, why,
                with_emp: bool = False) -> str:
    """Группа: шапка с причиной и итогом, внутри — список организаций.

    Нативный `<details>`: клик и клавиатура работают без JS, а печать раскрывает
    содержимое сама. Шапка размечена теми же тремя колонками, что и строка
    организации, — числа групп и числа организаций стоят в одной вертикали.
    """
    sub = f'{g["sub"]} · это {g["share"] * 100:.0f}% от всех ушедших · {work}'
    return (
        f'<details class="gd-grp"{" open" if open_ else ""} '
        f'style="--share:{g["share"] * 100:.0f}%">'
        f'<summary><span class="gd-gt">{C.esc(g["title"])}'
        f'<i>{g["n"]} орг.</i></span>'
        f'<span>−{C.fmt_num(g["fl"])}</span>'
        f'<span class="gd-sub">{C.esc(sub)}</span></summary>'
        f'<div class="gd-rows">'
        + _org_rows(g["rows"], key, g["tail_n"], g["tail_fl"], "", why=why,
                    with_emp=with_emp)
        + '</div></details>'
    )


# --------------------------------------------------------------------------- #
def _slide_orgs(a: analyze.Analysis, sid: str, idx: int,
                ai: str | None = None) -> str:
    """Последний слайд. Поимённый список: с кем работать в первую очередь.

    По умолчанию показаны ровно те организации, которыми закрывается план. Отбор
    считается внутри отстающих сегментов каждого отделения, а строке проставляется
    need_k — минимальная цель, при которой организация нужна. Поэтому фильтр «Цель»
    в HTML просто сравнивает need_k с коэффициентом.

    В файл едут не все кандидаты: на проме их около 70 тыс., и хвост организаций по
    одному-два человека давал 20 МБ HTML. Порог `explorer_min_fl` отсекает строки
    мельче него — но ТОЛЬКО из показа: ни отбор, ни расчёт плана, ни разбор
    отделений от него не зависят.
    """
    min_fl = float(a.explorer_min_fl or 0)
    rows, n_all, n_hidden_plan = [], 0, 0
    for r in a.to_work.itertuples():
        n_all += 1
        if min_fl > 0 and float(r.impact_fl) < min_fl:
            if float(getattr(r, "need_k", 0.0)) > 0:
                n_hidden_plan += 1
            continue
        ins = a.insights.get((int(r.new_gosb_id), int(r.inn)), {})
        reason = ins.get("reason") or r.reason
        if bool(getattr(r, "filler", False)):
            reason = "берём из другого сегмента · " + reason
        rows.append({
            "inn": int(r.inn), "company": (getattr(r, "company_name", "") or "")[:48],
            "lever": r.lever,
            "gosb": (r.gosb_name or "")[:28], "seg": r.seg_name or "—",
            "emp": (r.emp_fio.strip() if isinstance(getattr(r, "emp_fio", None), str)
                    else "") or "—",
            "fl": round(float(r.impact_fl)), "fot": round(float(r.impact_fot_mln), 1),
            "reason": reason, "action": ins.get("action", ""),
            "needk": float(getattr(r, "need_k", 0.0)),
        })
    gosb_opts = sorted({row["gosb"] for row in rows})
    seg_opts = [s for s in SEG_ORDER if s in {row["seg"] for row in rows}]
    cut = min_fl > 0 and len(rows) < n_all
    if cut:
        progress.done(f"{a.tb_short}: порог списка {min_fl:.0f} чел — в файл не попали "
                      f"{n_all - len(rows)} кандидатов из {n_all}, из них нужных под "
                      f"план {n_hidden_plan}. На отбор и на разбор отделений порог не "
                      f"влияет — он только про видимость строк")
    all_label = (f"Все, где эффект от {C.fmt_num(min_fl)} человек" if cut
                 else "Все организации")
    explorer = C.orgs_explorer(f"work{idx}", rows, gosb_opts, seg_options=seg_opts,
                               all_label=all_label)
    sim = a.sim
    n_ret = sum(1 for r in rows if r["lever"] == "Вернуть")
    ret_txt = (f' Из них <b>{n_ret}</b> — вернуть тех, кто ушёл и не вернулся, а '
               f'в месяц ухода с ними не работали.' if n_ret else "")
    hidden = (f' В таблице {C.fmt_num(len(rows))} из {C.fmt_num(n_all)} — остальные '
              f'мельче {C.fmt_num(min_fl)} человек.' if cut else "")
    lead = (f'<p class="note">План закрывают <b>{sim["k"]}</b> организаций.{ret_txt}'
            f'{hidden} Ищите по названию, номеру или фамилии сотрудника.</p>')
    return C.slide(sid, "С кем работать в первую очередь?",
                   lead + explorer + _ai(ai),
                   hint="Организации отобраны внутри отстающих сегментов каждого "
                        "отделения — по величине эффекта, пока план не закрыт")


# Листание. Экран (уровень) переключается lvlGo, слайд внутри экрана — slideGo.
# Скролл защёлкивается средствами CSS (scroll-snap), JS нужен только для переходов
# по кнопкам и для подсветки точек-индикатора.
_LVL_JS = """
<script>
function lvlGo(i){
  document.querySelectorAll('.lvl').forEach(function(el, k){ el.hidden = (k !== i); });
  if (location.hash !== '#lvl-' + i) location.hash = '#lvl-' + i;
  window.scrollTo({top: 0, behavior: 'instant'});
  markDots();
}
function slideGo(id){
  var el = document.getElementById(id);
  if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'});
}
function lvlFromHash(){
  var m = /^#lvl-(\\d+)$/.exec(location.hash || '');
  var i = m ? Number(m[1]) : 0;
  if (!document.getElementById('lvl-' + i)) i = 0;
  document.querySelectorAll('.lvl').forEach(function(el, k){ el.hidden = (k !== i); });
  document.querySelectorAll('select[id^="pick"]').forEach(function(s){ s.value = i; });
  markDots();
}
// какой слайд сейчас на экране — по тому, чей верх ближе к верху окна
function markDots(){
  var lvl = document.querySelector('.lvl:not([hidden])');
  if (!lvl) return;
  var best = null, bestD = 1e9;
  lvl.querySelectorAll('.slide').forEach(function(sl){
    var d = Math.abs(sl.getBoundingClientRect().top);
    if (d < bestD) { bestD = d; best = sl.id; }
  });
  lvl.querySelectorAll('.dot').forEach(function(b){
    b.classList.toggle('on', b.dataset.for === best); });
}
window.addEventListener('hashchange', lvlFromHash);
window.addEventListener('scroll', markDots, {passive: true});
window.addEventListener('resize', markDots);
lvlFromHash();
</script>
"""


def _log_llm_stats(a: analyze.Analysis) -> None:
    s = a.llm_stats or {}
    if not s:
        return
    capped = s.get("capped", 0)
    tail = (f" · не влезло в бюджет ({s.get('max_calls')} выз.) → правила: {capped}"
            if capped else "")
    progress.done(
        f"Разбор причин: пул {s.get('pool',0)} пар (эффект ≥ {s.get('min_impact',0):g}) · "
        f"чек-лист {s.get('checklist',0)} · ключевые слова {s.get('keyword',0)} · "
        f"без текста {s.get('no_text',0)} · LLM {s.get('llm',0)} "
        f"(батчей {s.get('batches',0)} по {s.get('batch')}) · фолбэк {s.get('fallback',0)}{tail}"
    )
    progress.done(
        f"Из них не требуют действий сейчас: повлиять нельзя {s.get('no_influence',0)} · "
        f"назван будущий срок {s.get('deadline',0)}"
    )


def _ai(text: str | None) -> str:
    """Короткий вывод в конце слайда — там, где стоят данные, к которым он относится."""
    if not text or not str(text).strip():
        return ""
    return C.card('<div class="ai-head">Что это значит</div>'
                  + C.narrative_html(str(text)), cls="ai")
