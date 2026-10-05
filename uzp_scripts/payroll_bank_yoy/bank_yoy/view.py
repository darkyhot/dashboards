"""HTML-отчёт: один файл, открывается без сети.

Устройство страницы:
* один ряд управления над всем (месяц сравнения) — он переключает ВСЕ помесячные
  блоки сразу, чтобы числа на странице всегда были про один месяц;
* у каждого блока — `<details>` с самодостаточным SQL, которым посчитаны его числа,
  и параметрами в шапке: запрос копируется и выполняется как есть;
* текст — по правилам из чисел (LLM в отчёте нет);
* светлая и тёмная темы — роли цветов в CSS-переменных.

Слово-идентификатор организации из трёх букв в видимом тексте не используется
(правило платформы 14: файл не пройдёт по почте); последним шагом `sanitize`
заменяет его на «Орг.», если оно всё же пришло из данных.
"""
from __future__ import annotations

import math
import re

import pandas as pd

from . import analyze as A
from . import charts as C
from . import months as M
from . import segments as S
from .charts import esc, fnum, fpct

_WORD = re.compile("(?<![А-Яа-яЁёA-Za-z])" + "И" + "НН" + "(?![А-Яа-яЁёA-Za-z])", re.IGNORECASE)


def sanitize(text: str) -> str:
    return _WORD.sub("Орг.", text or "")


# --------------------------------------------------------------------------- #
# Мелкие строители
# --------------------------------------------------------------------------- #
def _isnan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def table(head: list[str], rows: list[list], num: set[int] | None = None,
          cls: str = "", strong_rows: set[int] | None = None, sub_rows: set[int] | None = None) -> str:
    num = num if num is not None else set(range(1, len(head)))
    out = [f'<div class="scroll"><table class="data {cls}"><thead><tr>']
    out += [f'<th class="{"n" if i in num else ""}">{esc(h)}</th>' for i, h in enumerate(head)]
    out.append("</tr></thead><tbody>")
    for k, r in enumerate(rows):
        rc = " strong" if strong_rows and k in strong_rows else ""
        rc += " sub" if sub_rows and k in sub_rows else ""
        out.append(f'<tr class="{rc.strip()}">')
        for i, v in enumerate(r):
            out.append(f'<td class="{"n" if i in num else ""}">{v if isinstance(v, Raw) else esc(v)}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


class Raw(str):
    """Уже размеченный HTML внутри ячейки таблицы."""


def _params_comment(args: dict) -> str:
    lines = []
    for k, v in sorted(args.items()):
        if isinstance(v, (list, tuple)):
            if k == "months":
                v = "ARRAY[" + ", ".join(f"'{x}'" for x in v) + "]::date[]"
            else:
                v = "ARRAY[" + ", ".join(str(x) for x in v) + "]"
        elif isinstance(v, str):
            v = f"'{v}'"
        elif isinstance(v, bool):
            v = "true" if v else "false"
        lines.append(f"--   :{k} = {v}")
    return "-- Параметры (подставьте вместо :имя):\n" + "\n".join(lines) + "\n"


def sql_box(res: dict, names: list[str], title: str = "Как посчитано (SQL)") -> str:
    shown = res.get("shown", {})
    items = [(n, shown[n]) for n in names if n in shown]
    if not items:
        return ""
    body = []
    for n, (sql, args) in items:
        used = {k: v for k, v in args.items() if f":{k}" in sql}
        body.append(f'<p class="muted">Запрос «{esc(n)}»</p><pre>{esc(_params_comment(used) + sql)}</pre>')
    return f'<details class="sql"><summary>{esc(title)}</summary>{"".join(body)}</details>'


def per_month(res: dict, build) -> str:
    """Блок на каждый отчётный месяц; видим один — выбранный в ряду управления."""
    out = []
    for m in res["report"]:
        hidden = "" if m == res["report"][-1] else " hidden"
        out.append(f'<div class="pm" data-month="{m}"{hidden}>{build(m)}</div>')
    return "".join(out)


def tile(label: str, value: str, delta: str = "", note: str = "", tone: str = "") -> str:
    return (f'<div class="tile"><div class="tl">{esc(label)}</div>'
            f'<div class="tv {tone}">{esc(value)}</div>'
            f'<div class="td">{esc(delta)}</div><div class="tn">{esc(note)}</div></div>')


def _tone(v) -> str:
    return "" if _isnan(v) or v == 0 else ("up" if v > 0 else "down")


def section(sid: str, title: str, lead: str, body: str) -> str:
    return (f'<section id="{sid}"><h2>{esc(title)}</h2>'
            f'<p class="lead">{lead}</p>{body}</section>')


# --------------------------------------------------------------------------- #
# Вывод по правилам
# --------------------------------------------------------------------------- #
def _hyp(res: dict, m: str) -> dict:
    """Три гипотезы — вклад каждой в ухудшение годовой динамики месяца m."""
    did = res["did"][m]
    tab = did["table"]

    def sub(g, s):
        x = tab[(tab["group"] == g) & (tab["sub"] == s)]
        return float(x["diff"].iloc[0]) if not x.empty else 0.0

    newcomers = sub("lost", "new1") + sub("lost", "new2")
    collapse = float(tab[(tab["group"] == "inside") & tab["sub"].isna()]["diff"].sum())
    back = did["back"]
    season = None
    if back["has_next"] and not _isnan(back["cur"]) and not _isnan(back["prev"]):
        season = -(float(back["cur"]) - float(back["prev"]))
    return {"newcomers": newcomers, "collapse": collapse, "season": season,
            "inn": sub("inside", "inn"), "gosb": sub("inside", "gosb"), "total": did["expect"]}


def verdict(res: dict) -> str:
    rep = res["report"]
    yoy = res["yoy"]
    parts = []
    s = ", ".join(f"{M.name(m)} {fnum(yoy[m], True)}" for m in rep)
    parts.append(f"<p><b>Год к году по получателям:</b> {esc(s)}. Картина: <b>{esc(res['pattern'])}</b>.</p>")
    br = res.get("bridge")
    if br is not None and not br.empty:
        parts.append(f"<p><b>Следующий месяц:</b> {esc(A.august_verdict(br, res['temp_perm']))} "
                     f"Подробно — раздел «{esc(M.name(br.attrs['months']['cur']).capitalize())}: потеря или перенос».</p>")
    for i, m in enumerate(rep):
        prev = M.iso(M.shift(m, -1))
        if prev not in yoy:
            continue
        d = yoy[m] - yoy[prev]
        if d >= 0:
            continue
        did = res["did"][m]
        comp = res["comp"]
        cur, pre = comp[("cur", m)]["d"], comp[("prev", m)]["d"]
        h = _hyp(res, m)
        y1, y0 = M.parse(m).year, M.parse(m).year - 1
        txt = (f"<p><b>{esc(M.name(m).capitalize())} хуже {esc(M.gen(prev))} на {esc(fnum(-d))}.</b> "
               f"По построению это ровно разница месячных переходов: {esc(M.name(prev))}→{esc(M.name(m))} "
               f"{y1} года дал {esc(fnum(cur['delta_tr'], True))}, а год назад — "
               f"{esc(fnum(pre['delta_tr'], True))}.</p><ul>")
        txt += (f"<li><b>Растворились пришедшие:</b> {esc(fnum(h['newcomers'], True))} — "
                f"переставших получать среди пришедших за два предыдущих месяца больше, чем год назад.</li>")
        txt += (f"<li><b>Схлопнулось совместительство:</b> {esc(fnum(h['collapse'], True))} "
                f"(организаций у ФЛ {esc(fnum(h['inn'], True))}, ГОСБ в одной организации "
                f"{esc(fnum(h['gosb'], True))}).</li>")
        if h["season"] is not None:
            bk = did["back"]
            txt += (f"<li><b>Временные уходы</b> (перестали в {esc(M.prep(m))}, вернулись в "
                    f"{esc(M.prep(M.shift(m, 1)))}): {y1} — {esc(fnum(bk['cur']))} получателей "
                    f"({esc(fpct(bk['cur_share']))} переставших), {y0} — {esc(fnum(bk['prev']))} "
                    f"({esc(fpct(bk['prev_share']))}). Временных уходов в этом году "
                    f"{'больше' if h['season'] < 0 else 'меньше'} на {esc(fnum(abs(h['season'])))} — "
                    f"{'это часть ухудшения, но люди вернулись' if h['season'] < 0 else 'сезоном ухудшение не объясняется: переставшие чаще не вернулись'}.</li>")
        else:
            txt += (f"<li><b>Сезонность:</b> {esc(M.long(M.shift(m, 1)))} не загружен — возврат "
                    f"не проверить; см. повтор год к году в разделе «Сезонность».</li>")
        rest = float(did["expect"]) - h["newcomers"] - h["collapse"]
        txt += (f"<li>Остальное ({esc(fnum(rest, True))}) — старожилы и приход новых "
                f"получателей; разбивка — в разделе «Почему месяц хуже предыдущего».</li></ul>")
        parts.append(txt)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Разделы
# --------------------------------------------------------------------------- #
def s_summary(res: dict) -> str:
    rep = res["report"]
    mb = res["multi_bank"].set_index("report_dt")
    tiles = []
    for m in rep:
        p = M.iso(M.shift(m, -12))
        tr, ep = mb.loc[m, "n_triples"], mb.loc[m, "n_epk"]
        d, de = res["yoy"][m], res["yoy_epk"][m]
        dx = (tr - ep) - (mb.loc[p, "n_triples"] - mb.loc[p, "n_epk"])
        tiles.append(tile(f"{M.long(m)} к {M.long(p)}", fnum(d, True),
                          f"{fpct(d / mb.loc[p, 'n_triples'], True)} · получателей {fnum(tr)}",
                          f"ФЛ {fnum(de, True)} · совместительство {fnum(dx, True)}", _tone(d)))
    body = f'<div class="tiles">{"".join(tiles)}</div><div class="verdict">{verdict(res)}</div>'
    body += sql_box(res, ["multi_bank"])
    return section("summary", "Итог",
                   esc(A.DEF_REC) + " " + esc(A.DEF_GETS) + " Совместительство — «лишние» получатели: "
                   "получатели − ФЛ.", body)


def s_series(res: dict) -> str:
    bs = res["bank_series"]
    if bs.empty:
        return section("series", "Ряд", "Ряд не построен — см. предупреждения прогона.", "")
    last = M.parse(res["report_month"])
    cal = [M.shift(last, -11 + i) for i in range(12)]
    b = bs.set_index("report_dt")

    def val(d, col="n_triples"):
        d = M.iso(d)
        return float(b.loc[d, col]) if d in b.index and not _isnan(b.loc[d, col]) else None

    labels = [M.RU[d.month - 1] for d in cal]
    y1, y0 = last.year, last.year - 1
    # Короткие имена серий: 12 месяцев, кончающихся отчётным, и те же год назад.
    span_cur = f"{cal[0].year}–{cal[-1].year % 100:02d}"
    span_prev = f"{cal[0].year - 1}–{(cal[-1].year - 1) % 100:02d}"
    lines = C.lines(labels, [
        {"name": span_prev, "values": [val(M.shift(d, -12)) for d in cal], "color": "var(--neutral)"},
        {"name": span_cur, "values": [val(d) for d in cal], "color": "var(--series-1)", "emphasis": True}])
    yo = [val(d, "yoy") for d in cal]
    rep_idx = {i for i, d in enumerate(cal) if M.iso(d) in res["report"]}
    tips = [f"{M.long(d)}|год к году: {fnum(val(d, 'yoy'), True)} ({fpct(val(d, 'yoy_pct'), True)})"
            for d in cal]
    cols = C.columns([M.label(d) for d in cal], yo, highlight=rep_idx, tips=tips)
    pct = res["seg_yoy_pct"]
    hm = ""
    if not pct.empty:
        mcols = [c for c in pct.columns][-13:]
        hm = C.heatmap(list(pct.index), [M.label(c) for c in mcols],
                       lambda r, c: _get(pct, r, mcols[[M.label(x) for x in mcols].index(c)]),
                       lambda v: fpct(v, True), row_head="Сегмент",
                       tipf=lambda r, c, v: f"{r} · {c}|год к году {fpct(v, True)}")
    stp, measured = res["steps"], res["steps_measured"]
    st = ("Обрывов не найдено: изменение равномерное." if stp.empty else
          "Месяцы-обрывы (мерилось " + esc(measured) + "): " +
          ", ".join(f"{M.label(r.report_dt)} ({fnum(r.delta, True)})" for r in stp.itertuples()) +
          ". Обрыв в одном месяце — событие (смена кодировки, реорганизация, недогруз), а не текучесть.")
    ld = res["load"]
    load_tbl = ""
    if not ld.empty:
        rows = [[M.label(r.report_dt), fnum(r.n_rows), fpct(r.bad_inn_share), "недогружен" if r.underloaded else ""]
                for r in ld.itertuples()]
        bad = ld[ld["underloaded"]]
        warn = (f'<p class="warn">Недогруженные месяцы: {", ".join(M.label(d) for d in bad["report_dt"])}.</p>'
                if not bad.empty else "")
        load_tbl = (f'<details><summary>Полнота загрузки и пригодность номеров организаций</summary>{warn}'
                    + table(["Месяц", "Строк ведомостей", "Доля непригодных номеров", ""], rows) + "</details>")
    prof = res["mom_profile"]
    prof_html = ""
    if not prof.empty:
        p = prof.pivot_table(index="k", columns="cal", values="mom", aggfunc="sum")
        order = [M.shift(last, -11 + i).month for i in range(12)]
        rows_h = {0: f"{span_cur}", 1: f"{span_prev}"}
        prof_html = ("<h3>Сезонный профиль: изменение к предыдущему месяцу</h3>"
                     "<p class='muted'>Если август проседает к июлю оба года на похожую величину — это сезон; "
                     "отличие года — то, что надо объяснять.</p>" +
                     C.heatmap([rows_h[k] for k in (1, 0) if k in p.index],
                               [M.RU[c - 1] for c in order],
                               lambda r, c: _get(p, {v: k for k, v in rows_h.items()}[r], order[[M.RU[x - 1] for x in order].index(c)]),
                               lambda v: fnum(v, True), row_head="12 месяцев"))
    body = (f"<h3>Получатели по календарному месяцу: этот год против прошлого</h3>"
            f"<p class='muted'>{esc(span_cur)} — {esc(M.label(cal[0]))}…{esc(M.label(cal[-1]))}; "
            f"{esc(span_prev)} — те же месяцы годом раньше.</p>{lines}"
            f"<h3>Изменение год к году</h3>{cols}<p class='muted'>{st}</p>"
            f"<h3>Сегмент × месяц: изменение год к году, %</h3>{hm}{prof_html}{load_tbl}"
            + sql_box(res, [k for k in res.get("shown", {}) if k.startswith(("series_", "load_"))][:2]))
    return section("series", "Ряд и сезон",
                   "Когда началось изменение и повторяется ли оно каждый год. Ряд считается прямо по "
                   "витрине (с кэшем по месяцам) и сверяется с рабочим набором в общих месяцах.", body)


def _get(df: pd.DataFrame, r, c):
    try:
        v = df.loc[r, c]
        return None if _isnan(v) else float(v)
    except KeyError:
        return None


def s_decomp(res: dict) -> str:
    def build(m):
        cp = res["comp"][("yoy", m)]
        rows = cp["rows"]
        trs = [[r["title"], fnum(r["tr"], r["key"] not in ("base", "cur")),
                "" if _isnan(r["epk"]) else fnum(r["epk"], r["key"] not in ("base", "cur"))] for r in rows]
        strong = {i for i, r in enumerate(rows) if r["key"] in ("base", "cur", "delta")}
        subs = {i for i, r in enumerate(rows) if r.get("sub")}
        dec = table(["", "Получатели", "ФЛ"], trs, strong_rows=strong, sub_rows=subs, cls="decomp")
        cz = cp["causes"]
        crow = lambda side: [[r.title, fnum(r.tr), fnum(r.epk), fpct(r.share)]
                             for r in cz[cz["side"] == side].itertuples()]
        causes = ("<div class='two'><div><h4>Перестали получать: почему</h4>" +
                  table([f"Ситуация в {M.prep(cp['c'])} {M.parse(cp['c']).year}", "Получатели", "ФЛ", "Доля ФЛ"],
                        crow("lost")) +
                  "</div><div><h4>Начали получать: откуда</h4>" +
                  table([f"Ситуация в {M.prep(cp['b'])} {M.parse(cp['b']).year}", "Получатели", "ФЛ", "Доля ФЛ"],
                        crow("gained")) +
                  "</div></div>")
        sd = cp["seg"]
        segs = list(sd["seg"])
        cols = [t for _, t in A.SEG_COLS] + ["Изменение"]
        keys = [k for k, _ in A.SEG_COLS] + ["delta"]
        sdi = sd.set_index("seg")
        hm = C.heatmap(segs, cols, lambda r, c: float(sdi.loc[r, keys[cols.index(c)]]),
                       lambda v: fnum(v, True), row_head="Сегмент", total_col="Изменение",
                       tipf=lambda r, c, v: f"{r} · {c}|{fnum(v, True)} получателей")
        base_tbl = table(["Сегмент", "Было", "Стало", "Изменение", "%"],
                         [[r.seg, fnum(r.base), fnum(r.cur), fnum(r.delta, True),
                           fpct(r.delta / r.base if r.base else None, True)] for r in sd.itertuples()])
        return (f"<h3>{esc(M.long(cp['b']))} → {esc(M.long(cp['c']))}</h3>{dec}{causes}"
                f"<h3>По сегментам: из чего сложилось изменение получателей</h3>{hm}"
                f"<details><summary>Численность сегментов</summary>{base_tbl}</details>"
                + sql_box(res, [f"fl_flow_yoy_{m}", f"triple_flow_yoy_{m}"]))
    return section("decomp", "Разложение год к году",
                   "Одно разложение на весь отчёт: было − перестали получать ЗП в Сбере + начали ± "
                   "совместительство = стало. По ФЛ третье слагаемое ноль: человек, сменивший организацию "
                   "или ГОСБ, продолжает получать. В сегменте добавляются перетоки: ФЛ ушёл получать в "
                   "другой сегмент — для сегмента минус, для банка ничего.", per_month(res, build))


def s_did(res: dict) -> str:
    def build(m):
        did = res["did"][m]
        tab = did["table"]
        mp = M.shift(m, -1)
        y1, y0 = M.parse(m).year, M.parse(m).year - 1
        labels, vals, strong, tips = [], [], [], []
        grp_t = {"lost": A.T_LOST, "gained": A.T_GAINED, "inside": A.T_INSIDE}
        grp_s = {"lost": A.T_LOST, "gained": A.T_GAINED, "inside": "Совместительство"}
        for r in tab.itertuples():
            labels.append(r.title if isinstance(r.sub, str) else grp_s[r.group])
            vals.append(r.diff)
            strong.append(not isinstance(r.sub, str))
            tips.append(f"{r.title}|{y1}: {fnum(r.cur, True)} · {y0}: {fnum(r.prev, True)} · разница {fnum(r.diff, True)}")
        chart = C.hbars(labels, vals, tips=tips, strong=strong)
        rows = [[("  " if isinstance(r.sub, str) else "") + (r.title if isinstance(r.sub, str) else grp_t[r.group]),
                 fnum(r.cur, True), fnum(r.prev, True), fnum(r.diff, True)] for r in tab.itertuples()]
        tot = float(tab[tab["sub"].isna()]["diff"].sum())
        rows.append(["Итого: разница месячных переходов", "", "", fnum(tot, True)])
        strong_rows = {i for i, r in enumerate(tab.itertuples()) if not isinstance(r.sub, str)} | {len(rows) - 1}
        tb = table(["Слагаемое", f"{M.name(mp)}→{M.name(m)} {y1}", f"{M.name(mp)}→{M.name(m)} {y0}", "Разница"],
                   rows, strong_rows=strong_rows)
        cz = did["causes"]
        ctb = table(["Ситуация", str(y1), str(y0), "Разница"],
                    [[r.title, fnum(r.cur, True), fnum(r.prev, True), fnum(r.diff, True)] for r in cz.itertuples()])
        back = did["back"]
        bk = ""
        if back["has_next"]:
            bk = (f"<p>Из переставших получать в {esc(M.prep(m))} {y1} вернулись в "
                  f"{esc(M.prep(M.shift(m, 1)))}: <b>{esc(fnum(back['cur']))}</b> получателей "
                  f"({esc(fpct(back['cur_share']))}); год назад — {esc(fnum(back['prev']))} "
                  f"({esc(fpct(back['prev_share']))}). Вернувшиеся — временный уход, а не потеря.</p>")
        sd = did["seg"]
        hm = ""
        if not sd.empty:
            cols = [t for _, t in A.SEG_COLS] + ["Итого"]
            keys = [k for k, _ in A.SEG_COLS] + ["delta"]
            sdi = sd.set_index("seg")
            hm = ("<h3>По сегментам: разница месячных переходов, получатели</h3>" +
                  C.heatmap(list(sdi.index), cols, lambda r, c: float(sdi.loc[r, keys[cols.index(c)]]),
                            lambda v: fnum(v, True), row_head="Сегмент", total_col="Итого",
                            tipf=lambda r, c, v: f"{r} · {c}|{y1} минус {y0}: {fnum(v, True)}"))
        return (f"<h3>{esc(M.name(m).capitalize())} к {esc(M.gen(mp))}: ΔYoY изменилась на "
                f"{esc(fnum(did['expect'], True))}</h3>"
                f"<p class='muted'>Синий — слагаемое в этом году лучше, чем год назад; красный — хуже. "
                f"Подстроки «перестали» делятся по тому, когда ФЛ пришёл: в {esc(M.prep(mp))}, "
                f"в {esc(M.prep(M.shift(mp, -1)))} или раньше.</p>{chart}{tb}{bk}"
                f"<h4>По причинам</h4>{ctb}{hm}"
                + sql_box(res, [f"fl_flow_mom_{m}", f"fl_flow_mom_{M.iso(M.shift(m, -12))}",
                                f"triple_flow_mom_{m}", f"triple_flow_mom_{M.iso(M.shift(m, -12))}"]))
    return section("did", "Почему месяц хуже предыдущего",
                   "ΔYoY(m) − ΔYoY(m−1) = переход (m−1→m) этого года − тот же переход год назад. "
                   "Поэтому вопрос «почему август хуже июля» — это вопрос «чем переход июль→август "
                   "2026 года отличается от 2025-го», и оба перехода раскладываются одной лестницей.",
                   per_month(res, build))


def s_cohorts(res: dict) -> str:
    ct, ds = res["cohorts"], res["dissolved"]
    if ct.empty:
        return ""
    last = res["report"][-1]
    body = ""
    if not ds.empty:
        x = ds[ds["h"] > 0]
        labels = [f"пришли в {M.prep(r.k)}" for r in x.itertuples()]
        body += (f"<h3>Какая доля пришедших получает ЗП в {esc(M.prep(last))}</h3>" +
                 C.paired(labels, [r.surv_cur for r in x.itertuples()], [r.surv_prev for r in x.itertuples()],
                          (str(M.parse(last).year), str(M.parse(last).year - 1)), vmax=1.0))
        rows = [[f"{M.long(r.k)}", fnum(r.size_cur), fpct(r.surv_cur), fnum(r.size_prev), fpct(r.surv_prev),
                 fnum(-r.excess_loss if not _isnan(r.excess_loss) else None, True)] for r in ds.itertuples()]
        body += table(["Пришли (ФЛ, к предыдущему месяцу)", "Размер, этот год", f"Дожили до {M.gen(last)}",
                       "Размер, год назад", "Дожили год назад", "Сверх обычного, ФЛ"], rows)
        body += ("<p class='muted'>Сверх обычного = размер когорты этого года × (доживаемость в этом году − "
                 "год назад): сколько ФЛ потеряно (минус) или сохранено (плюс) сверх обычного для когорты "
                 "такого размера.</p>")
    segs = []
    for k, df in res["cohort_seg"].items():
        if df.empty:
            continue
        rows = [[r.seg, fnum(r.size), fnum(r.alive), fpct(r.surv)] for r in df.itertuples()]
        segs.append(f"<h4>Пришли в {esc(M.prep(k))}: по сегментам</h4>" +
                    table(["Сегмент (основной)", "Пришли", f"Получают в {M.prep(last)}", "Доля"], rows))
    body += "<div class='two'>" + "".join(f"<div>{s}</div>" for s in segs) + "</div>"
    yy = ct[ct["kind"] == "yoy"]
    if not yy.empty:
        rows = [[M.long(r.k), fnum(r.size), fpct(r.surv1), fpct(r.surv2), fpct(r.surv3)] for r in yy.itertuples()]
        body += ("<h3>Новые год к году: получатели месяца, которых не было год назад</h3>" +
                 table(["Месяц", "ФЛ", "Через 1 мес.", "Через 2 мес.", "Через 3 мес."], rows))
    keys = [k for k in res.get("shown", {}) if k.startswith("cohort_")][:1]
    return section("cohorts", "Растворились ли пришедшие",
                   "Когорта — ФЛ, ставшие получателями в месяце (не получали в предыдущем). Сравнивается "
                   "доживаемость когорт этого года с теми же месяцами год назад.", body + sql_box(res, keys))


def s_multi(res: dict) -> str:
    my = res["multi_yoy"]
    mb = res["multi_bank"]

    def build(m):
        x = my[my["report_dt"] == m]
        rows = [[r.seg, fpct(r.share_base), fpct(r.share_cur), fnum(r.extra_inn, True),
                 fnum(r.extra_gosb, True), fnum(r.extra, True)] for r in x.itertuples()]
        return table(["", f"Доля лишних, {M.parse(m).year - 1}", f"Доля лишних, {M.parse(m).year}",
                      "Δ несколько организаций", "Δ несколько ГОСБ в одной", "Δ лишних всего"], rows,
                     strong_rows={0})
    rows = [[M.label(r.report_dt), fnum(r.n_epk), fnum(r.n_triples), fnum(r.extra_inn), fnum(r.extra_gosb),
             fpct(r.extra / r.n_epk)] for r in mb.itertuples()]
    body = (per_month(res, build) +
            "<p class='muted'>Сегментная строка считает лишних ВНУТРИ сегмента; ФЛ с работой в двух сегментах "
            "даёт ещё межсегментных лишних, поэтому сумма сегментов меньше банка.</p>"
            "<details><summary>Совместительство по месяцам набора</summary>" +
            table(["Месяц", "ФЛ", "Получатели", "Лишние: организации", "Лишние: ГОСБ", "Доля лишних"], rows) +
            "</details>" + sql_box(res, ["multi_bank", "multi_seg"]))
    return section("multi", "Схлопывание совместительства",
                   "Лишние получатели = получатели − ФЛ. Два вида: у ФЛ несколько организаций (настоящее "
                   "совместительство) и одна организация платит через несколько ГОСБ (особенность счёта: "
                   "человек ничего не менял). Если второе упало ровно с какого-то месяца — организация свела "
                   "выплаты в один ГОСБ, это уровень, а не август.", body)


def s_season(res: dict) -> str:
    st = res["seasonal"]
    rows = []
    for r in st.itertuples():
        rows.append([M.name(r.report_dt), fnum(r.n_prev_both), fnum(r.n_gone_base), fnum(r.n_gone_cur),
                     fnum(r.n_seasonal), fpct(r.share_seasonal), fpct(r.back_base_share), fpct(r.back_cur_share)])
    body = table(["Месяц", "Получали в пред. месяце оба года, ФЛ", "Пропали год назад",
                  "Пропали сейчас", "Пропали оба года", "Доля повторяющихся",
                  "Вернулись: год назад", "сейчас"], rows)
    body += ("<p class='muted'>«Получал в июле, не получает в августе» — ещё не сезон: доказать возврат нечем. "
             "Сезон — когда ПОВТОРЯЕТСЯ: те же ФЛ пропадают в этом месяце оба года. Такие люди в сравнении "
             "год к году взаимно уничтожаются; объяснять нужно разницу.</p>")

    def build(m):
        ct = res["code_tables"].get(m)
        if ct is None or ct.empty:
            return "<p class='muted'>Зарплатные коды не прочитаны.</p>"
        y1, y0 = M.parse(m).year, M.parse(m).year - 1
        mp = M.name(M.shift(m, -1))
        rows = [[r.code_name, fnum(r.cur), fnum(r.yoy, True), fnum(r.mom_cur, True), fnum(r.mom_prev, True),
                 fnum(r.did, True)] for r in ct.itertuples()]
        return (f"<h3>Зарплатные коды: какой вид выплаты просел ({esc(M.name(m))})</h3>" +
                table(["Вид зачисления", f"ФЛ, {M.name(m)} {y1}", "Год к году", f"{mp}→{M.name(m)} {y1}",
                       f"{mp}→{M.name(m)} {y0}", "Разница переходов"], rows) +
                "<p class='muted'>ФЛ — сумма по сегментам. Только коды метрики: коды вне списка на неё не влияют.</p>")
    keys = [k for k in res.get("shown", {}) if k.startswith(("seasonal_", "code_month_"))][:2]
    return section("season", "Сезонность", "Повтор год к году и возврат в следующем месяце.",
                   body + per_month(res, build) + sql_box(res, keys))


def s_flows(res: dict) -> str:
    def build(m):
        cp = res["comp"][("yoy", m)]
        mx = cp["mx"]
        if mx.empty:
            return ""
        b, c = cp["b"], cp["c"]
        flows = [(r, k, float(mx.loc[r, k])) for r in mx.index for k in mx.columns if r != k]
        order = list(mx.index)
        sk = C.sankey(flows, order, order, M.long(b), M.long(c))
        cols = list(mx.columns)
        diag = {r: float(mx.loc[r, r]) for r in mx.index}
        hm_vals = mx.astype(float).copy()
        for r in hm_vals.index:
            hm_vals.loc[r, r] = float("nan")
        hm = C.heatmap(order, cols, lambda r, k: (None if r == k else float(mx.loc[r, k])),
                       lambda v: fnum(v), row_head=f"{M.label(b)} ↓ / {M.label(c)} →",
                       tipf=lambda r, k, v: f"{r} → {k}|{fnum(diag[r] if r == k else v)} ФЛ"
                       + (" (остались)" if r == k else ""),
                       vmax=C.div_scale([v for r in order for k in cols if r != k for v in [float(mx.loc[r, k])]]))
        sf = cp["seg_fl"]
        tb = table(["Сегмент", "Было ФЛ", "Стало", "Перестали получать", "Начали",
                    "Ушли в другие сегменты", "Пришли из других", "Изменение"],
                   [[r.seg, fnum(r.base), fnum(r.cur), fnum(-r.stopped, True), fnum(r.started, True),
                     fnum(-r.to_other, True), fnum(r.from_other, True), fnum(r.delta, True)] for r in sf.itertuples()])
        return (f"<h3>{esc(M.long(b))} → {esc(M.long(c))}</h3>"
                "<p class='muted'>Без оставшихся в своём сегменте: только переходы между сегментами и "
                f"обмен с «{esc(A.OUTSIDE)}» (перестали и начали получать ЗП в Сбере).</p>{sk}"
                f"<h4>Матрица: откуда (строки) → куда (столбцы), ФЛ</h4>{hm}{tb}"
                + sql_box(res, [f"fl_flow_yoy_{m}"]))
    return section("flows", "Перетоки между сегментами",
                   "ФЛ относится к ОСНОВНОМУ сегменту — сегменту организации с наибольшей зарплатой в месяце. "
                   "Так у каждого ФЛ ровно один сегмент, и матрица замкнута: строки дают «было», столбцы — «стало». "
                   "Сегмент организации — текущий срез справочника, одинаковый для обоих лет.",
                   per_month(res, build))


def s_tb(res: dict) -> str:
    def build(m):
        d = res["tb"].get(m)
        if d is None or d.empty:
            return "<p class='muted'>Территория не прочитана.</p>"
        cols = [c for c in d.columns if c not in ("base_total",)]
        return (C.heatmap(list(d.index), cols, lambda r, c: float(d.loc[r, c]), lambda v: fnum(v, True),
                          row_head="ТБ", total_col="Итого",
                          tipf=lambda r, c, v: f"{r} · {c}|{fnum(v, True)} получателей год к году")
                + sql_box(res, ["tb_seg", "tb_dim"]))
    return section("tb", "Территория",
                   "ТБ × сегмент: изменение получателей год к году. ТБ — системный номер ведомостей "
                   "(`sys_tb_id`), сортировка — по итогу ТБ.", per_month(res, build))


def s_threshold(res: dict) -> str:
    th = res["thresholds"]
    if th.empty:
        return ""
    pv = th.pivot_table(index="threshold", columns="report_dt", values="delta", aggfunc="sum")
    rows = [[fnum(t) + " ₽"] + [fnum(pv.loc[t, m], True) for m in pv.columns] for t in pv.index]
    return section("threshold", "Порог 2 500 ₽",
                   "Порог фиксирован, а зарплаты индексируются: сам по себе он год к году добавляет получателей. "
                   "Если картина (знаки и замедление) сохраняется при нулевом пороге, порог ни при чём.",
                   table(["Порог по организации"] + [f"Год к году, {M.name(m)}" for m in pv.columns], rows))


def s_orgs(res: dict) -> str:
    oo = res["org_opts"]

    def build(m):
        o = res["orgs"][m]
        lst, smr = o["list"], o["summary"]
        b = M.iso(M.shift(m, -12))
        sm = ""
        if not smr.empty:
            rows = [[r.title, fnum(r.n_orgs), fnum(r.base_fl), fnum(r.cur_fl), fnum(r.out_stopped),
                     fnum(r.out_moved), fnum(r.out_reorg), fnum(r.in_new), fnum(r.real_cut)] for r in smr.itertuples()]
            sm = table(["Класс организаций", "Организаций", "ФЛ было", "ФЛ стало", "Перестали в Сбере",
                        "Ушли в другие орг.", "Реорганизация", "Новые в Сбере", "Реальное сокращение"], rows)
        if lst.empty:
            return sm + "<p>Организаций с реальным сокращением по заданным порогам нет.</p>"
        n_all = int(lst["n_picked"].iloc[0])
        segs = S.ordered(set(lst["seg"]))
        tbs = sorted(set(lst["tb"]))
        flt = (f'<div class="flt" data-for="org-{m}"><label>Сегмент <select data-k="seg"><option value="">все</option>'
               + "".join(f'<option>{esc(s)}</option>' for s in segs) +
               '</select></label><label>ТБ <select data-k="tb"><option value="">все</option>'
               + "".join(f'<option>{esc(t)}</option>' for t in tbs) +
               '</select></label><label>Поиск <input data-k="q" type="search" placeholder="название или номер"></label>'
               '<span class="cnt"></span></div>')
        head = ["Организация", "Номер", "Сегмент", "ТБ", "Было ФЛ", "Стало", "Нетто", "Реальное сокращение",
                "% базы", "Перестали в Сбере", "из них: нет зачислений", "ниже порога", "только незарплатные",
                "Переток в др. орг.", "Реорг.", "Пришли новые", "Пришли из др. орг."]
        rows = []
        for r in lst.itertuples():
            name = esc(r.company_name) + (' <span class="tag">ликвидирована</span>' if r.is_liquidated else "")
            rows.append(f'<tr data-seg="{esc(r.seg)}" data-tb="{esc(r.tb)}" '
                        f'data-q="{esc(str(r.company_name).lower())} {int(r.inn)}">'
                        f'<td>{name}</td><td class="n">{int(r.inn)}</td><td>{esc(r.seg)}</td><td>{esc(r.tb)}</td>'
                        + "".join(f'<td class="n">{x}</td>' for x in (
                            fnum(r.base_fl), fnum(r.cur_fl), fnum(r.net, True), f"<b>{fnum(r.real_cut)}</b>",
                            fpct(r.real_share), fnum(r.out_stopped), fnum(r.out_left_bank), fnum(r.out_below),
                            fnum(r.out_other_codes), fnum(r.out_moved), fnum(r.out_reorg), fnum(r.in_new),
                            fnum(r.in_moved))) + "</tr>")
        tbl = (f'<div class="scroll tall"><table class="data orgs" id="org-{m}"><thead><tr>' +
               "".join(f'<th class="{"" if i < 4 else "n"}">{esc(h)}</th>' for i, h in enumerate(head)) +
               "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
        shown = (f"Показано {fnum(len(lst))} из {fnum(n_all)} организаций с реальным сокращением; "
                 f"их сокращение — {fnum(lst['sum_real_picked'].iloc[0])} ФЛ.")
        rg = o["reorg"]
        rgh = ""
        if rg is not None and not rg.empty:
            rgh = ("<details><summary>Реорганизации — исключены из списка</summary>" +
                   table(["Откуда", "Номер", "Куда", "Номер приёмника", "Переехало ФЛ", "Ушло всего"],
                         [[r.name_from or "—", int(r.inn_from), r.name_to or "—", int(r.inn_to), fnum(r.n_mv),
                           fnum(r.n_lv)] for r in rg.itertuples()], num={1, 3, 4, 5}) + "</details>")
        return (f"<h3>{esc(M.long(b))} → {esc(M.long(m))}</h3>{sm}"
                f"<details open><summary>Список: {esc(shown)}</summary>{flt}{tbl}</details>{rgh}"
                + sql_box(res, [f"org_list_{m}", f"org_summary_{m}"]))
    lead = (f"Грейн — ФЛ внутри организации, поэтому переводы между ГОСБ одной организации сюда не попадают. "
            f"<b>Реальное сокращение</b> = min(перестали получать ЗП в Сбере; падение численности без учёта "
            f"реорганизации). В список — только где численность действительно упала: «5 ушли, 5 пришли» "
            f"не попадает. Переток в другие организации — часть снижения, но не потеря для Сбера: отдельная "
            f"колонка. Реорганизация — ушло ≥ {fpct(oo['reorg_min_share'], digits=0)} ушедших и ≥ "
            f"{oo['reorg_min_movers']} ФЛ в одну организацию-приёмник; исключается. Пороги: база ≥ "
            f"{oo['min_base']} ФЛ, сокращение ≥ {oo['min_real']} ФЛ и ≥ {fpct(oo['min_share'], digits=0)} базы.")
    return section("orgs", "Организации с реальным сокращением", lead, per_month(res, build))


def s_checks(res: dict) -> str:
    ch = res["checks"]
    bad = [c for c in ch if not c["ok"]]
    head = (f"<p class='{'warn' if bad else 'ok'}'>{'Не сошлось: ' + str(len(bad)) if bad else 'Все проверки сошлись'}"
            f" ({len(ch)} проверок).</p>")
    rows = [["✓" if c["ok"] else "✗", c["check"], "" if c["residual"] is None else fnum(c["residual"], digits=1),
             c["detail"]] for c in ch]
    lim = ("<ul>"
           "<li>Сегмент организации — текущий срез справочника ЕПК: переклассификация организации между годами не видна, "
           "оба года меряются одной линейкой.</li>"
           "<li>Если у одного номера организации в справочнике несколько сегментов, берётся первый по алфавиту короткий.</li>"
           "<li>Номера организаций в ведомостях, не прошедшие маску, в разбор не входят — доля по месяцам в разделе «Ряд».</li>"
           "<li>Куда ушёл человек вне банка (другой банк, увольнение) по ведомостям не видно.</li>"
           "<li>Стаж и возврат считаются по месяцам рабочего набора; вне его метка «неизвестно».</li></ul>")
    tm = res.get("timing", {})
    tmh = ""
    if tm:
        top = sorted(tm.items(), key=lambda kv: -kv[1])[:10]
        tmh = ("<details><summary>Время запросов</summary>" +
               table(["Запрос", "с"], [[k, fnum(v, digits=1)] for k, v in top]) + "</details>")
    return section("checks", "Проверки и ограничения",
                   "Части обязаны складываться в итог; расхождение — ошибка расчёта, а не свойство данных.",
                   head + "<details><summary>Все проверки</summary>" +
                   table(["", "Проверка", "Невязка", ""], rows, num={2}) + "</details>" + lim + tmh)


def s_august(res: dict) -> str:
    br = res.get("bridge")
    if br is None or br.empty:
        return ""
    ms = br.attrs["months"]
    nm = {k: M.name(v) for k, v in ms.items()}
    y1 = M.parse(ms["cur"]).year
    y0 = y1 - 1
    b = br.set_index("key")
    titles = [("lvl_cur", f"Получателей, {y1}"), ("lvl_prev", f"Получателей, {y0}"),
              ("yoy", "Год к году"), ("mom_cur", f"К предыдущему месяцу, {y1}"),
              ("mom_prev", f"К предыдущему месяцу, {y0}"), ("did", "Разница переходов")]
    rows = [[t] + [fnum(b.loc[k, c], k not in ("lvl_cur", "lvl_prev")) for c in ("prev", "cur", "next")]
            for k, t in titles]
    two = br.attrs["two"]
    body = table(["", nm["prev"], nm["cur"], nm["next"]], rows, strong_rows={2, 5})
    body += (f"<p>За два месяца {esc(nm['prev'])}→{esc(nm['next'])}: {y1} — {esc(fnum(two['cur'], True))}, "
             f"{y0} — {esc(fnum(two['prev'], True))}, разница {esc(fnum(two['cur'] - two['prev'], True))}. "
             f"<b>Вывод:</b> {esc(A.august_verdict(br, res['temp_perm']))}</p>")
    tp = res["temp_perm"]
    body += (f"<h3>Переставшие в {esc(M.prep(ms['cur']))}: вернулись ли в {esc(M.prep(ms['next']))}</h3>" +
             table(["", f"{nm['prev']}→{nm['cur']} {y1}", f"{nm['prev']}→{nm['cur']} {y0}", "Разница"],
                   [[r.title, fnum(r.cur, True), fnum(r.prev, True), fnum(r.diff, True)] for r in tp.itertuples()],
                   strong_rows={2}) +
             "<p class='muted'>Получатели. «Временно» — ФЛ снова получатель в следующем месяце: это не потеря.</p>")
    aug = res["aug_raw"]
    hc, hp = A.hole_summary(aug["hole_cur"]), A.hole_summary(aug["hole_prev"])
    body += (f"<h3>Организации, пропустившие {esc(nm['cur'])}</h3>"
             f"<p class='muted'>В {esc(M.prep(ms['prev']))} ≥ {res['org_opts']['hole_min_base']} получателей, "
             f"в {esc(M.prep(ms['cur']))} — не больше половины, в {esc(M.prep(ms['next']))} — снова ≥ 80%. "
             f"Похоже на перенос даты выплаты организацией, а не на уход людей.</p>" +
             table(["", "Организаций", f"«Дыра» в {M.prep(ms['cur'])}, получателей"],
                   [[str(y1), fnum(hc["n_orgs"]), fnum(hc["sum_hole"])],
                    [str(y0), fnum(hp["n_orgs"]), fnum(hp["sum_hole"])],
                    ["Разница", fnum(hc["n_orgs"] - hp["n_orgs"], True), fnum(hc["sum_hole"] - hp["sum_hole"], True)]],
                   strong_rows={2}))
    hl = aug["hole_cur"]
    if hl is not None and not hl.empty:
        rows = [[r.company_name or "Организация не в справочнике", int(r.inn), r.seg, fnum(r.n_prev),
                 fnum(r.n_cur), fnum(r.n_next), fnum(r.hole)] for r in hl.head(30).itertuples()]
        body += (f"<details><summary>Крупнейшие организации с провалом, {y1} (30 из {fnum(hc['n_orgs'])})</summary>" +
                 table(["Организация", "Номер", "Сегмент", nm["prev"], nm["cur"], nm["next"], "Дыра"], rows,
                       num={1, 3, 4, 5, 6}) + "</details>")
    pr = []
    for tag, y in (("cur", y1), ("prev", y0)):
        d = aug[f"pay_{tag}"]
        if d is None or d.empty:
            continue
        for r in d.itertuples():
            grp = "пропали и вернулись" if r.grp == "gap" else "получали все три месяца"
            pr.append([str(y), grp, fnum(r.n_epk), fnum(float(r.median_ratio), digits=2),
                       fpct(float(r.share_double)), fpct(float(r.share_single))])
    if pr:
        body += (f"<h3>Пришла ли в {esc(M.prep(ms['next']))} двойная выплата</h3>" +
                 table(["Год", "Группа ФЛ", "ФЛ", f"Медиана {nm['next']}/{nm['prev']}", "Доля ≥1,6×",
                        "Доля 0,7–1,4×"], pr, num={2, 3, 4, 5}) +
                 "<p class='muted'>Зарплатная сумма ФЛ за месяц. Если у пропавших и вернувшихся медиана около 2 — "
                 "выплату за пропущенный месяц перенесли (организация, график, выходные); около 1 — человек "
                 "просто не получал в этом месяце (отпуск без выплаты, перерыв).</p>")
    keys = [k for k in res.get("shown", {}) if k.startswith(("org_hole_", "return_pay_"))][:2]
    return section("august", f"{M.name(ms['cur']).capitalize()}: потеря или перенос в {M.name(ms['next'])}",
                   "Провал месяца, который в следующем месяце вернулся, — сдвиг во времени, а не потеря людей. "
                   "Здесь — какая часть минуса вернулась, кто это и похоже ли это на перенос выплаты.",
                   body + sql_box(res, keys))


# --------------------------------------------------------------------------- #
CSS = """
:root{color-scheme:light;
 --page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
 --grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);
 --series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;
 --series-5:#e87ba4;--series-6:#008300;--neutral:#a3a19a;
 --div-pos:#256abf;--div-neg:#c63a3a;--div-mid:#f0efec;--on-strong:#ffffff;
 --pos:#2a78d6;--neg:#e34948;--warn:#b54708;--good:#006300}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;
 --page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink2:#c3c2b7;--muted:#898781;
 --grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);
 --series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;
 --series-5:#d55181;--series-6:#008300;--neutral:#6e6c66;
 --div-pos:#3987e5;--div-neg:#e66767;--div-mid:#383835;--pos:#3987e5;--neg:#e66767;--warn:#fab219;--good:#0ca30c}}
:root[data-theme="dark"]{color-scheme:dark;
 --page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink2:#c3c2b7;--muted:#898781;
 --grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);
 --series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;
 --series-5:#d55181;--series-6:#008300;--neutral:#6e6c66;
 --div-pos:#3987e5;--div-neg:#e66767;--div-mid:#383835;--pos:#3987e5;--neg:#e66767;--warn:#fab219;--good:#0ca30c}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:24px max(16px,4vw) 8px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:18px;margin:0 0 6px}
h3{font-size:15px;margin:22px 0 8px}
h4{font-size:14px;margin:16px 0 6px}
.meta,.muted{color:var(--ink2)}
.lead{color:var(--ink2);max-width:980px}
nav.bar{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;
 padding:8px max(16px,4vw);background:var(--page);border-bottom:1px solid var(--grid)}
nav.bar a{color:var(--ink2);text-decoration:none;font-size:13px}
nav.bar a:hover{color:var(--ink)}
.months{display:flex;gap:4px;margin-right:10px}
.months button{font:inherit;font-size:13px;padding:3px 10px;border-radius:6px;border:1px solid var(--grid);
 background:var(--surface);color:var(--ink);cursor:pointer}
.months button[aria-pressed="true"]{background:var(--ink);color:var(--surface);border-color:var(--ink)}
main{padding:0 max(16px,4vw) 40px}
section{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:18px 20px;margin:16px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.tile{border:1px solid var(--grid);border-radius:8px;padding:12px 14px}
.tl{color:var(--ink2);font-size:13px}.tv{font-size:28px;font-weight:600}
.tv.up{color:var(--good)}.tv.down{color:var(--neg)}
.td{font-size:13px}.tn{color:var(--muted);font-size:12px}
.verdict{margin-top:12px;max-width:980px}
.verdict ul{margin:4px 0 10px;padding-left:20px}
.scroll{overflow-x:auto;max-width:100%}
.scroll.tall{max-height:640px;overflow:auto}
table{border-collapse:collapse;font-size:13px}
table.data th,table.data td{padding:4px 10px;border-bottom:1px solid var(--grid);text-align:left;vertical-align:top}
table.data th{color:var(--ink2);font-weight:500;position:sticky;top:0;background:var(--surface)}
table.data .n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.data tr.strong td{font-weight:600}
table.data tr.sub td{color:var(--ink2);font-size:12px}
table.data tr.sub td:first-child{padding-left:26px}
table.data td:first-child{min-width:180px}
table.orgs td:first-child{min-width:240px}
table.heat th,table.heat td{padding:5px 9px;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.heat td{border:2px solid var(--surface);border-radius:4px;min-width:64px}
table.heat th{color:var(--ink2);font-weight:500}
table.heat th.rh{text-align:left}
table.heat td.tot{font-weight:600}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
svg.chart{width:100%;max-width:820px;height:auto;display:block;margin:6px 0}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .base{stroke:var(--axis);stroke-width:1}
svg .tick{fill:var(--muted);font-size:11px}
svg .rlabel{fill:var(--ink2);font-size:12px}
svg .rlabel.strong{fill:var(--ink);font-weight:600}
svg .vlabel{fill:var(--ink2);font-size:11px}
svg .dlabel{fill:var(--ink);font-size:12px}
svg .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .dot{stroke:var(--surface);stroke-width:2}
svg .pos{fill:var(--pos)}svg .neg{fill:var(--neg)}
svg .dim{opacity:.45}
svg .key-cur{fill:var(--series-1)}svg .key-prev{fill:var(--neutral)}
svg .band{opacity:.35}svg .band:hover,svg .band:focus{opacity:.7;outline:none}
svg .hit rect:first-child{fill:transparent}
svg .hit .cross{stroke:var(--axis);stroke-width:1;visibility:hidden}
svg .hit:hover .cross,svg .hit:focus .cross{visibility:visible}
svg .hit:hover path,svg .hit:hover rect.pos,svg .hit:hover rect.neg,svg .hit:hover rect.key-cur,svg .hit:hover rect.key-prev{opacity:.8}
svg .hit:focus{outline:none}
#tip{position:fixed;pointer-events:none;z-index:10;background:var(--surface);color:var(--ink);
 border:1px solid var(--ring);box-shadow:0 4px 16px rgba(0,0,0,.12);border-radius:6px;padding:6px 9px;
 font-size:12px;max-width:360px}
#tip b{display:block;font-size:13px}
details{margin:8px 0}
summary{cursor:pointer;color:var(--ink2)}
details.sql pre{background:var(--page);border:1px solid var(--grid);border-radius:6px;padding:10px;
 overflow-x:auto;font-size:11.5px;line-height:1.4;max-height:420px}
.flt{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin:8px 0}
.flt select,.flt input{font:inherit;font-size:13px;padding:3px 6px;border:1px solid var(--grid);border-radius:6px;
 background:var(--surface);color:var(--ink)}
.cnt{color:var(--ink2);font-size:12px}
.tag{font-size:11px;color:var(--warn);border:1px solid currentColor;border-radius:4px;padding:0 4px;margin-left:4px}
.warn{color:var(--warn);font-weight:600}.ok{color:var(--good);font-weight:600}
@media print{nav.bar{position:static}details{display:block}}
"""

JS = """
(function(){
var tip=document.getElementById('tip');
function show(el,x,y){var t=el.getAttribute('data-tip');if(!t)return;var p=t.split('|');
 tip.textContent='';var b=document.createElement('b');b.textContent=p[p.length>1?1:0];tip.appendChild(b);
 if(p.length>1){var h=document.createElement('div');h.textContent=p[0];tip.insertBefore(h,b);
  for(var i=2;i<p.length;i++){var d=document.createElement('div');d.textContent=p[i];tip.appendChild(d);}}
 tip.hidden=false;var w=tip.offsetWidth,hh=tip.offsetHeight;
 tip.style.left=Math.min(x+14,window.innerWidth-w-8)+'px';tip.style.top=Math.max(8,y-hh-10)+'px';}
document.addEventListener('pointermove',function(e){var el=e.target.closest&&e.target.closest('[data-tip]');
 if(el)show(el,e.clientX,e.clientY);else tip.hidden=true;});
document.addEventListener('focusin',function(e){var el=e.target.closest&&e.target.closest('[data-tip]');
 if(el){var r=el.getBoundingClientRect();show(el,r.left+r.width/2,r.top);}});
document.addEventListener('focusout',function(){tip.hidden=true;});
var btns=document.querySelectorAll('.months button');
function pick(m){btns.forEach(function(b){b.setAttribute('aria-pressed',b.dataset.month===m?'true':'false');});
 document.querySelectorAll('.pm').forEach(function(d){d.hidden=d.dataset.month!==m;});}
btns.forEach(function(b){b.addEventListener('click',function(){pick(b.dataset.month);});});
document.querySelectorAll('.flt').forEach(function(f){var tb=document.getElementById(f.dataset.for);
 if(!tb)return;var rows=tb.querySelectorAll('tbody tr'),cnt=f.querySelector('.cnt');
 function apply(){var s=f.querySelector('[data-k=seg]').value,t=f.querySelector('[data-k=tb]').value,
  q=f.querySelector('[data-k=q]').value.toLowerCase(),n=0;
  rows.forEach(function(r){var ok=(!s||r.dataset.seg===s)&&(!t||r.dataset.tb===t)&&(!q||r.dataset.q.indexOf(q)>=0);
   r.hidden=!ok;if(ok)n++;});cnt.textContent='строк: '+n;}
 f.addEventListener('input',apply);f.addEventListener('change',apply);apply();});
})();
"""


def render(res: dict) -> str:
    rep = res["report"]
    last = rep[-1]
    title = (f"Получатели ЗП по всему Сберу: {M.name(rep[0])}–{M.name(last)} {M.parse(last).year} "
             f"к {M.parse(last).year - 1}")
    nav = [("summary", "Итог"), ("august", "Перенос?"), ("series", "Ряд"), ("decomp", "Разложение"), ("did", "Почему хуже"),
           ("cohorts", "Пришедшие"), ("multi", "Совместительство"), ("season", "Сезон"),
           ("flows", "Перетоки"), ("tb", "Территория"), ("threshold", "Порог"),
           ("orgs", "Организации"), ("checks", "Проверки")]
    buttons = "".join(f'<button type="button" data-month="{m}" aria-pressed="{"true" if m == last else "false"}">'
                      f'{esc(M.name(m))}</button>' for m in rep)
    body = "".join([s_summary(res), s_august(res), s_series(res), s_decomp(res), s_did(res), s_cohorts(res), s_multi(res),
                    s_season(res), s_flows(res), s_tb(res), s_threshold(res), s_orgs(res), s_checks(res)])
    page = (f"<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
            f"<header><h1>{esc(title)}</h1><div class='meta'>Сформирован {esc(res['generated'])} · "
            f"схема {esc(res['schema'])} · порог {fnum(res['amt_min'])} ₽ · кодов {len(res['codes'])}</div></header>"
            f"<nav class='bar'><span class='months' role='group' aria-label='Месяц сравнения'>"
            f"<span class='muted' style='font-size:13px;margin-right:4px'>Месяц:</span>{buttons}</span>"
            + "".join(f"<a href='#{a}'>{esc(t)}</a>" for a, t in nav) +
            f"</nav><main>{body}</main><div id='tip' hidden></div><script>{JS}</script></body></html>")
    return sanitize(page)
