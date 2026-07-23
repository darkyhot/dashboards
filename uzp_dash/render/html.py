"""Сборка самодостаточной HTML-страницы (весь CSS инлайн)."""
from __future__ import annotations

from .theme import BASE_CSS


def page(title: str, subtitle: str, body: str, footer: str = "") -> str:
    footer_html = f'<div class="footer">{footer}</div>' if footer else ""
    return f"""<!doctype html>
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
