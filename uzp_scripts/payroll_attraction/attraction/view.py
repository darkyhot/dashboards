"""HTML: один файл, открывается без сети.

* Главный элемент — полоса каналов: 2025 над 2026 в одном масштабе, сжатие
  канала видно как укоротившийся сегмент его цвета.
* Цвет закреплён за каналом во всём отчёте; изменения — расходящаяся пара.
* У каждого блока — `<details>` с самодостаточным SQL.
* Слово-идентификатор организации не пишется нигде (правило 14): кириллическое —
  «id орг», латинское имя колонки в SQL — кодами символов (`hide_col`).
"""
from __future__ import annotations

import math
import re

import pandas as pd

from . import analyze as A
from . import charts as C
from . import months as M
from .charts import esc, fnum, fpct

BLOCKED = "".join(map(chr, (1048, 1053, 1053)))
_WORD = re.compile("(?<![А-Яа-яЁёA-Za-z])" + BLOCKED + "(?![А-Яа-яЁёA-Za-z])", re.IGNORECASE)
ORG_ID = "id орг"
_LAT = "".join(map(chr, (105, 110, 110)))
_IDENT = re.compile(r"(?<![\w&\"\\])[A-Za-z_][A-Za-z0-9_]*")
SQL_NOTE = ('Имена колонок, где есть три латинские буквы номера организации, записаны кодами '
            'символов — U&"\\0069…": для Postgres и Greenplum это то же имя, запрос выполняется как есть.')

COLOR = {"mzp": "var(--ch-mzp)", "vsp": "var(--ch-vsp)", "dig": "var(--ch-dig)", "other": "var(--ch-other)"}


def sanitize(text: str) -> str:
    return _WORD.sub(ORG_ID, text or "")


def hide_col(sql: str) -> str:
    def f(m):
        tok = m.group(0)
        if _LAT not in tok.lower():
            return tok
        return 'U&"' + tok.lower().replace(_LAT, "\\0069" + _LAT[1:]) + '"'
    return _IDENT.sub(f, sql)


# --------------------------------------------------------------------------- #
class Raw(str):
    """Уже размеченный HTML внутри ячейки."""


def _isnan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def table(head: list[str], rows: list[list], num: set[int] | None = None, cls: str = "",
          strong_rows: set[int] | None = None, sub_rows: set[int] | None = None, tid: str = "",
          row_attrs: list[str] | None = None) -> str:
    num = num if num is not None else set(range(1, len(head)))
    out = [f'<div class="scroll"><table class="data {cls}"{f" id={chr(34)}{tid}{chr(34)}" if tid else ""}>'
           f'<thead><tr>']
    out += [f'<th class="{"n" if i in num else ""}">{esc(h)}</th>' for i, h in enumerate(head)]
    out.append("</tr></thead><tbody>")
    for k, r in enumerate(rows):
        rc = (" strong" if strong_rows and k in strong_rows else "") + (" sub" if sub_rows and k in sub_rows else "")
        attrs = row_attrs[k] if row_attrs else ""
        out.append(f'<tr class="{rc.strip()}"{attrs}>')
        for i, v in enumerate(r):
            out.append(f'<td class="{"n" if i in num else ""}">{v if isinstance(v, Raw) else esc(v)}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def chip(ch: str) -> Raw:
    return Raw(f'<span class="chip" style="--c:{COLOR[ch]}"></span>{esc(A.CH_T[ch])}')


def delta(v, digits=0, bad_up: bool = False) -> Raw:
    """Изменение с цветом «хорошо / плохо». bad_up — рост плохой (ячеек снижения стало больше)."""
    if _isnan(v) or v == 0:
        return Raw(esc(fnum(v, True, digits)))
    good = (v > 0) != bad_up
    return Raw(f'<span class="{"up" if good else "down"}">{esc(fnum(v, True, digits))}</span>')


def dpct(v, bad_up: bool = False) -> Raw:
    if _isnan(v) or v == 0:
        return Raw(esc(fpct(v, True)))
    good = (v > 0) != bad_up
    return Raw(f'<span class="{"up" if good else "down"}">{esc(fpct(v, True))}</span>')


def _params_comment(args: dict) -> str:
    lines = []
    for k, v in sorted(args.items()):
        if isinstance(v, (list, tuple)):
            if v and all(isinstance(x, str) for x in v):
                v = "ARRAY[" + ", ".join(f"'{x}'" for x in v) + "]::date[]"
            else:
                v = "ARRAY[" + ", ".join(str(x) for x in v) + "]"
        elif isinstance(v, str):
            v = f"'{v}'"
        elif isinstance(v, bool):
            v = "true" if v else "false"
        lines.append(f"--   :{k} = {v}")
    return "-- Параметры (подставьте вместо :имя):\n" + "\n".join(lines) + "\n"


def sql_box(res: dict, names: list[str]) -> str:
    shown = res.get("shown", {})
    items = [(n, shown[n]) for n in names if n in shown]
    if not items:
        return ""
    body = []
    for n, (sql, args) in items:
        used = {k: v for k, v in args.items() if re.search(rf":{k}\b", sql)}
        body.append(f'<p class="muted">Запрос «{esc(n)}»</p><pre>{esc(_params_comment(used) + hide_col(sql))}</pre>')
    return (f'<details class="sql"><summary>Как посчитано (SQL)</summary><p class="muted">{esc(SQL_NOTE)}</p>'
            f'{"".join(body)}</details>')


def section(sid: str, title: str, lead: str, body: str) -> str:
    return f'<section id="{sid}"><h2>{esc(title)}</h2><p class="lead">{lead}</p>{body}</section>'


def _y(res, y: str) -> str:
    """Подпись периода года: «янв–авг 2026»."""
    ms = res["m_cur"] if y == "cur" else res["m_prev"]
    return f"{M.RU[0]}–{M.RU[M.parse(ms[-1]).month - 1]} {M.parse(ms[-1]).year}"


# --------------------------------------------------------------------------- #
# Разделы
# --------------------------------------------------------------------------- #
def s_hero(res: dict) -> str:
    tot = res["nfl"]["tot"]
    rows = [(str(M.parse(res["m_prev"][-1]).year), {c: float(tot.loc["prev", c]) for c in A.CH}),
            (str(M.parse(res["m_cur"][-1]).year), {c: float(tot.loc["cur", c]) for c in A.CH})]
    strip = C.channel_strip(rows, A.CH, A.CH_T, COLOR)
    yoy = res["nfl"]["yoy"]
    legend = "".join(
        f'<li><span class="chip" style="--c:{COLOR[r.ch]}"></span><b>{esc(r.title)}</b> '
        f'{esc(fnum(r.prev))} → {esc(fnum(r.cur))} {delta(r.delta)} <span class="muted">({esc(fpct(r.pct, True))})</span></li>'
        for r in yoy[yoy["ch"] != "all"].itertuples())
    mz = res["mzp"]["year"]
    idx = []
    for k, t in (("staff", "Штат МЗП"), ("deals", "Сделок МЗП"), ("fact", "Факт по сделкам, ФЛ"),
                 ("nfl_mzp", "НФЛ через МЗП"), ("nfl_per_mgr", "НФЛ на менеджера"),
                 ("deals_per_mgr", "Сделок на менеджера")):
        a, b = mz.loc["cur", k], mz.loc["prev", k]
        idx.append((t, A.pct(a, b), a, b))
    bars = C.hbars([t for t, *_ in idx], [100 * (v if not _isnan(v) else 0) for _, v, *_ in idx],
                   fmt=lambda v, s=True: fnum(v, s, 0) + "%",
                   tips=[f"{t}|{fnum(b, digits=1 if 'менеджера' in t else 0)} → "
                         f"{fnum(a, digits=1 if 'менеджера' in t else 0)}" for t, _, a, b in idx])
    vd = "".join(f"<li>{esc(x)}</li>" for x in res["verdict"])
    return (f'<section id="hero" class="hero">'
            f'<p class="kicker">Новые получатели зарплаты в Сбере (НФЛ), {esc(_y(res, "prev"))} и {esc(_y(res, "cur"))}</p>'
            f'{strip}<ul class="legend">{legend}</ul>'
            f'<div class="hero-grid"><div><h3>Что произошло</h3><ul class="verdict">{vd}</ul></div>'
            f'<div><h3>Штат МЗП и отдача, изменение год к году</h3>{bars}</div></div>'
            + sql_box(res, ["nfl_month"]) + "</section>")


def s_channels(res: dict) -> str:
    nfl = res["nfl"]
    mo = nfl["month"]
    months = nfl["months"]
    y1, y0 = M.parse(res["m_cur"][-1]).year, M.parse(res["m_prev"][-1]).year
    labels = [M.RU[m - 1] for m in months]
    small = []
    for c in A.CH:
        def col(y):
            return [float(mo[(y, c)].get(m, 0)) if (y, c) in mo.columns else None for m in months]
        small.append(f'<figure class="small"><figcaption>{chip(c)}</figcaption>' +
                     C.lines(labels, [{"name": str(y0), "values": col("prev"), "color": "var(--neutral)"},
                                      {"name": str(y1), "values": col("cur"),
                                       "color": "var(--ink2)" if c == "other" else COLOR[c], "emphasis": True}],
                             height=230, y_from_zero=True) + "</figure>")
    yoy = nfl["yoy"]
    rows = [[chip(r.ch) if r.ch != "all" else "Всего НФЛ", fnum(r.prev), fnum(r.cur), delta(r.delta), dpct(r.pct),
             fpct(r.share_prev), fpct(r.share_cur), fpct(r.contrib, True) if r.ch != "all" else ""]
            for r in yoy.itertuples()]
    tb = table(["Канал", f"НФЛ {_y(res, 'prev')}", f"НФЛ {_y(res, 'cur')}", "Изменение", "%",
                f"Доля {y0}", f"Доля {y1}", "Вклад в изменение"], rows, strong_rows={len(rows) - 1})
    mrows = []
    for m in months:
        r = [M.RU_FULL[m - 1]]
        for c in A.CH + ["all"]:
            if c == "all":
                a = sum(float(mo[("cur", x)].get(m, 0)) for x in A.CH if ("cur", x) in mo.columns)
                b = sum(float(mo[("prev", x)].get(m, 0)) for x in A.CH if ("prev", x) in mo.columns)
            else:
                a = float(mo[("cur", c)].get(m, 0)) if ("cur", c) in mo.columns else 0
                b = float(mo[("prev", c)].get(m, 0)) if ("prev", c) in mo.columns else 0
            r += [fnum(b), fnum(a), delta(a - b)]
        mrows.append(r)
    head = ["Месяц"] + [f"{t} {y}" if y != "Δ" else "Δ" for t in [A.CH_T[c] for c in A.CH] + ["Всего"]
                        for y in (y0, y1, "Δ")]
    mt = table(head, mrows)
    return section("channels", "Каналы по месяцам",
                   "НФЛ относится к месяцу, в котором ФЛ впервые получил зарплату в Сбере. Канал — первое по дате "
                   "действие (сделка МЗП, консультация в ВСП или в СБОЛ), которое действовало на дату первой "
                   "зарплаты: от дня действия до конца второго месяца после него. Нет действия — «Остальное».",
                   f'<div class="smalls">{"".join(small)}</div>{tb}'
                   f'<details><summary>По месяцам: каналы, два года</summary>{mt}</details>'
                   + sql_box(res, ["nfl_month"]))


def s_cells(res: dict) -> str:
    ct = res["cell_t"]
    g, cls = ct["grp"], ct["cls"]
    pc, pp = ct["pairs"]["cur"], ct["pairs"]["prev"]
    lab_c, lab_p = f"{M.label(pc[0])}→{M.label(pc[1])}", f"{M.label(pp[0])}→{M.label(pp[1])}"

    def gv(pair, key, col):
        try:
            return float(g.loc[(pair, key), col])
        except KeyError:
            return 0.0

    def cv(pair, key, col):
        try:
            return float(cls.loc[(pair, key), col])
        except KeyError:
            return 0.0
    rows, strong, sub = [], set(), set()
    for grp, subs in (("grow", ["grow", "new"]), ("flat", []), ("decl", ["decl", "closed"])):
        strong.add(len(rows))
        a, b = gv("cur", grp, "n_cells"), gv("prev", grp, "n_cells")
        bad = grp == "decl"
        rows.append([A.GROUP_T[grp], fnum(b), fnum(a), delta(a - b, bad_up=bad), dpct(A.pct(a, b), bad),
                     fnum(gv("prev", grp, "d"), True), fnum(gv("cur", grp, "d"), True)])
        for k in subs:
            sub.add(len(rows))
            a, b = cv("cur", k, "n_cells"), cv("prev", k, "n_cells")
            rows.append([A.CLS_T[k], fnum(b), fnum(a), delta(a - b, bad_up=bad), dpct(A.pct(a, b), bad),
                         fnum(cv("prev", k, "d"), True), fnum(cv("cur", k, "d"), True)])
    t = ct["tot"]
    tot_row = ["Портфель, ФЛ"] + [""] * 4 + [
        fnum(t.loc[M.iso(pp[1]), "n_fl"] - t.loc[M.iso(pp[0]), "n_fl"], True) if M.iso(pp[0]) in t.index else "",
        fnum(t.loc[M.iso(pc[1]), "n_fl"] - t.loc[M.iso(pc[0]), "n_fl"], True) if M.iso(pc[0]) in t.index else ""]
    rows.append(tot_row)
    strong.add(len(rows) - 1)
    tb = table(["Ячейка ГОСБ × организация", f"Ячеек {lab_p}", f"Ячеек {lab_c}", "Изменение", "%",
                f"ФЛ {lab_p}", f"ФЛ {lab_c}"], rows, strong_rows=strong, sub_rows=sub)
    st = ct["steps"]
    steps = C.paired([f"+{s}" for s in st.index],
                     [float(st.loc[s, "cur"]) if "cur" in st else 0 for s in st.index],
                     [float(st.loc[s, "prev"]) if "prev" in st else 0 for s in st.index],
                     (lab_c, lab_p), fmt=fnum, vmax=float(st.max().max() or 1))
    nc = res["nfl_cell"]
    nct = ""
    if not nc.empty:
        r2 = []
        for grp in ("grow", "flat", "decl", "none"):
            for y in ("prev", "cur"):
                if (y, grp) not in nc.index:
                    continue
                v = nc.loc[(y, grp)]
                r2.append([A.GROUP_T[grp], _y(res, y)] + [fnum(v[c]) for c in A.CH] + [fnum(v.sum())])
        nct = ("<h3>НФЛ по классу своей ячейки</h3><p class='muted'>НФЛ года — к классу ячейки за тот же год "
               "(август к августу). Сколько НФЛ пришло в растущие ячейки и через какие каналы.</p>"
               + table(["Ячейка", "Период"] + [A.CH_T[c] for c in A.CH] + ["Всего"], r2, num=set(range(2, 7))))
    top = res["cell_top"]
    tt = ""
    if top is not None and not top.empty:
        r3 = []
        for r in top.itertuples():
            hid = ' <span class="tag">ФИО скрыто</span>' if getattr(r, "org_name_hidden", False) else ""
            r3.append([Raw(esc(r.org) + hid), int(r.inn), r.holding, r.seg, r.tb, fnum(r.n_b), fnum(r.n_c),
                       delta(r.delta), fnum(r.nfl_mzp), fnum(r.nfl_vsp), fnum(r.nfl_dig), fnum(r.nfl_other)])
        tt = (f"<details><summary>Крупнейший рост ячеек {esc(lab_c)}: {len(top)} ячеек, с НФЛ этого года по каналам"
              f"</summary>" + table(["Организация", ORG_ID, "Холдинг", "Сегмент", "ТБ", M.label(pc[0]),
                                     M.label(pc[1]), "Δ", "МЗП", "ВСП", "Digital", "Остальное"], r3,
                                    num=set(range(5, 12)), cls="wide") + "</details>")
    return section("cells", "Ячейки ГОСБ × организация: рост, снижение, без изменений",
                   "Численность ячейки — ФЛ с зарплатой в портфеле (правила портфеля заказчика) в августе. Рост — "
                   "+1 ФЛ и больше, включая ячейки, которых год назад не было; снижение — −1 и больше, включая "
                   "закрывшиеся. «10 ушли — 10 пришли» — это «без изменений».",
                   tb + f"<h3>Насколько выросли растущие ячейки</h3>{steps}{nct}{tt}"
                   + sql_box(res, ["cell_sum", "cell_tot", "nfl_cell", "cell_top"]))


def s_mzp(res: dict) -> str:
    mz = res["mzp"]
    t = mz["year"]
    y1, y0 = int(t.loc["cur", "year"]), int(t.loc["prev", "year"])
    spec = [("staff", "Менеджеров МЗП (на конец периода)", 0), ("deals", "Сделок МЗП, создано", 0),
            ("plan", "План по сделкам, ФЛ", 0), ("fact", "Факт по сделкам, ФЛ", 0),
            ("fact_plan", "Факт / план", None), ("nfl_mzp", "НФЛ через МЗП (атрибуция)", 0),
            ("deals_per_mgr", "Сделок на менеджера", 1), ("fact_per_mgr", "Факт на менеджера, ФЛ", 1),
            ("nfl_per_mgr", "НФЛ МЗП на менеджера", 1), ("fact_per_deal", "Факт на сделку, ФЛ", 1),
            ("nfl_per_deal", "НФЛ на сделку (сделки периода)", 2)]
    rows = []
    for k, title, dg in spec:
        a, b = t.loc["cur", k], t.loc["prev", k]
        if dg is None:
            rows.append([title, fpct(b), fpct(a), Raw(esc(fnum(100 * (a - b), True, 1)) + " п.п."), ""])
        else:
            rows.append([title, fnum(b, digits=dg), fnum(a, digits=dg), delta(a - b, dg), dpct(A.pct(a, b))])
    tb = table(["", f"{y0}", f"{y1}", "Изменение", "%"], rows, strong_rows={0, 5, 8})
    mo = mz["month"]
    ch = ""
    if not mo.empty:
        ms = list(mo.index)
        labels = [M.RU[m - 1] for m in ms]

        def ser(metric, y):
            return [float(mo[(metric, y)].get(m, 0)) if (metric, y) in mo.columns else None for m in ms]
        ch = ('<div class="smalls two">'
              + "".join(f'<figure class="small"><figcaption>{esc(cap)}</figcaption>' +
                        C.lines(labels, [{"name": str(y0), "values": ser(met, y0), "color": "var(--neutral)"},
                                         {"name": str(y1), "values": ser(met, y1), "color": "var(--ch-mzp)",
                                          "emphasis": True}], height=200, y_from_zero=True) + "</figure>"
                        for met, cap in (("n_deals", "Сделок создано, по месяцам"),
                                         ("fact_qty", "Факт по сделкам, ФЛ")))
              + "</div>")
    tbt = mz["tb"]
    tbh = ""
    if tbt is not None and not tbt.empty:
        tbh = ("<h3>По ТБ</h3>" + table(["ТБ", f"Менеджеров {y0}", f"{y1}", f"НФЛ МЗП {y0}", f"{y1}",
                                          f"На менеджера {y0}", f"{y1}", "Изменение"],
                                         [[r.tb, fnum(r.staff_prev), fnum(r.staff_cur), fnum(r.nfl_prev),
                                           fnum(r.nfl_cur), fnum(r.per_prev, digits=1), fnum(r.per_cur, digits=1),
                                           dpct(A.pct(r.per_cur, r.per_prev))] for r in tbt.itertuples()]))
    dm = res.get("deal_match")
    dmh = ""
    if dm is not None and not dm.empty:
        lab = {"same_gosb": "есть ячейка ведомостей с этой организацией в этом ГОСБ",
               "other_gosb": "организация есть в ведомостях, но в другом ГОСБ — НФЛ к сделке не привяжутся",
               "no_org": "организации нет в ведомостях"}
        tot = float(dm["n_deals"].sum())
        dmh = ("<details><summary>Привязка сделок по ГОСБ</summary><p class='muted'>Сделка МЗП привязывается к НФЛ "
               "по организации и ГОСБ. Сделки в «чужом» ГОСБ НФЛ не получают — их доля показывает, сколько "
               "МЗП-привлечения может теряться на кодировке ГОСБ.</p>"
               + table(["Сделки", "Число", "Доля"], [[lab.get(r.match, r.match), fnum(r.n_deals),
                                                     fpct(r.n_deals / tot if tot else None)]
                                                    for r in dm.itertuples()]) + "</details>")
    return section("mzp", "МЗП: штат и продажи",
                   f"Штат — менеджеры по продаже зарплатных проектов на последний день периода. Сделки — "
                   f"с кодом сделки у сотрудников с ролью МЗП, созданные в {esc(M.RU[0])}–"
                   f"{esc(M.RU[M.parse(res['m_cur'][-1]).month - 1])}; план и факт — из последнего снимка воронки. "
                   f"НФЛ через МЗП — по атрибуции этого отчёта (сделка на организацию и ГОСБ, действовала на дату "
                   f"первой зарплаты).",
                   tb + ch + tbh + dmh + sql_box(res, ["staff_tot", "staff", "deal_month", "deal_match"]))


def s_dims(res: dict) -> str:
    d = res["dims"]
    bm = res["break_max"]
    y1, y0 = _y(res, "cur"), _y(res, "prev")
    tabs, panes = [], []
    for k, (dim, title) in enumerate(A.DIM_T.items()):
        t = d.get(dim)
        if t is None or t.empty:
            continue
        x = t
        note = ""
        if dim in ("holding", "industry") and len(x) > 2 * bm:
            x = pd.concat([x.head(bm), x.tail(bm)]).drop_duplicates("label")
            note = (f"<p class='muted'>Показаны {bm} с наибольшим падением НФЛ и {bm} с наибольшим ростом из "
                    f"{fnum(len(t))}. Поиск — по показанным.</p>")
        rows, attrs = [], []
        for r in x.itertuples():
            rows.append([r.label, fnum(r.all_prev), fnum(r.all_cur), delta(r.all_d), dpct(r.all_pct)]
                        + [delta(getattr(r, f"{c}_d")) for c in A.CH]
                        + [fnum(getattr(r, "n_grow_prev", float("nan"))), fnum(getattr(r, "n_grow_cur", float("nan")))])
            attrs.append(f' data-q="{esc(str(r.label).lower())}"')
        tid = f"dim-{dim}"
        head = [title, f"НФЛ {y0}", f"НФЛ {y1}", "Δ", "%"] + [f"Δ {A.CH_T[c]}" for c in A.CH] + \
            [f"Ячеек роста {res['cell_pairs']['prev']}", res["cell_pairs"]["cur"]]
        hm = ""
        if dim in ("seg", "tb"):
            xi = x.set_index("label")
            hm = C.heatmap(list(xi.index), [A.CH_T[c] for c in A.CH] + ["Всего"],
                           lambda r, c: float(xi.loc[r, ("all" if c == "Всего" else
                                                         {v: k2 for k2, v in A.CH_T.items()}[c]) + "_d"]),
                           lambda v: fnum(v, True), row_head=title, total_col="Всего",
                           tipf=lambda r, c, v: f"{r} · {c}|{fnum(v, True)} НФЛ год к году")
        search = (f'<div class="flt"><label>Поиск <input type="search" data-for="{tid}" '
                  f'placeholder="название"></label></div>' if dim in ("holding", "industry") else "")
        tabs.append(f'<button type="button" role="tab" data-pane="{dim}" aria-selected="{"true" if k == 0 else "false"}">'
                    f'{esc(title)}</button>')
        panes.append(f'<div class="pane" data-pane="{dim}"{"" if k == 0 else " hidden"}>{hm}{note}{search}'
                     + table(head, rows, tid=tid, row_attrs=attrs, cls="wide") + "</div>")
    return section("dims", "Привлечение год к году по разрезам",
                   "Разрез — по справочнику организаций (сегмент, холдинг, отрасль), один для обоих лет; ТБ — из НФЛ. "
                   "Δ по каналам складываются в Δ НФЛ разреза. Ячейки роста — число ячеек ГОСБ × организация "
                   "разреза, выросших август к августу. Холдинг с ФИО в названии показан как «название скрыто».",
                   f'<div class="tabs" role="tablist">{"".join(tabs)}</div>{"".join(panes)}'
                   + sql_box(res, ["nfl_dim", "cell_dim"]))


def s_overlap(res: dict) -> str:
    ov = res["overlap"]
    if ov is None or ov.empty:
        return ""
    y1, y0 = _y(res, "cur"), _y(res, "prev")
    pv = ov.pivot_table(index=["combo", "ch"], columns="yr", values="n", aggfunc="sum").fillna(0)
    pv["k"] = [0 if c == "нет действий" else c.count("+") + 1 for c, _ in pv.index]
    pv = pv.sort_values(["k", "cur"], ascending=[True, False])
    rows = [[combo, A.CH_T[ch], fnum(r.get("prev", 0)), fnum(r.get("cur", 0)),
             delta(r.get("cur", 0) - r.get("prev", 0))] for (combo, ch), r in pv.iterrows()]
    tb = table(["Какие каналы действовали", "Кому засчитано", y0, y1, "Δ"], rows, num={2, 3, 4})
    cov = []
    for c, f in (("mzp", "has_mzp"), ("vsp", "has_vsp"), ("dig", "has_dig")):
        a = float(ov.loc[(ov["yr"] == "cur") & ov[f], "n"].sum())
        b = float(ov.loc[(ov["yr"] == "prev") & ov[f], "n"].sum())
        w_a = float(ov.loc[(ov["yr"] == "cur") & (ov["ch"] == c), "n"].sum())
        w_b = float(ov.loc[(ov["yr"] == "prev") & (ov["ch"] == c), "n"].sum())
        cov.append([chip(c), fnum(b), fnum(a), delta(a - b), dpct(A.pct(a, b)), fnum(w_b), fnum(w_a)])
    tb = ("<h3>Охват: у скольких НФЛ канал действовал, даже если засчитан другому</h3>"
          + table(["Канал", f"Действовал {y0}", y1, "Δ", "%", f"Засчитан {y0}", y1], cov)
          + "<h3>Сочетания каналов</h3>" + tb)
    lg = res["lag"]
    lh = ""
    if lg is not None and not lg.empty:
        cols = list(lg.columns)
        r2 = []
        for c in A.CH[:-1]:
            for y in ("prev", "cur"):
                if (y, c) not in lg.index:
                    continue
                v = lg.loc[(y, c)]
                tot = v.sum()
                r2.append([A.CH_T[c], _y(res, y)] + [fpct(v[k] / tot if tot else None) for k in cols] + [fnum(tot)])
        lh = ("<h3>Сколько месяцев от действия до первой зарплаты</h3>"
              + table(["Канал", "Период"] + [f"{int(k)} мес." for k in cols] + ["НФЛ"], r2,
                      num=set(range(2, len(cols) + 3))))
    return section("overlap", "Пересечения каналов и задержка",
                   "У одного НФЛ может действовать несколько каналов — засчитывается самый ранний. Таблица "
                   "показывает, сколько привлечения делят каналы и как бы изменилась картина при другом правиле.",
                   tb + lh + sql_box(res, ["nfl_overlap", "nfl_lag"]))


def s_checks(res: dict) -> str:
    ch = res["checks"]
    bad = [c for c in ch if not c["ok"]]
    rows = [["✓" if c["ok"] else "✗", c["check"], "" if c["residual"] is None else fnum(c["residual"], digits=1),
             c["detail"]] for c in ch]
    ne = res["raw"].get("nfl_no_epk")
    ne_txt = (f"<li>НФЛ без ЕПК ФЛ в атрибуцию не идут: {fnum(float(ne['n'].sum()))} строк.</li>"
              if ne is not None and not ne.empty else "")
    lim = ("<ul>"
           "<li>НФЛ — строки витрины «ФЛ × организация» с признаком НФЛ без перетока; до "
           f"{esc(res['params']['nfl_split'])} — из архивной таблицы, после — из текущей.</li>"
           "<li>Окно действия одно для всех каналов: от дня действия до конца второго месяца после него. "
           "Действие после даты первой зарплаты не засчитывается.</li>"
           "<li>Сделка МЗП привязывается к НФЛ по организации и ГОСБ; ГОСБ перекодируется по правилам портфеля "
           "(ТБ 38, ГОСБ 1009, ГОСБ 0 в ТБ 40). Правило по сегменту КСБ у НФЛ и сделок не применяется — сегмента "
           "там нет.</li>"
           "<li>Сегмент, холдинг и отрасль — текущий срез справочника организаций для обоих лет.</li>"
           + ne_txt + "</ul>")
    tm = res.get("timing", {})
    tmh = ("<details><summary>Время шагов</summary>" + table(["Шаг", "с"], [[k, fnum(v, digits=1)] for k, v in
                                                                       sorted(tm.items(), key=lambda kv: -kv[1])[:15]])
           + "</details>") if tm else ""
    return section("checks", "Проверки и ограничения",
                   "Части обязаны складываться в итог; расхождение — ошибка расчёта, а не свойство данных.",
                   f"<p class='{'warn' if bad else 'ok'}'>{'Не сошлось: ' + str(len(bad)) if bad else 'Все проверки сошлись'}"
                   f" ({len(ch)}).</p>" + table(["", "Проверка", "Невязка", ""], rows, num={2}) + lim + tmh)


# --------------------------------------------------------------------------- #
CSS = """
:root{color-scheme:light;
 --page:#F5F6F3;--surface:#FFFFFF;--ink:#1D2733;--ink2:#4A5563;--muted:#7A838F;
 --grid:#E2E5E0;--axis:#C3C8C0;--ring:rgba(29,39,51,.12);
 --ch-mzp:#0F7B63;--ch-vsp:#B86E00;--ch-dig:#2F68D8;--ch-other:#9A9FA6;
 --series-1:#0F7B63;--neutral:#A4A9A2;
 --div-pos:#2F68D8;--div-neg:#C2423A;--div-mid:#EEF0EC;--on-strong:#FFFFFF;
 --pos:#2F68D8;--neg:#C2423A;--up:#1F5FC9;--down:#B6352D;--warn:#9A5B00;--good:#0F7B63;
 --strip-ink:#FFFFFF;--strip-ink-other:#1D2733}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
 --page:#14181D;--surface:#1B2027;--ink:#E8ECEF;--ink2:#B4BCC5;--muted:#8A939D;
 --grid:#2A3038;--axis:#3A424C;--ring:rgba(232,236,239,.12);
 --ch-mzp:#2FA585;--ch-vsp:#D98A1C;--ch-dig:#5B8DEB;--ch-other:#5A6068;
 --series-1:#2FA585;--neutral:#5E656E;
 --div-pos:#5B8DEB;--div-neg:#E0675F;--div-mid:#262C34;--pos:#5B8DEB;--neg:#E0675F;
 --up:#7EA6F2;--down:#EE8A83;--warn:#E3A33B;--good:#2FA585;
 --strip-ink:#0E1318;--strip-ink-other:#E8ECEF}}
:root[data-theme="dark"]{color-scheme:dark;
 --page:#14181D;--surface:#1B2027;--ink:#E8ECEF;--ink2:#B4BCC5;--muted:#8A939D;
 --grid:#2A3038;--axis:#3A424C;--ring:rgba(232,236,239,.12);
 --ch-mzp:#2FA585;--ch-vsp:#D98A1C;--ch-dig:#5B8DEB;--ch-other:#5A6068;
 --series-1:#2FA585;--neutral:#5E656E;
 --div-pos:#5B8DEB;--div-neg:#E0675F;--div-mid:#262C34;--pos:#5B8DEB;--neg:#E0675F;
 --up:#7EA6F2;--down:#EE8A83;--warn:#E3A33B;--good:#2FA585;
 --strip-ink:#0E1318;--strip-ink-other:#E8ECEF}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--page);color:var(--ink);
 font:15px/1.55 "Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif}
h1,h2,h3,.kicker,.strip-y,.strip-tot,.strip-v,.tabs button{font-family:Bahnschrift,"DIN Alternate","DIN 2014",
 "Arial Narrow","Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:1120px;margin:0 auto;padding-inline:max(16px,3vw)}
header{padding-block:28px 6px}
h1{font-size:30px;font-weight:600;letter-spacing:-.01em;margin:0 0 4px;line-height:1.15}
.meta,.muted{color:var(--ink2)}
.meta{font-size:13px}
nav.bar{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--page);
 border-bottom:1px solid var(--grid)}
nav.bar .wrap{display:flex;flex-wrap:wrap;gap:4px 18px;padding-block:9px}
nav.bar a{color:var(--ink2);text-decoration:none;font-size:13.5px}
nav.bar a:hover,nav.bar a:focus-visible{color:var(--ink);text-decoration:underline}
section{padding-block:30px 8px;border-top:1px solid var(--grid)}
section.hero{border-top:0;padding-top:12px}
h2{font-size:22px;font-weight:600;margin:0 0 6px}
h3{font-size:16.5px;font-weight:600;margin:24px 0 8px}
.lead{color:var(--ink2);max-width:76ch;margin:0 0 14px}
.kicker{font-size:17px;color:var(--ink2);margin:6px 0 12px}
svg.chart{width:100%;height:auto;display:block;margin:4px 0}
svg.chart{max-width:780px}
svg.strip,.smalls svg.chart{max-width:none}
svg.strip{max-width:1120px}
.strip-y{font-size:22px;font-weight:600;fill:var(--ink)}
.strip-in{font-size:13px;fill:var(--strip-ink)}
.strip-v{font-size:17px;font-weight:600;fill:var(--strip-ink)}
.strip-in.ch-other,.strip-v.ch-other{fill:var(--strip-ink-other)}
.strip-tot{font-size:20px;font-weight:600;fill:var(--ink)}
.legend{list-style:none;padding:0;margin:4px 0 0;display:flex;flex-wrap:wrap;gap:6px 26px;font-size:14px}
.chip{display:inline-block;width:11px;height:11px;border-radius:3px;background:var(--c);margin-right:7px;
 vertical-align:-1px}
.hero-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:8px 40px}
.verdict{padding-left:18px;margin:0;max-width:62ch}
.verdict li{margin:4px 0}
.up{color:var(--up)}.down{color:var(--down)}
.smalls{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:4px 36px;margin:4px 0 6px}
.smalls.two{grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr))}
figure.small{margin:0}
figure.small figcaption{font-size:14px;font-weight:600;margin:6px 0 0}
.scroll{overflow-x:auto;max-width:100%}
table{border-collapse:collapse;font-size:13.5px}
table.data th,table.data td{padding:5px 10px;border-bottom:1px solid var(--grid);text-align:left;vertical-align:top}
table.data th{color:var(--ink2);font-weight:500;position:sticky;top:0;background:var(--page)}
table.data .n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.data tr.strong td{font-weight:600}
table.data tr.sub td{color:var(--ink2);font-size:12.5px}
table.data tr.sub td:first-child{padding-left:24px}
table.wide td:first-child{min-width:200px}
table.heat th,table.heat td{padding:5px 9px;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.heat td{border:2px solid var(--page);border-radius:4px;min-width:72px}
table.heat th{color:var(--ink2);font-weight:500}
table.heat th.rh{text-align:left}
table.heat td.tot{font-weight:600}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .base{stroke:var(--axis);stroke-width:1}
svg .tick{fill:var(--muted);font-size:11px}
svg .rlabel{fill:var(--ink2);font-size:12.5px}
svg .rlabel.strong{fill:var(--ink);font-weight:600}
svg .vlabel{fill:var(--ink2);font-size:11.5px}
svg .dlabel{fill:var(--ink);font-size:12px}
svg .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .dot{stroke:var(--surface);stroke-width:2}
svg .pos{fill:var(--pos)}svg .neg{fill:var(--neg)}
svg .dim{opacity:.45}
svg .key-cur{fill:var(--ch-mzp)}svg .key-prev{fill:var(--neutral)}
svg:not(.strip) .hit rect:first-child{fill:transparent}
svg .hit .cross{stroke:var(--axis);stroke-width:1;visibility:hidden}
svg .hit:hover .cross,svg .hit:focus .cross{visibility:visible}
svg .hit:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
svg.strip .hit:hover rect{opacity:.85}
#tip{position:fixed;pointer-events:none;z-index:10;background:var(--surface);color:var(--ink);
 border:1px solid var(--ring);box-shadow:0 6px 18px rgba(20,24,29,.16);border-radius:6px;padding:6px 9px;
 font-size:12.5px;max-width:360px}
#tip b{display:block;font-size:13.5px}
details{margin:10px 0}
summary{cursor:pointer;color:var(--ink2)}
summary:focus-visible,button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--ch-dig);outline-offset:2px}
details.sql pre{background:var(--surface);border:1px solid var(--grid);border-radius:6px;padding:10px;
 overflow-x:auto;font-size:11.5px;line-height:1.4;max-height:420px}
.tabs{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0 10px;border-bottom:1px solid var(--grid)}
.tabs button{font-size:15px;font-weight:600;padding:6px 14px 7px;border:0;border-bottom:3px solid transparent;
 background:none;color:var(--ink2);cursor:pointer;margin-bottom:-1px}
.tabs button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--ch-mzp)}
.flt{margin:8px 0}
.flt input{font:inherit;font-size:13.5px;padding:4px 8px;border:1px solid var(--axis);border-radius:5px;
 background:var(--surface);color:var(--ink)}
.tag{font-size:11.5px;color:var(--warn);border:1px solid currentColor;border-radius:4px;padding:0 4px;margin-left:4px}
.warn{color:var(--warn);font-weight:600}.ok{color:var(--good);font-weight:600}
footer{padding-block:24px 40px;color:var(--muted);font-size:12.5px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media print{nav.bar{position:static}}
"""

JS = """
(function(){
var tip=document.getElementById('tip');
function show(el,x,y){var t=el.getAttribute('data-tip');if(!t)return;var p=t.split('|');
 tip.textContent='';var b=document.createElement('b');b.textContent=p[p.length>1?1:0];tip.appendChild(b);
 if(p.length>1){var h=document.createElement('div');h.textContent=p[0];tip.insertBefore(h,b);
  for(var i=2;i<p.length;i++){var d=document.createElement('div');d.textContent=p[i];tip.appendChild(d);}}
 tip.hidden=false;var w=tip.offsetWidth,hh=tip.offsetHeight;
 tip.style.left=Math.min(x+14,document.documentElement.clientWidth-w-8)+'px';tip.style.top=Math.max(8,y-hh-10)+'px';}
document.addEventListener('pointermove',function(e){var el=e.target.closest&&e.target.closest('[data-tip]');
 if(el)show(el,e.clientX,e.clientY);else tip.hidden=true;});
document.addEventListener('focusin',function(e){var el=e.target.closest&&e.target.closest('[data-tip]');
 if(el){var r=el.getBoundingClientRect();show(el,r.left+r.width/2,r.top);}});
document.addEventListener('focusout',function(){tip.hidden=true;});
document.querySelectorAll('.tabs').forEach(function(t){var sec=t.parentNode;
 t.querySelectorAll('button').forEach(function(b){b.addEventListener('click',function(){
  t.querySelectorAll('button').forEach(function(x){x.setAttribute('aria-selected',x===b?'true':'false');});
  sec.querySelectorAll('.pane').forEach(function(p){p.hidden=p.dataset.pane!==b.dataset.pane;});});});});
document.querySelectorAll('input[data-for]').forEach(function(inp){var tb=document.getElementById(inp.dataset.for);
 if(!tb)return;var rows=tb.querySelectorAll('tbody tr');
 inp.addEventListener('input',function(){var q=inp.value.toLowerCase();
  rows.forEach(function(r){r.hidden=q&&r.dataset.q.indexOf(q)<0;});});});
})();
"""


def render(res: dict) -> str:
    y1, y0 = M.parse(res["m_cur"][-1]).year, M.parse(res["m_prev"][-1]).year
    title = f"Привлечение получателей зарплаты: {_y(res, 'cur')} против {y0}"
    nav = [("hero", "Итог"), ("channels", "Каналы по месяцам"), ("cells", "Ячейки роста"), ("mzp", "МЗП"),
           ("dims", "Разрезы"), ("overlap", "Пересечения"), ("checks", "Проверки")]
    body = "".join([s_hero(res), s_channels(res), s_cells(res), s_mzp(res), s_dims(res), s_overlap(res),
                    s_checks(res)])
    page = (f"<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
            f"<header class='wrap'><h1>{esc(title)}</h1><div class='meta'>Сформирован {esc(res['generated'])}, "
            f"схемы {esc(res['schema'])} и {esc(res['schema_t'])}</div></header>"
            f"<nav class='bar'><div class='wrap'>" + "".join(f"<a href='#{a}'>{esc(t)}</a>" for a, t in nav) +
            f"</div></nav><main class='wrap'>{body}</main>"
            f"<footer class='wrap'>Каналы: МЗП — сделка менеджера по зарплатным проектам; ВСП — заявление в офисе; "
            f"Digital — заявка в СБОЛ; Остальное — НФЛ без действующего действия.</footer>"
            f"<div id='tip' hidden></div><script>{JS}</script></body></html>")
    return sanitize(page)
