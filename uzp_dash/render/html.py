"""Сборка самодостаточной HTML-страницы (весь CSS инлайн)."""
from __future__ import annotations

import re

from .theme import BASE_CSS

# Требование безопасности: слово «ИНН» в файле блокирует пересылку отчёта.
# Меняем на «Орг.» ТОЛЬКО как отдельное слово (иначе пострадают «длинный»,
# «старинный» и т.п.). Латинские идентификаторы (r.inn, inn=) не затрагиваются.
_INN_WORD = re.compile(r"(?<![А-Яа-яЁёA-Za-z])ИНН(?![А-Яа-яЁёA-Za-z])", re.IGNORECASE)


def sanitize(text: str) -> str:
    """Убрать из готового текста слово «ИНН» (в т.ч. пришедшее из ответа LLM)."""
    return _INN_WORD.sub("Орг.", text or "")


def page(title: str, subtitle: str, body: str, footer: str = "") -> str:
    footer_html = f'<div class="footer">{footer}</div>' if footer else ""
    html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{BASE_CSS}</style>
</head>
<body>
<div class="wrap">
<header>
<div class="eyebrow">УЗП · Дэшборд</div>
<h1>{title}</h1>
<p class="sub">{subtitle}</p>
</header>
{body}
{footer_html}
</div>
</body>
</html>"""
    return sanitize(html)
