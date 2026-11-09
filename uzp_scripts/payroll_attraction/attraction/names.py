"""Названия организаций и холдингов: показываются, КРОМЕ содержащих ФИО.

У ИП, глав КФХ, адвокатов и нотариусов в названии — фамилия, имя и отчество
человека. Такое название не показывается нигде: вместо него — id орг (номер
организации цифрами), у холдинга — пометка «название скрыто».

Маска применяется ОДИН раз и централизованно — к каждой выборке из БД
(`fetch.Workspace.sql`), до того как название попадёт в расчёты, HTML или CSV.
Колонки с названиями во всех запросах называются только так, как перечислено в
`NAME_COLS`; самопроверка `check_names_masked` следит, чтобы новая колонка не
обошла маску.

Признаки ФИО (любой из):
* ИП, КФХ, «индивидуальный предприниматель», «крестьянское (фермерское)
  хозяйство», «глава», адвокат, нотариус;
* слово-отчество: -ович, -евич, -ьич, -овна, -евна, -ична, -инична; оглы, кызы,
  улы, уулу;
* инициалы «И.И.».
Ложное срабатывание (организация «им. А.С. Пушкина») безопасно: покажется номер.
"""
from __future__ import annotations

import re

import pandas as pd

NAME_COLS = ("org_name", "holding_name")
HIDDEN_HOLDING = "название скрыто (ФИО)"

_L = "А-Яа-яЁё"
_MARKERS = re.compile(
    rf"(?<![{_L}])(?:ИП|КФХ|ГКФХ|ИЧП|ЧП)(?![{_L}])"
    rf"|индивидуальн[{_L}]*\s+предпринимател"
    rf"|крестьянск[{_L}]*\s*\(?\s*фермерск"
    rf"|(?<![{_L}])глава(?![{_L}])"
    rf"|(?<![{_L}])(?:адвокат|нотариус)"
    rf"|[{_L}]+(?:ович|евич|ьич|овна|евна|ична|инична)(?![{_L}])"
    rf"|(?<![{_L}])(?:оглы|кызы|улы|уулу)(?![{_L}])",
    re.IGNORECASE)
# Инициалы — только заглавные: «т.д.» и «г.о.» не инициалы.
_INITIALS = re.compile(rf"(?<![{_L}])[А-ЯЁ]\.\s?[А-ЯЁ]\.")


def has_fio(name) -> bool:
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return False
    s = str(name)
    return bool(_MARKERS.search(s) or _INITIALS.search(s))


def safe(name):
    """Название, если в нём нет ФИО; иначе None."""
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return None
    s = str(name).strip()
    return None if not s or has_fio(s) else s


def mask_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Заменить названия с ФИО на None во всех колонках-названиях выборки.
    Рядом ставится флаг `<колонка>_hidden`: «скрыто» отличается от «нет названия»."""
    if df is None or df.empty:
        return df
    for c in NAME_COLS:
        if c in df.columns:
            hidden = df[c].map(has_fio)
            df[c] = df[c].map(safe)
            df[f"{c}_hidden"] = hidden
    return df


def org_label(name, org_id) -> str:
    """Подпись организации: название либо (нет или скрыто) id орг цифрами."""
    s = safe(name)
    if s:
        return s
    try:
        return f"id орг {int(org_id)}"
    except (TypeError, ValueError):
        return "id орг —"


def holding_label(name, hidden=False) -> str:
    if hidden:
        return HIDDEN_HOLDING
    s = safe(name)
    return s if s else "без холдинга"
