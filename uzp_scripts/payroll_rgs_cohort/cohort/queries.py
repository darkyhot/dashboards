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

# Порог получателя: пара (человек, ИНН) засчитывается, если СУММА зачислений за
# месяц по этим кодам строго больше порога.
AMT_MIN = 2500

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
WHERE p.report_dt IN (CAST(:d_base AS date), CAST(:d_cur AS date))
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

# --------------------------------------------------------------------------- #
# Рабочий набор
# --------------------------------------------------------------------------- #
#
# Шесть выборок, на которых стоит весь разбор. Каждая описана РОВНО ОДИН РАЗ, а
# как она материализуется — временной таблицей или CTE — решает `fetch`, по итогу
# разведки. Две копии одного определения (одна для быстрого пути, другая для
# запасного) разъехались бы молча, и два пути начали бы отвечать по-разному на
# один и тот же вопрос — при том, что запасной включается сам и без объявления.
#
# Порядок в словаре ЗНАЧИМ: в режиме CTE каждая следующая выборка ссылается на
# предыдущие, а CTE видит только те, что объявлены до неё.

# Бюджетные ИНН по ТЕКУЩЕМУ срезу справочника ЕПК (см. комментарий к _SEG_INN).
_T_SEG = _SEG_INN

# Пары (человек, ИНН) за ОБА опорных месяца сразу.
#
# Один запрос на два месяца, а не два по одному: витрина партиционирована по
# report_dt, оба месяца отсекаются одним `IN`, и второй проход по индексу не нужен.
_T_PAIRS = """
  SELECT c.report_dt, c.epk_id, c.inn, c.gosb_id, c.amt
  FROM (
    SELECT p.report_dt,
           p.epk_id,
           CAST(p.inn AS bigint) AS inn,
           min(p.gosb_id)        AS gosb_id,
           sum(p.amt)            AS amt
    FROM {schema}.uzp_data_payroll_m p
    WHERE p.report_dt IN (CAST(:d_base AS date), CAST(:d_cur AS date))
      AND p.{code_col} = ANY(:codes)
      AND p.epk_id IS NOT NULL
      AND """ + INN_OK + """
    GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint)
    HAVING sum(p.amt) > :amt_min
  ) c
  JOIN t_seg s ON s.inn = c.inn
"""

# Присутствие в отчётном месяце БЕЗ фильтров по коду и порогу — по всему банку.
#
# Единственный способ отличить «человек ушёл из банка» от «человек на месте, а из
# метрики выпал». Если epk_id вообще не встречается в ведомостях отчётного месяца
# — он ушёл; если встречается, но пары нет — сработал порог, код или смена
# работодателя. Без этой выборки все три случая слиплись бы в «отток», и разбор
# показал бы ровно то же, что агрегаты, только дороже.
_T_SEEN = """
  SELECT p.epk_id,
         CAST(p.inn AS bigint) AS inn,
         sum(p.amt)            AS amt_all,
         sum(CASE WHEN p.{code_col} = ANY(:codes) THEN p.amt ELSE 0 END) AS amt_codes
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt = CAST(:d_cur AS date)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.epk_id, CAST(p.inn AS bigint)
"""

# Свёртки: по ним лестница причин отвечает на «а где ещё есть этот человек» одним
# соединением, а не подзапросом на каждую строку.
_T_SEEN_EPK = "  SELECT epk_id, sum(amt_all) AS amt_all FROM t_seen GROUP BY epk_id"
_T_SEEN_INN = "  SELECT inn, count(*) AS n_pairs FROM t_seen GROUP BY inn"

# Сколько бюджетных ИНН у человека в каждом из двух месяцев. Разница этих двух
# чисел и есть совместительство: человек с двумя ИНН весит в метрике два
# получателя, и потеря одного из них выглядит оттоком, не будучи им.
_T_CUR_EPK = """
  SELECT epk_id, count(*) AS n_inn
  FROM t_pairs WHERE report_dt = CAST(:d_cur AS date)
  GROUP BY epk_id
"""

_T_BASE_EPK = """
  SELECT epk_id, count(*) AS n_inn
  FROM t_pairs WHERE report_dt = CAST(:d_base AS date)
  GROUP BY epk_id
"""

# Бюджетные ИНН базового месяца — по ним отличается «организация пришла в сегмент»
# от «в старую организацию пришли люди».
_T_BASE_INN = """
  SELECT inn, count(*) AS n_pairs
  FROM t_pairs WHERE report_dt = CAST(:d_base AS date)
  GROUP BY inn
"""

# Имя -> (тело выборки, колонка распределения). Распределение выбрано по тому,
# ЧЕМ выборка соединяется дальше: почти вся арифметика разбора идёт по человеку,
# и распределение по ИНН заставило бы Greenplum перекидывать данные между
# сегментами на каждом шаге.
WORKSET: list[tuple[str, str, str]] = [
    ("t_seg",      _T_SEG,      "inn"),
    ("t_pairs",    _T_PAIRS,    "epk_id"),
    ("t_seen",     _T_SEEN,     "epk_id"),
    ("t_seen_epk", _T_SEEN_EPK, "epk_id"),
    ("t_seen_inn", _T_SEEN_INN, "inn"),
    ("t_cur_epk",  _T_CUR_EPK,  "epk_id"),
    ("t_base_epk", _T_BASE_EPK, "epk_id"),
    ("t_base_inn", _T_BASE_INN, "inn"),
]

CREATE_TMP = "CREATE TEMP TABLE {name} AS\n{body}\nDISTRIBUTED BY ({dist})"
# Запасной вариант DDL: DISTRIBUTED BY — расширение Greenplum, и на обычном
# PostgreSQL (открытый контур) оператор с ним не разбирается вовсе.
CREATE_TMP_PLAIN = "CREATE TEMP TABLE {name} AS\n{body}"
ANALYZE_TMP = "ANALYZE {name}"
DROP_TMP = "DROP TABLE IF EXISTS {name}"


# --------------------------------------------------------------------------- #
# Лестница причин
# --------------------------------------------------------------------------- #

# Каждая потерянная пара получает РОВНО ОДНУ причину — первую сработавшую сверху
# вниз. Аддитивность не пожелание, а условие осмысленности: если пара попадёт в
# две ветки, «из чего состоит падение» перестанет складываться в падение, и
# читатель не сможет проверить ни одну цифру.
#
# Порядок ветвей значим и обоснован так:
#   1. ИНН исчез из ведомостей целиком — это про организацию, а не про человека,
#      и людей такой организации незачем разбирать по одному;
#   2. организация ликвидирована — она не «ушла в другой банк», её просто нет;
#   3. человека нет в ведомостях банка вовсе — единственный НАСТОЯЩИЙ отток ФЛ;
#   4. человек есть, но в этом ИНН зачислений нет — сменил работодателя;
#   5. зачисления есть, коды нужные, сумма не дотянула — порог;
#   6. зачисления есть, а коды не те — кодировка;
#   7. пары в этом ИНН нет, но человек остался в бюджетной сфере через другой —
#      схлопнулось совместительство.
#
# Ветви 5–7 выглядят оттоком в любом агрегате, но оттоком не являются. Ради них
# разбор и опускается до физлица.
_LOST_CASE = """
    CASE
      WHEN s.is_liquidated       THEN 'liquidated'
      WHEN si.inn IS NULL        THEN 'inn_gone'
      WHEN se.epk_id IS NULL     THEN 'person_left_bank'
      WHEN t.amt_codes > 0       THEN 'below_threshold'
      WHEN t.amt_all > 0         THEN 'code_out_of_list'
      WHEN ce.epk_id IS NULL     THEN 'left_rgs'
      WHEN be.n_inn > 1          THEN 'multi_collapsed'
      ELSE 'moved_within_rgs'
    END
"""

# Общая часть запросов по потерянным парам: пары базового месяца, которых нет в
# отчётном, с причиной. Вынесена в кусок, чтобы итог и разрезы считались ПО ОДНОМУ
# И ТОМУ ЖЕ определению — разъехавшись, они дали бы разные ответы на один вопрос.
#
# Ветки «сумма ниже порога» и «код вне списка» проверяются РАНЬШЕ веток про
# другие организации и не требуют отдельной проверки `t.epk_id IS NOT NULL`:
# если зачислений в этом ИНН нет вовсе, обе суммы приходят NULL, оба условия
# ложны, и разбор идёт дальше сам.
_LOST_BASE = """
  SELECT b.epk_id, b.inn, b.gosb_id, b.amt,
         """ + _LOST_CASE + """ AS cause
  FROM t_pairs b
  LEFT JOIN t_pairs c ON c.report_dt = CAST(:d_cur AS date)
                     AND c.epk_id = b.epk_id AND c.inn = b.inn
  LEFT JOIN t_seg s  ON s.inn = b.inn
  LEFT JOIN t_seen_inn si ON si.inn = b.inn
  LEFT JOIN t_seen_epk se ON se.epk_id = b.epk_id
  LEFT JOIN t_seen  t  ON t.epk_id = b.epk_id AND t.inn = b.inn
  LEFT JOIN t_cur_epk  ce ON ce.epk_id = b.epk_id
  LEFT JOIN t_base_epk be ON be.epk_id = b.epk_id
  WHERE b.report_dt = CAST(:d_base AS date) AND c.epk_id IS NULL
"""

# Зеркальная лестница по ПРИШЕДШИМ парам. Зеркальная не ради симметрии отчёта: без
# неё видно только половину движения, и «пришло меньше, чем раньше» не отличить от
# «ушло больше» — а это разные болезни с разным лечением.
#
# «Новый в сегменте», а не «новый в банке»: базовый месяц свёрнут только по
# бюджетной сфере, и утверждать про весь банк по нему нельзя. Честное имя ветки
# важнее красивого: по красивому сделали бы вывод, которого данные не выдерживают.
_GAINED_BASE = """
  SELECT c.epk_id, c.inn, c.gosb_id, c.amt,
         CASE
           WHEN bp.epk_id IS NULL              THEN 'person_new_to_rgs'
           WHEN bi.inn IS NULL                 THEN 'inn_new'
           WHEN ce.n_inn > bp.n_inn            THEN 'multi_new'
           ELSE 'moved_in'
         END AS cause
  FROM t_pairs c
  LEFT JOIN t_pairs b ON b.report_dt = CAST(:d_base AS date)
                     AND b.epk_id = c.epk_id AND b.inn = c.inn
  LEFT JOIN t_base_inn bi ON bi.inn = c.inn
  LEFT JOIN t_base_epk bp ON bp.epk_id = c.epk_id
  LEFT JOIN t_cur_epk  ce ON ce.epk_id = c.epk_id
  WHERE c.report_dt = CAST(:d_cur AS date) AND b.epk_id IS NULL
"""

LOST_TOTALS = """
WITH lost AS (""" + _LOST_BASE + """)
SELECT cause, count(*) AS n_pairs, count(DISTINCT epk_id) AS n_epk,
       sum(amt) AS amt
FROM lost GROUP BY cause ORDER BY count(*) DESC
"""

GAINED_TOTALS = """
WITH gained AS (""" + _GAINED_BASE + """)
SELECT cause, count(*) AS n_pairs, count(DISTINCT epk_id) AS n_epk,
       sum(amt) AS amt
FROM gained GROUP BY cause ORDER BY count(*) DESC
"""

# Потери в разрезе организации — с составом причин. Именно это агрегаты показать не
# могут: «утекло образование» без состава причин остаётся утверждением без
# содержания. Возвращаются ВСЕ бюджетные ИНН с потерями; их десятки тысяч, а не
# миллионы, и обрезать выборку в SQL нельзя — итог по разрезам перестал бы
# сходиться с общим итогом.
LOST_BY_INN = """
WITH lost AS (""" + _LOST_BASE + """)
SELECT l.inn, l.cause, min(l.gosb_id) AS gosb_id, count(*) AS n_pairs
FROM lost l GROUP BY l.inn, l.cause
"""

GAINED_BY_INN = """
WITH gained AS (""" + _GAINED_BASE + """)
SELECT g.inn, min(g.gosb_id) AS gosb_id, count(*) AS n_pairs
FROM gained g GROUP BY g.inn
"""

# Итоги двух месяцев: то самое число, которое сравнивают с отчётностью.
# Пары И люди сразу — разница между ними и есть вклад совместительства.
MONTH_TOTALS = """
SELECT report_dt,
       count(*)               AS n_pairs,
       count(DISTINCT epk_id) AS n_epk,
       count(DISTINCT inn)    AS n_inn,
       sum(amt)               AS amt
FROM t_pairs GROUP BY report_dt ORDER BY report_dt
"""

# Атрибуты бюджетных организаций — для разрезов. Отдельным запросом по СПИСКУ ИНН,
# а не джойном в каждый разрез: справочник маленький, а джойн в тяжёлый запрос
# стоит дорого и рискует задвоить строки.
SEG_ATTRS = """
SELECT * FROM t_seg
"""


# --------------------------------------------------------------------------- #
# Ряд, дожитие, чувствительность
# --------------------------------------------------------------------------- #

# Помесячный ряд бюджетной сферы. Отвечает на «КОГДА»: обрыв в одном месяце — это
# событие (загрузка, переклассификация, смена кодировки), плавное снижение — это
# текучесть. По двум точкам года эти два случая неразличимы.
#
# Отношение пар к людям — коэффициент совместительства. Его падение и есть та
# часть минуса, где не потерян ни один человек.
MONTHLY = """
WITH pairs AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn, sum(p.amt) AS amt
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt >= CAST(:d_from AS date)
    AND p.report_dt <= CAST(:d_to AS date)
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint)
  HAVING sum(p.amt) > :amt_min
)
SELECT x.report_dt,
       count(*)                  AS n_pairs,
       count(DISTINCT x.epk_id)  AS n_epk,
       count(DISTINCT x.inn)     AS n_inn,
       sum(x.amt)                AS amt
FROM pairs x JOIN t_seg s ON s.inn = x.inn
GROUP BY x.report_dt
ORDER BY x.report_dt
"""

# Полнота загрузки ПО ВСЕМУ БАНКУ, без фильтра сегмента и порога. Месяц с
# аномально низким числом строк — недогруженная партиция, и любой вывод по нему
# будет выводом про загрузку, а не про людей. Проверяется ДО того, как объяснять
# падение.
# Только счёт строк и сумма: `count(DISTINCT epk_id)` по ВСЕМУ банку за 25
# месяцев — сортировка миллиардов значений ради числа, которого нет ни в одном
# разделе. Недогруженную партицию видно и по числу строк.
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

# Кривая дожития когорты базового месяца: сколько её пар живо в каждом следующем
# месяце. Когорта берётся из рабочей таблицы, ряд — из витрины; join идёт по паре.
SURVIVAL = """
WITH cohort AS (
  SELECT epk_id, inn FROM t_pairs WHERE report_dt = CAST(:d_base AS date)
),
pairs AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn, sum(p.amt) AS amt
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt >= CAST(:d_base AS date)
    AND p.report_dt <= CAST(:d_cur AS date)
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint)
  HAVING sum(p.amt) > :amt_min
)
SELECT x.report_dt, count(*) AS n_alive, count(DISTINCT x.epk_id) AS n_epk_alive
FROM pairs x JOIN cohort c ON c.epk_id = x.epk_id AND c.inn = x.inn
GROUP BY x.report_dt
ORDER BY x.report_dt
"""

# Чувствительность к порогу. Порог фиксирован (2500), а зарплаты индексируются —
# сам по себе он должен год к году ДОБАВЛЯТЬ получателей, а не убавлять. Если
# падение сохраняется при пороге 0, порог ни при чём; если исчезает — объяснение
# найдено. Проверяется в лоб, а не рассуждением.
THRESHOLD_SENS = """
WITH pairs AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn, sum(p.amt) AS amt
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt IN (CAST(:d_base AS date), CAST(:d_cur AS date))
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint)
)
SELECT x.report_dt,
       count(*) FILTER (WHERE x.amt > 0)     AS t0,
       count(*) FILTER (WHERE x.amt > 1000)  AS t1000,
       count(*) FILTER (WHERE x.amt > 2500)  AS t2500,
       count(*) FILTER (WHERE x.amt > 5000)  AS t5000,
       count(*) FILTER (WHERE x.amt > 10000) AS t10000
FROM pairs x JOIN t_seg s ON s.inn = x.inn
GROUP BY x.report_dt
ORDER BY x.report_dt
"""

# Смесь кодов зачисления помесячно. Считается ПО ЛЮДЯМ, а не по строкам: строк на
# пару бывает разное число, и их динамика говорила бы о дроблении выплат, а не о
# том, сколько человек этот код получает. Коды берутся ВСЕ, включая те, что вне
# списка: исчезнувший из метрики код обычно не исчезает из витрины, он переезжает
# в соседний — и увидеть это можно, только глядя на оба.
#
# Считается по ДВУМ опорным месяцам, а не по всему ряду: разбор сравнивает
# базовый месяц с отчётным, и остальные двадцать три месяца были бы выгружены,
# чтобы быть отброшенными в первой же строке расчёта.
CODE_MIX = """
SELECT p.report_dt,
       p.{code_col}                          AS code,
       min(p.enrollment_transcription)       AS code_name,
       count(DISTINCT p.epk_id)              AS n_epk,
       sum(p.amt)                            AS amt
FROM {schema}.uzp_data_payroll_m p
JOIN t_seg s ON s.inn = CAST(p.inn AS bigint)
WHERE p.report_dt IN (CAST(:d_base AS date), CAST(:d_cur AS date))
  AND """ + INN_OK + """
GROUP BY p.report_dt, p.{code_col}
ORDER BY p.report_dt, p.{code_col}
"""

# Миграция ИНН: куда переехали люди, потерявшие свой ИНН.
#
# Это единственная проверка, отличающая реорганизацию от оттока: если заметная
# доля людей одного ИНН оказалась в одном и том же новом ИНН — организацию
# переоформили, люди никуда не уходили. Ни один агрегат по холдингам этого не
# покажет, потому что новый ИНН к старому холдингу ещё не привязан.
#
# Приёмник ищется по ВСЕЙ витрине отчётного месяца, без фильтра сегмента: при
# переоформлении новый ИНН часто ещё не размечен как бюджетный.
INN_MIGRATION = """
WITH lost AS (
  SELECT b.epk_id, b.inn AS inn_from
  FROM t_pairs b
  LEFT JOIN t_pairs c ON c.report_dt = CAST(:d_cur AS date)
                     AND c.epk_id = b.epk_id AND c.inn = b.inn
  WHERE b.report_dt = CAST(:d_base AS date) AND c.epk_id IS NULL
)
SELECT l.inn_from, t.inn AS inn_to, count(*) AS n_epk
FROM lost l
JOIN t_seen t ON t.epk_id = l.epk_id
LEFT JOIN t_pairs b2 ON b2.report_dt = CAST(:d_base AS date)
                    AND b2.epk_id = l.epk_id AND b2.inn = t.inn
WHERE b2.epk_id IS NULL                      -- приёмник НОВЫЙ для этого человека
GROUP BY l.inn_from, t.inn
HAVING count(*) >= :min_movers
ORDER BY count(*) DESC
"""

# Территория: ГОСБ -> ТБ -> регион. Регион нужен отдельным разрезом: один регион
# обслуживается несколькими ГОСБ, и по ГОСБ картина региона не видна.
GOSB_DIM = """
SELECT d.new_gosb_id,
       min(d.tb_id)          AS tb_id,
       min(d.tb_short_name)  AS tb_short_name,
       min(d.new_gosb_name)  AS gosb_name,
       min(NULLIF(btrim(d.region_name), '')) AS region_name
FROM {schema}.uzp_dim_gosb d
WHERE """ + Q._NO_CA + """
GROUP BY d.new_gosb_id
"""

# Соответствие старых ГОСБ новым — тем же запросом, что в дэше.
GOSB_MAP = "SELECT * FROM (" + _GMAP + ") g"
