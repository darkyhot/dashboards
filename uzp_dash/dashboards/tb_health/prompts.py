"""LLM-слой дэша: анализ свободного текста активностей и исполнительный нарратив.

В LLM уходят ТОЛЬКО пары (ГОСБ, ИНН), у которых есть содержательный текст и не
сработали детерминированные правила (чек-лист/ключевые слова). Батчами, с
проверкой покрытия: непокрытые добираются детерминированным фолбэком в analyze.
"""
from __future__ import annotations

import json
import re

from ... import progress


def _parse_json(text: str):
    """Достать JSON из ответа LLM (снять ```-обёртки, найти массив/объект)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    m = re.search(r"[\{\[].*[\}\]]", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
def text_insights(ctx, items: list[dict], batch: int = 30) -> tuple[dict, int]:
    """Анализ свободного текста по парам (ГОСБ, ИНН).

    items: [{gosb_id, inn, gosb, company, segment, notes:[{text,...}]}, ...]
    Возврат: ({(gosb_id, inn): {reason, action, worth, verdict, source}}, n_батчей)
    """
    result: dict = {}
    if not items:
        return result, 0

    n_batches = 0
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        n_batches += 1
        label = f"текст {start + 1}–{start + len(chunk)} из {len(items)}"
        lines = []
        for o in chunk:
            notes = " ⏵ ".join(n["text"] if isinstance(n, dict) else str(n)
                               for n in o["notes"])
            lines.append(
                f'gosb_id={o["gosb_id"]} inn={o["inn"]} | {o["company"]} '
                f'| ГОСБ: {o["gosb"]} | сегмент: {o["segment"]} | активности: {notes}'
            )
        prompt = (
            "Ты — аналитик зарплатных проектов банка. Ниже по каждой паре "
            "(ГОСБ, организация) даны ВСЕ содержательные комментарии сотрудников и "
            "анкеты по задачам за 3 месяца. Работа ведётся отдельно в каждом ГОСБ.\n"
            "Для КАЖДОЙ строки верни объект в JSON-массиве с полями:\n"
            '  "gosb_id" (число, как во входе), "inn" (число, как во входе),\n'
            '  "reason" (краткая причина текущего положения, ≤8 слов),\n'
            '  "worth" ("да"|"нет" — есть ли смысл вести работу в ЭТОМ ГОСБ),\n'
            '  "action" (что конкретно сделать, ≤12 слов).\n'
            "Верни ровно столько объектов, сколько строк на входе. Только JSON.\n\n"
            + "\n".join(lines)
        )
        progress.llm_request(label, prompt)
        try:
            raw = ctx.llm(prompt, temperature=0.1)
            progress.llm_response(label, raw)
            data = _parse_json(raw)
            if data is None:
                progress.llm_error(label, "невалидный JSON — батч уйдёт в фолбэк")
                data = []
        except Exception as ex:
            progress.llm_error(label, ex)
            data = []

        got = 0
        for r in data if isinstance(data, list) else []:
            try:
                key = (int(r["gosb_id"]), int(r["inn"]))
            except (KeyError, ValueError, TypeError):
                continue
            worth = str(r.get("worth", "")).strip().lower()
            result[key] = {
                "reason": str(r.get("reason", ""))[:80],
                "action": str(r.get("action", ""))[:100],
                "worth": worth,
                "verdict": "no_point" if worth.startswith("нет") else "work",
                "source": "LLM",
            }
            got += 1
        if got < len(chunk):
            progress.llm_error(label, f"покрыто {got} из {len(chunk)} — остальные фолбэком")

    return result, n_batches


# --------------------------------------------------------------------------- #
def narrative(ctx, a) -> str:
    """Исполнительный нарратив «что плохо и что делать» для управляющего ТБ."""
    v = a.verdict
    cells = "; ".join(
        f"{r.gosb_name}/{r.seg_name} ({r.execution_percent*100:.0f}%, −{r.nedobor:.0f})"
        for r in a.top_cells.head(5).itertuples()
    )
    top_orgs = "; ".join(
        f'{(getattr(r, "company_name", "") or r.inn)} [{r.gosb_name}] '
        f'({r.lever}, +{r.impact_fl:.0f} чел)'
        for r in a.to_work.head(6).itertuples()
    )
    ctx_txt = (
        f"ТБ: {a.tb_full}. Опорный месяц: {a.ref_date}.\n"
        f"Получатели: факт {v['rcp']['fact']:.0f} из плана {v['rcp']['plan']:.0f} "
        f"({v['rcp']['exec']*100:.0f}%, ранг {v['rcp']['rank']}/{v['rcp']['n_tb']}), "
        f"недобор {a.gap_rcp:.0f} чел.\n"
        f"ФОТ: {v['fot']['exec']*100:.0f}% плана, недобор {a.gap_fot_mln:.0f} млн ₽.\n"
        f"Провальные ГОСБ×сегмент: {cells}.\n"
        f"Активности за 3 мес: {a.activity.get('n',0)} задач по "
        f"{a.activity.get('orgs',0)} орг, успех {a.activity.get('success_rate',0)*100:.0f}%, "
        f"привлечено по сделкам {a.activity.get('fact_deal',0)} из {a.activity.get('plan_deal',0)}.\n"
        f"Частые причины из комментариев: {a.themes}.\n"
        f"К отработке отобрано организаций (ГОСБ×ИНН): {len(a.to_work)}; "
        f"исключено как бесперспективные: {len(a.no_point)}.\n"
        f"Приоритетные: {top_orgs}."
    )
    prompt = (
        "Ты — руководитель по продажам зарплатных проектов. На основе данных ниже "
        "напиши управляющему ТБ краткое резюме на русском в деловом тоне, без воды, "
        "тремя частями с подзаголовками:\n"
        "**Диагноз** — 2–3 предложения: что именно не выполняется и где.\n"
        "**Что сделать** — 3–5 конкретных действий (какие ГОСБ, сегменты, организации, "
        "привлечение vs возврат).\n"
        "**Ожидаемый эффект** — 1–2 предложения с числами.\n\n"
        + ctx_txt
    )
    progress.llm_request("нарратив", prompt)
    try:
        resp = ctx.llm(prompt, temperature=0.2)
        progress.llm_response("нарратив", resp)
        if not resp or not resp.strip():
            progress.llm_error("нарратив", "пустой ответ LLM — использую фолбэк")
            return _fallback(a) + "\n\n_(LLM вернул пустой ответ)_"
        return resp
    except Exception as ex:
        progress.llm_error("нарратив", ex)
        return _fallback(a) + f"\n\n_(LLM недоступен: {type(ex).__name__}: {ex})_"


def _fallback(a) -> str:
    v = a.verdict
    return (
        f"**Диагноз.** {a.tb_full}: получатели {v['rcp']['exec']*100:.0f}% плана "
        f"(недобор {a.gap_rcp:.0f} чел, ранг {v['rcp']['rank']}/{v['rcp']['n_tb']}). "
        f"Основной провал — в сегментах с наибольшим недобором.\n\n"
        f"**Что сделать.** Отработать {len(a.to_work)} организаций из списка "
        f"(приоритет — максимальный потенциал привлечения); частые причины: {a.themes}; "
        f"закрыть недоработки по сделкам.\n\n"
        f"**Ожидаемый эффект.** Закрытие недобора {a.gap_rcp:.0f} получателей, "
        f"эффект ФОТ ~{a.sim.get('fot_mln', 0):.0f} млн ₽."
    )
