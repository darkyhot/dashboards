"""Обезличенный markdown-документ — единственное, что можно отправлять наружу.

* названия организаций, холдингов и ТБ → устойчивые токены (`Орг-01`, `ТБ-02`):
  один объект везде назван одинаково, рассуждать о нём можно, узнать — нельзя;
* номера организаций УДАЛЯЮТСЯ, а не маскируются: рядом стоят численность и
  территория, и токен от номера не защитил бы;
* заглушки («Не в справочнике») — не имена и токенов не получают;
* все численности, доли и даты сохраняются.

Обезличивание не декларируется, а ПРОВЕРЯЕТСЯ: готовый текст сверяется с каждым
настоящим названием из данных, и при единственном совпадении файл не пишется.
"""
from __future__ import annotations

import html
import re

import pandas as pd

from . import analyze as A
from . import months as M
from . import segments as S
from . import view as V
from .charts import fnum, fpct


class Aliases:
    def __init__(self) -> None:
        self.by_name: dict[tuple[str, str], str] = {}
        self.n: dict[str, int] = {}

    def __call__(self, kind: str, name) -> str:
        name = "" if name is None or (isinstance(name, float) and pd.isna(name)) else str(name).strip()
        if not name or name in S.PLACEHOLDERS or name.startswith("Организация не в"):
            return name or "—"
        key = (kind, name)
        if key not in self.by_name:
            self.n[kind] = self.n.get(kind, 0) + 1
            self.by_name[key] = f"{kind}-{self.n[kind]:02d}"
        return self.by_name[key]


def _md_table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |")
    return "\n".join(out) + "\n"


def _plain(h: str) -> str:
    h = re.sub(r"<li>", "\n- ", h)
    h = re.sub(r"</p>|</ul>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    return html.unescape(re.sub(r"\n{3,}", "\n\n", h)).strip()


def _names(res: dict) -> set[str]:
    """Всё, чего в документе быть не должно: номера организаций (названий отчёт
    не берёт вовсе — у ИП в названии ФИО) и названия ТБ."""
    out: set[str] = set()
    frames = [o[k] for o in res.get("orgs", {}).values() for k in ("list", "reorg")]
    if res.get("aug_raw"):
        frames += [res["aug_raw"].get("hole_cur"), res["aug_raw"].get("hole_prev")]
    for df in frames:
        if df is None or df.empty:
            continue
        for c in ("inn", "inn_from", "inn_to", "top_dest_inn"):
            if c in df:
                # Только длинные номера: короткое число совпало бы с численностью.
                out |= {str(int(x)) for x in df[c].dropna() if len(str(int(x))) >= 5}
    tbd = res.get("tb_dim")
    if tbd is not None and not tbd.empty:
        out |= {str(x).strip() for x in tbd["tb_short_name"].dropna()}
    return {n for n in out if len(n) >= 4 and n not in S.PLACEHOLDERS
            and not n.startswith("Организация не в")}


def render(res: dict) -> tuple[str, list[str]]:
    al = Aliases()
    rep = res["report"]
    last = rep[-1]
    L = []
    L.append(f"# Получатели ЗП по всему Сберу: {M.name(rep[0])}–{M.name(last)} {M.parse(last).year} "
             f"к {M.parse(last).year - 1}\n")
    L.append("**Документ обезличен.** Названия организаций, холдингов и ТБ заменены устойчивыми токенами "
             "(Орг-01, ТБ-01); номера организаций удалены; численности, доли и даты — как есть. "
             "Запросы, которыми посчитаны цифры, — в приложении.\n")
    L.append("## Определения\n")
    L.append(f"- {A.DEF_REC}\n- {A.DEF_GETS}\n- Сегмент организации — текущий срез справочника ЕПК, "
             f"один на оба года.\n- Совместительство — «лишние» получатели: получатели − ФЛ.\n")

    L.append("## Итог\n")
    mb = res["multi_bank"].set_index("report_dt")
    rows = []
    for m in rep:
        p = M.iso(M.shift(m, -12))
        rows.append([M.long(m), fnum(mb.loc[p, "n_triples"]), fnum(mb.loc[m, "n_triples"]),
                     fnum(res["yoy"][m], True), fpct(res["yoy"][m] / mb.loc[p, "n_triples"], True),
                     fnum(res["yoy_epk"][m], True)])
    L.append(_md_table(["Месяц", "Получателей год назад", "Сейчас", "Изменение", "%", "Изменение ФЛ"], rows))
    L.append(_plain(V.verdict(res)) + "\n")

    br = res.get("bridge")
    if br is not None and not br.empty:
        ms = br.attrs["months"]
        y1 = M.parse(ms["cur"]).year
        L.append(f"## {M.name(ms['cur']).capitalize()}: потеря или перенос в {M.name(ms['next'])}\n")
        b = br.set_index("key")
        titles = [("lvl_cur", f"Получателей, {y1}"), ("lvl_prev", f"Получателей, {y1 - 1}"),
                  ("yoy", "Год к году"), ("mom_cur", f"К предыдущему месяцу, {y1}"),
                  ("mom_prev", f"К предыдущему месяцу, {y1 - 1}"), ("did", "Разница переходов")]
        L.append(_md_table(["", M.name(ms["prev"]), M.name(ms["cur"]), M.name(ms["next"])],
                           [[t] + [fnum(b.loc[k, c], k not in ("lvl_cur", "lvl_prev"))
                                   for c in ("prev", "cur", "next")] for k, t in titles]))
        L.append(f"Вывод: {A.august_verdict(br, res['temp_perm'])}\n")
        tp = res["temp_perm"]
        L.append(_md_table(["Переставшие", str(y1), str(y1 - 1), "Разница"],
                           [[r.title, fnum(r.cur, True), fnum(r.prev, True), fnum(r.diff, True)]
                            for r in tp.itertuples()]))
        aug = res["aug_raw"]
        hc, hp = A.hole_summary(aug["hole_cur"]), A.hole_summary(aug["hole_prev"])
        L.append(f"Организации, пропустившие {M.name(ms['cur'])} (были в {M.prep(ms['prev'])}, "
                 f"вернулись в {M.prep(ms['next'])}): {y1} — {fnum(hc['n_orgs'])} организаций, "
                 f"{fnum(hc['sum_hole'])} получателей; {y1 - 1} — {fnum(hp['n_orgs'])}, "
                 f"{fnum(hp['sum_hole'])}.\n")
        hl = aug["hole_cur"]
        if hl is not None and not hl.empty:
            L.append(_md_table(["Организация", "Сегмент", M.name(ms["prev"]), M.name(ms["cur"]),
                                M.name(ms["next"]), "Дыра"],
                               [[al("Орг", int(r.inn)), r.seg, fnum(r.n_prev), fnum(r.n_cur),
                                 fnum(r.n_next), fnum(r.hole)] for r in hl.head(15).itertuples()]))
        pr = []
        for tag, y in (("cur", y1), ("prev", y1 - 1)):
            d = aug[f"pay_{tag}"]
            for r in (d.itertuples() if d is not None and not d.empty else []):
                grp = "пропали и вернулись" if r.grp == "gap" else "получали все три месяца"
                pr.append([y, grp, fnum(r.n_epk), fnum(float(r.median_ratio), digits=2),
                           fpct(float(r.share_double)), fpct(float(r.share_single))])
        if pr:
            L.append(_md_table(["Год", "Группа ФЛ", "ФЛ", f"Медиана {M.name(ms['next'])}/{M.name(ms['prev'])}",
                                "Доля ≥1,6×", "Доля 0,7–1,4×"], pr))

    for m in rep:
        cp = res["comp"][("yoy", m)]
        L.append(f"## Разложение: {M.long(cp['b'])} → {M.long(cp['c'])}\n")
        L.append(_md_table(["", "Получатели", "ФЛ"],
                           [[r["title"].strip(), fnum(r["tr"], r["key"] not in ("base", "cur")),
                             "" if pd.isna(r["epk"]) else fnum(r["epk"], r["key"] not in ("base", "cur"))]
                            for r in cp["rows"]]))
        cz = cp["causes"]
        L.append(_md_table(["Ситуация", "Получатели", "ФЛ", "Доля ФЛ"],
                           [[("перестали: " if r.side == "lost" else "начали: ") + r.title, fnum(r.tr),
                             fnum(r.epk), fpct(r.share)] for r in cz.itertuples()]))
        sd = cp["seg"]
        L.append("По сегментам (получатели):\n")
        L.append(_md_table(["Сегмент", "Было", "Стало"] + [t for _, t in A.SEG_COLS] + ["Изменение"],
                           [[r.seg, fnum(r.base), fnum(r.cur)] + [fnum(getattr(r, k), True) for k, _ in A.SEG_COLS]
                            + [fnum(r.delta, True)] for r in sd.itertuples()]))

        did = res["did"][m]
        tab = did["table"]
        mp = M.shift(m, -1)
        y1, y0 = M.parse(m).year, M.parse(m).year - 1
        L.append(f"### Почему {M.name(m)} к {M.gen(mp)}: ΔYoY изменилась на {fnum(did['expect'], True)}\n")
        grp_t = {"lost": A.T_LOST, "gained": A.T_GAINED, "inside": A.T_INSIDE}
        L.append(_md_table(["Слагаемое", f"{M.name(mp)}→{M.name(m)} {y1}", f"{M.name(mp)}→{M.name(m)} {y0}",
                            "Разница"],
                           [[("— " + r.title) if isinstance(r.sub, str) else f"**{grp_t[r.group]}**",
                             fnum(r.cur, True), fnum(r.prev, True), fnum(r.diff, True)] for r in tab.itertuples()]))
        if not did["seg"].empty:
            L.append(_md_table(["Сегмент"] + [t for _, t in A.SEG_COLS] + ["Итого"],
                               [[r.seg] + [fnum(getattr(r, k), True) for k, _ in A.SEG_COLS] + [fnum(r.delta, True)]
                                for r in did["seg"].itertuples()]))

    ds = res["dissolved"]
    if not ds.empty:
        L.append("## Растворились ли пришедшие\n")
        L.append(_md_table(["Пришли", "Размер", f"Дожили до {M.gen(last)}", "Размер год назад",
                            "Дожили год назад", "Лишняя убыль ФЛ"],
                           [[M.long(r.k), fnum(r.size_cur), fpct(r.surv_cur), fnum(r.size_prev),
                             fpct(r.surv_prev), fnum(-r.excess_loss if pd.notna(r.excess_loss) else None, True)]
                            for r in ds.itertuples()]))

    my = res["multi_yoy"]
    if not my.empty:
        L.append("## Совместительство год к году\n")
        L.append(_md_table(["Месяц", "", "Доля лишних было", "стало", "Δ организации", "Δ ГОСБ", "Δ всего"],
                           [[M.name(r.report_dt), r.seg, fpct(r.share_base), fpct(r.share_cur),
                             fnum(r.extra_inn, True), fnum(r.extra_gosb, True), fnum(r.extra, True)]
                            for r in my.itertuples()]))

    st = res["seasonal"]
    if not st.empty:
        L.append("## Сезонность (повтор год к году)\n")
        L.append(_md_table(["Месяц", "Получали в пред. месяце оба года", "Пропали год назад", "Пропали сейчас",
                            "Пропали оба года", "Вернулись год назад", "Вернулись сейчас"],
                           [[M.name(r.report_dt), fnum(r.n_prev_both), fnum(r.n_gone_base), fnum(r.n_gone_cur),
                             fnum(r.n_seasonal), fpct(r.back_base_share), fpct(r.back_cur_share)]
                            for r in st.itertuples()]))

    cp = res["comp"][("yoy", last)]
    mx = cp["mx"]
    if not mx.empty:
        L.append(f"## Перетоки ФЛ между сегментами: {M.long(cp['b'])} → {M.long(cp['c'])}\n")
        L.append(_md_table(["откуда \\ куда"] + list(mx.columns),
                           [[r] + [fnum(mx.loc[r, c]) for c in mx.columns] for r in mx.index]))

    tb = res["tb"].get(last)
    if tb is not None and not tb.empty:
        L.append(f"## Территория: изменение получателей год к году, {M.long(last)}\n")
        cols = [c for c in tb.columns if c != "base_total"]
        L.append(_md_table(["ТБ"] + cols, [[al("ТБ", r)] + [fnum(tb.loc[r, c], True) for c in cols]
                                            for r in tb.index]))

    for m in rep:
        o = res["orgs"][m]
        smr, lst = o["summary"], o["list"]
        L.append(f"## Организации с реальным сокращением: {M.long(M.shift(m, -12))} → {M.long(m)}\n")
        if not smr.empty:
            L.append(_md_table(["Класс", "Организаций", "ФЛ было", "ФЛ стало", "Перестали в Сбере",
                                "Ушли в др. орг.", "Реорг.", "Реальное сокращение"],
                               [[r.title, fnum(r.n_orgs), fnum(r.base_fl), fnum(r.cur_fl), fnum(r.out_stopped),
                                 fnum(r.out_moved), fnum(r.out_reorg), fnum(r.real_cut)] for r in smr.itertuples()]))
        if not lst.empty:
            top = lst.head(30)
            L.append(f"Крупнейшие 30 из {fnum(int(lst['n_picked'].iloc[0]))}:\n")
            L.append(_md_table(["Организация", "Сегмент", "ТБ", "Было", "Стало", "Реальное сокращение",
                                "Перестали в Сбере", "Переток", "Пришли новые"],
                               [[al("Орг", int(r.inn)), r.seg, al("ТБ", r.tb), fnum(r.base_fl),
                                 fnum(r.cur_fl), fnum(r.real_cut), fnum(r.out_stopped), fnum(r.out_moved),
                                 fnum(r.in_new)] for r in top.itertuples()]))

    ch = res["checks"]
    bad = [c for c in ch if not c["ok"]]
    L.append("## Проверки сходимости\n")
    L.append(f"{len(ch) - len(bad)} из {len(ch)} сошлись." + ("" if not bad else
             " Не сошлись: " + "; ".join(c["check"] for c in bad)) + "\n")

    L.append("## Приложение: запросы\n")
    shown = res.get("shown", {})
    keys = ["multi_bank", f"fl_flow_yoy_{last}", f"fl_flow_mom_{last}", f"triple_flow_yoy_{last}",
            f"org_list_{last}"]
    for k in keys:
        if k in shown:
            sql, args = shown[k]
            used = {a: v for a, v in args.items() if f":{a}" in sql}
            L.append(f"### {k}\n\n```sql\n{V._params_comment(used)}{sql}\n```\n")

    doc = V.sanitize("\n".join(L))
    leaks = sorted(n for n in _names(res) if n in doc)
    return doc, leaks
