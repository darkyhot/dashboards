"""Шаг 4: текстовые выводы. LLM с обязательным фолбэком на правила.

Ни один сбой модели не должен ронять прогон и не должен оставлять раздел пустым.
Поэтому у КАЖДОГО раздела есть детерминированный текст по правилам, и он же
используется, если модель не ответила, ответила пусто или ответ не разобрался.
В отчёте такой раздел помечается: читатель должен видеть, что вывод сделан
расчётом, а не моделью.

Бюджет вызовов задаётся НА РАЗДЕЛ, а не на прогон: иначе первый же раздел
съедает лимит, и остальные остаются без обработки. Отдельно стоит защёлка: если
шлюз не ответил ни разу подряд заданное число раз, это его отказ, а не свойство
данных, — остальные разделы сразу уходят в фолбэк, не тратя по полному таймауту.
"""
from __future__ import annotations

import re
import time

import pandas as pd

from uzp_dash import llm, progress
from uzp_dash.anonymize import Aliases

# Столько неудач подряд — и шлюз считается недоступным до конца прогона.
ZERO_STREAK_ABORT = 2
# Длиннее этого промпт не отправляем: у разделов он фиксированной формы, и такой
# размер означает, что в таблицу попало лишнее.
PROMPT_CHAR_LIMIT = 40_000


class Narrator:
    """Сборщик текстов: один на прогон, считает вызовы и хранит защёлку."""

    def __init__(self, complete=None, max_calls: int = 6, temperature: float = 0.2):
        self._complete = complete or llm.complete
        self.max_calls = int(max_calls)
        self.temperature = float(temperature)
        self.calls = 0
        self.failures = 0
        self.gateway_down = False
        self.used_fallback: list[str] = []

    # ------------------------------------------------------------------ #
    def ask(self, label: str, prompt: str, aliases: Aliases | None = None) -> str:
        """Один вызов. При ЛЮБОЙ ошибке возвращает пустую строку, а не исключение."""
        if self.gateway_down:
            progress.warn(f"LLM [{label}]: шлюз уже признан недоступным — фолбэк")
            return ""
        if self.calls >= self.max_calls:
            progress.warn(f"LLM [{label}]: исчерпан бюджет вызовов "
                          f"({self.max_calls}) — фолбэк на правила")
            return ""
        if len(prompt) > PROMPT_CHAR_LIMIT:
            progress.warn(f"LLM [{label}]: промпт {len(prompt):,} симв. — больше "
                          f"лимита {PROMPT_CHAR_LIMIT:,}; раздел уходит в фолбэк")
            return ""

        self.calls += 1
        progress.llm_request(label, prompt, note=f"вызов {self.calls}/{self.max_calls}")
        t0 = time.time()
        try:
            raw = self._complete(prompt, temperature=self.temperature)
        except Exception as ex:
            # Отказ по blacklist разбирается только по тексту запроса — печатаем его.
            if "blacklist" in str(ex).lower():
                progress.warn(f"LLM [{label}] отказ по blacklist. Текст запроса: "
                              f"{prompt[:2000]}")
            progress.llm_error(label, f"{type(ex).__name__}: {ex}")
            progress.llm_dump(label, prompt, f"<ошибка> {ex}", dict(llm.LAST_META))
            self.failures += 1
            self._maybe_latch()
            return ""

        meta = {**dict(llm.LAST_META), "elapsed": round(time.time() - t0, 1)}
        text = _clean(raw)
        progress.llm_response(label, raw, meta, ok=bool(text))
        progress.llm_dump(label, prompt, raw, {**meta, "parsed_len": len(text)})
        if not text:
            # Диагностика пустого ответа: без неё причину не установить
            if meta.get("finish_reason") == "length":
                progress.warn(f"LLM [{label}]: ответ обрезан → поднимите max_tokens")
            if not meta.get("content_len") and meta.get("reasoning_len"):
                progress.warn(f"LLM [{label}]: модель ответила только размышлениями "
                              f"→ отключите thinking через llm_opts['extra']")
            self.failures += 1
            self._maybe_latch()
            return ""

        self.failures = 0
        if aliases is not None:
            text = aliases.restore(text)
            if aliases.leaked:
                progress.warn(f"LLM [{label}]: модель назвала {aliases.leaked} "
                              f"токенов, которых в промпте не было — обезличены")
        return text

    def _maybe_latch(self) -> None:
        if self.failures >= ZERO_STREAK_ABORT:
            self.gateway_down = True
            progress.warn(f"LLM: {self.failures} неудачи подряд — шлюз признан "
                          f"недоступным, оставшиеся разделы уйдут в фолбэк")

    # ------------------------------------------------------------------ #
    def section(self, label: str, prompt: str, fallback: str,
                aliases: Aliases | None = None) -> tuple[str, bool]:
        """Текст раздела. Второе значение — True, если сработал фолбэк."""
        text = self.ask(label, prompt, aliases)
        if text:
            return text, False
        self.used_fallback.append(label)
        return fallback, True


# --------------------------------------------------------------------------- #
_WS = re.compile(r"[ \t]+")


def _clean(raw: str) -> str:
    """Причесать ответ: убрать обёртки, разметку списков и лишние пробелы."""
    if not raw:
        return ""
    text = raw.strip()
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()
    lines = []
    for ln in text.splitlines():
        ln = _WS.sub(" ", ln).strip()
        ln = re.sub(r"^[-*•]\s*", "", ln)
        ln = re.sub(r"^#+\s*", "", ln)
        if ln:
            lines.append(ln)
    return " ".join(lines).strip()


# --------------------------------------------------------------------------- #
# Обезличивание: что уходит в модель вместо настоящих названий
# --------------------------------------------------------------------------- #

def mask_frame(df: pd.DataFrame, col: str, kind: str, aliases: Aliases
               ) -> pd.DataFrame:
    """Заменить названия в колонке токенами. Пустой кадр — не ошибка."""
    if df is None or df.empty or col not in df:
        return df
    out = df.copy()
    out[col] = [aliases.alias(kind, v) for v in out[col]]
    return out


def check_masked(prompt: str, forbidden: list[str]) -> list[str]:
    """Проверка обезличивания: ни одно настоящее имя не должно найтись в промпте.

    Отдельная проверка нужна потому, что промпт собирается из нескольких таблиц,
    и колонку легко забыть замаскировать. Отказ шлюза по blacklist обнаружился бы
    только на проме — и стоил бы инцидента.
    """
    hits = []
    for name in forbidden:
        name = str(name or "").strip()
        if len(name) < 4:            # короткие токены дают ложные срабатывания
            continue
        if re.search(rf"(?<![А-Яа-яЁёA-Za-z]){re.escape(name)}(?![А-Яа-яЁёA-Za-z])",
                     prompt, re.IGNORECASE):
            hits.append(name)
    return hits


# --------------------------------------------------------------------------- #
# Фолбэки: те же выводы, посчитанные правилами
# --------------------------------------------------------------------------- #

def fb_overview(month: str, kept: float, ret: float, base_fl: float,
                agencies: pd.DataFrame, direction: dict) -> str:
    if agencies is None or agencies.empty:
        return f"За {month} данных по ведомствам нет."
    top = agencies.iloc[0]
    rate = agencies.sort_values("out_rate", ascending=False).iloc[0]
    parts = [
        f"За {month} из бюджетной сферы ушло и не вернулось {kept:,.0f} человек "
        f"при численности {base_fl:,.0f} ({kept / max(base_fl, 1):.1%}); "
        f"вернулось {ret:,.0f}.",
        f"Больше всего потерь в ведомстве «{top['agency']}» — {top['out_kept']:,.0f} "
        f"человек, это {top['share']:.0%} всего оттока сегмента.",
    ]
    if rate["agency"] != top["agency"]:
        parts.append(f"Самая высокая доля оттока — в ведомстве «{rate['agency']}»: "
                     f"{rate['out_rate']:.1%} численности.")
    if direction.get("measurable"):
        parts.append(f"За последние {direction['window']} мес. отток "
                     f"{direction['word']} на {abs(direction['rel']):.0%} "
                     f"относительно предыдущих {direction['window']}.")
    return " ".join(parts)


def fb_territory(tb: pd.DataFrame, regions: pd.DataFrame) -> str:
    parts = []
    if tb is not None and not tb.empty:
        t = tb.iloc[0]
        parts.append(f"Основной объём оттока даёт {t['tb_short_name']} — "
                     f"{t['out_kept']:,.0f} человек ({t['share']:.0%} сегмента) "
                     f"при доле оттока {t['out_rate']:.1%}.")
    if regions is not None and not regions.empty:
        r = regions.sort_values("out_rate", ascending=False).iloc[0]
        parts.append(f"Самая высокая доля оттока по регионам — {r['region_name']}: "
                     f"{r['out_rate']:.1%} численности при {r['n_org']:.0f} "
                     f"организациях.")
    return " ".join(parts) or "Территориальный разрез построить не из чего."


def fb_causes(window: int, drop: float, staff: float, comp: float,
              by_region: pd.DataFrame) -> str:
    if drop <= 0:
        return "За выбранное окно численность получателей не падала."
    share = staff / drop
    lead = ("сокращение штата в самих организациях" if share >= 0.5
            else "уход получателей к другим банкам")
    parts = [f"За {window} мес. численность упала на {drop:,.0f} человек: "
             f"{staff:,.0f} объясняется сокращением штата, {comp:,.0f} — уходом "
             f"к конкуренту; преобладает {lead}."]
    if by_region is not None and not by_region.empty:
        w = by_region.sort_values("competitor", ascending=False).iloc[0]
        if w["competitor"] > 0:
            parts.append(f"Больше всего уходов к конкурентам в регионе "
                         f"{w['region_name']} — {w['competitor']:,.0f} человек "
                         f"из {w['drop']:,.0f} падения.")
    return " ".join(parts)


def fb_competitors(coverage: float, banks: pd.DataFrame) -> str:
    if banks is None or banks.empty:
        return (f"Банк-конкурент не назван ни у одной организации с оттоком. "
                f"Данные есть только по ключевым клиентам — {coverage:.0%} сегмента.")
    b = banks.iloc[0]
    return (f"Чаще других конкурентом назван {b['bank']}: {b['n_org']:.0f} "
            f"организаций, {b['out_kept']:,.0f} человек оттока. "
            f"Оценка неполная: банк-конкурент известен только по ключевым "
            f"клиентам, это {coverage:.0%} организаций сегмента.")


def fb_outlook(horizon: str, months: int, base: float, scenarios: pd.DataFrame,
               season_ok: bool) -> str:
    if scenarios is None or scenarios.empty:
        return "Прогноз не построен: не хватило истории."
    row = {r["scenario"]: r for _, r in scenarios.iterrows()}
    hold = row.get("Сохранение")
    rec = row.get("Восстановление")
    risk = row.get("Риск")
    parts = [f"При сохранении текущих темпов численность к {horizon} составит "
             f"{hold['fl_end']:,.0f} человек против {base:,.0f} сейчас "
             f"({hold['delta_perc']:+.1%} за {months} мес.)."]
    if rec is not None:
        parts.append(f"Восстановление до {rec['fl_end']:,.0f} возможно, если приход "
                     f"выйдет на уровень лучшего месяца окна.")
    if risk is not None:
        parts.append(f"При повторении худшего месяца по оттоку численность падает "
                     f"до {risk['fl_end']:,.0f} ({risk['delta_perc']:+.1%}).")
    if not season_ok:
        parts.append("Сезонность не учтена: истории для её оценки не хватило.")
    return " ".join(parts)
