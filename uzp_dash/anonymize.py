"""Псевдонимы для запросов к LLM.

Зачем: корпоративный шлюз (GigaChat) блокирует запрос и отправляет обращение в
blacklist, если в промпте встречаются чувствительные названия — например «ГО по
Донецкой Народной Республике». Поэтому названия ГОСБ и компаний в LLM не уходят
вовсе: вместо них подставляются нейтральные токены («ГОСБ-01»), а реальные имена
возвращаются в ответ модели уже на нашей стороне.
"""
from __future__ import annotations

import re

# Разделители, которыми модель может «переписать» токен: «ГОСБ-01», «ГОСБ 1», «ГОСБ—01»
_SEP = r"[\s\-–—_]*"

# Чем заменить токен, которому не нашлось имени. Ключи — те же `kind`, что уходят
# в `alias()`; вид, которого здесь нет, получит формулировку по умолчанию.
UNKNOWN = {
    "ГОСБ": "другие подразделения",
    "ТБ": "другие территориальные банки",
    "Организация": "другие организации",
}


class Aliases:
    """Двусторонний словарь «настоящее имя ↔ токен» на один вызов LLM."""

    def __init__(self) -> None:
        self._by_name: dict[tuple[str, str], str] = {}
        self._by_token: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self.leaked = 0          # сколько выдуманных токенов пришлось обезличить

    def alias(self, kind: str, name) -> str:
        """Токен для имени. Повторный вызов даёт тот же токен."""
        name = (str(name) if name is not None else "").strip()
        if not name:
            return name
        key = (kind, name)
        if key not in self._by_name:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            token = f"{kind}-{self._counters[kind]:02d}"
            self._by_name[key] = token
            self._by_token[token] = name
        return self._by_name[key]

    def _expand_lists(self, text: str) -> str:
        """Развернуть перечисления вида «ГОСБ-01, 02 и 03» в полные токены.

        Модель часто сокращает повтор префикса; без этого шага восстановилось бы
        только первое имя, а остальные остались бы голыми числами.
        """
        for kind, last in self._counters.items():
            pattern = re.compile(
                rf"({re.escape(kind)}{_SEP}0*(\d+))((?:\s*(?:,|и)\s*0*\d{{1,2}}(?!\d))+)")

            def repl(m, kind=kind, last=last):
                head, tail = m.group(1), m.group(3)
                out = head
                for part in re.finditer(r"(\s*(?:,|и)\s*)0*(\d{1,2})(?!\d)", tail):
                    sep, num = part.group(1), int(part.group(2))
                    if not 1 <= num <= last:      # это не номер ГОСБ — оставляем как есть
                        return m.group(0)
                    out += f"{sep}{kind}-{num:02d}"
                return out

            text = pattern.sub(repl, text)
        return text

    def restore(self, text: str) -> str:
        """Вернуть настоящие имена в текст ответа LLM.

        Последним шагом — страховка от НЕИЗВЕСТНЫХ токенов. Модель называет и то,
        чего в промпте не было: пишет «ГОСБ-04 и ГОСБ-05», хотя псевдонимов завели
        три. Вернуть такое имя неоткуда, а оставить токен в отчёте нельзя — читатель
        видит «ГОСБ-05» и не понимает, о ком речь. Заменяем нейтральной формулировкой
        и считаем, сколько раз это понадобилось (`leaked`): если счётчик не ноль,
        промпту не хватило псевдонимов, и это видно в прогрессе.
        """
        self.leaked = 0
        if not text:
            return text
        if self._by_token:
            text = self._expand_lists(text)
            # от длинных номеров к коротким: иначе «ГОСБ-1» съест начало «ГОСБ-12»
            for token in sorted(self._by_token, key=len, reverse=True):
                kind, _, num = token.rpartition("-")
                pattern = re.compile(rf"{re.escape(kind)}{_SEP}0*{int(num)}(?!\d)")
                name = self._by_token[token]
                text = pattern.sub(lambda _m, n=name: n, text)  # без спецсимволов замены
        return self._drop_unknown(text)

    def _drop_unknown(self, text: str) -> str:
        """Убрать токены, которых нет в словаре, — модель их выдумала."""
        for kind in self._kinds():
            repl = UNKNOWN.get(kind, f"другие {kind}")
            pattern = re.compile(rf"{re.escape(kind)}{_SEP}0*\d+(?!\d)")
            text, n = pattern.subn(repl, text)
            self.leaked += n
            if n > 1:
                # «ГОСБ-04 и ГОСБ-05» превратилось бы в «другие подразделения и
                # другие подразделения» — схлопываем повтор в одну формулировку
                dup = re.compile(rf"{re.escape(repl)}(?:\s*(?:,|и)\s*{re.escape(repl)})+")
                text = dup.sub(repl, text)
        return text

    def _kinds(self) -> set:
        """Все виды токенов: и заведённые, и те, что модель могла выдумать сама."""
        return set(self._counters) | set(UNKNOWN)

    def __len__(self) -> int:
        return len(self._by_token)
