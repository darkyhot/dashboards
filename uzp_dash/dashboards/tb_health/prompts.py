"""LLM-слой дэша: анализ свободного текста активностей и исполнительный нарратив.

Оба вызова устойчивы к сбою LLM (возвращают безопасный фолбэк).
"""
from __future__ import annotations

import json
import re


def _parse_json(text: str):
    """Достать JSON из ответа LLM (снять ```-обёртки, найти объект)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    m = re.search(r"[\{\[].*[\}\]]", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
def text_insights(ctx, priority_text: list) -> dict:
    """По свободному тексту (комментарии/анкеты) вернуть по каждой организации:
    причина, стоит ли работать, рекомендация. dict: inn -> {...}."""
    if not priority_text:
        return {}
    items = []
    for o in priority_text:
        notes = " ⏵ ".join(o["notes"])
        items.append(f'ИНН {o["inn"]} ({o["company"]}, сегмент {o["segment"]}): {notes}')
    listing = "\n".join(items)

    prompt = (
        "Ты — аналитик зарплатных проектов банка. Ниже по каждой организации даны "
        "комментарии сотрудников и анкеты по задачам за 3 месяца.\n"
        "Для КАЖДОЙ организации верни строго JSON-массив объектов с полями:\n"
        '  "inn" (число), "reason" (краткая причина оттока/незакрытия, ≤8 слов), '
        '"worth" ("да"|"нет" — стоит ли работать), "action" (рекомендация ≤12 слов).\n'
        "Только JSON, без пояснений.\n\n" + listing
    )
    try:
        raw = ctx.llm(prompt, temperature=0.1)
        data = _parse_json(raw) or []
    except Exception:
        data = []
    result = {}
    for r in data if isinstance(data, list) else []:
        try:
            result[int(r["inn"])] = {
                "reason": str(r.get("reason", ""))[:80],
                "worth": str(r.get("worth", "")).strip().lower(),
                "action": str(r.get("action", ""))[:100],
            }
        except (KeyError, ValueError, TypeError):
            continue
    return result


# --------------------------------------------------------------------------- #
def narrative(ctx, a) -> str:
    """Исполнительный нарратив «что плохо и что делать» для управляющего ТБ."""
    v = a.verdict
    cells = "; ".join(
        f"{r.gosb_name}/{r.seg_name} ({r.execution_percent*100:.0f}%, −{r.nedobor:.0f})"
        for r in a.top_cells.head(5).itertuples()
    )
    themes = _themes(a)
    top_orgs = "; ".join(
        f'{r.company if hasattr(r,"company") else r.inn} ({r.lever}, +{r.impact_fl:.0f} чел)'
        for r in a.to_work.head(6).itertuples()
    )
    ctx_txt = (
        f"ТБ: {a.tb_full}. Опорный месяц: {a.ref_date}.\n"
        f"Получатели: факт {v['rcp']['fact']:.0f} из плана {v['rcp']['plan']:.0f} "
        f"({v['rcp']['exec']*100:.0f}%, ранг {v['rcp']['rank']}/{v['rcp']['n_tb']}), "
        f"недобор {a.gap_rcp:.0f} чел.\n"
        f"ФОТ: {v['fot']['exec']*100:.0f}% плана, недобор {a.gap_fot:.0f} млн ₽.\n"
        f"Провальные ГОСБ×сегмент: {cells}.\n"
        f"Активности за 3 мес: {a.activity.get('n',0)} задач по "
        f"{a.activity.get('orgs',0)} орг, успех {a.activity.get('success_rate',0)*100:.0f}%, "
        f"привлечено по сделкам {a.activity.get('fact_deal',0)} из {a.activity.get('plan_deal',0)}.\n"
        f"Частые причины из комментариев: {themes}.\n"
        f"Симуляция: топ-{a.sim['k']} организаций «в работу» закрывают недобор "
        f"(+{a.sim['attract']:.0f} привлечение, +{a.sim['retention']:.0f} возврат, "
        f"эффект ФОТ ~{a.sim['fot_mln']:.0f} млн ₽).\n"
        f"Приоритетные организации: {top_orgs}."
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
    try:
        return ctx.llm(prompt, temperature=0.2)
    except Exception as ex:
        return _fallback(a, themes) + f"\n\n_(LLM недоступен: {type(ex).__name__})_"


def _themes(a) -> str:
    """Частые причины из свободного текста (по ключевым словам)."""
    words = {}
    for o in a.priority_text:
        for n in o["notes"]:
            for key in ["ликвидац", "другой банк", "текучка", "сокращен", "не заинтересован",
                        "повторная встреча", "не дозвонились", "отпуск", "недовольств"]:
                if key in n.lower():
                    words[key] = words.get(key, 0) + 1
    if not words:
        return "—"
    top = sorted(words.items(), key=lambda x: -x[1])[:4]
    names = {"ликвидац": "ликвидация", "другой банк": "уход в другой банк",
             "текучка": "текучка персонала", "сокращен": "сокращение штата",
             "не заинтересован": "ЛПР не заинтересован", "повторная встреча": "нужна встреча",
             "не дозвонились": "не дозвонились", "отпуск": "сезонные отпуска",
             "недовольств": "недовольство условиями"}
    return ", ".join(f"{names[k]} ({n})" for k, n in top)


def _fallback(a, themes: str) -> str:
    v = a.verdict
    return (
        f"**Диагноз.** {a.tb_full}: получатели {v['rcp']['exec']*100:.0f}% плана "
        f"(недобор {a.gap_rcp:.0f} чел, ранг {v['rcp']['rank']}/{v['rcp']['n_tb']}). "
        f"Основной провал — в сегментах Малые/Микро проблемных ГОСБ.\n\n"
        f"**Что сделать.** Сфокусировать привлечение на топ-{a.sim['k']} организациях "
        f"с наибольшим потенциалом; отработать возврат по оттоку "
        f"(частые причины: {themes}); закрыть недоработки по сделкам.\n\n"
        f"**Ожидаемый эффект.** Закрытие недобора {a.gap_rcp:.0f} получателей, "
        f"эффект ФОТ ~{a.sim['fot_mln']:.0f} млн ₽."
    )
