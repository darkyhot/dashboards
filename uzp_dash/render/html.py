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


def page(title: str, subtitle: str, body: str, footer: str = "",
         chrome: bool = True) -> str:
    """Каркас страницы.

    `chrome=False` убирает шапку с заголовком: у отчёта-презентации первый экран
    должен начинаться прямо с первого слайда, а не съезжать вниз на высоту шапки.
    Заголовок такой отчёт показывает сам — в закреплённой полосе сверху.
    """
    footer_html = f'<div class="footer">{footer}</div>' if footer else ""
    head_html = ("" if not chrome else
                 f'<header><div class="eyebrow">УЗП · Дэшборд</div>'
                 f'<h1>{title}</h1><p class="sub">{subtitle}</p></header>')
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
{head_html}
{body}
{footer_html}
</div>
</body>
</html>"""
    return sanitize(html)
