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
import re

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


def _facts_line(f: dict) -> str:
    """Строка ФАКТОВ из витрины — формулировки без внутреннего жаргона.

    Раньше здесь стояло «свежих сделок нет», и модель читала это как «сделки нет»
    даже когда рядом шли план/факт по сделке. Теперь про сделку говорится ровно одно
    утверждение, и отдельно проговаривается случай, когда сделка НЕ предполагается.

    По сделкам берём ТОЛЬКО «старый» месяц: свежие (последние 2 мес) ещё могут
    реализоваться, их факт=0 — норма, а не недоработка.
    """
    if not f:
        return "—"
    parts: list[str] = []
    if f.get("potential"):
        parts.append(f"потенциал привлечения {int(f['potential'])} чел")
    if f.get("outflow_fl"):
        parts.append(f"отток {int(f['outflow_fl'])} чел "
                     f"(ФОТ {float(f.get('outflow_fot_mln', 0)):.1f} млн ₽)")
    if f.get("avg_salary"):
        parts.append(f"средняя ЗП ~{float(f['avg_salary']) / 1000:.0f} тыс ₽")
    # состояние задач: открытая и не просроченная задача — это НЕ недоработка
    n_ip, n_cl = int(f.get("n_in_progress", 0)), int(f.get("n_closed", 0))
    if n_ip:
        parts.append(f"задач ещё В РАБОТЕ {n_ip} (не закрыты и НЕ просрочены)")
    if n_cl:
        parts.append(f"закрытых задач {n_cl}"
                     + (", среди них есть успешные" if f.get("any_success")
                        else ", успешных среди них нет"))
    elif not n_ip:
        parts.append("закрытых задач нет")
    if f.get("n_overdue"):
        parts.append(f"ПРОСРОЧЕНО задач {int(f['n_overdue'])}")
    # Сделки: одно утверждение вместо двух противоречивых. План берётся АГРЕГАТОМ по
    # закрытым месяцам (у организации могло быть несколько сделок на один месяц), факт —
    # реально пришедшие получатели из витрины премирования.
    if float(f.get("plan_np_due", 0)) > 0:
        parts.append(f"по сделкам, срок которых уже прошёл ({int(f.get('due_months', 0))} мес): "
                     f"планировали {float(f['plan_np_due']):.0f} получателей, "
                     f"пришло {float(f.get('fact_np_due', 0)):.0f}")
    elif not f.get("deal_expected"):
        parts.append("сделка по этим задачам НЕ предполагается — её отсутствие НЕ дефект")
    else:
        parts.append("сделка заведена недавно — о зачислениях судить рано")
    if f.get("deadline"):
        parts.append(f"в тексте назван срок {f['deadline']} — он ЕЩЁ НЕ НАСТУПИЛ")
    # --- прогноз на текущий месяц ---
    # Оба факта снимают ложные «нужна активность»: сезонный отток банк удержать не
    # может, а организация с планом на этот месяц уже в работе.
    if f.get("out_exp"):
        cls = f.get("out_class") or ""
        note = f.get("out_note") or ""
        tail = f" — {note}" if note else (f" ({cls})" if cls else "")
        parts.append(f"ожидаемый отток в ТЕКУЩЕМ месяце {float(f['out_exp']):.0f} чел"
                     f"{tail}; из них уже не зачислились "
                     f"{float(f.get('out_observed', 0)):.0f}")
    if f.get("pipe_np_cur"):
        parts.append(f"на текущий месяц в пайплайне запланировано "
                     f"{float(f['pipe_np_cur']):.0f} получателей — работа уже идёт")
    return "; ".join(parts)


def _note_line(n) -> str:
    if not isinstance(n, dict):
        return f" - {n}"
    meta = " · ".join(x for x in (n.get("date", ""), n.get("author", ""),
                                  n.get("role", "")) if x)
    ts = f'[{n.get("type", "")}/{n.get("status", "")}]'
    if n.get("overdue"):
        state = " (ПРОСРОЧЕНА)"
    elif n.get("in_progress"):
        state = " (ещё в работе — результата ждать рано)"
    elif n.get("closed_same_day"):
        state = " (закрыта в день создания)"
    else:
        state = ""
    return f' - {meta} · {ts}{state} {n.get("text", "")}'.rstrip()


def _prompt(chunk: list[dict], ref_label: str = "") -> str:
    # Названия ГОСБ/компаний и ФИО в LLM НЕ отправляем (шлюз блокирует чувствительное;
    # автор — обезличенный токен «Сотрудник-NN»). Ключ ответа — числовые id.
    blocks = []
    for o in chunk:
        notes = "\n".join(_note_line(n) for n in o["notes"])
        blocks.append(
            f'## gosb_id={o["gosb_id"]} inn={o["inn"]} | сегмент: {o.get("segment", "")}'
            f' | рычаг: {o.get("lever", "")}\n'
            f'ФАКТЫ: {_facts_line(o.get("facts", {}))}\n'
            f'ХРОНОЛОГИЯ:\n{notes}'
        )
    now = (f"Сейчас {ref_label}. Всё, что назначено на месяц ПОЗЖЕ {ref_label}, ещё не "
           f"наступило и недоработкой НЕ является.\n" if ref_label else "")
    return (
        "Ты — старший аналитик зарплатных проектов банка. По каждой паре "
        "(ГОСБ, организация) даны ФАКТЫ из витрины и ХРОНОЛОГИЯ активностей "
        "сотрудников за 3 месяца. Работа ведётся отдельно в каждом ГОСБ.\n"
        + now +
        "ГЛАВНЫЙ ВОПРОС: есть ли ПРЯМО СЕЙЧАС действие, которое зависит ОТ БАНКА и "
        "может дать получателей. Если такого действия нет — так и скажи, не придумывай "
        "поручений. Придирки к формулировкам никому не нужны.\n"
        "Ответь БЕЗ рассуждений: на каждую пару — ровно один JSON-объект на отдельной "
        "строке (JSONL, без общего массива, без ```):\n"
        '{"gosb_id": <как во входе>, "inn": <как во входе>, '
        '"verdict": "work|no_point|in_progress", '
        '"can_influence": "да|нет", '
        '"quality": "качественно|формально|не отработана|—", '
        '"contradiction": "да|нет", '
        '"outflow_worked": "да|нет|—", "attract_real": "да|нет|—", '
        '"reason": "<причина/суть, ≤10 слов>", "action": "<что сделать, ≤12 слов>"}\n'
        "Правила оценки:\n"
        "• can_influence=нет — причина ВНЕ зоны влияния банка: отпуска или сезонность, "
        "сокращение штата либо ликвидация у клиента, перевод людей в другой регион, "
        "решение принимает головной офис вне этого ГОСБ. Тогда verdict=no_point, "
        "action=«Мониторинг, действий не требуется». Сезонный отток или отпуска значат, "
        "что клиент ОСТАЛСЯ с нами — это не потеря.\n"
        "• verdict=in_progress — работа идёт и ждать нормально: задача не закрыта и НЕ "
        "просрочена, или назван срок позже текущего месяца, или сделка заведена недавно. "
        "Это НЕ недоработка; action — «Проконтролировать в <срок>».\n"
        "• verdict=work — только если банк может сделать конкретный следующий шаг СЕЙЧАС "
        "(перезвонить, встретиться с ЛПР, пересмотреть условия, закрыть просрочку).\n"
        "• verdict=no_point — влиять нечем (см. can_influence) либо ликвидация/банкротство.\n"
        "• quality=«не отработана» — ТОЛЬКО когда задачи закрыты или просрочены, а "
        "содержательной работы в них нет. Если задача ещё в работе и не просрочена — "
        "ставь «—»: спрашивать результат рано.\n"
        "• quality=формально — заявлен успех, но по заведённой сделке факт 0 при плане>0, "
        "либо задача закрыта в день создания, либо комментарий дежурный. Если сделка по "
        "задачам НЕ предполагается, её отсутствие формальностью НЕ считается.\n"
        "• contradiction=да — разные сотрудники противоречат друг другу (напр. один "
        "«клиент согласился», другой «отказался»).\n"
        "• outflow_worked=нет — только при РЕАЛЬНОМ оттоке, который банк мог удержать и "
        "не удержал. Если отток объективный (отпуска, сезон, сокращение штата) — ставь «—».\n"
        "• attract_real — реально ли привлечение/расширение по смыслу текста и потенциалу. "
        "Отсутствие сделки само по себе НЕ делает привлечение нереальным.\n"
        "В reason и action НЕ употребляй слово «ИНН» — пиши «организация».\n"
        f"Пар на входе: {len(chunk)} — верни столько же строк ответа.\n\n"
        + "\n\n".join(blocks)
    )


def _ask(ctx, chunk: list[dict], label: str, ref_label: str = "") -> dict:
    """Один вызов LLM по чанку. Возвращает {(gosb_id, inn): insight}."""
    prompt = _prompt(chunk, ref_label)
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
        result[key] = {
            "reason": str(r.get("reason", ""))[:80],
            "action": str(r.get("action", ""))[:100],
            "verdict": _norm_verdict(r.get("verdict")),
            "can_influence": _yn(r.get("can_influence")),
            "quality": _norm_quality(r.get("quality")),
            "contradiction": _yn(r.get("contradiction")),
            "outflow_worked": _yn(r.get("outflow_worked")),
            "attract_real": _yn(r.get("attract_real")),
            "source": "LLM",
        }
    return result


# Явные словари вместо startswith("нет"): «нет данных» не должно давать no_point.
_VERDICT_MAP = (
    ("no_point", "no_point"), ("нет смысл", "no_point"), ("бесперспектив", "no_point"),
    ("ликвидац", "no_point"), ("monitor", "no_point"), ("мониторинг", "no_point"),
    ("влиять неч", "no_point"),
    ("in_progress", "in_progress"), ("в процесс", "in_progress"), ("в работе", "in_progress"),
    ("ждать", "in_progress"), ("wait", "in_progress"),
    ("work", "work"), ("работать", "work"), ("да", "work"),
)


def _norm_verdict(raw) -> str:
    r = str(raw or "").strip().lower()
    for pref, val in _VERDICT_MAP:
        if r.startswith(pref):
            return val
    return "work"      # безопасный дефолт: не «прибиваем» потенциал из-за неясности


def _norm_quality(raw) -> str:
    r = str(raw or "").strip().lower()
    if r.startswith("формал"):
        return "формально"
    if r.startswith("не отраб"):
        return "не отработана"
    if r.startswith("качеств"):
        return "качественно"
    return "—"


def _yn(raw) -> str:
    r = str(raw or "").strip().lower()
    if r.startswith("да"):
        return "да"
    if r.startswith("нет"):
        return "нет"
    return "—"


def _split(chunk: list[dict], size: int) -> list[list[dict]]:
    return [chunk[i:i + size] for i in range(0, len(chunk), size)]


def _next_size(n: int) -> int:
    """Следующий (меньший) размер батча из лестницы деградации."""
    for s in BATCH_STEPS:
        if s < n:
            return s
    return max(1, n // 2)


def _fits(chunk: list[dict], ref_label: str = "") -> bool:
    return len(_prompt(chunk, ref_label)) <= PROMPT_CHAR_LIMIT


# --------------------------------------------------------------------------- #
def text_insights(ctx, items: list[dict], batch: int = 30,
                  max_calls: int | None = None,
                  ref_label: str = "") -> tuple[dict, int]:
    """Анализ свободного текста по парам (ГОСБ, ИНН).

    items: [{gosb_id, inn, segment, lever, facts:{...}, notes:[{text,date,author,…}]}, …]
    ref_label: опорный месяц «MM.YYYY» — без него модель не может отличить «срок ещё
    не наступил» от «просрочено» и требует результата по будущим датам.
    Возврат: ({(gosb_id, inn): {reason, action, verdict, can_influence, quality,
              contradiction, outflow_worked, attract_real, source}}, n_вызовов)
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
        while len(chunk) > 1 and not _fits(chunk, ref_label):
            size = _next_size(len(chunk))
            parts = _split(chunk, size)
            progress.done(f"промпт великоват ({len(_prompt(chunk, ref_label))} симв.) → "
                          f"дроблю на {len(parts)} по {size}")
            chunk = parts[0]
            queue = parts[1:] + queue
        if calls >= max_calls:
            left = len(chunk) + sum(len(c) for c in queue)
            progress.llm_error("текст", f"исчерпан бюджет вызовов LLM ({max_calls}) — "
                                        f"остаток {left} орг уйдёт в фолбэк на правила")
            break

        label = f"батч {calls + 1} · {len(chunk)} орг из {total}"
        got = _ask(ctx, chunk, label, ref_label)
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
def section_narratives(ctx, a) -> dict:
    """Выводы LLM по разделам отчёта: {forecast, matrix, gosb, orgs}.

    ОДИН вызов на все четыре блока: так дешевле и, главное, выводы не противоречат
    друг другу — модель видит всю картину сразу. При сбое или неполном ответе
    недостающие блоки заменяются своим текстом (`_fallback_blocks`), чтобы раздел
    не остался без вывода.

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
    wf = getattr(a, "wf", {}) or {}
    d = getattr(a, "dates", {}) or {}
    cl = (getattr(a, "closed", {}) or {}).get("rcp", {})
    ctx_txt = (
        f"ТБ: {a.tb_full}. Месяц ещё НЕ ЗАКРЫТ: это {d.get('label', a.ref_date)}, "
        f"факт зачислений по {d.get('act_dt', '')}. "
        f"Все цифры по получателям — ПРОГНОЗ, а не факт.\n"
        f"Получатели: прогноз {v['rcp']['fact']:.0f} из плана {v['rcp']['plan']:.0f} "
        f"({(v['rcp']['exec'] or 0)*100:.0f}%), недобор {a.gap_rcp:.0f} чел. "
        f"Ранг {v['rcp']['rank']}/{v['rcp']['n_tb']} — за ЗАКРЫТЫЙ месяц "
        f"{d.get('closed_label', '')} (тогда выполнение было "
        f"{(cl.get('exec') or 0)*100:.0f}%).\n"
        f"Прогноз складывается так: портфель закрытого месяца {wf.get('base', 0):.0f} "
        f"− ожидаемый отток {wf.get('out_exp', 0):.0f} "
        f"(из них {wf.get('observed', 0):.0f} уже не зачислились, "
        f"{wf.get('risk', 0):.0f} — риск, на который ещё можно повлиять) "
        f"+ сезонный приход {wf.get('in_exp', 0):.0f} "
        f"+ пайплайн {wf.get('pipe', 0):.0f} (заявлено {wf.get('pipe_raw', 0):.0f}, "
        f"остальное срезано поправкой на историческую реализуемость сделок).\n"
        f"Если пайплайн отработают на 100%, прогноз вырастет до {wf.get('ceiling', 0):.0f}.\n"
        f"ФОТ: {(v['fot']['exec'] or 0)*100:.0f}% плана, недобор {a.gap_fot_mln:.0f} млн ₽.\n"
        f"Провальные ГОСБ×сегмент: {cells}.\n"
        f"Активности за 3 мес: {a.activity.get('n',0)} задач по "
        f"{a.activity.get('orgs',0)} орг, успех {a.activity.get('success_rate',0)*100:.0f}%, "
        f"привлечено по сделкам {a.activity.get('fact_deal',0)} из {a.activity.get('plan_deal',0)}.\n"
        f"Частые причины из комментариев: {a.themes}.\n"
        f"Западающие сегменты по ГОСБ: {bad_segs or '—'}.\n"
        f"Организации к отработке подбираются ВНУТРИ западающих сегментов; там, где "
        f"своих организаций не хватает, добираем из других сегментов ГОСБ "
        f"(таких добров: {a.sim.get('filler_n', 0)}).\n"
        f"Рычагов три: «Привлечь» (новые получатели), «Вернуть» (ушедшие в прошлом "
        f"месяце) и «Удержать» — те, кто оттекает ПРЯМО СЕЙЧАС и кого ещё можно не "
        f"потерять до конца месяца; удержание срочнее остальных. Прогноз — сценарий "
        f"«если ничего не делать», а список организаций показывает, чем его улучшить: "
        f"приход из пайплайна в прогнозе уже учтён, потенциал и удержание — нет.\n"
        f"Для выполнения плана нужно отработать {a.sim['k']} организаций (ГОСБ×организация) "
        f"с суммарным эффектом +{a.sim['closable']:.0f} чел и ФОТ ~{a.sim['fot_mln']:.0f} млн ₽ "
        f"(привлечение {a.sim['attract']:.0f}, возврат {a.sim['retention']:.0f}); "
        f"весь доступный потенциал покрывает разрыв на {a.sim['coverage']*100:.0f}%; "
        f"исключено как бесперспективные или уже в работе: {len(a.no_point)}.\n"
        f"Приоритетные: {top_orgs}."
    )
    prompt = (
        "Ты — руководитель по продажам зарплатных проектов. По данным ниже напиши "
        "управляющему ТБ ЧЕТЫРЕ коротких вывода на русском, в деловом тоне, без воды. "
        "Каждый вывод — 2–4 предложения и относится к своему разделу отчёта.\n\n"
        "Формат ответа СТРОГО такой, каждый блок начинается со своей метки на отдельной "
        "строке (без markdown-заголовков, без ```):\n"
        "[[forecast]]\n<вывод>\n[[matrix]]\n<вывод>\n[[gosb]]\n<вывод>\n[[orgs]]\n<вывод>\n\n"
        "Что писать в каждом блоке:\n"
        "• forecast — раздел «Из чего складывается прогноз». Выполняется ли план текущего "
        "месяца ПО ПРОГНОЗУ и за счёт чего складывается разрыв: отток, слабый пайплайн "
        "или и то и другое. Назови главный источник разрыва числом.\n"
        "• matrix — раздел «Где провал: ГОСБ × сегмент». Где именно концентрируется "
        "недобор и есть ли общий для ТБ провальный сегмент.\n"
        "• gosb — раздел с карточками ГОСБ. На каких ГОСБ сосредоточиться и почему; "
        "если у ГОСБ не хватает своих организаций — сказать об этом.\n"
        "• orgs — раздел «Организации к работе». Что делать в первую очередь: "
        "удержание тех, кто оттекает сейчас, привлечение, возврат. С числами.\n\n"
        "Правила: пиши «прогноз», а не «факт» — месяц не закрыт. Не употребляй слово "
        "«ИНН», пиши «организация». Обозначения вида ГОСБ-01 пиши ПОЛНОСТЬЮ при каждом "
        "упоминании («ГОСБ-01, ГОСБ-02»), не сокращай перечисления до «ГОСБ-01, 02». "
        "Не повторяй один и тот же вывод в разных блоках.\n\n"
        + ctx_txt
    )
    progress.llm_request("выводы", prompt, note=f"псевдонимов {len(al)}")
    llm_mod.LAST_META = {}
    fb = _fallback_blocks(a)
    try:
        resp = ctx.llm(prompt, temperature=0.2)
        meta = dict(llm_mod.LAST_META)
        progress.llm_response("выводы", resp, meta, ok=bool(resp and resp.strip()))
        progress.llm_dump("выводы", prompt, resp, meta)
        got = _parse_blocks(resp)
        if not got:
            progress.llm_error("выводы", "не разобрано ни одного блока — фолбэк на все")
            return fb
        miss = [k for k in NARRATIVE_BLOCKS if k not in got]
        if miss:
            progress.llm_error("выводы", f"не вернулись блоки: {', '.join(miss)} — "
                                         f"по ним фолбэк")
        # каждый блок восстанавливаем отдельно: сбой одного не рушит остальные
        return {k: (al.restore(got[k]) if k in got else fb[k]) for k in NARRATIVE_BLOCKS}
    except Exception as ex:
        progress.llm_error("выводы", f"{type(ex).__name__}: {ex}")
        progress.llm_dump("выводы", prompt, f"<ошибка> {ex}", dict(llm_mod.LAST_META))
        return fb


# Метки блоков = разделы отчёта, после которых печатается вывод
NARRATIVE_BLOCKS = ("forecast", "matrix", "gosb", "orgs")
_BLOCK_RE = re.compile(r"\[\[\s*(forecast|matrix|gosb|orgs)\s*\]\]", re.I)


def _parse_blocks(raw: str) -> dict:
    """Разобрать ответ на блоки по меткам [[имя]].

    Разметка вместо JSON выбрана намеренно: при обрыве ответа уцелевшие блоки всё
    равно разбираются, а модель реже ломает её, чем структуру объекта.
    """
    if not raw or not raw.strip():
        return {}
    parts = _BLOCK_RE.split(raw)
    out: dict = {}
    # split даёт [мусор, имя1, текст1, имя2, текст2, …]
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].strip().lower()
        text = parts[i + 1].strip()
        if text:
            out[key] = text
    return out


def _fallback_blocks(a) -> dict:
    """Свой текст на каждый раздел — чтобы при сбое LLM ни один не остался пустым."""
    v = a.verdict
    wf = getattr(a, "wf", {}) or {}
    d = getattr(a, "dates", {}) or {}
    cells = ", ".join(f'{r.gosb_name}/{r.seg_name} ({r.execution_percent*100:.0f}%)'
                      for r in a.top_cells.head(3).itertuples()) or "—"
    n_hold = int((a.to_work.lever == "Удержать").sum()) if len(a.to_work) else 0
    return {
        "forecast": (
            f"Прогноз на {d.get('label', a.ref_date)}: "
            f"{(v['rcp']['exec'] or 0)*100:.0f}% плана, недобор {a.gap_rcp:.0f} чел. "
            f"Портфель закрытого месяца {wf.get('base', 0):.0f} минус ожидаемый отток "
            f"{wf.get('out_exp', 0):.0f} плюс пайплайн {wf.get('pipe', 0):.0f}. "
            f"Если пайплайн отработают полностью, прогноз вырастет до "
            f"{wf.get('ceiling', 0):.0f}."),
        "matrix": (
            f"Наибольший вклад в недобор дают: {cells}. "
            f"Всего западающих зон — {len(a.top_cells)} из показанных в матрице."),
        "gosb": (
            f"Карточек ГОСБ: {len(getattr(a, 'gosb_cards', []))}. "
            f"Потенциал западающих сегментов покрывает разрыв на "
            f"{a.sim.get('coverage', 0)*100:.0f}%; добор из других сегментов — "
            f"{a.sim.get('filler_n', 0)} организаций."),
        "orgs": (
            f"Под план нужно отработать {a.sim.get('k', 0)} организаций с эффектом "
            f"+{a.sim.get('closable', 0):.0f} чел и ФОТ ~{a.sim.get('fot_mln', 0):.0f} млн ₽. "
            f"В первую очередь — удержание: {n_hold} организаций оттекают прямо сейчас. "
            f"Частые причины из комментариев: {a.themes}."),
    }
