"""Обезличенный текстовый документ — тот, который уезжает наружу на разбор.

Задача документа: передать ВСЮ картину падения человеку (или модели), у которого
нет доступа к контуру, и не вынести при этом ни одного названия.

Что уходит и что не уходит
--------------------------

Уходят:  численности, доли, даты, месяцы, коэффициенты, раскладка причин, состав
         разрезов — то есть всё, из чего складывается вывод.
Не уходят: наименования организаций и холдингов, номера организаций, названия
         ГОСБ и ТБ, коды подразделений. Вместо них — токены
         (`Орг-01`, `Холдинг-03`, `ГОСБ-02`), устойчивые внутри документа:
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
from .view import CUT_TITLES

# Колонки, в которых лежат названия. Список общий для маскирования и для проверки
# утечки: разъехавшись, они дали бы документ, который замаскирован не полностью,
# но проверку проходит.
NAME_COLUMNS: dict[str, str] = {
    "company_name":  "Орг",
    "holding_name":  "Холдинг",
    "name_from":     "Орг",
    "name_to":       "Орг",
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

# Значения, которые названиями НЕ являются: заглушки расчёта и словарные группы.
# Список общий с `analyze`, потому что разъехавшись, он даст либо токен вместо
# заглушки, либо ложное срабатывание проверки утечки на обычном слове.
_PLACEHOLDERS = set(A.FILL.values()) | set(LV.ORDER) | {A.AG_UNKNOWN}


def _n(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{float(v):,.0f}".replace(",", " ")


def _pct(v, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{float(v) * 100:.{digits}f}%"


def _is_placeholder(v) -> bool:
    """Заглушка ли это значение, а не название.

    «Холдинг не указан» и «ТБ неизвестен» — не имена, а признание, что имени нет.
    Выдав заглушке токен, документ показывает «ТБ-01» там, где на самом деле
    ничего не известно, и читатель принимает пробел за подразделение. Ровно так и
    вышло на прогоне по проме: весь территориальный разрез состоял из одной
    заглушки и выглядел как настоящий ТБ со стопроцентной долей.
    """
    return str(v) in _PLACEHOLDERS


def mask(df: pd.DataFrame, al: Aliases) -> pd.DataFrame:
    """Заменить названия токенами, убрать колонки с номерами.

    Заглушки остаются как есть — см. `_is_placeholder`.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    for col, kind in NAME_COLUMNS.items():
        if col not in out:
            continue
        out[col] = [v if _is_placeholder(v) else
                    (al.alias(kind, v) if pd.notna(v) else "—") for v in out[col]]
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
    skip = _PLACEHOLDERS
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
PCT_COLUMNS = frozenset({"share", "delta_pct", "d_pairs_pct",
                         "d_month_pct", "d_year_pct", "d_triples_pct",
                         "share_lb", "share_loss", "lb_share", "same_gosb_share",
                         "ratio", "b1", "b2", "b3", "b4", "b5"})
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


def build(t: dict, t_prev: dict | None, causes: pd.DataFrame,
          gains: pd.DataFrame, causes_epk: pd.DataFrame,
          gains_epk: pd.DataFrame, both: pd.DataFrame, tr: pd.DataFrame,
          st: pd.DataFrame, measured: str, cmp_months: pd.DataFrame,
          surv: pd.DataFrame, thr: pd.DataFrame, seas: dict,
          codes_m: pd.DataFrame,
          to_seg: pd.DataFrame, to_codes: pd.DataFrame,
          to_codes_inn: pd.DataFrame, mig: pd.DataFrame,
          cuts: dict, orgs: pd.DataFrame, tenure: pd.DataFrame,
          checks: list[dict], warnings: list[str], probe: dict, meta: dict,
          shown: dict, texts: dict, why: dict | None = None,
          gone_x: dict | None = None,
          multi: dict | None = None) -> tuple[str, list[str]]:
    """Собрать документ. Возвращает (текст, список утечек).

    Утечки возвращаются, а не бросаются исключением: решение, что делать с
    неудавшимся обезличиванием, принимает вызывающий — но записать файл он уже
    не сможет.
    """
    al = Aliases()
    m_cuts = {k: mask(v, al) for k, v in cuts.items()}
    m_orgs, m_mig = mask(orgs, al), mask(mig, al)
    # Номер организации `mask` не заменяет токеном, а УДАЛЯЕТ (ID_COLUMNS).
    # Поэтому в рамке обязано быть название: без него наружу уехала бы таблица
    # из одних чисел, по которой ничего не сверить.
    m_codes_inn = mask(to_codes_inn, al)
    why, gone_x = why or {}, gone_x or {}
    lso = gone_x.get("lso", pd.DataFrame())
    org_top = why.get("org_top", pd.DataFrame())
    m_lso = mask(lso, al) if lso is not None and not lso.empty else pd.DataFrame()
    m_org_top = (mask(org_top, al) if org_top is not None and not org_top.empty
                 else pd.DataFrame())
    below = gone_x.get("below", pd.DataFrame())
    lm = gone_x.get("lso_meta", {}) or {}

    # Настоящие названия собираются ДО сборки текста: ими маскируются и абзацы
    # выводов, и по ним же потом идёт проверка утечки. Один и тот же словарь на
    # оба шага — иначе маскирование и проверка разъедутся, и документ, прошедший
    # проверку, окажется замаскирован не полностью.
    multi = multi or {}
    m_orgs_multi = {k: mask(v.rename(columns={"company_to": "name_to"}), al)
                    for k, v in (multi.get("orgs") or {}).items()}
    names = collect_names([causes, gains, tr, thr, to_seg, to_codes, codes_m,
                           to_codes_inn, mig, orgs, lso, org_top,
                           *[v.rename(columns={"company_to": "name_to"})
                             for v in (multi.get("orgs") or {}).values()],
                           *cuts.values()])
    # Тексты выводов приходят с ВОССТАНОВЛЕННЫМИ названиями — они нужны такими в
    # HTML, но не здесь. Прогоняем их через те же токены, что и таблицы.
    texts = {k: mask_text(v, al, names) for k, v in (texts or {}).items()}

    bm, cm = f"{t['base_month']:%m.%Y}", f"{t['report_month']:%m.%Y}"
    parts: list[str] = []

    parts.append(f"""# Численность бюджетного сегмента: {bm} → {cm}

**Документ обезличен.** Наименования организаций, холдингов, ТБ и ГОСБ заменены
устойчивыми токенами (Орг-01, Холдинг-02, ГОСБ-03): один и тот же объект везде
назван одинаково. Номера организаций удалены целиком. Все численности, доли и
даты приведены как есть. Запросы, которыми посчитаны цифры, — в приложении.

## Определения

- **{A.DEF_GETS}** Список зарплатных кодов — {len(probe.get('codes', []))} кодов
  видов зачисления; порог — {_n(probe.get('amt_min', 0))} ₽ по организации.
- **{A.DEF_ROW}**
- Сегмент организации берётся **текущим срезом** справочника ЕПК — отчётной даты
  в этой витрине нет. Один и тот же список организаций применён к обоим годам.

## Итог

{_decomp_md(t)}
ФЛ перестало получать зарплату в РГС, только если в {cm} у него нет ни одной
бюджетной организации с зарплатой больше 2 500 ₽. Сменило организацию или ГОСБ,
осталось в одной организации из двух — продолжает получать зарплату в РГС.

| Показатель | {bm} | {cm} | Изменение |
|---|---|---|---|
| Организаций | {_n(t['inn_base'])} | {_n(t['inn_cur'])} | {_n(t['inn_cur'] - t['inn_base'])} |
| Доля совместителей | {_pct(t['multi_share_base'], 2)} | {_pct(t['multi_share_cur'], 2)} | {_pct(t['multi_share_cur'] - t['multi_share_base'], 2)} |

{texts.get('overview', '')}
""")

    parts.append(f"""## Кто перестал и кто начал получать зарплату в РГС

{texts.get('net', '')}

### {A.T_LOST}: почему (ФЛ)

У каждого ФЛ **ровно одна** ситуация — первая сработавшая по порядку, поэтому
ситуации складываются в {_n(t['real_lost_epk'])}.

{_table(causes_epk, ['title', 'n_epk', 'share'], ['Ситуация', 'ФЛ', 'Доля'])}
### {A.T_GAINED}: откуда (ФЛ)

{_table(gains_epk, ['title', 'n_epk', 'share'], ['Ситуация', 'ФЛ', 'Доля'])}
Расшифровка ситуаций:

{chr(10).join(_cause_line(k, v) for k, v in A.CAUSES.items())}
{chr(10).join(_cause_line(k, v) for k, v in A.GAINS.items())}""")

    parts.append(_multi_md(multi, t, m_orgs_multi, texts.get("multi", "")))

    prev_txt = ""
    if t_prev is not None:
        pm = f"{t_prev['base_month']:%m.%Y}"
        prev_txt = f"""
### Что произошло за один последний месяц: {pm} → {cm}

{_decomp_md(t_prev)}
То же разложение, что в итоге, но к предыдущему месяцу: видно, какая часть
годового изменения случилась за последний месяц.
"""

    parts.append(f"""## Когда

{texts.get('when', '')}

### Отчётный месяц против соседних и против года назад

{_table(cmp_months, ['report_dt', 'n_triples', 'n_epk', 'multi', 'vs_report', 'vs_report_epk'],
        ['Месяц', 'Получателей', 'ФЛ', 'Получателей на ФЛ',
         'Получателей к отчётному', 'ФЛ к отчётному'], limit=8)}
Если отчётный месяц ниже соседних так же, как был ниже год назад, годовое
падение повторяет обычный сезонный провал, и тренд из него не выводится.

### Помесячный ряд

Колонки `yoy` — изменение к тому же месяцу год назад.

{_table(tr, ['report_dt', 'n_triples', 'n_epk', 'multi', 'yoy', 'yoy_epk'],
        ['Месяц', 'Получателей', 'ФЛ', 'Получателей на ФЛ',
         'Год к году', 'Год к году, ФЛ'], limit=40)}
### Сезонность: повторяется ли провал год к году

Сезонным считается только ПОВТОРЯЮЩЕЕСЯ поведение: человек получал в предыдущем
месяце оба года и не получал в отчётном оба года. Просто «был в прошлом месяце,
нет сейчас» сезонностью не является — доказать, что человек вернётся, нечем.

{_seasonal_md(seas)}
### Какой вид выплаты просел

Только зарплатные коды — те, что входят в метрику. Провал одного вида выплаты
(стипендия в каникулы, премия в конце квартала) выглядит падением численности,
хотя человек никуда не ушёл: у него просто нет выплаты этого вида в этом месяце.

{_table(codes_m, ['name', 'base', 'prev', 'cur', 'd_month', 'd_month_pct', 'd_year', 'd_year_pct'],
        ['Вид зачисления', 'Год назад', 'Пред. месяц', 'Отчётный',
         'К пред. месяцу', '%', 'Год к году', '%'], limit=20)}
### Месяцы-обрывы

Мерилось: {measured}. Регулярный сезонный провал обрывом не считается.

{_table(st, ['report_dt', 'delta', 'score'],
        ['Месяц', 'Изменение', 'Во сколько раз резче обычного'], limit=8)}
### Сколько получателей базового месяца получают до сих пор

{_table(surv, ['report_dt', 'n_alive', 'share'],
        ['Месяц', 'Получают', 'Доля от базового месяца'], limit=40)}{prev_txt}""")

    parts.append(f"""## Куда делись люди

{texts.get('gone', '')}

Ситуации различаются по данным: зарплата от бюджетной организации есть, но до
2 500 ₽; от бюджетной организации приходят только незарплатные выплаты; зарплата
от небюджетной организации; зачислений в банке нет вовсе.

### В каком сегменте теперь получают зарплату

{_table(to_seg, ['segment_name', 'n_epk', 'share'],
        ['Сегмент', 'ФЛ', 'Доля'], limit=12)}
### Чем заменились зарплатные зачисления

Это ФЛ, которым от бюджетной организации приходят только незарплатные выплаты.
Зарплату в РГС они получать перестали — это уже учтено в итоге. Таблица говорит,
что с ними случилось.

{_table(to_codes, ['code', 'code_name', 'n_epk', 'share'],
        ['Код', 'Вид зачисления', 'ФЛ', 'Доля'], limit=15)}
### От каких организаций приходят только незарплатные выплаты

Доля считается ВНУТРИ вида зачисления: вопрос здесь — какую часть перешедших даёт
одна организация. Верх из одной-двух — это адрес, куда идти; ровный список — фон
по всему сегменту.

{_table(m_codes_inn, ['code_name', 'company_name', 'n_epk', 'share'],
        ['Вид зачисления', 'Организация', 'ФЛ', 'Доля вида'], limit=60)}
### От каких небюджетных организаций теперь получают зарплату

Всего {_n(lm.get('n_orgs', 0))} организаций; десять крупнейших дают
{_pct(lm.get('top10_share', 0))} таких ФЛ, в том же ГОСБ осталось
{_pct(lm.get('same_gosb_share', 0))}. Организация у ФЛ одна — та, что платит
больше всех.

{_table(m_lso, ['company_name', 'segment_name', 'industry_name', 'n_epk', 'share',
                'same_gosb_share'],
        ['Организация', 'Сегмент', 'Отрасль', 'ФЛ', 'Доля',
         'В том же ГОСБ'], limit=15)}
### Ниже порога — насколько

«80–100% порога» вместе с «почти не изменилась» — артефакт порога: зарплата была
чуть выше и стала чуть ниже. «Упала больше чем вдвое» — неполная ставка, простой.

{_table(below, ['axis', 'bucket', 'n_epk', 'share'],
        ['Что меряем', 'Диапазон', 'ФЛ', 'Доля'], limit=10)}
### Похоже на переоформление

{_table(m_mig, ['name_from', 'name_to', 'n_epk', 'share', 'to_in_segment'],
        ['Откуда', 'Куда', 'ФЛ', 'Доля от ушедших получателей организации',
         'Новая организация в РГС'], limit=12)}
Если новая организация в РГС — ФЛ продолжают получать зарплату в РГС. Если нет —
они учтены в ситуации «зарплата от небюджетной организации».

### Сколько месяцев переставшие получали зарплату в РГС

{_table(tenure, list(tenure.columns) if tenure is not None and not tenure.empty else [],
        ['Месяцев в РГС'] + [str(c) for c in (list(tenure.columns)[1:]
                                                   if tenure is not None and not tenure.empty else [])],
        limit=10)}
### А не в пороге ли дело

{_table(thr, ['threshold', 'base', 'cur', 'delta', 'delta_pct'],
        ['Порог, ₽', bm, cm, 'Изменение', '%'], limit=8)}
Порог фиксирован, а зарплаты индексируются — сам по себе он должен год к году
добавлять получателей. Если падение сохраняется при пороге 0, порог ни при чём.
""")

    meta_o = why.get("org_meta", {}) or {}
    parts.append(f"""## Почему ушли

{texts.get('why', '')}

Мотивы людей — цена, бонусы конкурента, увольнение против смены работы — в
разрешённых источниках не видны. Раздел отделяет решения организаций от решений
людей и показывает, можно ли было уход заметить заранее.

### Ушла организация или уходят люди

На организации, которые увели зарплатный проект целиком или массово, приходится
**{_pct(meta_o.get('share_lb_org', 0))}** всех ФЛ без зачислений в банке. «Ушла целиком» —
в отчётном месяце ни одного получателя и ни одного зачисления; «массовый уход» —
из банка ушло не меньше {_pct(meta_o.get('mass_share', 0.5))} получателей;
«перестала платить, люди остались в банке» — реорганизация или новый ИНН, а не уход
клиента; организации меньше {meta_o.get('min_base', 10)} получателей в классы не
делятся.

{_table(why.get('org_sum'), ['org_class', 'n_org', 'n_base', 'left_bank', 'share_lb',
                             'loss', 'share_loss'],
        ['Организации', 'Сколько', 'Получателей в базовом', 'Нет зачислений в банке',
         'Доля от всех без зачислений', A.T_LOST, 'Доля от всех переставших'])}
### Договор зарплатного проекта

Номер договора известен у {_pct(meta_o.get('agr_known', 0))} организаций.

{_table(why.get('org_agr'), ['org_class', 'agr', 'n_org', 'loss'],
        ['Организации', 'Договор', 'Сколько', A.T_LOST])}
### Организации, которые увели проект целиком или массово

{_table(m_org_top, ['company_name', 'org_class', 'agr', 'n_base', 'n_cur',
                    'left_bank', 'loss', 'lb_share'],
        ['Организация', 'Что произошло', 'Договор', 'Было', 'Стало',
         'Нет зачислений в банке', A.T_LOST, 'Доля без зачислений в банке'], limit=15)}
### Как уходили из банка

Два последних месяца с зачислениями против трёх до них. Постепенный уход — сначала
уводилась часть зарплаты или сокращалась ставка: такого клиента можно было
заметить заранее.

{_table(why.get('pat'), ['pattern', 'n_epk', 'share', 'ratio'],
        ['Как', 'ФЛ', 'Доля', 'Последние месяцы к прежним (в среднем)'])}
### В каком месяце уходили

{_table(why.get('gone_m'), ['gone_month', 'n_epk', 'share'],
        ['Месяц ухода', 'ФЛ', 'Доля'], limit=16)}
### Сколько получали ушедшие по сравнению с коллегами

Зарплата против средней по своей организации в базовом месяце.

{_table(why.get('pay'), ['fate_title', 'n_triples', 'b1', 'b2', 'b3', 'b4', 'b5'],
        ['Группа', 'Получателей'] + list(A.PAY_BUCKETS.values()))}""")

    where_parts = []
    titles = CUT_TITLES
    for dim, df in m_cuts.items():
        if df is None or df.empty:
            continue
        spec = A.cut_columns(dim, df)
        cols, heads = [dim] + [c for c, _ in spec], [titles.get(dim, dim)] + [h for _, h in spec]
        top_n = int(meta.get("top_n", 12))
        tot = A.cut_total(df)
        tot_line = ("| **Итого по всем группам** | " + " | ".join(
            f"**{_cell(c, tot[c])}**" if c in tot else "" for c, _ in spec) + " |\n")
        block = (f"### {titles.get(dim, dim)}\n\nПо числу переставших получать:\n\n"
                 + _table(A.top_cut(df, A.LOSS, top_n), cols, heads, limit=top_n)
                 + tot_line)
        if A.DELTA in df:
            block += ("\nПо изменению получателей (сначала самые большие минусы):\n\n"
                      + _table(A.top_cut(df, A.DELTA, top_n), cols, heads,
                               limit=top_n) + tot_line)
        where_parts.append(block)
    parts.append(f"""## Где

{texts.get('where', '')}

{chr(10).join(where_parts)}
### Организации с наибольшим изменением

Считается по организации: «было и не стало» включает и перешедших в другую
организацию РГС, поэтому сумма больше, чем «перестали получать» в итоге.
Сортировка по изменению.

{_table(m_orgs, ['company_name', 'net', 'lost', 'gained', 'cause_title', 'agency', 'level'],
        ['Организация', 'Изменение получателей', 'Было и не стало', 'Не было и стало', 'Чаще всего',
         'Ведомство', 'Уровень'], limit=15)}""")

    checks_txt = "\n".join(
        f"- {c['name']}: {_n(c['left'])} против {_n(c['right'])} — "
        f"{'сошлось' if c['ok'] else 'НЕ СОШЛОСЬ'}" for c in checks)
    warn_txt = "\n".join(f"- {w}" for w in warnings) or "- нет"
    geo = ("" if meta.get("gosb_key") else
           "\n- ГОСБ ведомостей не опознаются справочником — в разрезе они "
           "подписаны номерами (в документе — токенами);")
    parts.append(f"""## Проверки и ограничения

Сходимость:

{checks_txt}

Предупреждения прогона:

{warn_txt}

Чего этот разбор не говорит:

- в какой банк ушли люди — в разрешённых источниках такого поля нет;
- увольнение и смену работодателя вне клиентской базы банка он не различает;
- сегмент восстановлен текущим срезом, а не на дату;
- сезонность подтверждается только повтором год к году: про ФЛ, пропавшее
  впервые, разбор не говорит, что оно вернётся — следующего месяца в данных нет;{geo}

Доля организаций, чей номер пригоден к сопоставлению со справочником:
{_pct(probe.get('inn_ok_base') or 0, 2)} в базовом месяце,
{_pct(probe.get('inn_ok_cur') or 0, 2)} в отчётном.
""")

    parts.append(_sql_appendix(shown))

    text = "\n\n".join(parts)
    leaks = check_leak(text, names)
    return text, leaks


def _multi_md(multi: dict, t: dict, m_orgs: dict, text: str) -> str:
    """Раздел «Почему стало меньше совместителей» — те же числа, что в HTML."""
    if not multi:
        return ""
    sgn = lambda v: ("+" if v > 0 else "") + _n(v)  # noqa: E731
    out = [f"## {A.T_MULTI}", "", text, "",
           "| | Получателей |", "|---|---|",
           f"| **Изменение получателей** | **{sgn(multi['d_triples'])}** |",
           f"| из них изменение ФЛ | {sgn(multi['d_epk'])} |",
           f"| **из них изменение совместительства** | **{sgn(multi['d_multi'])}** |",
           f"| совместители перестали получать зарплату в РГС | {sgn(multi['lost'])} |",
           f"| совместители среди начавших получать | {sgn(multi['gained'])} |",
           f"| {A.inside_title(multi['inside'])} | {sgn(multi['inside'])} |", "",
           "Совместитель в трёх организациях — один ФЛ и три получателя, то есть два "
           "«лишних». Три части совместительства — те же числа, что в итоге.", ""]
    lbc = multi.get("lost_by_cause")
    if lbc is not None and not lbc.empty:
        out += ["### Совместители, переставшие получать: почему", "",
                _table(lbc, ["title", "n_triples", "n_epk", "extra"],
                       ["Ситуация", "Получателей", "ФЛ", "Лишних получателей"])]
    st = multi.get("structure")
    if st is not None and not st.empty:
        out += ["### Как устроено совместительство", "",
                _table(st, ["title", "base", "cur", "delta"],
                       ["Показатель", f"{t['base_month']:%m.%Y}",
                        f"{t['report_month']:%m.%Y}", "Изменение"], limit=20),
                "«Одна организация через несколько ГОСБ» — ФЛ получает от одной "
                "организации через два и более ГОСБ и считается несколькими "
                "получателями; минус здесь — сведение выплат в один ГОСБ, а не уход.",
                ""]
    mr = multi.get("rows")
    if mr is not None and not mr.empty:
        out += [f"### {A.inside_title(multi['inside'])}: почему", "",
                _table(mr, ["title", "lost", "gained", "net"],
                       ["Что случилось с местом работы", "Было и не стало",
                        "Не было и стало", "Итог"], limit=10),
                f"Не уход человека (ГОСБ, закрытие и слияние организаций): "
                f"**{sgn(mr[~mr['is_exit']]['net'].sum())}**; уход со второй работы "
                f"или её зарплаты: **{sgn(mr[mr['is_exit']]['net'].sum())}**; итого "
                f"**{sgn(mr['net'].sum())}**.", "",
                *[f"- **{v[0]}** — {v[1]}" for v in A.MULTI_SITUATIONS.values()], ""]
    if m_orgs.get("org_stopped") is not None:
        out += ["### Организации, которые больше не платят зарплату в РГС никому", "",
                _table(m_orgs["org_stopped"],
                       ["company_name", "n_triples", "name_to", "n_epk_to"],
                       ["Организация", "Мест работы исчезло",
                        "Куда перешло больше всего этих ФЛ", "ФЛ"], limit=15)]
    if m_orgs.get("no_pay") is not None:
        out += ["### Организации, откуда совместители ушли с места работы", "",
                _table(m_orgs["no_pay"], ["company_name", "n_triples"],
                       ["Организация", "Мест работы исчезло"], limit=15)]
    return "\n".join(out)


def _decomp_md(t: dict) -> str:
    """Единственное разложение изменения — те же строки, что в шапке HTML."""
    lines = ["| | Получателей | ФЛ |", "|---|---|---|"]
    for r in A.decomposition(t):
        signed = r["key"] in ("lost", "gained", "inside", "delta")
        fmt = (lambda v: ("+" if v > 0 else "") + _n(v)) if signed else _n
        title = r["title"]
        cells = [title, fmt(r["triples"]), fmt(r["epk"])]
        if r["key"] in ("base", "cur", "delta"):
            cells = [f"**{c}**" for c in cells]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _cause_line(key: str, v: tuple) -> str:
    """Строка расшифровки ветки: название, вид словом, описание."""
    return f"- **{v[0]}** — {v[1]}"


def _seasonal_md(seas: dict) -> str:
    """Проверка сезонности таблицей. Пусто — честная строка, а не пустая таблица."""
    if not seas or not seas.get("n_prev_both"):
        return "_проверка сезонности не считалась_\n"
    pm, rm = seas["prev_month"], seas["report_month"]
    py, ry = pm - pd.DateOffset(months=12), rm - pd.DateOffset(months=12)
    return (
        f"| Показатель | ФЛ |\n|---|---|\n"
        f"| Получали зарплату в {pm:%m.%Y} и в {py:%m.%Y} | {_n(seas['n_prev_both'])} |\n"
        f"| Из них пропали в {rm:%m.%Y} | {_n(seas['n_gone_cur'])} |\n"
        f"| Из них пропадали и в {ry:%m.%Y} — это сезонность | "
        f"{_n(seas['n_seasonal'])} |\n\n"
        f"Сезонными оказались {_pct(seas['share_of_gone'], 0)} от всех пропавших.\n")


def _sql_appendix(shown: dict) -> str:
    """Приложение с запросами: как проверить каждую цифру документа.

    Персональных данных в SQL нет — только имена таблиц, колонок и значения
    параметров, — поэтому обезличиванию приложение не мешает. А вот разбирать
    цифры по документу без него нельзя: получатель видит числа и не может
    проверить ни одного.

    Показывается СЕБЯ-ДОСТАТОЧНАЯ форма, с подставленными выборками: текст
    `FROM t_pairs` выполнить негде, временная таблица жила в чужой сессии.
    """
    if not shown:
        return "## Приложение: запросы\n\n_запросы не сохранились_\n"
    out = ["## Приложение: запросы, которыми посчитаны цифры", "",
           "Каждый запрос самодостаточен — его можно скопировать и выполнить как "
           "есть. Значения параметров приведены комментарием в шапке.", ""]
    for name in sorted(shown):
        text, params = shown[name]
        out.append(f"### `{name}`")
        out.append("")
        out.append("```sql")
        out.append((_params_comment(params) + text.strip()).rstrip())
        out.append("```")
        out.append("")
    return "\n".join(out)


def _params_comment(params: dict) -> str:
    """Значения параметров шапкой запроса — без них цифра не воспроизводится."""
    if not params:
        return ""
    lines = ["-- Параметры:"]
    for key in sorted(params):
        val = params[key]
        if isinstance(val, (list, tuple)):
            val = ", ".join(str(v) for v in val)
        lines.append(f"--   :{key} = {val}")
    return "\n".join(lines) + "\n"


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
