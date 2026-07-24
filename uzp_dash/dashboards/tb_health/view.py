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
    progress.step("LLM: нарратив «что плохо и что делать»")
    story = prompts.narrative(ctx, a)
    progress.step("Сборка HTML")

    body = (
        _hero(a)
        + _kpis(a)
        + _matrix(a)
        + _problem_gosb(a)
        + _orgs(a)
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
          f'недобор {C.fmt_num(a.gap_fot_mln)} млн ₽</div>'
    )
    return C.card(inner, cls="hero")


def _kpis(a: analyze.Analysis) -> str:
    def kpi(title, d, unit, scale=1.0):
        return C.card(
            f'<div class="label">{C.esc(title)}</div>'
            f'<div class="value">{C.fmt_num(d["fact"] / scale, unit)}</div>'
            f'<div class="delta">план {C.fmt_num(d["plan"] / scale, unit)} · '
            f'<b style="color:{_col(d["exec"])}">{(d["exec"] or 0)*100:.0f}%</b></div>'
            + C.meter(d["exec"]),
            cls="kpi",
        )
    # ФОТ в БД — рубли, выводим в млн ₽ (÷ 1e6)
    cards = (kpi("Получатели, чел", a.verdict["rcp"], "")
             + kpi("Общий ФОТ, млн ₽", a.verdict["fot"], "", scale=1e6))
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
        st = "warn" if c["seg_only"] else ("bad" if c["exec"] < 0.9 else "warn")
        # строки по западающим сегментам: разрыв -> сколько организаций нужно
        lines = []
        for s in c["segs"][:5]:
            cov = s["coverage"]
            if not s["n_avail"]:
                tail = " · своих организаций к работе нет — добор из других сегментов"
            elif cov is not None and cov < 0.999:
                tail = (f' · в сегменте хватает на {cov*100:.0f}% ({s["n_avail"]} орг), '
                        f'добор из других')
            else:
                tail = ""
            chip = C.badge("%s %.0f%%" % (s["seg"], s["exec"] * 100), C.status_of(s["exec"]))
            lines.append(
                f'<div class="g-seg">{chip}'
                f' −{C.fmt_num(s["nedobor"])} чел → <b>{s["n_need"]}</b> орг '
                f'(+{C.fmt_num(s["fl_need"])}){C.esc(tail)}</div>')
        seg_html = "".join(lines) or '<div class="g-seg">в норме по сегментам</div>'
        act = c["act"]
        if c["seg_only"]:
            head_badge = C.badge(f'план выполняется, но западает '
                                 f'{", ".join(s["seg"] for s in c["segs"][:3])}', "warn")
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
            f'<div class="g-act">Активности 3 мес: {act["act_n"]} по {act["worked_orgs"]} орг, '
            f'успех {act["success"]*100:.0f}% · из нужных под план не работали с '
            f'<b>{c["not_worked"]}</b></div>'
        )
        inner = (
            f'<div class="g-head"><h3 style="margin:0">{C.esc(c["gosb_name"])}</h3>'
            f'<span class="g-ex" style="color:{_col(c["exec"])}">{c["exec"]*100:.0f}%</span></div>'
            f'<div style="margin:2px 0 6px">{head_badge}</div>'
            f'{seg_html}{do}'
        )
        cards.append(C.card(inner, cls=f"gcard {st}"))
    grid = f'<div class="gcards">{"".join(cards)}</div>'
    return C.section("Проблемные ГОСБ — что сделать по каждому", grid,
                     eyebrow="Приоритет по ГОСБ и сегменту")


def _orgs(a: analyze.Analysis) -> str:
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
    bad_segs = sorted({s["seg"] for c in a.gosb_cards for s in c["segs"]},
                      key=lambda x: SEG_ORDER.index(x) if x in SEG_ORDER else 99)
    head = (f'<h3>С кем работать — {sim["k"]} организаций закрывают план</h3>'
            f'<p class="sub" style="font-size:14px;margin:-4px 0 14px">'
            f'отбор идёт внутри ЗАПАДАЮЩИХ сегментов каждого ГОСБ '
            f'({C.esc(", ".join(bad_segs)) or "—"}), по величине эффекта, пока разрыв '
            f'сегмента не закрыт · переключатель «Цель» задаёт перевыполнение · '
            f'всего кандидатов {len(rows)}</p>')
    return C.section("Организации к работе", C.card(head + explorer), eyebrow="Список к отработке")


def _log_llm_stats(a: analyze.Analysis) -> None:
    s = a.llm_stats or {}
    if not s:
        return
    progress.done(
        f"Разбор текста: кандидатов {s.get('cand',0)} (top-{s.get('top_n')} на ГОСБ) · "
        f"чек-лист {s.get('checklist',0)} · ключевые слова {s.get('keyword',0)} · "
        f"без текста {s.get('no_text',0)} · LLM {s.get('llm',0)} "
        f"(батчей {s.get('batches',0)} по {s.get('batch')}) · фолбэк {s.get('fallback',0)}"
    )


def _col(exec_pct):
    return {"good": "var(--good)", "warn": "var(--warn)", "bad": "var(--bad)"}[C.status_of(exec_pct)]
