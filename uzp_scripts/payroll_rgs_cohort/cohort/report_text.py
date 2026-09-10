"""Обезличенный текстовый документ — тот, который уезжает наружу на разбор.

Задача документа: передать ВСЮ картину падения человеку (или модели), у которого
нет доступа к контуру, и не вынести при этом ни одного названия.

Что уходит и что не уходит
--------------------------

Уходят:  численности, доли, даты, месяцы, коэффициенты, раскладка причин, состав
         разрезов — то есть всё, из чего складывается вывод.
Не уходят: наименования организаций и холдингов, номера организаций, названия
         ГОСБ, ТБ и регионов, коды подразделений. Вместо них — токены
         (`Орг-01`, `Холдинг-03`, `Регион-02`), устойчивые внутри документа:
         одна и та же организация везде названа одинаково, поэтому рассуждать о
         ней можно, а узнать её — нельзя.

Почему проверка утечки, а не аккуратность
-----------------------------------------

Документ собирается из десятка кадров, и забыть замаскировать одну колонку —
вопрос времени, а не внимательности. Поэтому перед записью готовый текст
проверяется на КАЖДОЕ известное название из данных. Нашлось хоть одно — файл не
пишется вовсе. Отчёт, который «почти обезличен», хуже отсутствующего: его
отправят.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from uzp_dash import progress
from uzp_dash.anonymize import Aliases

from . import analyze as A
from . import level as LV

# Колонки, в которых лежат названия. Список общий для маскирования и для проверки
# утечки: разъехавшись, они дали бы документ, который замаскирован не полностью,
# но проверку проходит.
NAME_COLUMNS: dict[str, str] = {
    "company_name":  "Орг",
    "holding_name":  "Холдинг",
    "name_from":     "Орг",
    "name_to":       "Орг",
    "region_name":   "Регион",
    "tb_short_name": "ТБ",
    "gosb_name":     "ГОСБ",
}

# Колонки с номерами организаций. Номер — прямой идентификатор, он не
# маскируется, а УДАЛЯЕТСЯ: токен от номера не защищает, если рядом стоят
# численность и территория.
ID_COLUMNS = ("inn", "inn_from", "inn_to", "epk_id", "gosb_id", "new_gosb_id")

# Короче этого названия не проверяются: короткое слово даёт ложные срабатывания
# на обычном тексте, и проверка начала бы валить документ на пустом месте.
MIN_NAME_LEN = 4

# Исключение — АББРЕВИАТУРЫ без строчных букв («СРБ», «ЮЗБ», «ВВБ»): сокращённые
# имена территориальных банков короче четырёх знаков, но опознают подразделение
# ничуть не хуже полного названия. Сравнение регистрозависимое и по границам
# слова, поэтому на обычной прозе такое правило не срабатывает.
MIN_ABBR_LEN = 3
_HAS_LOWER = re.compile(r"[а-яёa-z]")


def _long_enough(name: str) -> bool:
    """Достаточно ли имя длинное, чтобы его стоило искать в тексте."""
    if len(name) >= MIN_NAME_LEN:
        return True
    return len(name) >= MIN_ABBR_LEN and not _HAS_LOWER.search(name)

# Вид для номеров организаций: они не заменяются токеном, а вычищаются.
ID_KIND = "__id__"


def _n(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{float(v):,.0f}".replace(",", " ")


def _pct(v, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{float(v) * 100:.{digits}f}%"


def mask(df: pd.DataFrame, al: Aliases) -> pd.DataFrame:
    """Заменить названия токенами, убрать колонки с номерами."""
    if df is None or df.empty:
        return df
    out = df.copy()
    for col, kind in NAME_COLUMNS.items():
        if col in out:
            out[col] = [al.alias(kind, v) if pd.notna(v) else "—" for v in out[col]]
    drop = [c for c in ID_COLUMNS if c in out]
    return out.drop(columns=drop) if drop else out


def collect_names(frames: list[pd.DataFrame]) -> dict[str, str]:
    """Настоящие названия и номера из данных: имя -> вид объекта.

    Вид нужен, чтобы то же самое название, встреченное В ТЕКСТЕ, получило ТОТ ЖЕ
    токен, что и в таблице: иначе «Холдинг-02» в таблице и «Холдинг-05» в абзаце
    рядом означали бы один и тот же холдинг, и документ читался бы неверно.

    Заглушки («Холдинг не указан») и словарные значения («Образование»,
    «Муниципальное») названиями НЕ являются и в проверку не попадают: приняв
    заглушку за наименование, проверка не дала бы записать совершенно чистый
    документ — и разбираться с этим пришлось бы в момент отправки.
    """
    skip = set(A.FILL.values()) | set(LV.ORDER) | {A.AG_UNKNOWN}
    names: dict[str, str] = {}
    for df in frames:
        if df is None or df.empty:
            continue
        for col, kind in list(NAME_COLUMNS.items()) + [(c, ID_KIND)
                                                       for c in ID_COLUMNS]:
            if col not in df:
                continue
            for v in df[col].dropna().unique():
                val = str(v).strip()
                if _long_enough(val) and val not in skip:
                    names.setdefault(val, kind)
    return names


def mask_text(text: str, al: Aliases, names: dict[str, str]) -> str:
    """Заменить настоящие названия в СВОБОДНОМ тексте на те же самые токены.

    Зачем это нужно отдельно от маскирования таблиц. Тексты выводов приходят с
    ВОССТАНОВЛЕННЫМИ названиями: в HTML-отчёте, который читают внутри контура,
    «СибБ» обязан быть «СибБ», а не «ТБ-03». Тот же самый текст, попав в
    обезличенный документ как есть, вынес бы наружу и названия подразделений, и
    имена холдингов — при полностью замаскированных таблицах рядом.

    Это не гипотетическая ошибка: ровно так и вышло на первом прогоне с моделью,
    и поймала её проверка утечки, а не внимательность.

    Длинные названия заменяются раньше коротких: «Мурманская область» обязана
    смениться целиком, а не превратиться в «Регион-01 область» после того, как
    отработает более короткое вхождение.
    """
    if not text:
        return text
    for name in sorted(names, key=len, reverse=True):
        kind = names[name]
        repl = "номер скрыт" if kind == ID_KIND else al.alias(kind, name)
        text = re.sub(
            rf"(?<![А-Яа-яЁёA-Za-z0-9]){re.escape(name)}(?![А-Яа-яЁёA-Za-z0-9])",
            repl.replace("\\", "\\\\"), text)
    return text


def check_leak(text: str, names) -> list[str]:
    """Что из настоящих названий просочилось в готовый текст."""
    hits = []
    for name in names:
        if re.search(rf"(?<![А-Яа-яЁёA-Za-z0-9]){re.escape(name)}"
                     rf"(?![А-Яа-яЁёA-Za-z0-9])", text):
            hits.append(name)
            if len(hits) >= 20:
                break
    return hits


# Колонки, значения которых — ДОЛИ, а не количества. Без этого списка доля
# печатается как «0.4040» и читается как число получателей: документ уезжает
# наружу на разбор, и там переспросить будет не у кого.
PCT_COLUMNS = frozenset({"share", "delta_pct", "d_pairs_pct"})
# Колонки, где дробь — это коэффициент, а не доля: печатается как есть.
RATIO_COLUMNS = frozenset({"multi", "score"})


def _cell(col: str, v) -> str:
    """Одно значение таблицы. Формат выбирается по СМЫСЛУ колонки, а не по величине.

    Выбор по величине («меньше единицы — значит дробь») ошибается ровно там, где
    это дороже всего: доля 0,4 и коэффициент 1,09 напечатались бы одинаково, а
    «да/нет» превратилось бы в True/False.
    """
    if isinstance(v, bool) or isinstance(v, np.bool_):
        return "да" if v else "нет"
    if isinstance(v, (int, float, np.integer, np.floating)):
        if pd.isna(v):
            return "—"
        if col in PCT_COLUMNS:
            return _pct(v)
        if col in RATIO_COLUMNS:
            return f"{float(v):.4f}"
        return _n(v)
    if isinstance(v, (pd.Timestamp,)):
        return f"{v:%m.%Y}"
    return str(v)


def _table(df: pd.DataFrame, cols: list[str], head: list[str],
           limit: int = 12) -> str:
    """Кадр в markdown-таблицу. Пустой кадр — честная строка, а не пустая таблица."""
    if df is None or df.empty:
        return "_данных нет_\n"
    use = [c for c in cols if c in df.columns]
    if not use:
        return "_данных нет_\n"
    head = head[:len(use)]
    lines = ["| " + " | ".join(head) + " |",
             "|" + "|".join(["---"] * len(use)) + "|"]
    for row in df[use].head(limit).itertuples(index=False):
        lines.append("| " + " | ".join(_cell(c, v) for c, v in zip(use, row)) + " |")
    return "\n".join(lines) + "\n"


def build(t: dict, causes: pd.DataFrame, gains: pd.DataFrame, tr: pd.DataFrame,
          st: pd.DataFrame, surv: pd.DataFrame, thr: pd.DataFrame,
          codes: pd.DataFrame, mig: pd.DataFrame, cuts: dict,
          orgs: pd.DataFrame, checks: list[dict], warnings: list[str],
          probe: dict, texts: dict) -> tuple[str, list[str]]:
    """Собрать документ. Возвращает (текст, список утечек).

    Утечки возвращаются, а не бросаются исключением: решение, что делать с
    неудавшимся обезличиванием, принимает вызывающий — но записать файл он уже
    не сможет.
    """
    al = Aliases()
    m_cuts = {k: mask(v, al) for k, v in cuts.items()}
    m_orgs, m_mig = mask(orgs, al), mask(mig, al)

    # Настоящие названия собираются ДО сборки текста: ими маскируются и абзацы
    # выводов, и по ним же потом идёт проверка утечки. Один и тот же словарь на
    # оба шага — иначе маскирование и проверка разъедутся, и документ, прошедший
    # проверку, окажется замаскирован не полностью.
    names = collect_names([causes, gains, tr, thr, codes, mig, orgs,
                           *cuts.values()])
    # Тексты выводов приходят с ВОССТАНОВЛЕННЫМИ названиями — они нужны такими в
    # HTML, но не здесь. Прогоняем их через те же токены, что и таблицы.
    texts = {k: mask_text(v, al, names) for k, v in (texts or {}).items()}

    bm, cm = f"{t['base_month']:%m.%Y}", f"{t['report_month']:%m.%Y}"
    parts: list[str] = []

    parts.append(f"""# Численность бюджетного сегмента: {bm} → {cm}

**Документ обезличен.** Наименования организаций, холдингов, территориальных
банков и регионов заменены устойчивыми токенами (Орг-01, Холдинг-02, Регион-03):
один и тот же объект везде назван одинаково. Номера организаций удалены целиком.
Все численности, доли и даты приведены как есть.

## Как считается метрика

Получатель — **пара (человек, организация)**, а не человек: сотрудник, получающий
в двух организациях, весит двух получателей. Пара засчитывается за месяц, если
сумма зачислений в эту организацию за месяц **строго больше {_n(probe.get('amt_min', 0))} ₽**
по заданному списку кодов видов зачисления ({len(probe.get('codes', []))} кодов).

Сегмент организации берётся **текущим срезом** справочника ЕПК — отчётной даты в
этой витрине нет. Один и тот же список организаций применён к обоим годам,
поэтому перекоса год к году это не создаёт.

## Итог

| Показатель | {bm} | {cm} | Изменение |
|---|---|---|---|
| Пар (человек × организация) | {_n(t['pairs_base'])} | {_n(t['pairs_cur'])} | {_n(t['d_pairs'])} |
| Людей | {_n(t['epk_base'])} | {_n(t['epk_cur'])} | {_n(t['d_epk'])} |
| Организаций | {_n(t['inn_base'])} | {_n(t['inn_cur'])} | {_n(t['inn_cur'] - t['inn_base'])} |
| Организаций на человека | {t['multi_base']:.4f} | {t['multi_cur']:.4f} | {t['multi_cur'] - t['multi_base']:+.4f} |
| Доля совместителей | {_pct(t['multi_share_base'], 2)} | {_pct(t['multi_share_cur'], 2)} | {_pct(t['multi_share_cur'] - t['multi_share_base'], 2)} |

Падение метрики раскладывается на два **разных** вклада:

- людей стало меньше: **{_n(t['d_by_people'])}** пар;
- совместительство схлопнулось: **{_n(t['d_by_multi'])}** пар — при этом
  не потерян ни один человек.

{texts.get('overview', '')}
""")

    parts.append(f"""## Из чего состоит потеря

Каждая потерянная пара получает **ровно одну** причину — первую сработавшую по
лестнице приоритетов, поэтому части складываются в целое.

Потеряно пар: **{_n(t['lost'])}**. Пришло: **{_n(t['gained'])}**.
Из потерянных **{_n(t['lost_not_real'])}** ({_pct(t['lost_not_real'] / (t['lost'] or 1), 0)})
оттоком людей **не являются**.

{_table(causes, ['title', 'n_pairs', 'n_epk', 'share', 'is_real_loss'],
        ['Причина', 'Пар', 'Человек', 'Доля потерь', 'Это отток?'])}
Расшифровка веток:

{chr(10).join(f"- **{v[0]}** — {v[1]}" for k, v in A.CAUSES.items())}

### Пришло за тот же год

{_table(gains, ['title', 'n_pairs', 'n_epk', 'share'],
        ['Откуда', 'Пар', 'Человек', 'Доля'])}""")

    parts.append(f"""## Когда

{texts.get('when', '')}

Помесячный ряд:

{_table(tr, ['report_dt', 'n_pairs', 'n_epk', 'multi', 'd_pairs'],
        ['Месяц', 'Пар', 'Человек', 'Орг. на человека', 'Изменение'], limit=40)}
Месяцы-обрывы (изменение резко выпадает из обычного шага ряда):

{_table(st, ['report_dt', 'd_pairs', 'd_pairs_pct', 'score'],
        ['Месяц', 'Изменение', 'К предыдущему', 'Во сколько раз резче'], limit=8)}
Дожитие пар базового месяца:

{_table(surv, ['report_dt', 'n_alive', 'share'],
        ['Месяц', 'Пар живо', 'Доля от базы'], limit=40)}""")

    parts.append(f"""## Почему

{texts.get('why', '')}

### Чувствительность к порогу получателя

{_table(thr, ['threshold', 'base', 'cur', 'delta', 'delta_pct'],
        ['Порог, ₽', bm, cm, 'Изменение', '%'], limit=8)}
### Коды видов зачисления

«В списке» — коды, которые метрика считает; остальные не считает вовсе.

{_table(codes, ['code', 'code_name', 'in_list', 'base', 'cur', 'delta'],
        ['Код', 'Вид зачисления', 'В списке', bm, cm, 'Изменение'], limit=16)}
### Похоже на реорганизацию

Организации, чьи люди дружно перешли в один и тот же новый номер.

{_table(m_mig, ['name_from', 'name_to', 'n_epk', 'share', 'to_in_segment'],
        ['Откуда', 'Куда', 'Человек', 'Доля потерь организации',
         'Приёмник в сегменте'], limit=12)}""")

    where_parts = []
    titles = {"holding_name": "Холдинги", "agency": "Ведомства (по наименованию)",
              "level": "Уровень подчинения", "industry_name": "Отрасль справочника",
              "tb_short_name": "Территориальные банки", "region_name": "Регионы"}
    for dim, df in m_cuts.items():
        if df is None or df.empty:
            continue
        where_parts.append(
            f"### {titles.get(dim, dim)}\n\n"
            + _table(df, [dim, "n_pairs", "share", "n_inn", "real_loss", "not_real"],
                     [titles.get(dim, dim), "Потеряно пар", "Доля", "Организаций",
                      "Из них отток", "Методология"]))
    parts.append(f"""## Где

{texts.get('where', '')}

{chr(10).join(where_parts)}
### Организации с наибольшей потерей

{_table(m_orgs, ['company_name', 'n_pairs', 'cause_title', 'agency', 'level'],
        ['Организация', 'Потеряно пар', 'Преобладающая причина', 'Ведомство',
         'Уровень'], limit=15)}""")

    checks_txt = "\n".join(
        f"- {c['name']}: {_n(c['left'])} против {_n(c['right'])} — "
        f"{'сошлось' if c['ok'] else 'НЕ СОШЛОСЬ'}" for c in checks)
    warn_txt = "\n".join(f"- {w}" for w in warnings) or "- нет"
    parts.append(f"""## Проверки и ограничения

Сходимость:

{checks_txt}

Предупреждения прогона:

{warn_txt}

Чего этот разбор не говорит:

- в какой банк ушли люди — в разрешённых источниках такого поля нет;
- увольнение и смену работодателя вне клиентской базы банка он не различает:
  человек, которого нет в ведомостях, в обоих случаях выглядит одинаково;
- сегмент восстановлен текущим срезом, а не на дату: переклассификация
  организации между годами в разбор не попадёт.

Доля организаций, чей номер пригоден к сопоставлению со справочником:
{_pct(probe.get('inn_ok_base') or 0, 2)} в базовом месяце,
{_pct(probe.get('inn_ok_cur') or 0, 2)} в отчётном.
""")

    text = "\n\n".join(parts)
    leaks = check_leak(text, names)
    return text, leaks


def write(path: Path, text: str, leaks: list[str]) -> bool:
    """Записать документ, если обезличивание прошло. Иначе — не записать.

    Именно не записать, а не «записать с предупреждением»: файл с предупреждением
    внутри всё равно отправят, предупреждение прочитают потом.
    """
    if leaks:
        progress.warn(
            f"ОБЕЗЛИЧИВАНИЕ НЕ ПРОШЛО: в текст попали настоящие названия "
            f"({', '.join(leaks[:5])}{'…' if len(leaks) > 5 else ''}). "
            f"Файл {path.name} НЕ записан — отправлять было бы нечего проверять.")
        return False
    path.write_text(text, encoding="utf-8")
    progress.done(f"обезличенный документ: {path} ({len(text) / 1024:.1f} КБ)")
    return True
