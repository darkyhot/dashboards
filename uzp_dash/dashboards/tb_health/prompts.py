"""LLM-слой дэша: анализ свободного текста активностей и исполнительный нарратив.

В LLM уходят ТОЛЬКО пары (ГОСБ, ИНН), у которых есть содержательный текст и не
сработали детерминированные правила (чек-лист/ключевые слова).

Устойчивость (важно для закрытого контура, где модель может «уйти в размышления»
или обрезать ответ по max_tokens):
  * ответ просим в формате JSONL — по объекту на строку, обрезка теряет только хвост;
  * разбор со спасением частичного ответа (сканер сбалансированных скобок);
  * при неполном покрытии батч автоматически дробится 30 → 10 → 3 → 1 в пределах
    бюджета вызовов; что не покрыто и после этого — уходит в фолбэк на правила.
"""
from __future__ import annotations

import json
import math

from ... import llm as llm_mod
from ... import progress
from ...anonymize import Aliases

BATCH_STEPS = (30, 10, 3, 1)   # шаги деградации размера батча
PROMPT_CHAR_LIMIT = 40_000     # больше в один запрос не отправляем — делим заранее
CALLS_BUDGET_FACTOR = 4        # максимум вызовов = фактор × число исходных батчей
ZERO_STREAK_ABORT = 3          # столько пустых ответов подряд = LLM недоступна по смыслу


def notes_text(notes) -> str:
    """Заметки (список словарей из analyze._collect_notes) -> плоский текст."""
    return " ".join(
        (n.get("text") or n.get("comment") or "") if isinstance(n, dict) else str(n)
        for n in (notes or [])
    ).strip()


# --------------------------------------------------------------------------- #
def _iter_objects(text: str):
    """Достать все верхнеуровневые {...} из текста (в т.ч. из обрезанного массива).

    Сканер по скобкам с учётом строк и экранирования: последний, незакрытый объект
    (обрыв по max_tokens) просто отбрасывается, всё что до него — сохраняется.
    """
    depth = 0; start = -1; in_str = False; esc = False
    for i, chunk in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif chunk == "\\":
                esc = True
            elif chunk == '"':
                in_str = False
            continue
        if chunk == '"':
            in_str = True
        elif chunk == "{":
            if depth == 0:
                start = i
            depth += 1
        elif chunk == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:i + 1]
                    start = -1


def _parse_items(raw: str) -> list[dict]:
    """Разобрать ответ LLM в список объектов. Терпим к мусору и обрезке."""
    text = (raw or "").strip()
    if not text:
        return []
    out: list[dict] = []
    # 1) честный JSON целиком (массив или объект) — самый частый удачный случай
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            for key in ("items", "results", "data"):
                if isinstance(data.get(key), list):
                    return [d for d in data[key] if isinstance(d, dict)]
            return [data]
    except json.JSONDecodeError:
        pass
    # 2) построчно (JSONL) и посимвольно (спасение частичного/обёрнутого ответа)
    for frag in _iter_objects(text):
        try:
            obj = json.loads(frag)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _prompt(chunk: list[dict]) -> str:
    # Названия ГОСБ и компаний в LLM НЕ отправляем (шлюз блокирует запросы с
    # чувствительной географией). Для ответа они не нужны: ключ — числовые id.
    lines = []
    for o in chunk:
        notes = " ⏵ ".join(n["text"] if isinstance(n, dict) else str(n) for n in o["notes"])
        lines.append(
            f'gosb_id={o["gosb_id"]} inn={o["inn"]} '
            f'| сегмент: {o.get("segment", "")} | активности: {notes}'
        )
    return (
        "Ты — аналитик зарплатных проектов банка. Ниже по каждой паре "
        "(ГОСБ, организация) даны ВСЕ содержательные комментарии сотрудников и "
        "анкеты по задачам за 3 месяца. Работа ведётся отдельно в каждом ГОСБ.\n"
        "Ответь БЕЗ рассуждений и пояснений: на каждую входную строку — ровно один "
        "JSON-объект на отдельной строке (формат JSONL, без общего массива, без ```):\n"
        '{"gosb_id": <число как во входе>, "inn": <число как во входе>, '
        '"reason": "<причина текущего положения, ≤8 слов>", '
        '"worth": "да|нет — есть ли смысл вести работу в ЭТОМ ГОСБ", '
        '"action": "<что конкретно сделать, ≤12 слов>"}\n'
        "В текстах reason и action НЕ употребляй слово «ИНН» — пиши «организация».\n"
        f"Строк на входе: {len(chunk)} — верни столько же строк ответа.\n\n"
        + "\n".join(lines)
    )


def _ask(ctx, chunk: list[dict], label: str) -> dict:
    """Один вызов LLM по чанку. Возвращает {(gosb_id, inn): insight}."""
    prompt = _prompt(chunk)
    progress.llm_request(label, prompt, note=f"орг {len(chunk)}")
    llm_mod.LAST_META = {}
    raw, meta = "", {}
    try:
        raw = ctx.llm(prompt, temperature=0.1)
        meta = dict(llm_mod.LAST_META)
    except Exception as ex:
        meta = dict(llm_mod.LAST_META)
        progress.llm_error(label, f"{type(ex).__name__}: {ex}")
        progress.llm_dump(label, prompt, f"<ошибка> {ex}", meta)
        return {}

    data = _parse_items(raw)
    progress.llm_response(label, raw, meta, ok=bool(data))
    progress.llm_dump(label, prompt, raw, {**meta, "parsed": len(data)})
    if not data:
        progress.llm_error(label, "не удалось разобрать ни одного объекта из ответа")
        if meta.get("finish_reason") == "length":
            progress.llm_error(label, "finish_reason=length → ответ обрезан, "
                                      "увеличьте llm_opts['max_tokens'] или уменьшите llm_batch")
        if meta.get("content_len") == 0 and meta.get("reasoning_len"):
            progress.llm_error(label, "модель ответила только размышлениями "
                                      "(reasoning_content) → отключите thinking через "
                                      "llm_opts['extra']")
        return {}

    result: dict = {}
    for r in data:
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
    return result


def _split(chunk: list[dict], size: int) -> list[list[dict]]:
    return [chunk[i:i + size] for i in range(0, len(chunk), size)]


def _next_size(n: int) -> int:
    """Следующий (меньший) размер батча из лестницы деградации."""
    for s in BATCH_STEPS:
        if s < n:
            return s
    return max(1, n // 2)


def _fits(chunk: list[dict]) -> bool:
    return len(_prompt(chunk)) <= PROMPT_CHAR_LIMIT


# --------------------------------------------------------------------------- #
def text_insights(ctx, items: list[dict], batch: int = 30,
                  max_calls: int | None = None) -> tuple[dict, int]:
    """Анализ свободного текста по парам (ГОСБ, ИНН).

    items: [{gosb_id, inn, gosb, company, segment, notes:[{text,...}]}, ...]
    Возврат: ({(gosb_id, inn): {reason, action, worth, verdict, source}}, n_вызовов)
    """
    result: dict = {}
    if not items:
        return result, 0

    if max_calls is None:
        max_calls = CALLS_BUDGET_FACTOR * max(1, math.ceil(len(items) / max(batch, 1)))
    queue: list[list[dict]] = _split(items, max(1, batch))
    calls = 0
    zero_streak = 0                # подряд идущие вызовы без единого разобранного объекта
    total = len(items)

    while queue:
        chunk = queue.pop(0)
        # слишком объёмный промпт делим, не тратя вызов
        while len(chunk) > 1 and not _fits(chunk):
            size = _next_size(len(chunk))
            parts = _split(chunk, size)
            progress.done(f"промпт великоват ({len(_prompt(chunk))} симв.) → "
                          f"дроблю на {len(parts)} по {size}")
            chunk = parts[0]
            queue = parts[1:] + queue
        if calls >= max_calls:
            left = len(chunk) + sum(len(c) for c in queue)
            progress.llm_error("текст", f"исчерпан бюджет вызовов LLM ({max_calls}) — "
                                        f"остаток {left} орг уйдёт в фолбэк на правила")
            break

        label = f"батч {calls + 1} · {len(chunk)} орг из {total}"
        got = _ask(ctx, chunk, label)
        calls += 1
        result.update(got)

        # Если несколько вызовов подряд не дали ни одного объекта — проблема
        # системная (модель/шлюз), дробить дальше бессмысленно и дорого по времени.
        zero_streak = 0 if got else zero_streak + 1
        if zero_streak >= ZERO_STREAK_ABORT:
            left = sum(len(c) for c in queue) + len(chunk)
            progress.llm_error("текст", f"{zero_streak} вызова подряд без единого ответа — "
                                        f"дальнейшие попытки прекращены, остаток {left} орг "
                                        f"уйдёт в фолбэк на правила")
            break

        missing = [o for o in chunk if (o["gosb_id"], o["inn"]) not in got]
        if not missing:
            continue
        if len(chunk) > 1:
            size = _next_size(len(chunk))
            parts = _split(missing, size)
            progress.llm_error(label, f"покрыто {len(got)} из {len(chunk)} — "
                                      f"повтор {len(missing)} орг батчами по {size}")
            queue = parts + queue
        else:
            progress.llm_error(label, "не покрыто и по одной организации — фолбэк на правила")

    return result, calls


# --------------------------------------------------------------------------- #
def narrative(ctx, a) -> str:
    """Исполнительный нарратив «что плохо и что делать» для управляющего ТБ.

    Названия ГОСБ и компаний уходят в LLM только как псевдонимы («ГОСБ-01»);
    настоящие имена подставляются обратно в ответ модели (Aliases.restore).
    """
    v = a.verdict
    al = Aliases()
    cells = "; ".join(
        f"{al.alias('ГОСБ', r.gosb_name)}/{r.seg_name} "
        f"({r.execution_percent*100:.0f}%, −{r.nedobor:.0f})"
        for r in a.top_cells.head(5).itertuples()
    )
    sel = (a.to_work[(a.to_work.need_k > 0) & (a.to_work.need_k <= 1.0)]
           if "need_k" in a.to_work else a.to_work)
    bad_segs = "; ".join(
        f'{al.alias("ГОСБ", c["gosb_name"])}: ' + ", ".join(
            f'{s["seg"]} ({s["exec"]*100:.0f}%, −{s["nedobor"]:.0f})' for s in c["segs"][:3])
        for c in getattr(a, "gosb_cards", [])[:6] if c["segs"])
    top_orgs = "; ".join(
        f'{al.alias("Организация", getattr(r, "company_name", "") or r.inn)} '
        f'[{al.alias("ГОСБ", r.gosb_name)}] ({r.lever}, +{r.impact_fl:.0f} чел)'
        for r in sel.head(6).itertuples()
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
        f"Западающие сегменты по ГОСБ: {bad_segs or '—'}.\n"
        f"Организации к отработке подбираются ВНУТРИ западающих сегментов; там, где "
        f"своих организаций не хватает, добираем из других сегментов ГОСБ "
        f"(таких добров: {a.sim.get('filler_n', 0)}).\n"
        f"Для выполнения плана нужно отработать {a.sim['k']} организаций (ГОСБ×организация) "
        f"с суммарным эффектом +{a.sim['closable']:.0f} чел и ФОТ ~{a.sim['fot_mln']:.0f} млн ₽ "
        f"(привлечение {a.sim['attract']:.0f}, возврат {a.sim['retention']:.0f}); "
        f"весь доступный потенциал покрывает разрыв на {a.sim['coverage']*100:.0f}%; "
        f"исключено как бесперспективные или уже в работе: {len(a.no_point)}.\n"
        f"Приоритетные: {top_orgs}."
    )
    prompt = (
        "Ты — руководитель по продажам зарплатных проектов. На основе данных ниже "
        "напиши управляющему ТБ краткое резюме на русском в деловом тоне, без воды, "
        "тремя частями с подзаголовками:\n"
        "**Диагноз** — 2–3 предложения: что именно не выполняется и где.\n"
        "**Что сделать** — 3–5 конкретных действий (какие ГОСБ, сегменты, организации, "
        "привлечение vs возврат).\n"
        "**Ожидаемый эффект** — 1–2 предложения с числами.\n"
        "Не употребляй слово «ИНН» — пиши «организация».\n"
        "Обозначения вида ГОСБ-01 пиши ПОЛНОСТЬЮ при каждом упоминании "
        "(«ГОСБ-01, ГОСБ-02»), не сокращай перечисления до «ГОСБ-01, 02».\n\n"
        + ctx_txt
    )
    progress.llm_request("нарратив", prompt, note=f"псевдонимов {len(al)}")
    llm_mod.LAST_META = {}
    try:
        resp = ctx.llm(prompt, temperature=0.2)
        meta = dict(llm_mod.LAST_META)
        progress.llm_response("нарратив", resp, meta, ok=bool(resp and resp.strip()))
        progress.llm_dump("нарратив", prompt, resp, meta)
        if not resp or not resp.strip():
            progress.llm_error("нарратив", "пустой ответ LLM — использую фолбэк")
            return _fallback(a) + "\n\n_(LLM вернул пустой ответ)_"
        return al.restore(resp)      # вернуть настоящие названия ГОСБ/организаций
    except Exception as ex:
        meta = dict(llm_mod.LAST_META)
        progress.llm_error("нарратив", f"{type(ex).__name__}: {ex}")
        progress.llm_dump("нарратив", prompt, f"<ошибка> {ex}", meta)
        return _fallback(a) + f"\n\n_(LLM недоступен: {type(ex).__name__}: {ex})_"


def _fallback(a) -> str:
    v = a.verdict
    return (
        f"**Диагноз.** {a.tb_full}: получатели {v['rcp']['exec']*100:.0f}% плана "
        f"(недобор {a.gap_rcp:.0f} чел, ранг {v['rcp']['rank']}/{v['rcp']['n_tb']}). "
        f"Основной провал — в сегментах с наибольшим недобором.\n\n"
        f"**Что сделать.** Отработать {a.sim.get('k', 0)} организаций из списка — именно "
        f"столько закрывает план (отбор по величине эффекта внутри каждого ГОСБ); "
        f"частые причины: {a.themes}; закрыть недоработки по сделкам.\n\n"
        f"**Ожидаемый эффект.** +{a.sim.get('closable', 0):.0f} получателей при недоборе "
        f"{a.gap_rcp:.0f}, эффект ФОТ ~{a.sim.get('fot_mln', 0):.0f} млн ₽."
    )
