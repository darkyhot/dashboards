"""Сборка apple-страницы дэша tb_health.

Отчёт двухуровневый и живёт в одном файле: вкладка СБ (единица разбора — ТБ) и по
вкладке на каждый ТБ (единица — ГОСБ). Структура уровня одна и та же: вердикт области,
из чего сложился прогноз, матрица «единица × сегмент», карточки единиц с оверлеем
разбора и — только на уровне ТБ — интерактивный список организаций к работе.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from ...registry import Context, dashboard
from ...render import components as C
from ...render import page
from ... import progress
from . import analyze, bank, prompts, segments

SEG_ORDER = segments.ORDER   # короткие названия сегментов (КСБ, РГС, …)


@dashboard("tb_health")
def build(ctx: Context) -> str:
    """Отчёт СБ → ТБ → ГОСБ одним файлом.

    Отчёт всегда строится по ВСЕМУ банку: вкладка СБ (единица разбора — ТБ) и по
    вкладке на каждый ТБ (единица — ГОСБ). Разбор одного ТБ и разбор банка нужны
    рядом, а не в разных файлах, поэтому уровни живут вкладками одного документа.

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
        progress.done("Аудит отработки отключён (llm_max_calls=0): тексты активностей "
                      "не читаем, причины берутся из правил")

    for a in preps:
        progress.step(f"═══ ТБ {a.tb_short}: разбор отработки ═══")
        analyze.finish(ctx, b, a, text_df)
        _log_llm_stats(a)

    sb = analyze.build_sb(b, preps)
    # карточка ТБ на уровне банка знает номер вкладки своего разбора — по ней и
    # устроен переход СБ → ТБ
    lvl_of = {a.tb_id: i + 1 for i, a in enumerate(preps)}
    for c in sb.gosb_cards:
        c["lvl"] = lvl_of.get(c["gosb_id"])

    # Выводы по разделам — 12 независимых вызовов LLM, они и есть основное время
    # отчёта. Считаются пачкой, при `llm_workers > 1` — параллельно.
    levels = _narratives(ctx, [sb] + preps)

    progress.step("Сборка HTML")
    bodies = "".join(
        f'<div class="lvl" id="lvl-{i}"{"" if i == 0 else " hidden"}>'
        f'{_level_body(a, story, i)}</div>'
        for i, (a, story) in enumerate(levels))
    d = sb.dates or {}
    return page(
        title="Здоровье сети — СБ и территориальные банки",
        subtitle=f"Прогноз на {C.esc(d.get('label', sb.ref_date))}",
        body=_tabs([a for a, _ in levels]) + bodies + _GD_JS + _LVL_JS,
    )


LLM_WORKERS_DEFAULT = 4          # параллельных вызовов LLM за выводами по разделам


def _narratives(ctx: Context, levels: list) -> list:
    """Выводы по разделам для всех уровней → [(Analysis, story), …] в том же порядке.

    Вызовы независимы: каждый уровень описывает сам себя, общего состояния нет. При
    последовательном прогоне 12 вызовов по ~35 с давали 7 минут на ровном месте.
    `llm_workers=1` возвращает прежнее последовательное поведение — на случай, если
    корпоративный шлюз не переносит параллельных запросов.
    """
    n = max(1, int(ctx.params.get("llm_workers", LLM_WORKERS_DEFAULT)))
    if n == 1 or len(levels) < 2:
        out = []
        for a in levels:
            progress.step(f"LLM: выводы по разделам — {a.tb_short}")
            out.append((a, prompts.section_narratives(ctx, a)))
        return out

    progress.step(f"LLM: выводы по разделам — {len(levels)} уровней, "
                  f"{n} параллельных запросов")
    with ThreadPoolExecutor(max_workers=n) as pool:
        # порядок вкладок задаёт сам список, а не порядок ответов шлюза
        stories = list(pool.map(lambda a: prompts.section_narratives(ctx, a), levels))
    return list(zip(levels, stories))


def _tabs(levels: list) -> str:
    """Панель уровней. Липкая: при длинном разборе переход наверх не нужен."""
    btns = "".join(
        f'<button class="lvl-tab{" on" if i == 0 else ""}" data-lvl="{i}" '
        f'onclick="lvlGo({i})">{C.esc(a.tb_short)}</button>'
        for i, a in enumerate(levels))
    return f'<nav class="lvls">{btns}</nav>'


def _level_body(a: analyze.Analysis, story: dict, idx: int) -> str:
    """Содержимое одного уровня. Одинаково для СБ и для ТБ — меняется единица разбора.

    `idx` уходит в id элементов: в одном документе живут 13 отчётов, и повторяющийся
    id сломал бы и оверлеи, и списки организаций (по id их находит скрипт).
    """
    back = ('<div class="lvl-back"><button onclick="lvlGo(0)">← К отчёту по банку</button>'
            f'<span>{C.esc(a.tb_full)}</span></div>' if idx else "")
    body = (
        back
        + _hero(a)
        + _kpis(a)
        + _waterfall(a, story.get("forecast"))
        + _matrix(a, story.get("matrix"))
        + _problem_gosb(a, story.get("gosb"), idx)
    )
    # список организаций живёт только на уровне ТБ: на уровне банка работают с ТБ,
    # а имена — один переход вниз (и это сотни тысяч строк, которые никто не листает)
    if a.level != "sb":
        body += _orgs(a, story.get("orgs"), idx)
    return body


# --------------------------------------------------------------------------- #
def _hero(a: analyze.Analysis) -> str:
    """Вердикт по ПРОГНОЗУ текущего месяца. Закрытый месяц — строкой ниже:
    это единственная твёрдая цифра, и по ней же считается ранг ТБ."""
    r = a.verdict["rcp"]
    d = a.dates or {}
    st = C.status_of(r["exec"])
    word = {"good": "План выполняется", "warn": "План под угрозой",
            "bad": "План не выполняется"}[st]
    # сами цифры закрытого месяца живут в KPI-карточках ниже (по каждой метрике),
    # здесь остаётся только ранг: он один на ТБ и к отдельной метрике не привязан.
    # У банка ранга нет — сравнивать не с кем, и строка про него не пишется вовсе.
    closed_txt = (f'закрытый месяц {C.esc(d.get("closed_label", ""))}' if not r["rank"]
                  else f'ранг ТБ {r["rank"]}/{r["n_tb"]} за закрытый месяц '
                       f'{C.esc(d.get("closed_label", ""))}')
    inner = (
        f'<div class="eyebrow">Прогноз по получателям на {C.esc(d.get("label", ""))}</div>'
        f'<div class="verdict">{C.esc(word)} · '
        f'<span class="big">{(r["exec"] or 0)*100:.0f}%</span> плана по прогнозу</div>'
        f'<div>{C.badge("−" + C.fmt_num(a.gap_rcp) + " получателей до плана", st)}</div>'
        + C.meter(r["exec"])
        + f'<div class="row2">ФОТ: {(a.verdict["fot"]["exec"] or 0)*100:.0f}% плана · '
          f'недобор {C.fmt_num(a.gap_fot_mln)} млн ₽</div>'
        + f'<div class="row2">{closed_txt}</div>'
    )
    return C.card(inner, cls="hero")


def _kpis(a: analyze.Analysis) -> str:
    """Две карточки по метрикам: сверху ПРОГНОЗ против плана, снизу — твёрдые факты
    ЗАКРЫТОГО месяца и прирост год к году. Разделены линией, потому что это разные по
    природе числа: прогноз может не сбыться, факт закрытого месяца — уже нет."""
    d = a.dates or {}
    closed = a.closed or {}
    yoy = a.yoy or {}

    def foot(key, scale, unit):
        cl = closed.get(key, {})
        if not cl.get("fact"):
            return ""
        head = (f'{C.esc(d.get("closed_label", ""))} закрыт: '
                f'{C.fmt_num(cl["fact"] / scale, unit)} '
                f'({(cl.get("exec") or 0) * 100:.0f}% плана)')
        y = yoy.get(key)
        if not y:
            # год к году не рассчитан — честное «—», а не молчаливый ноль
            tail = 'год к году —'
        else:
            col = "var(--good)" if y["delta"] >= 0 else "var(--bad)"
            sign = "+" if y["delta"] >= 0 else "−"
            tail = (f'год к году <b style="color:{col}">{sign}'
                    f'{C.fmt_num(abs(y["delta"]) / scale, unit)} '
                    f'({sign}{abs(y["pct"]) * 100:.1f}%)</b>')
        return f'<div class="foot">{head}<br>{tail}</div>'

    def kpi(title, v, key, scale=1.0, unit=""):
        return C.card(
            f'<div class="label">{C.esc(title)}</div>'
            f'<div class="value">{C.fmt_num(v["fact"] / scale, unit)}</div>'
            f'<div class="delta">план {C.fmt_num(v["plan"] / scale, unit)} · '
            f'<b style="color:{_col(v["exec"])}">{(v["exec"] or 0)*100:.0f}%</b></div>'
            + C.meter(v["exec"]) + foot(key, scale, unit),
            cls="kpi",
        )
    # ФОТ в БД — рубли, выводим в млн ₽ (÷ 1e6)
    cards = (kpi("Получатели (прогноз), чел", a.verdict["rcp"], "rcp")
             + kpi("Общий ФОТ (прогноз), млн ₽", a.verdict["fot"], "fot", scale=1e6))
    return f'<div class="grid cols-2">{cards}</div>'


def _wf_lines(wf: dict, d: dict, conv: float, conv_diag: dict | None = None,
              conv_is_tb: bool = False) -> str:
    """Строки водопада. Общие для блока по ТБ и для оверлея по ГОСБ — слагаемые
    и порядок одни и те же, меняется только срез данных.

    `conv` — коэффициент ИМЕННО ЭТОГО уровня (у ГОСБ свой), `conv_is_tb` — что он
    подменён коэффициентом ТБ из-за малого объёма истории.
    """
    # если фактическая конверсия ниже пола, показываем и её: иначе в отчёте стоит
    # ровно «0.20» и не отличить настоящую конверсию от сработавшей границы
    raw = (conv_diag or {}).get("tb_raw")
    if (conv_diag or {}).get("tb_clipped") and raw is not None and conv_is_tb:
        conv_txt = f'коэф. ТБ {raw:.2f} → поднят до пола {conv:.2f}, своей истории мало'
    elif conv_is_tb:
        conv_txt = f'коэф. ТБ {conv:.2f} — своей истории мало'
    else:
        conv_txt = f'коэф. {conv:.2f}'
    # Время у пайплайна меряется в КАЛЕНДАРНЫХ днях от реальной даты — показываем
    # именно дни, а не проценты: «осталось 1 из 31 дн.» читается однозначно, а «3%»
    # можно спутать с долей отыгранных выплат в строке оттока выше.
    dl, dm = int(d.get("days_left", 0)), int(d.get("days_in_month", 0) or 1)
    pipe_hint = (f'заявлено {C.fmt_num(wf.get("pipe_raw", 0))} · '
                 f'пришло {C.fmt_num(wf.get("pipe_fact", 0))} · '
                 f'остаток {C.fmt_num(wf.get("pipe_rest", 0))} × {conv_txt} × '
                 f'осталось {dl} из {dm} дн. → +{C.fmt_num(wf.get("pipe_expect", 0))}')
    rows = [
        (f'Портфель — {C.esc(d.get("closed_label", ""))} закрыт', wf["base"], 0, ""),
        (f'Ежедневный отток на {C.esc(str(d.get("act_dt", "")))}', wf["observed"], -1, ""),
        ("Риск оттока до конца месяца", wf["risk"], -1, "прогноз по истории"),
        ("Сезонный приход", wf["in_exp"], 1, "клиенты, которые обычно возвращаются"),
        ("Пайплайн на месяц", wf["pipe"], 1, pipe_hint),
    ]
    # подсказка идёт классом g-hint, а НЕ .sub: .sub — это стиль подзаголовка
    # страницы (19px), внутри строки водопада он выглядит крупнее самой строки
    return "".join(
        f'<div class="g-seg"><b>{lbl}</b> '
        f'<span style="color:{"var(--bad)" if sign < 0 else "var(--good)"}">'
        f'{"−" if sign < 0 else "+" if sign > 0 else ""}{C.fmt_num(val)}</span>'
        + (f' <span class="g-hint">· {hint}</span>' if hint else "") + '</div>'
        for lbl, val, sign, hint in rows)


def _waterfall(a: analyze.Analysis, ai: str | None = None) -> str:
    """Из чего складывается прогноз: база закрытого месяца → отток → пайплайн.

    Отток намеренно разбит на «уже не зачислились» и «риск»: первое уже случилось,
    второе — то, на что ещё можно повлиять до конца месяца.
    """
    wf = a.wf or {}
    if not wf:
        return ""
    d = a.dates or {}
    fc = a.fc_stats or {}
    body = _wf_lines(wf, d, fc.get("conv_tb", 1.0), fc.get("conv"), conv_is_tb=False)
    ex = wf.get("exec") or 0
    st = C.status_of(wf.get("exec"))
    total = (
        f'<div class="g-do">Прогноз на {C.esc(d.get("label", ""))}: '
        f'<b>{C.fmt_num(wf["forecast"])}</b> при плане {C.fmt_num(wf["plan"])} → '
        + C.badge(f"{ex * 100:.0f}% плана", st) + '</div>'
    )
    upside = ""
    if wf.get("pipe_upside", 0) >= 1:
        upside = (f'<div class="g-act">Если пайплайн отработают на 100%, прогноз '
                  f'вырастет до <b>{C.fmt_num(wf["ceiling"])}</b> '
                  f'(+{C.fmt_num(wf["pipe_upside"])} чел) — это потолок месяца.</div>')
    note = ('<p class="sub" style="font-size:14px;margin:-4px 0 12px">'
            'прогноз — пассивный сценарий «если ничего не делать»: приход из пайплайна '
            'в нём уже учтён, а потенциал привлечения и удержание — нет. '
            'Список организаций ниже показывает, чем прогноз можно улучшить.</p>')
    return C.section("Из чего складывается прогноз",
                     C.card('<h3>Расчёт по получателям</h3>' + note + body + total + upside)
                     + _ai(ai),
                     eyebrow="Метод")


def _matrix(a: analyze.Analysis, ai: str | None = None) -> str:
    m = a.matrix
    if m.empty:
        return ""
    gg = a.gosb_gap.sort_values("nedobor", ascending=False)
    present = set(m.unit_id)
    rows_id_label = [(int(r.unit_id), (r.unit_name or "")[:26])
                     for r in gg.itertuples() if int(r.unit_id) in present]
    segs = [s for s in SEG_ORDER if s in set(m.seg_name)]
    # Красная ячейка = западающий сегмент из карточек единицы: тот же порог в одного
    # получателя (недобор меньше человека — округление, показываем как выполнено).
    cells = {(int(row.unit_id), row.seg_name):
             (row.execution_percent if analyze._failing_seg(row.nedobor) else
              max(float(row.execution_percent or 0), 1.0), row.nedobor)
             for row in m.itertuples()}
    heat = C.card(
        '<h3>Прогноз выполнения плана по получателям, %</h3>'
        '<p class="sub" style="font-size:14px;margin:-4px 0 12px">'
        'красное — сильнее отстаёт от плана ТЕКУЩЕГО месяца по прогнозу</p>'
        + C.heat_matrix(rows_id_label, segs, cells))
    top = [(f'{r.unit_name} · {r.seg_name}',
            C.badge(f'{r.execution_percent*100:.0f}%', C.status_of(r.execution_percent)),
            C.fmt_num(r.nedobor), f'{r.share*100:.0f}%') for r in a.top_cells.itertuples()]
    top_tbl = C.card('<h3>Наибольший вклад в недобор</h3>'
                     + C.table(["Провальная зона", "Выполн.", "Недобор, чел", "Доля разрыва"],
                               top, num_cols=[2, 3]))
    return C.section(f"Где провал — {a.unit_label} × сегмент",
                     f'<div class="grid cols-2">{heat}{top_tbl}</div>' + _ai(ai),
                     eyebrow="Диагностика по прогнозу")


def _gap_cell(v: float) -> str:
    """Ячейка недобора. План выполнен (недобор ≤ 0) — ставим «—», а не «−0»."""
    return "—" if v <= 0.5 else "−" + C.fmt_num(v)


def _seg_badge(s: dict) -> str:
    """Бейдж сегмента: имя + выполнение, цвет по статусу — состояние не кодируется
    одним лишь цветом. При 99.5–99.9% показываем десятую долю, иначе рядом с
    недобором стояло бы «100%»."""
    fmt = "%s %.1f%%" if s["exec"] >= 0.995 else "%s %.0f%%"
    return C.badge(fmt % (s["seg"], s["exec"] * 100), C.status_of(s["exec"]))


def _gosb_table(c: dict, wide: bool = False) -> str:
    """Таблица карточки: строка «Всего» по ГОСБ + строки ВСЕХ сегментов.

    Одни и те же колонки на обоих уровнях — итог и сегменты сравниваются по вертикали.
    Выполняющие сегменты идут ниже западающих и приглушены: видно, за счёт чего ГОСБ
    вытягивает план, но взгляд по-прежнему цепляется за проблемные.
    У строки «Всего» бейджа нет — процент уже стоит крупно в шапке карточки.

    wide=True (в оверлее) добавляет колонки оттока и пайплайна — это детализация
    прогноза на грейне (ГОСБ, сегмент).
    """
    def row(label, d, cls=""):
        pipe = d.get("pipe_np", 0)
        extra = (f'<span>{_gap_cell(d.get("out_exp", 0))}</span>'
                 f'<span>{"+" + C.fmt_num(pipe) if pipe >= 1 else "—"}</span>'
                 if wide else "")
        return (f'<div class="g-row {cls}"><span>{label}</span>'
                f'<span>{C.fmt_num(d["forecast"])}</span>'
                f'<span>{C.fmt_num(d["plan"])}</span>'
                f'<span>{_gap_cell(d["nedobor"])}</span>'
                f'{extra}<span>{d.get("n_need") or "—"}</span></div>')

    cols = ('<span>отток</span><span>пайплайн</span>' if wide else "")
    head = (f'<div class="g-row head"><span>сегмент</span><span>прогноз</span>'
            f'<span>план</span><span>недобор</span>{cols}<span>орг</span></div>')
    tot = {"forecast": c["forecast"], "plan": c["plan"], "nedobor": c["gap"],
           "n_need": c["n_need"],
           "out_exp": sum(s.get("out_exp", 0) for s in c["segs"]),
           "pipe_np": sum(s.get("pipe_np", 0) for s in c["segs"])}
    rows = [row(_seg_badge(s), s, "" if s["failing"] else "ok") for s in c["segs"]]
    if not rows:
        rows.append('<div class="g-row"><span>нет данных по сегментам</span></div>')
    rest = row("прочие", c["rest"], "rest") if c.get("rest") else ""
    cls_w = " wide" if wide else ""
    return (f'<div class="g-tbl{cls_w}">{head}{row("Всего", tot, "total")}'
            f'{"".join(rows)}{rest}</div>')


def _org_rows(rows: list, key: str, tail_n: int, tail_fl: float,
              tail_txt: str, sign: int = -1, why=None,
              cover: float = 0.0, n_all: int = 0) -> str:
    """Строки именной детализации: организация · вклад · причина и что сделать.

    Названия компаний приходят из БД — обязательно через C.esc.

    Хвост не прячем, и покрытие тоже: подпись всегда говорит, сколько организаций из
    общего числа показано и какую долю блока они объясняют. На проме в блоке бывает
    несколько тысяч организаций, и 8 названных могут объяснять лишь пятую часть —
    читатель обязан это видеть, иначе примет часть за целое.
    """
    if not rows:
        return '<div class="gd-note">нет организаций с заметным вкладом</div>'
    # пояснение зависит от блока: причина оттока к пайплайну и к годовому тренду
    # отношения не имеет, поэтому текст задаётся вызывающим
    why_fn = why or (lambda r: " · ".join(x for x in (r.get("note"), r.get("action")) if x))
    out = []
    if cover and n_all > len(rows):
        out.append(f'<div class="gd-note">{len(rows)} из {C.fmt_num(n_all)} орг. — '
                   f'это {cover * 100:.0f}% блока</div>')
    for r in rows:
        mark = "−" if sign < 0 else "+"
        out.append(
            f'<div class="gd-row"><span>{C.esc(r["name"])}</span>'
            f'<span>{mark}{C.fmt_num(abs(r[key]))}</span>'
            f'<span class="gd-why">{C.esc(why_fn(r)) or "—"}</span></div>')
    if tail_n:
        out.append(f'<div class="gd-note">ещё {C.fmt_num(tail_n)} орг. на '
                   f'{C.fmt_num(tail_fl)} чел {C.esc(tail_txt)}</div>')
    return "".join(out)


def _why_out(r: dict) -> str:
    """Пояснение к строке оттока: причина, что делать, и — отдельно — можно ли вообще.

    Пометка о зоне нужна именно здесь: в списке крупнейших неизбежно окажутся
    организации, с которыми работать нельзя (нет в эталонной базе) или нечем (объективный
    отток). Без пометки читатель начнёт распределять то, что не его.

    У группы «только факт этого месяца» причины из модели нет вовсе (`note` пуст —
    истории оттока не было), и строка осталась бы без единого пояснения. Для неё
    подставляем `why` из стыковки: там сказано, что отток взят по факту дня.
    """
    parts = [x for x in (r.get("note") or r.get("why"), r.get("action")) if x]
    zone = r.get("zone")
    if zone and zone != "можно работать":
        parts.insert(0, zone)
    return " · ".join(parts)


def _why_pipe(r: dict) -> str:
    """Пояснение к строке пайплайна: сколько из заявленного дошло до прогноза."""
    return f'в прогнозе {C.fmt_num(r["pipe_adj"], "фл")} — с поправкой на реализуемость'


def _why_size(r: dict) -> str:
    """Пояснение к строке годового тренда: когда оттекали, работали ли тогда, причина.

    Про отработку говорим ровно то, что знаем: «задач не заводили» — это факт из
    выборки, «отработка неизвестна» — месяц оттока в окно воронки не попал. Подменять
    второе первым нельзя, это разные утверждения.
    """
    parts = [f'сейчас {C.fmt_num(r["cur"])} чел']
    months = r.get("out_months") or []
    if months:
        shown = ", ".join(months[:3])
        more = f" и ещё {len(months) - 3}" if len(months) > 3 else ""
        parts.append(f"отток: {shown}{more}")
        if r.get("yoy_key") == "unknown":
            parts.append("отработка за те месяцы неизвестна")
        elif r.get("yoy_tasks"):
            # задачи считаются и в самом месяце оттока, и в следующем: витрина
            # закрывает отток позже, чем он случился, и задачу заводят обоими
            parts.append(f'задач в те месяцы и следующие за ними: {r["yoy_tasks"]}')
        else:
            parts.append("задач ни в те месяцы, ни в следующие не заводили")
    elif r.get("has_hist"):
        parts.append("разовых оттоков в витрине нет")
    else:
        parts.append("истории по паре в витрине нет")
    parts.append(f'причина: {r["reason"]}' if r.get("reason") else "причина не зафиксирована")
    return " · ".join(parts)


def _out_group(g: dict, open_: bool = False) -> str:
    """Группа оттока: шапка с причиной и итогом, внутри — список организаций.

    Нативный `<details>`: клик и клавиатура работают без JS, а печать раскрывает
    содержимое сама. Шапка размечена теми же тремя колонками, что и строка
    организации, — числа групп и числа организаций стоят в одной вертикали.

    Покрытие внутри списка не подписываем: сколько организаций в группе и сколько
    в них человек, уже сказано в шапке — повторять это строкой ниже незачем.
    """
    work = (f'можно работать: {g["work_n"]} орг (−{C.fmt_num(g["work_fl"])})'
            if g["work_n"] else "работать не с кем — вне зоны влияния")
    return _group_html(g, open_, work, "out", _why_out)


def _yoy_group(g: dict, open_: bool = False) -> str:
    """Группа годового тренда. Тот же рендер, что у оттока, — меняется только то,
    что стоит в третьей строке шапки: у оттока это зона влияния, здесь — сколько
    организаций группы имеют зафиксированную причину."""
    work = (f'причина зафиксирована у {g["work_n"]} орг (−{C.fmt_num(g["work_fl"])})'
            if g["work_n"] else "причина не зафиксирована ни у одной")
    return _group_html(g, open_, work, "yoy", _why_size)


def _group_html(g: dict, open_: bool, work: str, key: str, why) -> str:
    """Группа: шапка с причиной и итогом, внутри — список организаций.

    Нативный `<details>`: клик и клавиатура работают без JS, а печать раскрывает
    содержимое сама. Шапка размечена теми же тремя колонками, что и строка
    организации, — числа групп и числа организаций стоят в одной вертикали.

    Покрытие внутри списка не подписываем: сколько организаций в группе и сколько
    в них человек, уже сказано в шапке — повторять это строкой ниже незачем.
    """
    sub = f'{g["sub"]} · {g["share"] * 100:.0f}% блока · {work}'
    return (
        f'<details class="gd-grp"{" open" if open_ else ""} '
        f'style="--share:{g["share"] * 100:.0f}%">'
        f'<summary><span class="gd-gt">{C.esc(g["title"])}'
        f'<i>{g["n"]} орг</i></span>'
        f'<span>−{C.fmt_num(g["fl"])}</span>'
        f'<span class="gd-sub">{C.esc(sub)}</span></summary>'
        f'<div class="gd-rows">'
        + _org_rows(g["rows"], key, g["tail_n"], g["tail_fl"], "— хвост", why=why)
        + '</div></details>'
    )


def _gosb_dialog(c: dict, det: dict, d: dict, uid: str, unit_label: str = "ГОСБ") -> str:
    """Оверлей «почему прогноз такой» по одному ГОСБ.

    Порядок блоков: водопад → отток по причинам → пайплайн → тренд портфеля →
    разбор по сегментам. Отток разложен на группы (см. `_out_group`) и покрыт целиком;
    в остальных блоках имена показываются только материальные, поэтому у них стоит
    подпись о покрытии.
    """
    if not det:
        return ""
    wf = det["wf"]
    ex = wf.get("exec") or 0
    # у единицы свой коэффициент реализуемости и своя доля пройденного месяца
    wf_html = _wf_lines(wf, d, det["conv"], det.get("conv_diag"),
                        det.get("conv_is_tb", False))
    yoy = det["yoy_total"]
    # отток разложен по причине: первая (крупнейшая) группа раскрыта, иначе оверлей
    # встречает читателя четырьмя закрытыми строками без единого имени
    groups = det.get("out_groups") or []
    out_html = ("".join(_out_group(g, i == 0) for i, g in enumerate(groups)) if groups
                else '<div class="gd-note">нет организаций с заметным вкладом</div>')
    n_out = det.get("out_n_all", 0)
    # в блоке только просевшие: он отвечает на «почему потеряли», и выросшие
    # организации ответа на этот вопрос не содержат
    yoy_groups = det.get("yoy_groups") or []
    yoy_html = ("".join(_yoy_group(g, i == 0) for i, g in enumerate(yoy_groups))
                if yoy_groups else
                '<div class="gd-note">просевших за год организаций нет</div>')
    yoy_head = (f'<h4>Портфель год к году: '
                f'<span style="color:{"var(--bad)" if yoy < 0 else "var(--good)"}">'
                f'{"−" if yoy < 0 else "+"}{C.fmt_num(abs(yoy))} чел</span>'
                f' · просели {C.fmt_num(det.get("yoy_n_all", 0))} орг на '
                f'−{C.fmt_num(abs(det.get("yoy_down_tot", 0)))}</h4>')
    return (
        f'<dialog class="gd" id="gd-{uid}"><div class="gd-sheet">'
        f'<div class="gd-head"><div><h3 style="margin:0">{C.esc(c["gosb_name"])}</h3>'
        f'<div class="gd-note">прогноз {C.fmt_num(wf["forecast"])} из плана '
        f'{C.fmt_num(wf["plan"])} · {ex*100:.0f}%</div></div>'
        f'<button class="gd-close" onclick="gdClose(\'{uid}\')" '
        f'aria-label="Закрыть">×</button></div>'

        f'<div class="gd-block"><h4>Из чего сложился прогноз</h4>{wf_html}</div>'

        f'<div class="gd-block"><h4>Отток по причинам — всего '
        f'{C.fmt_num(det["out_tot"])} чел, {C.fmt_num(n_out)} орг</h4>'
        + out_html
        + '</div>'

        f'<div class="gd-block"><h4>Пайплайн на месяц: заявлено '
        f'{C.fmt_num(wf["pipe_raw"])}, в прогнозе {C.fmt_num(wf["pipe"])}</h4>'
        + _org_rows(det["top_pipe"], "pipe", det["pipe_tail_n"], det["pipe_tail_fl"],
                    "— хвост", 1, _why_pipe, cover=det.get("pipe_cov", 0.0),
                    n_all=det.get("pipe_n_all", 0))
        + '</div>'

        f'<div class="gd-block">{yoy_head}{yoy_html}</div>'

        f'<div class="gd-block"><h4>Разбор по сегментам</h4>{_gosb_table(c, wide=True)}</div>'
        f'</div></dialog>'
    )


def _problem_gosb(a: analyze.Analysis, ai: str | None = None, idx: int = 0) -> str:
    """Карточки единиц уровня: ГОСБ внутри ТБ, ТБ внутри банка.

    `idx` — номер уровня в документе; из него собираются id оверлеев. В одном файле
    лежат все 13 отчётов, и без префикса id оверлея ГОСБ мог бы совпасть с id
    оверлея ТБ, а клик открывал бы чужую карточку.
    """
    if not a.gosb_cards:
        return ""
    cards, dialogs = [], []
    d = a.dates or {}
    unit = a.unit_label
    for c in a.gosb_cards:
        st = ("good" if c["healthy"] else
              "warn" if c["seg_only"] else
              "bad" if c["exec"] < 0.9 else "warn")
        seg_html = _gosb_table(c)
        # пояснения по западающим сегментам в строку таблицы не влезают — отдельно
        notes = []
        for s in c["segs_bad"]:
            cov = s["coverage"]
            if not s["n_avail"]:
                notes.append(f'в {s["seg"]} своих организаций нет — добор из других')
            elif cov is not None and cov < 0.999:
                notes.append(f'в {s["seg"]} хватает на {cov*100:.0f}% ({s["n_avail"]} орг)')
        note_html = (f'<div class="g-act">{C.esc(" · ".join(notes[:3]))}</div>'
                     if notes else "")
        act = c["act"]
        if c["healthy"]:
            head_badge = C.badge("план выполняется по всем сегментам", "good")
        elif c["seg_only"]:
            head_badge = C.badge(f'план выполняется, но западает '
                                 f'{", ".join(s["seg"] for s in c["segs_bad"][:3])}', "warn")
        else:
            head_badge = C.badge("−" + C.fmt_num(c["gap"]) + " чел до плана", st)
        filler = (f' · добор из других сегментов: <b>{c["filler_n"]}</b> орг '
                  f'(+{C.fmt_num(c["filler_fl"])})' if c["filler_n"] else "")
        cover = (c["fl_need"] / c["gap_seg"]) if c["gap_seg"] > 0 else None
        short = (f' · этого хватает лишь на <b>{cover*100:.0f}%</b> разрыва — '
                 f'потенциала в {unit} больше нет'
                 if cover is not None and cover < 0.999 else "")
        do = (
            f'<div class="g-do">Итого под план: <b>{c["n_need"]}</b> организаций '
            f'(+{C.fmt_num(c["fl_need"])} чел, привлечь {c["n_attract"]} / '
            f'вернуть {c["n_return"]}) · ФОТ <b>~{C.fmt_num(c["fot_need"])}</b> млн ₽'
            f'{filler}{short}</div>'
            f'{note_html}'
            f'<div class="g-act">Активности 3 мес: {act["act_n"]} по {act["worked_orgs"]} орг, '
            f'успех {act["success"]*100:.0f}% · из нужных под план не работали с '
            f'<b>{c["not_worked"]}</b></div>'
        )
        gid = c["gosb_id"]
        uid = f"{idx}-{gid}"           # уникален в пределах документа со всеми уровнями
        det = (a.gosb_detail or {}).get(gid)
        more = ('<div class="g-more">Почему такой прогноз →</div>' if det else "")
        # на уровне банка карточка ТБ ведёт ещё и в его собственный разбор — это и есть
        # переход СБ → ТБ; onclick останавливаем, чтобы не открылся заодно оверлей
        drill = (f'<div class="g-more g-drill" onclick="event.stopPropagation();'
                 f'lvlGo({c["lvl"]})">Открыть разбор {C.esc(c["gosb_name"])} →</div>'
                 if c.get("lvl") else "")
        inner = (
            f'<div class="g-head"><h3 style="margin:0">{C.esc(c["gosb_name"])}</h3>'
            f'<span class="g-ex" style="color:{_col(c["exec"])}">{c["exec"]*100:.0f}%</span></div>'
            + C.meter(c["exec"])
            + f'<div style="margin:2px 0 6px">{head_badge}</div>'
            + f'{seg_html}{do}{more}{drill}'
        )
        # карточка кликабельна целиком; role/tabindex — чтобы работала и с клавиатуры
        attrs = (f' role="button" tabindex="0" onclick="gdOpen(\'{uid}\')" '
                 f'onkeydown="if(event.key===\'Enter\'||event.key===\' \')'
                 f'{{event.preventDefault();gdOpen(\'{uid}\');}}"' if det else "")
        cards.append(f'<div class="card gcard {st}"{attrs}>{inner}</div>')
        if det:
            dialogs.append(_gosb_dialog(c, det, d, uid, unit))
    grid = f'<div class="gcards">{"".join(cards)}</div>{"".join(dialogs)}'
    return C.section(f"{unit} — что сделать по каждому", grid + _ai(ai),
                     eyebrow=f"Все {unit} · клик открывает разбор прогноза")


# Открытие/закрытие оверлея. Нативный <dialog>: Esc работает сам, фокус
# возвращается браузером. Клик по подложке закрываем вручную — по умолчанию не закрывает.
_GD_JS = """
<script>
function gdOpen(id){var d=document.getElementById('gd-'+id); if(d) d.showModal();}
function gdClose(id){var d=document.getElementById('gd-'+id); if(d) d.close();}
document.querySelectorAll('dialog.gd').forEach(function(d){
  d.addEventListener('click', function(e){ if(e.target===d) d.close(); });
});
</script>
"""

# Переключение уровней. Адрес страницы пишем в hash: тогда работает кнопка «назад»
# браузера и на конкретный уровень можно дать ссылку. Открытый оверлей перед
# переходом закрываем — иначе модалка одного уровня останется поверх другого.
_LVL_JS = """
<script>
function lvlGo(i){
  document.querySelectorAll('dialog.gd[open]').forEach(function(d){ d.close(); });
  document.querySelectorAll('.lvl').forEach(function(el, k){ el.hidden = (k !== i); });
  document.querySelectorAll('.lvl-tab').forEach(function(b){
    b.classList.toggle('on', Number(b.dataset.lvl) === i); });
  if (location.hash !== '#lvl-' + i) location.hash = '#lvl-' + i;
  window.scrollTo({top: 0, behavior: 'instant'});
}
function lvlFromHash(){
  var m = /^#lvl-(\\d+)$/.exec(location.hash || '');
  var i = m ? Number(m[1]) : 0;
  if (!document.getElementById('lvl-' + i)) i = 0;
  document.querySelectorAll('.lvl').forEach(function(el, k){ el.hidden = (k !== i); });
  document.querySelectorAll('.lvl-tab').forEach(function(b){
    b.classList.toggle('on', Number(b.dataset.lvl) === i); });
}
window.addEventListener('hashchange', lvlFromHash);
lvlFromHash();
</script>
"""


def _orgs(a: analyze.Analysis, ai: str | None = None, idx: int = 0) -> str:
    """Список к отработке: по умолчанию — ровно те, кем закрывается план.

    Отбор считается в Python внутри западающих сегментов каждого ГОСБ, а строке
    проставляется need_k — минимальная цель, при которой организация нужна. Поэтому
    фильтр «Цель» в HTML просто сравнивает need_k с коэффициентом и работает поверх
    остальных фильтров.

    В ФАЙЛ едут не все кандидаты: на проме их около 70 тыс., и хвост организаций по
    одному-два человека давал 20 МБ HTML, не давая ничего для работы. Порог
    `explorer_min_fl` отсекает строки мельче него — ВСЕ, включая нужные под план.

    Порог живёт ТОЛЬКО здесь, в представлении: ни отбор, ни sim, ни карточки ГОСБ от
    него не зависят, и это принципиально. Разрыв сегмента считается по прогнозу, а не
    по составу кандидатов; убери мелкие организации из отбора — сегменту искусственно
    «не хватит» своих, и включится добор из другого сегмента там, где своих хватало.

    Поэтому заголовок «N организаций закрывают план» считается по полному набору и
    заведомо больше числа показанных строк. Молчать об этом нельзя — расхождение
    подписывается явно.
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
            reason = "добор из другого сегмента · " + reason
        rows.append({
            "inn": int(r.inn), "company": (getattr(r, "company_name", "") or "")[:48],
            "lever": r.lever,
            "gosb": (r.gosb_name or "")[:28], "seg": r.seg_name or "—",
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
                      f"план {n_hidden_plan}. На отбор, n_need и покрытие порог не "
                      f"влияет — он только про видимость строк")
    all_label = (f"Все с эффектом от {C.fmt_num(min_fl)} чел" if cut
                 else "Все организации")
    # свой id на каждый уровень: в одном документе живут списки всех ТБ, и общий
    # префикс заставил бы скрипт одного уровня править таблицу другого
    explorer = C.orgs_explorer(f"work{idx}", rows, gosb_opts, seg_options=seg_opts,
                               all_label=all_label)
    sim = a.sim
    # именно segs_bad: в segs теперь лежат ВСЕ сегменты ГОСБ, включая выполняющие
    bad_segs = sorted({s["seg"] for c in a.gosb_cards for s in c["segs_bad"]},
                      key=lambda x: SEG_ORDER.index(x) if x in SEG_ORDER else 99)
    n_hold = sum(1 for r in rows if r["lever"] == "Удержать")
    hold = (f' · из них «Удержать» — <b>{n_hold}</b>: оттекают прямо сейчас'
            if n_hold else "")
    # хвост не прячем молча: сказано, сколько кандидатов есть всего, по какому правилу
    # часть из них в файл не попала и сколько среди скрытых нужных под план — иначе
    # заголовок «N закрывают план» выглядел бы расходящимся с таблицей без объяснения
    plan_hidden = (f' · из них нужных под план — {C.fmt_num(n_hidden_plan)}: '
                   f'на отбор и на карточки ГОСБ порог не влияет' if n_hidden_plan else "")
    cand = (f'в списке {C.fmt_num(len(rows))} из {C.fmt_num(n_all)} кандидатов — '
            f'остальные мельче {C.fmt_num(min_fl)} чел{plan_hidden}' if cut
            else f'всего кандидатов {C.fmt_num(len(rows))}')
    head = (f'<h3>С кем работать — {sim["k"]} организаций закрывают план</h3>'
            f'<p class="sub" style="font-size:14px;margin:-4px 0 14px">'
            f'отбор идёт внутри ЗАПАДАЮЩИХ сегментов каждого ГОСБ '
            f'({C.esc(", ".join(bad_segs)) or "—"}), по величине эффекта, пока разрыв '
            f'сегмента не закрыт · переключатель «Цель» задаёт перевыполнение · '
            f'{cand}{hold}</p>')
    return C.section("Организации к работе", C.card(head + explorer) + _ai(ai),
                     eyebrow="Список к отработке")


def _log_llm_stats(a: analyze.Analysis) -> None:
    s = a.llm_stats or {}
    if not s:
        return
    capped = s.get("capped", 0)
    tail = (f" · не влезло в бюджет ({s.get('max_calls')} выз.) → правила: {capped}"
            if capped else "")
    progress.done(
        f"Аудит отработки: пул {s.get('pool',0)} пар (эффект ≥ {s.get('min_impact',0):g}) · "
        f"чек-лист {s.get('checklist',0)} · ключевые слова {s.get('keyword',0)} · "
        f"без текста {s.get('no_text',0)} · LLM {s.get('llm',0)} "
        f"(батчей {s.get('batches',0)} по {s.get('batch')}) · фолбэк {s.get('fallback',0)}{tail}"
    )
    progress.done(
        f"Из них не требуют действий сейчас: влиять нечем {s.get('no_influence',0)} · "
        f"назван будущий срок {s.get('deadline',0)}"
    )


def _ai(text: str | None) -> str:
    """Вывод LLM карточкой в конце своего раздела.

    Раньше все выводы жили одним блоком «Что делать — резюме» в конце страницы, и
    читателю приходилось возвращаться к цифрам. Теперь вывод стоит там, где стоят
    данные, к которым он относится.
    """
    if not text or not str(text).strip():
        return ""
    return C.card('<div class="ai-head">Вывод</div>'
                  + C.narrative_html(str(text)), cls="ai")


def _col(exec_pct):
    return {"good": "var(--good)", "warn": "var(--warn)", "bad": "var(--bad)"}[C.status_of(exec_pct)]
