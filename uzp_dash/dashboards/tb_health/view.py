"""Сборка apple-страницы дэша tb_health.

Структура ГОСБ-центричная: сверху вердикт ТБ, затем — какие ГОСБ проблемные и
что конкретно сделать по каждому, интерактивный список организаций к работе
(поиск/фильтр/пагинация), симуляция и нарратив.
"""
from __future__ import annotations

from ...registry import Context, dashboard
from ...render import components as C
from ...render import page
from ... import progress
from . import analyze, prompts, segments

SEG_ORDER = segments.ORDER   # короткие названия сегментов (КСБ, РГС, …)


@dashboard("tb_health")
def build(ctx: Context) -> str:
    tb = ctx.params.get("tb", "ЮЗБ")
    a = analyze.run(ctx, tb)          # включает разбор текста (правила + LLM)
    _log_llm_stats(a)
    progress.step("LLM: выводы по разделам")
    story = prompts.section_narratives(ctx, a)
    progress.step("Сборка HTML")

    body = (
        _hero(a)
        + _kpis(a)
        + _waterfall(a, story.get("forecast"))
        + _matrix(a, story.get("matrix"))
        + _problem_gosb(a, story.get("gosb"))
        + _orgs(a, story.get("orgs"))
    )
    d = a.dates or {}
    return page(
        title=f"Здоровье ТБ — {C.esc(a.tb_full)}",
        subtitle=f"Прогноз на {C.esc(d.get('label', a.ref_date))}",
        body=body,
    )


# --------------------------------------------------------------------------- #
def _hero(a: analyze.Analysis) -> str:
    """Вердикт по ПРОГНОЗУ текущего месяца. Закрытый месяц — строкой ниже:
    это единственная твёрдая цифра, и по ней же считается ранг ТБ."""
    r = a.verdict["rcp"]
    d = a.dates or {}
    st = C.status_of(r["exec"])
    word = {"good": "План выполняется", "warn": "План под угрозой",
            "bad": "План не выполняется"}[st]
    rank = f'{r["rank"]}/{r["n_tb"]}' if r["rank"] else "—"
    # сами цифры закрытого месяца живут в KPI-карточках ниже (по каждой метрике),
    # здесь остаётся только ранг: он один на ТБ и к отдельной метрике не привязан
    closed_txt = f'ранг ТБ {rank} за закрытый месяц {C.esc(d.get("closed_label", ""))}'
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
    present = set(m.new_gosb_id)
    rows_id_label = [(int(r.new_gosb_id), (r.gosb_name or "")[:26])
                     for r in gg.itertuples() if int(r.new_gosb_id) in present]
    segs = [s for s in SEG_ORDER if s in set(m.seg_name)]
    # Красная ячейка = западающий сегмент из карточек ГОСБ: тот же порог в одного
    # получателя (недобор меньше человека — округление, показываем как выполнено).
    cells = {(int(row.new_gosb_id), row.seg_name):
             (row.execution_percent if analyze._failing_seg(row.nedobor) else
              max(float(row.execution_percent or 0), 1.0), row.nedobor)
             for row in m.itertuples()}
    heat = C.card(
        '<h3>Прогноз выполнения плана по получателям, %</h3>'
        '<p class="sub" style="font-size:14px;margin:-4px 0 12px">'
        'красное — сильнее отстаёт от плана ТЕКУЩЕГО месяца по прогнозу</p>'
        + C.heat_matrix(rows_id_label, segs, cells))
    top = [(f'{r.gosb_name} · {r.seg_name}',
            C.badge(f'{r.execution_percent*100:.0f}%', C.status_of(r.execution_percent)),
            C.fmt_num(r.nedobor), f'{r.share*100:.0f}%') for r in a.top_cells.itertuples()]
    top_tbl = C.card('<h3>Наибольший вклад в недобор</h3>'
                     + C.table(["Провальная зона", "Выполн.", "Недобор, чел", "Доля разрыва"],
                               top, num_cols=[2, 3]))
    return C.section("Где провал — ГОСБ × сегмент",
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
    """
    parts = [x for x in (r.get("note"), r.get("action")) if x]
    zone = r.get("zone")
    if zone and zone != "можно работать":
        parts.insert(0, zone)
    return " · ".join(parts)


def _why_pipe(r: dict) -> str:
    """Пояснение к строке пайплайна: сколько из заявленного дошло до прогноза."""
    return f'в прогнозе {C.fmt_num(r["pipe_adj"])} — с поправкой на реализуемость'


def _why_size(r: dict) -> str:
    """Пояснение к строке годового тренда: текущий размер организации."""
    return f'сейчас {C.fmt_num(r["cur"])} чел'


def _gosb_dialog(c: dict, det: dict, d: dict) -> str:
    """Оверлей «почему прогноз такой» по одному ГОСБ.

    Порядок блоков: водопад → крупнейшие в оттоке → пайплайн → тренд портфеля →
    разбор по сегментам. Имена показываются только материальные (топ-5 объясняют лишь
    около трети оттока), поэтому у каждого именного блока стоит подпись о покрытии.
    """
    if not det:
        return ""
    wf = det["wf"]
    gid = c["gosb_id"]
    ex = wf.get("exec") or 0
    # у ГОСБ свой коэффициент реализуемости и своя доля пройденного месяца
    wf_html = _wf_lines(wf, d, det["conv"], det.get("conv_diag"),
                        det.get("conv_is_tb", False))
    yoy = det["yoy_total"]
    yoy_head = (f'<h4>Портфель год к году: '
                f'<span style="color:{"var(--bad)" if yoy < 0 else "var(--good)"}">'
                f'{"−" if yoy < 0 else "+"}{C.fmt_num(abs(yoy))} чел</span></h4>')
    return (
        f'<dialog class="gd" id="gd-{gid}"><div class="gd-sheet">'
        f'<div class="gd-head"><div><h3 style="margin:0">{C.esc(c["gosb_name"])}</h3>'
        f'<div class="gd-note">прогноз {C.fmt_num(wf["forecast"])} из плана '
        f'{C.fmt_num(wf["plan"])} · {ex*100:.0f}%</div></div>'
        f'<button class="gd-close" onclick="gdClose({gid})" '
        f'aria-label="Закрыть">×</button></div>'

        f'<div class="gd-block"><h4>Из чего сложился прогноз</h4>{wf_html}</div>'

        f'<div class="gd-block"><h4>Крупнейшие в оттоке — всего '
        f'{C.fmt_num(det["out_tot"])} чел</h4>'
        + _org_rows(det["top_out"], "out", det["out_tail_n"], det["out_tail_fl"],
                    "— хвост", why=_why_out, cover=det.get("out_cov", 0.0),
                    n_all=det.get("out_n_all", 0))
        + '</div>'

        f'<div class="gd-block"><h4>Пайплайн на месяц: заявлено '
        f'{C.fmt_num(wf["pipe_raw"])}, в прогнозе {C.fmt_num(wf["pipe"])}</h4>'
        + _org_rows(det["top_pipe"], "pipe", det["pipe_tail_n"], det["pipe_tail_fl"],
                    "— хвост", 1, _why_pipe, cover=det.get("pipe_cov", 0.0),
                    n_all=det.get("pipe_n_all", 0))
        + '</div>'

        f'<div class="gd-block">{yoy_head}'
        f'<div class="gd-note">просели за год:</div>'
        f'{_org_rows(det["yoy_down"], "yoy", 0, 0, "", -1, _why_size)}'
        f'<div class="gd-note" style="margin-top:10px">выросли за год:</div>'
        f'{_org_rows(det["yoy_up"], "yoy", 0, 0, "", 1, _why_size)}</div>'

        f'<div class="gd-block"><h4>Разбор по сегментам</h4>{_gosb_table(c, wide=True)}</div>'
        f'</div></dialog>'
    )


def _problem_gosb(a: analyze.Analysis, ai: str | None = None) -> str:
    if not a.gosb_cards:
        return ""
    cards, dialogs = [], []
    d = a.dates or {}
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
                 f'потенциала в ГОСБ больше нет' if cover is not None and cover < 0.999 else "")
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
        det = (a.gosb_detail or {}).get(gid)
        more = ('<div class="g-more">Почему такой прогноз →</div>' if det else "")
        inner = (
            f'<div class="g-head"><h3 style="margin:0">{C.esc(c["gosb_name"])}</h3>'
            f'<span class="g-ex" style="color:{_col(c["exec"])}">{c["exec"]*100:.0f}%</span></div>'
            + C.meter(c["exec"])
            + f'<div style="margin:2px 0 6px">{head_badge}</div>'
            + f'{seg_html}{do}{more}'
        )
        # карточка кликабельна целиком; role/tabindex — чтобы работала и с клавиатуры
        attrs = (f' role="button" tabindex="0" onclick="gdOpen({gid})" '
                 f'onkeydown="if(event.key===\'Enter\'||event.key===\' \')'
                 f'{{event.preventDefault();gdOpen({gid});}}"' if det else "")
        cards.append(f'<div class="card gcard {st}"{attrs}>{inner}</div>')
        if det:
            dialogs.append(_gosb_dialog(c, det, d))
    grid = (f'<div class="gcards">{"".join(cards)}</div>{"".join(dialogs)}'
            + _GD_JS)
    return C.section("ГОСБ — что сделать по каждому", grid + _ai(ai),
                     eyebrow="Все ГОСБ · клик открывает разбор прогноза")


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


def _orgs(a: analyze.Analysis, ai: str | None = None) -> str:
    """Список к отработке: по умолчанию — ровно те, кем закрывается план.

    Отбор считается в Python внутри западающих сегментов каждого ГОСБ, а строке
    проставляется need_k — минимальная цель, при которой организация нужна. Поэтому
    фильтр «Цель» в HTML просто сравнивает need_k с коэффициентом и работает поверх
    остальных фильтров.
    """
    rows = []
    for r in a.to_work.itertuples():
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
    explorer = C.orgs_explorer("work", rows, gosb_opts, seg_options=seg_opts)
    sim = a.sim
    # именно segs_bad: в segs теперь лежат ВСЕ сегменты ГОСБ, включая выполняющие
    bad_segs = sorted({s["seg"] for c in a.gosb_cards for s in c["segs_bad"]},
                      key=lambda x: SEG_ORDER.index(x) if x in SEG_ORDER else 99)
    n_hold = sum(1 for r in rows if r["lever"] == "Удержать")
    hold = (f' · из них «Удержать» — <b>{n_hold}</b>: оттекают прямо сейчас'
            if n_hold else "")
    head = (f'<h3>С кем работать — {sim["k"]} организаций закрывают план</h3>'
            f'<p class="sub" style="font-size:14px;margin:-4px 0 14px">'
            f'отбор идёт внутри ЗАПАДАЮЩИХ сегментов каждого ГОСБ '
            f'({C.esc(", ".join(bad_segs)) or "—"}), по величине эффекта, пока разрыв '
            f'сегмента не закрыт · переключатель «Цель» задаёт перевыполнение · '
            f'всего кандидатов {len(rows)}{hold}</p>')
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
