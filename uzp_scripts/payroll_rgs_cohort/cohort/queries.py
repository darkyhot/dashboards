"""SQL разбора. Весь SQL — здесь, ни одной строки SQL в расчётах.

Плейсхолдеры `{schema}` / `{schema_t}` подставляет `uzp_dash.db.read_sql`,
значения передаются именованными параметрами `:name`.

ЧЕТЫРЕ ЛОВУШКИ, на которых этот разбор ломается тихо
----------------------------------------------------

1. **Партиция.** `uzp_data_payroll_m` партиционирована по `report_dt`. Запрос без
   `report_dt` в `WHERE` читает всю историю: на проме это не «медленно», это
   «никогда». Каждое обращение к витрине ниже имеет границу по дате, и это
   проверяется тестом `check_partition_filter` — он ищет обращения без даты во
   ВСЕХ запросах файла.

2. **Тип ИНН.** В ведомостях `inn` — `text` длиной 1–12, в справочнике ЕПК —
   `bigint`. `CAST(inn AS bigint)` на значении вида «ИНН123» роняет ВЕСЬ запрос,
   а не одну строку. Поэтому приведение идёт только под маской `~ '^[0-9]{{1,12}}$'`
   (скобки удвоены, см. ловушку 4), а доля непрошедших строк считается отдельно:
   если она выросла год к году, часть «падения» — артефакт джойна.

3. **Диалект.** Greenplum на ядре PostgreSQL 9.4: `make_interval(months => n)` не
   работает («=>» разбирается как оператор), сдвиг на месяцы пишется умножением
   интервала — `n * interval '1 month'`.

4. **Фигурные скобки.** `read_sql` прогоняет текст через `str.format()`, поэтому
   квантификатор регулярки `{{1,12}}` обязан быть записан удвоенными скобками.
   Иначе запрос падает с `IndexError: Replacement index ... out of range` —
   сообщение, по которому про SQL не догадаешься.

Соответствие ГОСБ берётся ИЗ ДЭША: вторая копия разъедется молча, и разбор начнёт
мерить не тот банк.
"""
from __future__ import annotations

from uzp_dash.dashboards.tb_health import queries as Q
from uzp_dash.dashboards.tb_health import segments as S

_GMAP = Q._GMAP     # noqa: SLF001 — единственный источник соответствия ГОСБ в проекте

# Сегмент бюджетной сферы. В справочнике ЕПК он записан БОЛЬШИМ именем — тем же,
# что в uzp_dim_company. Короткий код «РГС» тут не подойдёт: фильтр коротким именем
# вернул бы ноль строк, и это выглядело бы как «в бюджетной сфере никого нет».
SEG_CODE = 22
SEG_BIG = "Рег. госсектор"
SEG_SHORT = S.SHORT[SEG_CODE]
assert S.BIG_TO_CODE[SEG_BIG] == SEG_CODE, "словарь сегментов дэша разъехался с разбором"

# Коды зачисления, которые считаются зарплатными (список задан заказчиком).
#
# Уезжают в запрос ОДНИМ параметром-массивом, а не склейкой в текст: склейка
# списка из тетрадки в текст SQL — дыра, через которую в запрос попадает что
# угодно. Сравнение написано как `= ANY(:codes)`, а НЕ `IN :codes`: через
# `text()` кортеж подставился бы ОДНИМ значением, и условие сравнивало бы
# smallint с кортежем.
CODES = (1, 2, 16, 18, 19, 26, 28, 33, 38, 39, 40, 42, 49, 82, 87, 88, 94, 95)

# Порог получателя. Заказчик задал его как «зачисление за месяц В ИНН больше
# 2500»: сумма считается по организации, а получатели — тройками (человек, ИНН,
# ГОСБ). Порог и ключ счёта живут на РАЗНЫХ грейнах, и это не описка, а
# постановка; поэтому сумма по ИНН считается оконной функцией поверх группировки
# по тройке, а не в HAVING — HAVING умеет фильтровать только свою группу.
AMT_MIN = 2500

# Второй вариант — порог на саму тройку. Нужен не «на всякий случай»: если
# отчётность заказчика считает иначе, база разойдётся, и узнать об этом надо
# сравнением двух чисел, а не спором. Разведка печатает счёт при обоих.
AMT_SCOPE_INN = "inn"
AMT_SCOPE_TRIPLE = "inn_gosb"
AMT_COND = {
    AMT_SCOPE_INN:    "x.amt_inn > :amt_min",
    AMT_SCOPE_TRIPLE: "x.amt > :amt_min",
}

# Список опорных месяцев уезжает параметром-массивом, и сравнение с ним пишется
# как `report_dt = ANY(CAST(:months AS date[]))`. Без явного приведения драйвер
# передаёт массив ТЕКСТОВ, и Greenplum отказывается сравнивать date с text —
# «operator does not exist: date = text». Ошибка шумная, но вылезает уже на
# создании рабочего набора, то есть после самого дорогого скана.

# Маска ИНН. Вынесена в константу, потому что повторяется в каждом запросе, а
# разъехавшись, тихо изменит состав сегмента в одном разрезе и не изменит в другом.
INN_OK = "p.inn ~ '^[0-9]{{1,12}}$'"


# --------------------------------------------------------------------------- #
# Разведка
# --------------------------------------------------------------------------- #

# Имя колонки кода зачисления. В постановке она названа `enrollment_type_id`, в
# профиле пром-витрины — `enrollment_type`. Гадать нельзя: неверное имя роняет
# КАЖДЫЙ запрос разбора, и узнать об этом надо до первого из них.
PROBE_COLUMNS = """
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = :schema AND table_name = :table
"""

# Глубина ряда ведомостей. Отдельным запросом по одной колонке: min/max по
# партиционированной таблице читаются из метаданных партиций, а не сканом.
PROBE_MONTHS = """
SELECT report_dt, count(*) AS n_rows
FROM {schema}.uzp_data_payroll_m
WHERE report_dt >= CAST(:d_from AS date)
  AND report_dt <= CAST(:d_to AS date)
GROUP BY report_dt
ORDER BY report_dt
"""

# Что вообще лежит в справочнике ЕПК. `holding_name` на проме, по профилю, пуст —
# если это так, разрез по холдингам строить не из чего, и отчёт обязан сказать это
# словами, а не показать пустую таблицу.
PROBE_EPK = """
SELECT count(*)                                                  AS n_rows,
       count(DISTINCT inn)                                       AS n_inn,
       count(DISTINCT epk_id)                                    AS n_epk,
       count(*) FILTER (WHERE segment_name = :seg)               AS n_seg_rows,
       count(DISTINCT inn) FILTER (WHERE segment_name = :seg)    AS n_seg_inn,
       count(*) FILTER (WHERE COALESCE(holding_name, '') <> '')  AS n_holding,
       count(*) FILTER (WHERE COALESCE(industry_name, '') <> '') AS n_industry,
       count(*) FILTER (WHERE COALESCE(company_name, '') <> '')  AS n_name,
       count(*) FILTER (WHERE status_name = 'Активна')           AS n_active,
       count(*) FILTER (WHERE status_name = 'Ликвидирована')     AS n_liquidated
FROM {schema}.uzp_data_epk_consolidation
WHERE inn IS NOT NULL
"""

# Пригодность ИНН к джойну — по всей витрине, а не только по бюджетной сфере:
# сегмент известен лишь ПОСЛЕ джойна, а вопрос ровно в том, сколько строк до
# джойна не доживает.
#
# Берутся ТОЛЬКО два опорных месяца, а не вся история. Регулярка применяется к
# каждой строке витрины, и на проме это самый дорогой способ узнать то, что
# нужно ровно в двух точках: сравнение год к году. Счёт по всем 25 месяцам стоил
# бы двадцати пяти сканов ради одной строки предупреждения.
#
# `count(DISTINCT inn)` здесь не считается намеренно: в предупреждении участвует
# доля СТРОК, а уникальные номера по всему банку стоили бы отдельной сортировки
# миллиардов значений и не использовались бы ни в одном разделе.
PROBE_INN_MASK = """
SELECT p.report_dt,
       count(*)                                   AS n_rows,
       count(*) FILTER (WHERE """ + INN_OK + """) AS n_castable
FROM {schema}.uzp_data_payroll_m p
WHERE p.report_dt = ANY(CAST(:months AS date[]))
GROUP BY p.report_dt
ORDER BY p.report_dt
"""


# --------------------------------------------------------------------------- #
# Общие куски
# --------------------------------------------------------------------------- #

# Бюджетные ИНН по ТЕКУЩЕМУ срезу справочника ЕПК.
#
# Отчётной даты в этой витрине нет вовсе, поэтому сегмент известен только «на
# сегодня» — и применяется к обоим сравниваемым годам ОДИНАКОВО. Это осознанный
# выбор постановки: перекоса год к году он не создаёт.
#
# Свёртка до ИНН обязательна. У одного ИНН бывает несколько ЕПК, и джойн строкой
# витрины ЕПК задвоил бы ведомости — не справочник, а именно ведомости, потому что
# дублируется правая сторона. Численность выросла бы вдвое и выглядела бы
# правдоподобно.
#
# `is_liquidated` — «нет НИ ОДНОЙ активной записи». Смотреть на одну строку нельзя:
# при живой и мёртвой записи у одного ИНН организация жива.
_SEG_INN = """
  SELECT e.inn,
         min(e.company_name)                                   AS company_name,
         min(e.holding_name)                                   AS holding_name,
         min(e.industry_name)                                  AS industry_name,
         bool_or(COALESCE(e.is_educational, false))            AS is_educational,
         bool_or(COALESCE(e.is_military, false))               AS is_military,
         NOT bool_or(e.status_name = 'Активна')                AS is_liquidated
  FROM {schema}.uzp_data_epk_consolidation e
  WHERE e.inn IS NOT NULL AND e.segment_name = :seg
  GROUP BY e.inn
"""

# Сколько получателей даёт КАЖДЫЙ из двух вариантов порога.
#
# Считается до всего остального и печатается: если база разойдётся с отчётностью
# заказчика, вопрос «а как у вас считается порог» надо задать по двум готовым
# числам, а не после того, как весь разбор построен на неверном.
PROBE_AMT_SCOPE = """
WITH seg AS (""" + _SEG_INN + """),
pairs AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn,
         p.sys_gosb_id AS gosb_id,
         sum(p.amt) AS amt,
         sum(sum(p.amt)) OVER (PARTITION BY p.report_dt, p.epk_id,
                                            CAST(p.inn AS bigint)) AS amt_inn
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt = ANY(CAST(:months AS date[]))
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.sys_gosb_id
)
SELECT x.report_dt,
       count(*) FILTER (WHERE x.amt_inn > :amt_min) AS n_scope_inn,
       count(*) FILTER (WHERE x.amt     > :amt_min) AS n_scope_triple,
       count(DISTINCT x.epk_id) FILTER (WHERE x.amt_inn > :amt_min) AS n_epk_inn
FROM pairs x JOIN seg s ON s.inn = x.inn
GROUP BY x.report_dt
ORDER BY x.report_dt
"""


# --------------------------------------------------------------------------- #
# Рабочий набор
# --------------------------------------------------------------------------- #
#
# Выборки, на которых стоит весь разбор. Каждая описана РОВНО ОДИН РАЗ, а как она
# материализуется — временной таблицей или CTE — решает `fetch`, по итогу
# разведки. Две копии одного определения (одна для быстрого пути, другая для
# запасного) разъехались бы молча, и два пути начали бы отвечать по-разному на
# один и тот же вопрос — при том, что запасной включается сам и без объявления.
#
# Порядок в списке ЗНАЧИМ: в режиме CTE каждая следующая выборка ссылается на
# предыдущие, а CTE видит только те, что объявлены до неё.
#
# ТРИ ОПОРНЫХ МЕСЯЦА, а не два. Разбор считается дважды: отчётный месяц к тому же
# месяцу год назад и он же к предыдущему месяцу. Второе сравнение обязательно
# потому, что отчётный месяц может быть сезонной ямой: без него годовое падение
# невозможно отличить от обычного месячного провала. Поэтому базозависимые
# свёртки хранят `report_dt` и фильтруются в запросе, а не пересобираются под
# каждую базу.

# Бюджетные ИНН по ТЕКУЩЕМУ срезу справочника ЕПК (см. комментарий к _SEG_INN).
_T_SEG = _SEG_INN

# Сегмент ЛЮБОЙ организации, не только бюджетной.
#
# Нужен ровно для одного вопроса — «а куда человек ушёл». Без него уход из
# сегмента остаётся строкой «ушёл» без адреса: неизвестно, забрал его
# коммерческий клиент, малый бизнес или он просто сменил бюджетную работу на
# небюджетную. Справочник маленький, отдельная выборка ничего не стоит.
_T_SEG_ALL = """
  SELECT e.inn, min(e.segment_name) AS segment_name,
         min(e.company_name) AS company_name,
         min(e.industry_name) AS industry_name
  FROM {schema}.uzp_data_epk_consolidation e
  WHERE e.inn IS NOT NULL
  GROUP BY e.inn
"""

# Получатели на грейне (человек, ИНН, ГОСБ) за все опорные месяцы сразу.
#
# ГРЕЙН — ТРОЙКА. Человек, получающий в одном ИНН через два подразделения, весит
# двух получателей: так считает отчётность, с которой сверяется результат.
#
# ПОРОГ применяется к сумме за месяц В ИНН, а не в тройке: так он задан
# постановкой. Порог и ключ счёта живут на разных грейнах, поэтому сумма по ИНН
# считается оконной функцией поверх группировки, а не в HAVING — HAVING умеет
# фильтровать только свою группу.
#
# ЧЕТЫРЕ опорных месяца, а не два: отчётный, предыдущий, тот же месяц год назад и
# предыдущий год назад. Последний нужен для проверки сезонности — она требует
# СРАВНИТЬ поведение человека в двух парах месяцев, и по трём месяцам этого не
# сделать.
#
# ПОДРАЗДЕЛЕНИЕ И ТБ БЕРУТСЯ СИСТЕМНЫМИ НОМЕРАМИ — `sys_gosb_id` и `sys_tb_id`, а
# не `gosb_id` / `tb_id`. На проме «старые» колонки со справочником `uzp_dim_gosb`
# не сходятся, и разрезы по ТБ и региону схлопывались в одну строку-заглушку: ни
# ошибки, ни пустой таблицы, просто «ТБ неизвестен» со стопроцентной долей.
# Системный номер — третий элемент ТРОЙКИ, поэтому смена ключа меняет и саму
# численность получателей; старый номер едет рядом колонкой `gosb_id_legacy` —
# он ни во что не считается, но разведка по нему показывает, какой из двух ключей
# опознаётся справочником, и «не строится» перестаёт быть загадкой.
_T_PAIRS = """
  SELECT x.report_dt, x.epk_id, x.inn, x.gosb_id, x.gosb_id_legacy,
         x.tb_id, x.agrmnt_num, x.amt
  FROM (
    SELECT p.report_dt,
           p.epk_id,
           CAST(p.inn AS bigint) AS inn,
           p.sys_gosb_id         AS gosb_id,
           min(p.sys_tb_id)      AS tb_id,
           min(p.gosb_id)        AS gosb_id_legacy,
           min(p.agrmnt_num)     AS agrmnt_num,
           sum(p.amt)            AS amt,
           sum(sum(p.amt)) OVER (PARTITION BY p.report_dt, p.epk_id,
                                              CAST(p.inn AS bigint)) AS amt_inn
    FROM {schema}.uzp_data_payroll_m p
    WHERE p.report_dt = ANY(CAST(:months AS date[]))
      AND p.{code_col} = ANY(:codes)
      AND p.epk_id IS NOT NULL
      AND """ + INN_OK + """
    GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.sys_gosb_id
  ) x
  JOIN t_seg s ON s.inn = x.inn
  WHERE {amt_cond}
"""

# Присутствие в ведомостях БЕЗ фильтров по коду и порогу — по всему банку, но
# ТОЛЬКО по людям, которые хоть в одном опорном месяце были в сегменте.
#
# Ограничение по людям — не экономия, а единственный способ уложиться: витрина за
# месяц это клиенты всего банка, а разбору нужны только те, про кого он
# спрашивает. Спрашивает он ровно про людей из `t_pairs`.
#
# Без этой выборки нельзя отличить «ушёл из банка совсем» от «получает, но не по
# зарплатным кодам» — а это два разных диагноза с разными решениями.
_T_SEEN = """
  SELECT p.report_dt,
         p.epk_id,
         CAST(p.inn AS bigint) AS inn,
         p.sys_gosb_id         AS gosb_id,
         sum(p.amt)            AS amt_all,
         sum(CASE WHEN p.{code_col} = ANY(:codes) THEN p.amt ELSE 0 END) AS amt_codes
  FROM {schema}.uzp_data_payroll_m p
  JOIN (SELECT DISTINCT epk_id FROM t_pairs) k ON k.epk_id = p.epk_id
  WHERE p.report_dt = ANY(CAST(:seen_months AS date[]))
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.sys_gosb_id
"""

# Человек в ведомостях банка вообще — по месяцам.
_T_SEEN_EPK = """
  SELECT report_dt, epk_id, sum(amt_all) AS amt_all
  FROM t_seen GROUP BY report_dt, epk_id
"""

# Он же, но ТОЛЬКО по бюджетным организациям.
#
# По этим двум суммам различаются три ситуации, которые интересуют зарплатное
# подразделение: деньги от бюджетной организации идут по зарплатным кодам, но
# мало (порог); идут, но не по зарплатным кодам; не идут вовсе (ушёл в другой
# сегмент). Одной суммой их не разделить.
_T_SEEN_SEG = """
  SELECT t.report_dt, t.epk_id,
         sum(t.amt_all)   AS amt_all,
         sum(t.amt_codes) AS amt_codes
  FROM t_seen t JOIN t_seg s ON s.inn = t.inn
  GROUP BY t.report_dt, t.epk_id
"""

# Сколько бюджетных организаций у человека в каждом месяце. Разница между
# месяцами и есть совместительство.
_T_EPK_MONTH = """
  SELECT report_dt, epk_id,
         count(DISTINCT inn) AS n_inn,
         count(*)            AS n_triples
  FROM t_pairs GROUP BY report_dt, epk_id
"""

WORKSET: list[tuple[str, str, str]] = [
    ("t_seg",       _T_SEG,       "inn"),
    ("t_seg_all",   _T_SEG_ALL,   "inn"),
    ("t_pairs",     _T_PAIRS,     "epk_id"),
    ("t_seen",      _T_SEEN,      "epk_id"),
    ("t_seen_epk",  _T_SEEN_EPK,  "epk_id"),
    ("t_seen_seg",  _T_SEEN_SEG,  "epk_id"),
    ("t_epk_month", _T_EPK_MONTH, "epk_id"),
]

CREATE_TMP = "CREATE TEMP TABLE {name} AS\n{body}\nDISTRIBUTED BY ({dist})"
# DISTRIBUTED BY — расширение Greenplum; на обычном PostgreSQL (открытый контур)
# оператор с ним не разбирается вовсе.
CREATE_TMP_PLAIN = "CREATE TEMP TABLE {name} AS\n{body}"
ANALYZE_TMP = "ANALYZE {name}"
DROP_TMP = "DROP TABLE IF EXISTS {name}"


# --------------------------------------------------------------------------- #
# Лестница: получатели
# --------------------------------------------------------------------------- #
#
# Каждый потерянный получатель получает РОВНО ОДНУ причину — первую сработавшую
# сверху вниз. Аддитивность не пожелание, а условие осмысленности: если получатель
# попадёт в две ветки, части падения перестанут складываться в падение.
#
# ГЛАВНЫЙ ВОПРОС К КАЖДОЙ ВЕТКЕ ОДИН: остался ли человек получателем зарплаты в
# сегменте — по ВАШЕМУ определению, то есть по зарплатным кодам и выше порога?
#
#   Остался  -> потери нет. Он мог перейти в другую бюджетную организацию, в
#               другое подразделение или сократить число мест работы: для
#               сегмента это движение внутри, а не убыль.
#   Не остался -> ПОТЕРЯ. Неважно, ушёл он из банка, ушёл в другой сегмент,
#               перестал зачислять по зарплатным кодам или не дотянул до порога:
#               получателя не стало, и ветка говорит лишь КУДА он делся.
#
# Раньше здесь была третья категория — «особенности счёта», куда попадали порог и
# коды вне списка. Это было неверно: порог и список кодов — часть ОПРЕДЕЛЕНИЯ
# получателя, а не погрешность измерения. Человек, переставший зачислять по
# зарплатному коду, для зарплатного подразделения пропал, и прятать его в
# «методологию» значит занижать потерю.
_LOST_CASE = """
    CASE
      WHEN ce.epk_id IS NOT NULL THEN 'stayed_in_segment'
      WHEN se.epk_id IS NULL     THEN 'left_bank'
      WHEN sg.amt_codes > 0      THEN 'below_threshold'
      WHEN sg.amt_all > 0        THEN 'other_codes'
      ELSE 'left_segment'
    END
"""

# Общая часть запросов по потерянным получателям. Вынесена в кусок, чтобы итог и
# разрезы считались ПО ОДНОМУ И ТОМУ ЖЕ определению.
#
# ПЕРВАЯ ветка проверяется первой не случайно. Если человек остался получателем
# бюджетного сегмента — всё равно, что случилось с его прежней организацией: её
# могли ликвидировать, она могла исчезнуть из ведомостей, его могли перевести в
# другое подразделение или он сам сменил работодателя. Сегмент получателя не
# потерял, а больше ничего и не спрашивается. Раньше здесь стояли отдельные
# ветки про подразделение, организацию и ликвидацию — они отвечали на вопрос,
# которого никто не задавал, и заслоняли тот, который задавали.
_LOST_BASE = """
  SELECT b.epk_id, b.inn, b.gosb_id, b.tb_id, b.amt,
         """ + _LOST_CASE + """ AS cause
  FROM t_pairs b
  LEFT JOIN t_pairs c ON c.report_dt = CAST(:d_cur AS date)
                     AND c.epk_id = b.epk_id AND c.inn = b.inn
                     AND c.gosb_id = b.gosb_id
  LEFT JOIN t_epk_month ce ON ce.epk_id = b.epk_id
                          AND ce.report_dt = CAST(:d_cur AS date)
  LEFT JOIN t_seen_epk se ON se.epk_id = b.epk_id
                         AND se.report_dt = CAST(:d_cur AS date)
  LEFT JOIN t_seen_seg sg ON sg.epk_id = b.epk_id
                         AND sg.report_dt = CAST(:d_cur AS date)
  WHERE b.report_dt = CAST(:d_base AS date) AND c.epk_id IS NULL
"""

# Зеркальная лестница по ПРИШЕДШИМ получателям.
#
# Зеркальная не ради симметрии: без неё нельзя ответить на главный вопрос —
# ВЫРОСЛИ ЛИ МЫ. Вычитать из полного прихода только настоящие потери значит
# завысить рост ровно на ту величину, которую мы вычитаем со стороны потерь.
_GAINED_CASE = """
    CASE
      WHEN bp.epk_id IS NOT NULL THEN 'stayed_in_segment'
      WHEN bse.epk_id IS NULL    THEN 'new_to_bank'
      WHEN bsg.amt_codes > 0     THEN 'above_threshold'
      WHEN bsg.amt_all > 0       THEN 'back_to_codes'
      ELSE 'from_segment'
    END
"""

_GAINED_BASE = """
  SELECT c.epk_id, c.inn, c.gosb_id, c.tb_id, c.amt,
         """ + _GAINED_CASE + """ AS cause
  FROM t_pairs c
  LEFT JOIN t_pairs b ON b.report_dt = CAST(:d_base AS date)
                     AND b.epk_id = c.epk_id AND b.inn = c.inn
                     AND b.gosb_id = c.gosb_id
  LEFT JOIN t_epk_month bp ON bp.epk_id = c.epk_id
                          AND bp.report_dt = CAST(:d_base AS date)
  LEFT JOIN t_seen_epk bse ON bse.epk_id = c.epk_id
                          AND bse.report_dt = CAST(:d_base AS date)
  LEFT JOIN t_seen_seg bsg ON bsg.epk_id = c.epk_id
                          AND bsg.report_dt = CAST(:d_base AS date)
  WHERE c.report_dt = CAST(:d_cur AS date) AND b.epk_id IS NULL
"""

LOST_TOTALS = """
WITH lost AS (""" + _LOST_BASE + """)
SELECT cause, count(*) AS n_triples, count(DISTINCT epk_id) AS n_epk,
       sum(amt) AS amt
FROM lost GROUP BY cause ORDER BY count(*) DESC
"""

GAINED_TOTALS = """
WITH gained AS (""" + _GAINED_BASE + """)
SELECT cause, count(*) AS n_triples, count(DISTINCT epk_id) AS n_epk,
       sum(amt) AS amt
FROM gained GROUP BY cause ORDER BY count(*) DESC
"""

LOST_BY_INN = """
WITH lost AS (""" + _LOST_BASE + """)
SELECT l.inn, l.cause, min(l.gosb_id) AS gosb_id, min(l.tb_id) AS tb_id,
       count(*) AS n_triples
FROM lost l GROUP BY l.inn, l.cause
"""

# Приход в разрезе организации. Без него таблица крупнейших потерь врёт:
# организация, потерявшая двести тысяч получателей и набравшая столько же, в ней
# выглядит катастрофой, не потеряв ничего.
GAINED_BY_INN = """
WITH gained AS (""" + _GAINED_BASE + """)
SELECT g.inn, count(*) AS n_triples
FROM gained g GROUP BY g.inn
"""


# --------------------------------------------------------------------------- #
# Лестница: ЛЮДИ
# --------------------------------------------------------------------------- #
#
# Второй разбор, на грейне человека, со своим тождеством:
#     людей_база − потеряно_людей + пришло_людей = людей_отчёт
#
# Веток здесь ровно четыре, и все четыре — потеря. Движения внутри сегмента на
# уровне человека НЕ СУЩЕСТВУЕТ: перевод, смена организации и сокращение числа
# мест работы получателя убавляют, а человека нет. Разница между двумя
# лестницами и есть цена того, что метрика считает не людей.
_LOST_EPK_CASE = """
    CASE
      WHEN se.epk_id IS NULL   THEN 'left_bank'
      WHEN sg.amt_codes > 0    THEN 'below_threshold'
      WHEN sg.amt_all > 0      THEN 'other_codes'
      ELSE 'left_segment'
    END
"""

_LOST_EPK_BASE = """
  SELECT b.epk_id, """ + _LOST_EPK_CASE + """ AS cause
  FROM t_epk_month b
  LEFT JOIN t_epk_month c ON c.epk_id = b.epk_id
                         AND c.report_dt = CAST(:d_cur AS date)
  LEFT JOIN t_seen_epk se ON se.epk_id = b.epk_id
                         AND se.report_dt = CAST(:d_cur AS date)
  LEFT JOIN t_seen_seg sg ON sg.epk_id = b.epk_id
                         AND sg.report_dt = CAST(:d_cur AS date)
  WHERE b.report_dt = CAST(:d_base AS date) AND c.epk_id IS NULL
"""

_GAINED_EPK_CASE = """
    CASE
      WHEN bse.epk_id IS NULL  THEN 'new_to_bank'
      WHEN bsg.amt_codes > 0   THEN 'above_threshold'
      WHEN bsg.amt_all > 0     THEN 'back_to_codes'
      ELSE 'from_segment'
    END
"""

_GAINED_EPK_BASE = """
  SELECT c.epk_id, """ + _GAINED_EPK_CASE + """ AS cause
  FROM t_epk_month c
  LEFT JOIN t_epk_month b  ON b.epk_id = c.epk_id
                          AND b.report_dt = CAST(:d_base AS date)
  LEFT JOIN t_seen_epk bse ON bse.epk_id = c.epk_id
                          AND bse.report_dt = CAST(:d_base AS date)
  LEFT JOIN t_seen_seg bsg ON bsg.epk_id = c.epk_id
                          AND bsg.report_dt = CAST(:d_base AS date)
  WHERE c.report_dt = CAST(:d_cur AS date) AND b.epk_id IS NULL
"""

LOST_EPK_TOTALS = """
WITH lost AS (""" + _LOST_EPK_BASE + """)
SELECT cause, count(*) AS n_epk FROM lost GROUP BY cause ORDER BY count(*) DESC
"""

GAINED_EPK_TOTALS = """
WITH gained AS (""" + _GAINED_EPK_BASE + """)
SELECT cause, count(*) AS n_epk FROM gained GROUP BY cause ORDER BY count(*) DESC
"""


# --------------------------------------------------------------------------- #
# Куда именно делись люди
# --------------------------------------------------------------------------- #

# В КАКОЙ СЕГМЕНТ ушли те, кто ушёл из бюджетного.
#
# Строка «ушёл из сегмента» без адреса — это половина ответа. Забрал ли человека
# коммерческий клиент, малый бизнес или он просто сменил бюджетную работу на
# небюджетную — разные истории с разными выводами для зарплатного подразделения.
LEFT_SEGMENT = """
WITH lost AS (""" + _LOST_EPK_BASE + """)
SELECT COALESCE(sa.segment_name, 'Организация не в справочнике') AS segment_name,
       count(DISTINCT l.epk_id) AS n_epk
FROM lost l
JOIN t_seen t ON t.epk_id = l.epk_id AND t.report_dt = CAST(:d_cur AS date)
LEFT JOIN t_seg_all sa ON sa.inn = t.inn
WHERE l.cause = 'left_segment'
GROUP BY 1
ORDER BY 2 DESC
"""

# ЧЕМ заменились зарплатные зачисления у тех, кто перестал их получать.
#
# Это НЕ разбор кодов витрины — тот был выброшен как вредный. Это детализация
# ОДНОЙ ситуации из трёх: «зачисления есть, но не по зарплатным кодам». Сама
# ситуация — уже потеря, и таблица её не оправдывает, а объясняет: пенсия
# означает выход на пенсию, пособие на детей — декрет, расчёт при увольнении —
# увольнение. Из строки «получает по другим кодам» ни один из этих выводов не
# сделать, а решения по ним разные вплоть до противоположных.
LEFT_CODES = """
WITH lost AS (""" + _LOST_EPK_BASE + """)
SELECT p.{code_col}                        AS code,
       min(p.enrollment_transcription)     AS code_name,
       count(DISTINCT p.epk_id)            AS n_epk,
       sum(p.amt)                          AS amt
FROM {schema}.uzp_data_payroll_m p
JOIN (SELECT epk_id FROM lost WHERE cause = 'other_codes') l ON l.epk_id = p.epk_id
JOIN t_seg s ON s.inn = CAST(p.inn AS bigint)
WHERE p.report_dt = CAST(:d_cur AS date)
  AND NOT (p.{code_col} = ANY(:codes))
  AND """ + INN_OK + """
GROUP BY p.{code_col}
ORDER BY count(DISTINCT p.epk_id) DESC
"""

# КТО ИМЕННО перешёл на каждый вид зачисления — топ-N организаций по коду.
#
# Таблица выше отвечает «что это за выплата» и молчит про «у кого». Строка
# «пособие на детей — четыре тысячи человек» без организаций в действие не
# превращается: непонятно, идти в одну школу или это ровный фон по сегменту.
#
# Популяция — ТА ЖЕ, что в LEFT_CODES, до последнего условия: разойдись они, и
# доли внутри кода перестали бы складываться в его итог.
#
# Верхушка режется оконной функцией, а не отдельным запросом на каждый код:
# row_number() считается ПОСЛЕ группировки, на ядре 9.4 это работает, а витрина
# читается один раз. Второй ключ сортировки — номер организации: без него порядок
# при равном числе людей меняется от прогона к прогону, и диффы отчётов врут.
LEFT_CODES_INN = """
WITH lost AS (""" + _LOST_EPK_BASE + """)
SELECT x.code, x.code_name, x.inn, x.n_epk, x.amt
FROM (
  SELECT p.{code_col}                    AS code,
         min(p.enrollment_transcription) AS code_name,
         CAST(p.inn AS bigint)           AS inn,
         count(DISTINCT p.epk_id)        AS n_epk,
         sum(p.amt)                      AS amt,
         row_number() OVER (PARTITION BY p.{code_col}
                            ORDER BY count(DISTINCT p.epk_id) DESC,
                                     CAST(p.inn AS bigint)) AS rn
  FROM {schema}.uzp_data_payroll_m p
  JOIN (SELECT epk_id FROM lost WHERE cause = 'other_codes') l ON l.epk_id = p.epk_id
  JOIN t_seg s ON s.inn = CAST(p.inn AS bigint)
  WHERE p.report_dt = CAST(:d_cur AS date)
    AND NOT (p.{code_col} = ANY(:codes))
    AND """ + INN_OK + """
  GROUP BY p.{code_col}, CAST(p.inn AS bigint)
) x
WHERE x.rn <= :per_code
ORDER BY x.n_epk DESC
"""

# ЗАРПЛАТНЫЕ КОДЫ по месяцам — только те, что входят в метрику.
#
# Отвечает на вопрос, который не виден ни в одной другой таблице: КАКОЙ ИМЕННО ВИД
# ВЫПЛАТЫ просел. Метрика складывается из восемнадцати кодов, и провал одного из
# них — стипендии в каникулы, премии в конце квартала — выглядит в итоге как
# падение численности, хотя ни один человек никуда не ушёл: у него просто в этом
# месяце нет выплаты этого вида.
#
# Коды ВНЕ списка здесь не считаются вовсе. Они на метрику не влияют по
# построению, а в таблице занимали бы весь верх массовыми социальными выплатами и
# сбивали бы вывод — это уже случилось однажды.
#
# Группировка идёт по коду, но показывается НАЗВАНИЕ: код — внутренний
# идентификатор, читателю он ничего не говорит.
CODE_MONTHS = """
SELECT p.report_dt,
       p.{code_col}                      AS code,
       min(p.enrollment_transcription)   AS code_name,
       count(DISTINCT p.epk_id)          AS n_epk,
       sum(p.amt)                        AS amt
FROM {schema}.uzp_data_payroll_m p
JOIN t_seg s ON s.inn = CAST(p.inn AS bigint)
WHERE p.report_dt = ANY(CAST(:months AS date[]))
  AND p.{code_col} = ANY(:codes)
  AND """ + INN_OK + """
GROUP BY p.report_dt, p.{code_col}
ORDER BY p.report_dt, p.{code_col}
"""

# СЕЗОННОСТЬ, проверенная повтором год к году.
#
# Определение узкое нарочно. «Получал в июле, не получает в августе» — это ещё не
# сезон, это просто пропажа: доказать, что человек вернётся, нечем, следующего
# месяца в данных нет. Сезонным поведение становится тогда, когда оно ПОВТОРЯЕТСЯ:
# человек получал в июле ОБОИХ лет и не получал в августе ОБОИХ лет.
#
# Такие люди в сравнении август-к-августу не участвуют вовсе — их нет ни в базовом
# месяце, ни в отчётном, — но именно они объясняют, почему август ниже июля. Это
# не ветка потерь, а отдельный ответ на отдельный вопрос, и смешивать их нельзя.
SEASONAL = """
WITH bp AS (SELECT DISTINCT epk_id FROM t_pairs
            WHERE report_dt = CAST(:d_base_prev AS date)),
     bm AS (SELECT DISTINCT epk_id FROM t_pairs
            WHERE report_dt = CAST(:d_base AS date)),
     cp AS (SELECT DISTINCT epk_id FROM t_pairs
            WHERE report_dt = CAST(:d_prev AS date)),
     cm AS (SELECT DISTINCT epk_id FROM t_pairs
            WHERE report_dt = CAST(:d_cur AS date)),
     both_prev AS (
       SELECT bp.epk_id FROM bp JOIN cp ON cp.epk_id = bp.epk_id)
SELECT count(*)                                              AS n_prev_both,
       count(*) FILTER (WHERE cm.epk_id IS NULL)             AS n_gone_cur,
       count(*) FILTER (WHERE bm.epk_id IS NULL)             AS n_gone_base,
       count(*) FILTER (WHERE cm.epk_id IS NULL
                          AND bm.epk_id IS NULL)             AS n_seasonal
FROM both_prev x
LEFT JOIN bm ON bm.epk_id = x.epk_id
LEFT JOIN cm ON cm.epk_id = x.epk_id
"""


# --------------------------------------------------------------------------- #
# Итоги, разрезы, ряды
# --------------------------------------------------------------------------- #

MONTH_TOTALS = """
SELECT report_dt,
       count(*)                  AS n_triples,
       count(DISTINCT epk_id)    AS n_epk,
       count(DISTINCT inn)       AS n_inn,
       sum(amt)                  AS amt
FROM t_pairs GROUP BY report_dt ORDER BY report_dt
"""

SEG_ATTRS = """
SELECT * FROM t_seg
"""

# Помесячный ряд бюджетной сферы. Отвечает на «КОГДА»: обрыв в одном месяце — это
# событие, плавное снижение — текучесть. По двум точкам года они неразличимы.
MONTHLY = """
WITH pairs AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn,
         p.sys_gosb_id AS gosb_id,
         sum(p.amt) AS amt,
         sum(sum(p.amt)) OVER (PARTITION BY p.report_dt, p.epk_id,
                                            CAST(p.inn AS bigint)) AS amt_inn
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt >= CAST(:d_from AS date)
    AND p.report_dt <= CAST(:d_to AS date)
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.sys_gosb_id
)
SELECT x.report_dt,
       count(*)                  AS n_triples,
       count(DISTINCT x.epk_id)  AS n_epk,
       count(DISTINCT x.inn)     AS n_inn,
       sum(x.amt)                AS amt
FROM pairs x JOIN t_seg s ON s.inn = x.inn
WHERE {amt_cond}
GROUP BY x.report_dt
ORDER BY x.report_dt
"""

# Полнота загрузки ПО ВСЕМУ БАНКУ. Месяц с аномально низким числом строк —
# недогруженная партиция, и любой вывод по нему будет выводом про загрузку.
MONTHLY_ALL = """
SELECT p.report_dt,
       count(*)   AS n_rows,
       sum(p.amt) AS amt
FROM {schema}.uzp_data_payroll_m p
WHERE p.report_dt >= CAST(:d_from AS date)
  AND p.report_dt <= CAST(:d_to AS date)
GROUP BY p.report_dt
ORDER BY p.report_dt
"""

SURVIVAL = """
WITH cohort AS (
  SELECT epk_id, inn, gosb_id FROM t_pairs
  WHERE report_dt = CAST(:d_base AS date)
),
pairs AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn,
         p.sys_gosb_id AS gosb_id,
         sum(p.amt) AS amt,
         sum(sum(p.amt)) OVER (PARTITION BY p.report_dt, p.epk_id,
                                            CAST(p.inn AS bigint)) AS amt_inn
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt >= CAST(:d_base AS date)
    AND p.report_dt <= CAST(:d_cur AS date)
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.sys_gosb_id
)
SELECT x.report_dt, count(*) AS n_alive, count(DISTINCT x.epk_id) AS n_epk_alive
FROM pairs x JOIN cohort c ON c.epk_id = x.epk_id AND c.inn = x.inn
                          AND c.gosb_id = x.gosb_id
WHERE {amt_cond}
GROUP BY x.report_dt
ORDER BY x.report_dt
"""

# Чувствительность к порогу. Порог фиксирован, а зарплаты индексируются — сам по
# себе он должен год к году ДОБАВЛЯТЬ получателей. Если падение сохраняется при
# пороге 0, порог ни при чём.
THRESHOLD_SENS = """
WITH pairs AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn,
         p.sys_gosb_id AS gosb_id,
         sum(sum(p.amt)) OVER (PARTITION BY p.report_dt, p.epk_id,
                                            CAST(p.inn AS bigint)) AS amt_inn
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt = ANY(CAST(:months AS date[]))
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.sys_gosb_id
)
SELECT x.report_dt,
       count(*) FILTER (WHERE x.amt_inn > 0)     AS t0,
       count(*) FILTER (WHERE x.amt_inn > 1000)  AS t1000,
       count(*) FILTER (WHERE x.amt_inn > 2500)  AS t2500,
       count(*) FILTER (WHERE x.amt_inn > 5000)  AS t5000,
       count(*) FILTER (WHERE x.amt_inn > 10000) AS t10000
FROM pairs x JOIN t_seg s ON s.inn = x.inn
GROUP BY x.report_dt
ORDER BY x.report_dt
"""

# Миграция организаций: куда переехали люди, потерявшие свою организацию.
INN_MIGRATION = """
WITH lost AS (
  SELECT DISTINCT b.epk_id, b.inn AS inn_from
  FROM t_pairs b
  LEFT JOIN t_pairs c ON c.report_dt = CAST(:d_cur AS date)
                     AND c.epk_id = b.epk_id AND c.inn = b.inn
  WHERE b.report_dt = CAST(:d_base AS date) AND c.epk_id IS NULL
)
SELECT l.inn_from, t.inn AS inn_to, count(DISTINCT l.epk_id) AS n_epk
FROM lost l
JOIN t_seen t ON t.epk_id = l.epk_id AND t.report_dt = CAST(:d_cur AS date)
LEFT JOIN t_pairs b2 ON b2.report_dt = CAST(:d_base AS date)
                    AND b2.epk_id = l.epk_id AND b2.inn = t.inn
WHERE b2.epk_id IS NULL
GROUP BY l.inn_from, t.inn
HAVING count(DISTINCT l.epk_id) >= :min_movers
ORDER BY count(DISTINCT l.epk_id) DESC
"""

# Стаж ушедших: сколько месяцев человек был в сегменте до ухода.
TENURE = """
WITH lost AS (""" + _LOST_EPK_BASE + """),
mon AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn, sum(p.amt) AS amt
  FROM {schema}.uzp_data_payroll_m p
  JOIN (SELECT DISTINCT epk_id FROM lost) l ON l.epk_id = p.epk_id
  WHERE p.report_dt >= CAST(:d_from AS date)
    AND p.report_dt <= CAST(:d_base AS date)
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint)
  HAVING sum(p.amt) > :amt_min
),
ten AS (
  SELECT m.epk_id, count(DISTINCT m.report_dt) AS n_months
  FROM mon m JOIN t_seg s ON s.inn = m.inn
  GROUP BY m.epk_id
)
SELECT l.cause,
       CASE WHEN t.n_months IS NULL   THEN '0'
            WHEN t.n_months = 1       THEN '1'
            WHEN t.n_months <= 3      THEN '2-3'
            WHEN t.n_months <= 6      THEN '4-6'
            WHEN t.n_months <= 12     THEN '7-12'
            WHEN t.n_months <= 24     THEN '13-24'
            ELSE '25+' END AS bucket,
       count(*) AS n_epk
FROM lost l LEFT JOIN ten t ON t.epk_id = l.epk_id
GROUP BY l.cause, 2
"""

# Территория: ТБ берётся ПРЯМО ИЗ ВЕДОМОСТЕЙ (системным номером `sys_tb_id`, см.
# _T_PAIRS), а не выводится через справочник ГОСБ. На проме подразделение
# ведомостей со справочником не сходится, и разрез, построенный через ГОСБ,
# схлопывался в одну строку-заглушку.
TB_DIM = """
SELECT d.tb_id, min(d.tb_short_name) AS tb_short_name
FROM {schema}.uzp_dim_gosb d
WHERE """ + Q._NO_CA + """
GROUP BY d.tb_id
"""

GOSB_DIM = """
SELECT d.old_gosb_id, d.new_gosb_id,
       min(d.tb_id)          AS tb_id,
       min(d.tb_short_name)  AS tb_short_name,
       min(d.new_gosb_name)  AS gosb_name,
       min(NULLIF(btrim(d.region_name), '')) AS region_name
FROM {schema}.uzp_dim_gosb d
WHERE """ + Q._NO_CA + """
GROUP BY d.old_gosb_id, d.new_gosb_id
"""

# Каким ключом справочника опознаётся ГОСБ ведомостей. Угадывать нельзя: ошибка
# не видна по результату — разрез из одной строки легко принять за свойство данных.
#
# Сравниваются ЧЕТЫРЕ сочетания: номер ведомостей (системный `sys_gosb_id`, на
# котором теперь стоит грейн, и старый `gosb_id`, который едет рядом только ради
# этой проверки) против двух ключей справочника. Двух чисел было мало: когда
# разрез не строился, из них нельзя было понять, дело в ключе справочника или в
# самой колонке ведомостей.
# Номера дедуплицируются В ВЫБОРКАХ, а не через count(DISTINCT ...): на ядре 9.4
# несколько DISTINCT-агрегатов по РАЗНЫМ колонкам в одном запросе не считаются.
GOSB_MATCH = """
WITH sys_used AS (
  SELECT DISTINCT gosb_id AS id FROM t_pairs WHERE gosb_id IS NOT NULL
),
old_used AS (
  SELECT DISTINCT gosb_id_legacy AS id FROM t_pairs WHERE gosb_id_legacy IS NOT NULL
),
dim AS (
  SELECT old_gosb_id, new_gosb_id FROM {schema}.uzp_dim_gosb
  WHERE """ + Q._NO_CA + """
),
sys_m AS (
  SELECT count(*)                                                  AS n_used,
         count(*) FILTER (WHERE u.id IN (SELECT old_gosb_id FROM dim)) AS n_old,
         count(*) FILTER (WHERE u.id IN (SELECT new_gosb_id FROM dim)) AS n_new
  FROM sys_used u
),
old_m AS (
  SELECT count(*)                                                  AS n_used,
         count(*) FILTER (WHERE u.id IN (SELECT old_gosb_id FROM dim)) AS n_old,
         count(*) FILTER (WHERE u.id IN (SELECT new_gosb_id FROM dim)) AS n_new
  FROM old_used u
)
SELECT s.n_used, s.n_old, s.n_new,
       o.n_used AS n_used_legacy, o.n_old AS n_old_legacy, o.n_new AS n_new_legacy
FROM sys_m s, old_m o
"""

# Опознаётся ли номер ТБ ведомостей справочником. Раньше этого вопроса не
# задавали вовсе: ТБ считался колонкой, которая «заполнена всегда», и когда на
# проме разрез схлопнулся в «ТБ неизвестен», в отчёте не было ни предупреждения,
# ни числа, по которому это можно было бы заметить.
TB_MATCH = """
WITH used AS (SELECT DISTINCT tb_id FROM t_pairs WHERE tb_id IS NOT NULL),
dim AS (
  SELECT DISTINCT tb_id FROM {schema}.uzp_dim_gosb
  WHERE """ + Q._NO_CA + """
),
m AS (
  SELECT count(*)                                                  AS n_used,
         count(*) FILTER (WHERE u.tb_id IN (SELECT tb_id FROM dim)) AS n_matched
  FROM used u
),
r AS (
  SELECT count(*)                                   AS n_rows,
         count(*) FILTER (WHERE tb_id IS NULL)      AS n_null_rows
  FROM t_pairs
)
SELECT m.n_used, m.n_matched, r.n_null_rows, r.n_rows
FROM m, r
"""


# --------------------------------------------------------------------------- #
# Почему ушли. Всё, кроме EXIT_PATTERN, считается поверх рабочего набора — без
# новых сканов витрины: на проме помесячный ряд и так упирается в таймаут.
# --------------------------------------------------------------------------- #

# КУДА перешли те, кто остался в сегменте. Для сегмента это не потеря, а для
# строки разреза — может быть: человек, ушедший из школы ТБ-1 в школу ТБ-2,
# сегмент не покинул, но ТБ-1 его потерял. Без этой выборки колонка «остались в
# сегменте» не отвечает, потеря это для строки или нет.
#
# Вес назначения — 1/n: у совместителя в отчётном месяце несколько троек, и без
# веса один потерянный получатель посчитался бы дважды. С весом сумма по строкам
# ровно равна числу оставшихся — разрез остаётся аддитивным.
_STAYED_HEAD = """
WITH lost AS (""" + _LOST_BASE + """),
st AS (
  SELECT epk_id, inn, gosb_id, tb_id FROM lost WHERE cause = 'stayed_in_segment'
),
dest AS (
  SELECT epk_id, inn, gosb_id, tb_id,
         count(*) OVER (PARTITION BY epk_id) AS n_dest
  FROM t_pairs WHERE report_dt = CAST(:d_cur AS date)
)"""

# Признаки «та же строка» считаются здесь же, в SQL, и в ядро уезжает строка на
# ОРГАНИЗАЦИЮ, а не на направление перехода: на проме направлений больше лимита.
# У организации без холдинга «тот же холдинг» — та же организация: строка
# «Холдинг не указан» собирает несвязанные организации.
_STAYED_SAME = """
       sum(1.0 / d.n_dest)                                         AS n_triples,
       sum(CASE WHEN d.tb_id = s.tb_id THEN 1.0 / d.n_dest ELSE 0 END) AS n_same_tb,
       sum(CASE WHEN NULLIF(btrim(hf.holding_name), '') IS NULL
                THEN CASE WHEN d.inn = s.inn THEN 1.0 / d.n_dest ELSE 0 END
                WHEN ht.holding_name = hf.holding_name THEN 1.0 / d.n_dest
                ELSE 0 END)                                        AS n_same_holding"""

STAYED_DEST = _STAYED_HEAD + """
SELECT s.inn,""" + _STAYED_SAME + """
FROM st s
JOIN dest d ON d.epk_id = s.epk_id
LEFT JOIN t_seg hf ON hf.inn = s.inn
LEFT JOIN t_seg ht ON ht.inn = d.inn
GROUP BY s.inn
"""

# Тот же запрос с регионом — когда разведка опознала ключ справочника
# подразделений. Ключ подставляется в `fetch` из белого списка, а не приходит
# параметром: имя колонки параметром не передать.
STAYED_DEST_REGION = _STAYED_HEAD + """,
reg AS (
  SELECT d.__GOSB_KEY__ AS gid, min(NULLIF(btrim(d.region_name), '')) AS region
  FROM {schema}.uzp_dim_gosb d
  WHERE """ + Q._NO_CA + """
  GROUP BY d.__GOSB_KEY__
)
SELECT s.inn,""" + _STAYED_SAME + """,
       sum(CASE WHEN rf.region IS NOT NULL AND rf.region = rt.region
                THEN 1.0 / d.n_dest ELSE 0 END)                    AS n_same_region
FROM st s
JOIN dest d ON d.epk_id = s.epk_id
LEFT JOIN t_seg hf ON hf.inn = s.inn
LEFT JOIN t_seg ht ON ht.inn = d.inn
LEFT JOIN reg rf ON rf.gid = s.gosb_id
LEFT JOIN reg rt ON rt.gid = d.gosb_id
GROUP BY s.inn
"""

# Судьба ОРГАНИЗАЦИИ, а не человека. Главный вопрос «почему»: ушла ли
# организация целиком (увела зарплатный проект — потеря в B2B), или от неё
# точечно уходят люди (переводят зарплату по заявлению, увольняются). По одной
# строке «ушёл из банка» эти истории неразличимы, а решения по ним разные.
#
# Договор сравнивается по НАБОРУ номеров: у организации их бывает несколько, и
# сравнение одного min() с другим объявило бы переоформлением обычный второй
# договор. `n_agr_kept` — сколько договоров базового месяца живы в отчётном.
ORG_STATUS = """
WITH b AS (
  SELECT inn, count(*) AS n_base FROM t_pairs
  WHERE report_dt = CAST(:d_base AS date) GROUP BY inn
),
c AS (
  SELECT inn, count(*) AS n_cur FROM t_pairs
  WHERE report_dt = CAST(:d_cur AS date) GROUP BY inn
),
s AS (
  SELECT inn, sum(amt_all) AS amt_cur_all FROM t_seen
  WHERE report_dt = CAST(:d_cur AS date) GROUP BY inn
),
ab AS (
  SELECT DISTINCT inn, agrmnt_num FROM t_pairs
  WHERE report_dt = CAST(:d_base AS date) AND agrmnt_num IS NOT NULL
),
ac AS (
  SELECT DISTINCT inn, agrmnt_num FROM t_pairs
  WHERE report_dt = CAST(:d_cur AS date) AND agrmnt_num IS NOT NULL
),
ag AS (
  SELECT ab.inn, count(*) AS n_agr_base, count(ac.inn) AS n_agr_kept
  FROM ab LEFT JOIN ac ON ac.inn = ab.inn AND ac.agrmnt_num = ab.agrmnt_num
  GROUP BY ab.inn
),
agc AS (SELECT inn, count(*) AS n_agr_cur FROM ac GROUP BY inn)
SELECT b.inn, b.n_base,
       COALESCE(c.n_cur, 0)        AS n_cur,
       COALESCE(s.amt_cur_all, 0)  AS amt_cur_all,
       COALESCE(ag.n_agr_base, 0)  AS n_agr_base,
       COALESCE(ag.n_agr_kept, 0)  AS n_agr_kept,
       COALESCE(agc.n_agr_cur, 0)  AS n_agr_cur
FROM b
LEFT JOIN c   ON c.inn = b.inn
LEFT JOIN s   ON s.inn = b.inn
LEFT JOIN ag  ON ag.inn = b.inn
LEFT JOIN agc ON agc.inn = b.inn
"""

# КАК уходили из банка: обрывом или постепенно. Суммы и число зачислений по
# месяцам у тех, кто ушёл из банка совсем, — от трёх месяцев ДО базового (иначе
# у ушедших сразу после базы нет предыстории) до отчётного.
#
# ЕДИНСТВЕННЫЙ новый скан витрины в этом блоке — самый дорогой запрос раздела,
# выключается параметром `with_exit_pattern`. Разметка «обрыв / постепенно» и
# свёртка идут В SQL: на проме ушедших из банка полтора миллиона, и строка на
# человека упёрлась в лимит выборки. В ядро уезжает «шаблон × месяц» — десятки
# строк. Порог падения — параметр `:exit_drop`.
EXIT_PATTERN = """
WITH lost AS (""" + _LOST_EPK_BASE + """),
lb AS (SELECT epk_id FROM lost WHERE cause = 'left_bank'),
m AS (
  SELECT p.report_dt, p.epk_id,
         sum(p.amt)             AS amt,
         sum(p.transaction_qty) AS qty
  FROM {schema}.uzp_data_payroll_m p
  JOIN lb ON lb.epk_id = p.epk_id
  WHERE p.report_dt >= CAST(:d_pre AS date)
    AND p.report_dt <= CAST(:d_cur AS date)
  GROUP BY p.report_dt, p.epk_id
),
r AS (
  SELECT report_dt, epk_id, amt, qty,
         row_number() OVER (PARTITION BY epk_id ORDER BY report_dt DESC) AS k
  FROM m WHERE amt > 0
),
per AS (
  SELECT epk_id,
         max(report_dt)                                AS last_dt,
         avg(CASE WHEN k <= 2 THEN amt END)            AS amt_last,
         avg(CASE WHEN k BETWEEN 3 AND 5 THEN amt END) AS amt_prev,
         avg(CASE WHEN k <= 2 THEN qty END)            AS qty_last,
         avg(CASE WHEN k BETWEEN 3 AND 5 THEN qty END) AS qty_prev
  FROM r GROUP BY epk_id
)
SELECT CASE WHEN amt_prev IS NULL OR amt_prev <= 0     THEN 'short'
            WHEN amt_last < :exit_drop * amt_prev      THEN 'amt'
            WHEN qty_last < :exit_drop * qty_prev      THEN 'qty'
            ELSE 'abrupt' END                          AS pattern,
       last_dt,
       count(*)                                        AS n_epk,
       sum(CASE WHEN amt_prev > 0 THEN amt_last / amt_prev END) AS sum_ratio,
       count(CASE WHEN amt_prev > 0 THEN 1 END)        AS n_ratio
FROM per
GROUP BY 1, 2
"""

# Сколько получал ушедший ОТНОСИТЕЛЬНО КОЛЛЕГ по той же организации. Уходят
# низкооплачиваемые — похоже на текучку и сокращения; высокооплачиваемые — на то,
# что их переманивают, и это самая дорогая потеря. Сравнение с СОБСТВЕННОЙ
# организацией, а не с сегментом: средняя зарплата школы и министерства разная,
# и сравнение с общей средней мерило бы структуру сегмента, а не уход.
PAY_LEVEL = """
WITH lost AS (""" + _LOST_BASE + """),
b AS (
  SELECT epk_id, inn, gosb_id, amt,
         avg(amt) OVER (PARTITION BY inn) AS org_avg,
         count(*) OVER (PARTITION BY inn) AS org_n
  FROM t_pairs WHERE report_dt = CAST(:d_base AS date)
)
SELECT COALESCE(l.cause, 'retained') AS fate,
       CASE WHEN b.amt < 0.5 * b.org_avg THEN 1
            WHEN b.amt < 0.8 * b.org_avg THEN 2
            WHEN b.amt < 1.2 * b.org_avg THEN 3
            WHEN b.amt < 2.0 * b.org_avg THEN 4
            ELSE 5 END                   AS bucket,
       count(*)                          AS n_triples
FROM b
LEFT JOIN lost l ON l.epk_id = b.epk_id AND l.inn = b.inn AND l.gosb_id = b.gosb_id
WHERE b.org_n >= :pay_min_org
GROUP BY 1, 2
"""

# В КАКИЕ ОРГАНИЗАЦИИ ушли те, кто ушёл в другой сегмент. Сегмент уже показан;
# организация отвечает, что это было: много людей в одну коммерческую компанию
# того же подразделения — переезд работодателя или вывод функции на аутсорсинг,
# вразнобой — обычная смена работы.
#
# Приёмник у человека один — тот, кто платит больше всех: иначе совместитель
# посчитался бы в двух организациях, и доли перестали бы складываться.
#
# В ядро уезжает только топ приёмников, а итоги (сколько всего, доля десяти
# крупнейших, доля того же подразделения) считаются в SQL по ВСЕМ: приёмников на
# проме может быть больше лимита выборки.
LEFT_SEGMENT_ORGS = """
WITH lost AS (""" + _LOST_EPK_BASE + """),
ls AS (SELECT epk_id FROM lost WHERE cause = 'left_segment'),
bg AS (
  SELECT DISTINCT epk_id, gosb_id FROM t_pairs
  WHERE report_dt = CAST(:d_base AS date)
),
d AS (
  SELECT t.epk_id, t.inn, t.gosb_id,
         row_number() OVER (PARTITION BY t.epk_id
                            ORDER BY t.amt_all DESC, t.inn) AS rn
  FROM t_seen t
  JOIN ls ON ls.epk_id = t.epk_id
  LEFT JOIN t_seg s ON s.inn = t.inn
  WHERE t.report_dt = CAST(:d_cur AS date) AND s.inn IS NULL AND t.amt_all > 0
),
g AS (
  SELECT d.inn,
         min(COALESCE(sa.segment_name, 'Организация не в справочнике')) AS segment_name,
         min(sa.company_name)                                           AS company_name,
         min(sa.industry_name)                                          AS industry_name,
         count(*)                                                       AS n_epk,
         count(bg.epk_id)                                               AS n_same_gosb
  FROM d
  LEFT JOIN t_seg_all sa ON sa.inn = d.inn
  LEFT JOIN bg ON bg.epk_id = d.epk_id AND bg.gosb_id = d.gosb_id
  WHERE d.rn = 1
  GROUP BY d.inn
),
r AS (SELECT g.*, row_number() OVER (ORDER BY g.n_epk DESC, g.inn) AS k FROM g),
t AS (
  SELECT count(*)                                      AS n_orgs,
         sum(n_epk)                                    AS total,
         sum(n_same_gosb)                              AS total_same_gosb,
         sum(CASE WHEN k <= 10 THEN n_epk ELSE 0 END)  AS top10
  FROM r
)
SELECT r.inn, r.segment_name, r.company_name, r.industry_name, r.n_epk,
       r.n_same_gosb, t.n_orgs, t.total, t.total_same_gosb, t.top10
FROM r, t
WHERE r.k <= :top_n
ORDER BY r.k
"""

# НАСКОЛЬКО ниже порога. Упала вдвое — неполная ставка или простой; не дотягивает
# сотню рублей — артефакт порога: зарплаты индексируются, порог стоит на месте.
# Сумма берётся ПО ОРГАНИЗАЦИИ, как и сам порог, и по лучшей из организаций
# человека — ровно так, как решалось, получатель он или нет. Диапазоны считаются
# в SQL: строка на человека на проме упиралась бы в лимит выборки.
BELOW_DEPTH = """
WITH lost AS (""" + _LOST_EPK_BASE + """),
bt AS (SELECT epk_id FROM lost WHERE cause = 'below_threshold'),
b AS (
  SELECT x.epk_id, max(x.amt_inn) AS amt_base
  FROM (SELECT epk_id, inn, sum(amt) AS amt_inn FROM t_pairs
        WHERE report_dt = CAST(:d_base AS date) GROUP BY epk_id, inn) x
  JOIN bt ON bt.epk_id = x.epk_id
  GROUP BY x.epk_id
),
c AS (
  SELECT y.epk_id, max(y.amt_inn) AS amt_cur
  FROM (SELECT t.epk_id, t.inn, sum(t.amt_codes) AS amt_inn
        FROM t_seen t JOIN t_seg s ON s.inn = t.inn
        WHERE t.report_dt = CAST(:d_cur AS date) GROUP BY t.epk_id, t.inn) y
  JOIN bt ON bt.epk_id = y.epk_id
  GROUP BY y.epk_id
)
SELECT CASE WHEN COALESCE(c.amt_cur, 0) >= 0.8 * :amt_min THEN 1
            WHEN COALESCE(c.amt_cur, 0) >= 0.4 * :amt_min THEN 2
            ELSE 3 END                                         AS lvl,
       CASE WHEN COALESCE(c.amt_cur, 0) >= 0.8 * b.amt_base THEN 1
            WHEN COALESCE(c.amt_cur, 0) >= 0.5 * b.amt_base THEN 2
            ELSE 3 END                                         AS chg,
       count(*)                                                AS n_epk
FROM b LEFT JOIN c ON c.epk_id = b.epk_id
GROUP BY 1, 2
"""
