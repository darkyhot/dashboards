"""Сборка apple-страницы дэша tb_health.

Структура ГОСБ-центричная: сверху вердикт ТБ, затем — какие ГОСБ проблемные и
что конкретно сделать по каждому, интерактивный список организаций к работе
(поиск/фильтр/пагинация), симуляция и нарратив.
"""
from __future__ import annotations

from ...registry import Context, dashboard
from ...render import components as C
from ...render import page
from . import analyze, prompts, segments

SEG_ORDER = segments.ORDER   # короткие названия сегментов (КСБ, РГС, …)


@dashboard("tb_health")
def build(ctx: Context) -> str:
    tb = ctx.params.get("tb", "ЮЗБ")
    a = analyze.run(ctx, tb)
    insights = prompts.text_insights(ctx, a.priority_text)   # LLM: свободный текст
    story = prompts.narrative(ctx, a)                        # LLM: нарратив

    body = (
        _hero(a)
        + _kpis(a)
        + _matrix(a)
        + _problem_gosb(a)
        + _orgs(a, insights)
        + C.section("Что делать — резюме", C.card(C.narrative_html(story)), eyebrow="AI")
    )
    return page(
        title=f"Здоровье ТБ — {C.esc(a.tb_full)}",
        subtitle=f"Текущая ситуация на {C.esc(a.ref_date)} · получатели и ФОТ",
        body=body,
        footer="УЗП · дэш tb_health. Данные — синтетические (открытый контур).",
    )


# --------------------------------------------------------------------------- #
def _hero(a: analyze.Analysis) -> str:
    r = a.verdict["rcp"]
    st = C.status_of(r["exec"])
    word = {"good": "План выполняется", "warn": "План под угрозой", "bad": "План не выполняется"}[st]
    rank = f'{r["rank"]}/{r["n_tb"]}' if r["rank"] else "—"
    inner = (
        f'<div class="eyebrow">Вердикт по получателям</div>'
        f'<div class="verdict">{C.esc(word)} · <span class="big">{(r["exec"] or 0)*100:.0f}%</span> плана</div>'
        f'<div>{C.badge("−" + C.fmt_num(a.gap_rcp) + " получателей до плана", st)} '
        f'{C.badge("ранг ТБ " + rank, "warn" if r["rank"] and r["rank"] > r["n_tb"]/2 else "good")}</div>'
        + C.meter(r["exec"])
        + f'<div class="row2">ФОТ: {(a.verdict["fot"]["exec"] or 0)*100:.0f}% плана · '
          f'недобор {C.fmt_num(a.gap_fot)} млн ₽</div>'
    )
    return C.card(inner, cls="hero")


def _kpis(a: analyze.Analysis) -> str:
    def kpi(title, d, unit):
        return C.card(
            f'<div class="label">{C.esc(title)}</div>'
            f'<div class="value">{C.fmt_num(d["fact"], unit)}</div>'
            f'<div class="delta">план {C.fmt_num(d["plan"], unit)} · '
            f'<b style="color:{_col(d["exec"])}">{(d["exec"] or 0)*100:.0f}%</b></div>'
            + C.meter(d["exec"]),
            cls="kpi",
        )
    cards = kpi("Получатели, чел", a.verdict["rcp"], "") + kpi("Общий ФОТ, млн ₽", a.verdict["fot"], "")
    return f'<div class="grid cols-2">{cards}</div>'


def _matrix(a: analyze.Analysis) -> str:
    m = a.matrix
    if m.empty:
        return ""
    gg = a.gosb_gap.sort_values("nedobor", ascending=False)
    present = set(m.new_gosb_id)
    rows_id_label = [(int(r.new_gosb_id), (r.gosb_name or "")[:26])
                     for r in gg.itertuples() if int(r.new_gosb_id) in present]
    segs = [s for s in SEG_ORDER if s in set(m.seg_name)]
    cells = {(int(row.new_gosb_id), row.seg_name): (row.execution_percent, row.nedobor)
             for row in m.itertuples()}
    heat = C.card(
        '<h3>Выполнение плана по получателям, %</h3>'
        '<p class="sub" style="font-size:14px;margin:-4px 0 12px">красное — сильнее отстаёт от плана</p>'
        + C.heat_matrix(rows_id_label, segs, cells))
    top = [(f'{r.gosb_name} · {r.seg_name}',
            C.badge(f'{r.execution_percent*100:.0f}%', C.status_of(r.execution_percent)),
            C.fmt_num(r.nedobor), f'{r.share*100:.0f}%') for r in a.top_cells.itertuples()]
    top_tbl = C.card('<h3>Наибольший вклад в недобор</h3>'
                     + C.table(["Провальная зона", "Выполн.", "Недобор, чел", "Доля разрыва"],
                               top, num_cols=[2, 3]))
    return C.section("Где провал — ГОСБ × сегмент",
                     f'<div class="grid cols-2">{heat}{top_tbl}</div>',
                     eyebrow="Диагностика")


def _problem_gosb(a: analyze.Analysis) -> str:
    if not a.gosb_cards:
        return ""
    cards = []
    for c in a.gosb_cards:
        st = "bad" if c["exec"] < 0.9 else "warn"
        # компактные чипы западающих сегментов (детали — в тепловой карте выше)
        fails = [s for s in sorted(c["segs"], key=lambda x: x["exec"]) if s["exec"] < 0.95][:4]
        chips = "".join(C.badge(f'{s["seg"]} {s["exec"]*100:.0f}%', C.status_of(s["exec"]))
                        for s in fails) or '<span class="clab">в норме по сегментам</span>'
        act = c["act"]
        do = (
            f'<div class="g-do">Привлечь <b>{c["n_attract"]}</b> орг '
            f'(+{C.fmt_num(c["pot_fl_att"])} чел) · вернуть <b>{c["n_return"]}</b> '
            f'(+{C.fmt_num(c["pot_fl_ret"])}) · ФОТ <b>~{C.fmt_num(c["pot_fot"])}</b> млн ₽</div>'
            f'<div class="g-act">Активности 3 мес: {act["act_n"]} по {act["worked_orgs"]} орг, '
            f'успех {act["success"]*100:.0f}% · не работали с '
            f'<b>{c["not_worked"]}</b> приоритетными</div>'
        )
        inner = (
            f'<div class="g-head"><h3 style="margin:0">{C.esc(c["gosb_name"])}</h3>'
            f'<span class="g-ex" style="color:{_col(c["exec"])}">{c["exec"]*100:.0f}%</span></div>'
            f'<div style="margin:2px 0 4px">{C.badge("−" + C.fmt_num(c["gap"]) + " чел до плана", st)}</div>'
            f'<div class="chips"><span class="clab">западают:</span>{chips}</div>'
            f'{do}'
        )
        cards.append(C.card(inner, cls=f"gcard {st}"))
    grid = f'<div class="gcards">{"".join(cards)}</div>'
    return C.section("Проблемные ГОСБ — что сделать по каждому", grid, eyebrow="Приоритет по ГОСБ")


def _orgs(a: analyze.Analysis, insights: dict) -> str:
    rows = []
    for r in a.to_work.itertuples():
        ins = insights.get(int(r.inn), {})
        rows.append({
            "inn": int(r.inn), "company": (getattr(r, "company_name", "") or "")[:48],
            "lever": r.lever,
            "gosb": (r.gosb_name or "")[:28], "seg": r.seg_name or "—",
            "fl": round(float(r.impact_fl)), "fot": round(float(r.impact_fot_mln), 1),
            "reason": ins.get("reason") or r.reason, "action": ins.get("action", ""),
        })
    gosb_opts = sorted({row["gosb"] for row in rows})
    seg_opts = [s for s in SEG_ORDER if s in {row["seg"] for row in rows}]
    explorer = C.orgs_explorer("work", rows, gosb_opts, seg_options=seg_opts)
    head = (f'<h3>С кем работать — {len(rows)} организаций</h3>'
            f'<p class="sub" style="font-size:14px;margin:-4px 0 14px">'
            f'поиск, фильтр по ГОСБ и рычагу, листание</p>')
    return C.section("Организации к работе", C.card(head + explorer), eyebrow="Список к отработке")


def _col(exec_pct):
    return {"good": "var(--good)", "warn": "var(--warn)", "bad": "var(--bad)"}[C.status_of(exec_pct)]
