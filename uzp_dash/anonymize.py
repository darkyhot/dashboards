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


class Aliases:
    """Двусторонний словарь «настоящее имя ↔ токен» на один вызов LLM."""

    def __init__(self) -> None:
        self._by_name: dict[tuple[str, str], str] = {}
        self._by_token: dict[str, str] = {}
        self._counters: dict[str, int] = {}

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
        """Вернуть настоящие имена в текст ответа LLM."""
        if not text or not self._by_token:
            return text
        text = self._expand_lists(text)
        # от длинных номеров к коротким: иначе «ГОСБ-1» съест начало «ГОСБ-12»
        for token in sorted(self._by_token, key=len, reverse=True):
            kind, _, num = token.rpartition("-")
            pattern = re.compile(rf"{re.escape(kind)}{_SEP}0*{int(num)}(?!\d)")
            name = self._by_token[token]
            text = pattern.sub(lambda _m, n=name: n, text)   # без спецсимволов замены
        return text

    def __len__(self) -> int:
        return len(self._by_token)
