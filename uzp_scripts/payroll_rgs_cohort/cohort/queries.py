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
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn, p.gosb_id,
         sum(p.amt) AS amt,
         sum(sum(p.amt)) OVER (PARTITION BY p.report_dt, p.epk_id,
                                            CAST(p.inn AS bigint)) AS amt_inn
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt = ANY(CAST(:months AS date[]))
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.gosb_id
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

# Получатели на грейне (человек, ИНН, ГОСБ) за все опорные месяцы сразу.
#
# ГРЕЙН — ТРОЙКА. Человек, получающий в одном ИНН через два подразделения, весит
# двух получателей: так считает отчётность, с которой сверяется результат.
# Схлопывание ГОСБ через min() занижало бы базу и прятало целый класс движения —
# перевод человека между подразделениями одной организации.
#
# ПОРОГ применяется к сумме за месяц В ИНН, а не в тройке: так он задан
# постановкой. Порог и ключ счёта живут на разных грейнах, поэтому сумма по ИНН
# считается оконной функцией поверх группировки, а не в HAVING — HAVING умеет
# фильтровать только по своей группе. Вариант «порог на тройку» подставляется
# через {amt_cond}: разведка печатает счёт при обоих, чтобы можно было сверить
# с отчётностью и не гадать.
_T_PAIRS = """
  SELECT x.report_dt, x.epk_id, x.inn, x.gosb_id, x.tb_id, x.amt
  FROM (
    SELECT p.report_dt,
           p.epk_id,
           CAST(p.inn AS bigint) AS inn,
           p.gosb_id,
           min(p.tb_id)          AS tb_id,
           sum(p.amt)            AS amt,
           sum(sum(p.amt)) OVER (PARTITION BY p.report_dt, p.epk_id,
                                              CAST(p.inn AS bigint)) AS amt_inn
    FROM {schema}.uzp_data_payroll_m p
    WHERE p.report_dt = ANY(CAST(:months AS date[]))
      AND p.{code_col} = ANY(:codes)
      AND p.epk_id IS NOT NULL
      AND """ + INN_OK + """
    GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.gosb_id
  ) x
  JOIN t_seg s ON s.inn = x.inn
  WHERE {amt_cond}
"""

# Присутствие в ведомостях БЕЗ фильтров по коду и порогу — по всему банку, но
# ТОЛЬКО по людям, которые хоть в одном опорном месяце были в сегменте.
#
# Ограничение по людям — не экономия, а единственный способ уложиться: вся
# витрина за месяц это клиенты всего банка, а разбору нужны только те, про кого
# он спрашивает. Спрашивает он ровно про людей из `t_pairs`, поэтому остальные
# строки не понадобятся ни одной ветке.
#
# Без этой выборки «человек ушёл из банка» неотличимо от «человек на месте, а из
# метрики выпал»: если epk_id вообще не встречается в ведомостях — он ушёл; если
# встречается, но тройки нет — сработал порог, код, перевод или смена
# работодателя. Все четыре случая слиплись бы в «отток».
_T_SEEN = """
  SELECT p.report_dt,
         p.epk_id,
         CAST(p.inn AS bigint) AS inn,
         p.gosb_id,
         sum(p.amt)            AS amt_all,
         sum(CASE WHEN p.{code_col} = ANY(:codes) THEN p.amt ELSE 0 END) AS amt_codes
  FROM {schema}.uzp_data_payroll_m p
  JOIN (SELECT DISTINCT epk_id FROM t_pairs) k ON k.epk_id = p.epk_id
  WHERE p.report_dt = ANY(CAST(:seen_months AS date[]))
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.gosb_id
"""

# Человек в ведомостях банка вообще — по месяцам.
_T_SEEN_EPK = """
  SELECT report_dt, epk_id, sum(amt_all) AS amt_all
  FROM t_seen GROUP BY report_dt, epk_id
"""

# Он же, но ТОЛЬКО по бюджетным организациям: по этим суммам лестница ПО ЛЮДЯМ
# отличает «упал ниже порога» от «ушёл из сегмента». На грейне человека тройки
# не важны — важно, идут ли ему бюджетные деньги вообще.
_T_SEEN_SEG = """
  SELECT t.report_dt, t.epk_id,
         sum(t.amt_all)   AS amt_all,
         sum(t.amt_codes) AS amt_codes
  FROM t_seen t JOIN t_seg s ON s.inn = t.inn
  GROUP BY t.report_dt, t.epk_id
"""

# Существует ли организация в ведомостях — по месяцам. По ВСЕМ её получателям, а
# не только по сегментным: организация, чьи бюджетные сотрудники ушли все до
# одного, но которая продолжает платить остальным, из ведомостей не исчезла.
# Ограничение по списку ИНН оставлено — про чужие организации разбор не спрашивает.
_T_INN_SEEN = """
  SELECT p.report_dt, CAST(p.inn AS bigint) AS inn
  FROM {schema}.uzp_data_payroll_m p
  JOIN (SELECT DISTINCT inn FROM t_pairs) k ON k.inn = CAST(p.inn AS bigint)
  WHERE p.report_dt = ANY(CAST(:seen_months AS date[]))
    AND """ + INN_OK + """
  GROUP BY p.report_dt, CAST(p.inn AS bigint)
"""

# Был ли человек в ведомостях в месяцы ПЕРЕД отчётным.
#
# Ради этой выборки и затевалось окно. Отчётный месяц может быть сезонной ямой, и
# тогда «нет зачисления в августе» означает не уход, а перерыв: человек был в
# июне и июле и почти наверняка вернётся в сентябре. Различить эти два случая по
# одному месяцу нельзя никак, а разница между ними — миллионы человек.
_T_RECENT = """
  SELECT DISTINCT epk_id FROM t_seen
  WHERE report_dt = ANY(CAST(:recent_months AS date[]))
"""

# Сколько бюджетных организаций и подразделений у человека в каждом месяце.
# Разница между месяцами и есть совместительство: человек с двумя организациями
# весит двух получателей, и потеря одной из них выглядит оттоком, не будучи им.
_T_EPK_MONTH = """
  SELECT report_dt, epk_id,
         count(DISTINCT inn) AS n_inn,
         count(*)            AS n_triples
  FROM t_pairs GROUP BY report_dt, epk_id
"""

# Организации сегмента по месяцам — по ним отличается «организация пришла в
# сегмент» от «в старую организацию пришли люди».
_T_INN_MONTH = """
  SELECT report_dt, inn, count(*) AS n_triples
  FROM t_pairs GROUP BY report_dt, inn
"""

# Имя -> (тело выборки, колонка распределения). Распределение выбрано по тому,
# ЧЕМ выборка соединяется дальше: почти вся арифметика разбора идёт по человеку,
# и распределение по ИНН заставило бы Greenplum перекидывать данные между
# сегментами на каждом шаге.
WORKSET: list[tuple[str, str, str]] = [
    ("t_seg",       _T_SEG,       "inn"),
    ("t_pairs",     _T_PAIRS,     "epk_id"),
    ("t_seen",      _T_SEEN,      "epk_id"),
    ("t_seen_epk",  _T_SEEN_EPK,  "epk_id"),
    ("t_seen_seg",  _T_SEEN_SEG,  "epk_id"),
    ("t_inn_seen",  _T_INN_SEEN,  "inn"),
    ("t_recent",    _T_RECENT,    "epk_id"),
    ("t_epk_month", _T_EPK_MONTH, "epk_id"),
    ("t_inn_month", _T_INN_MONTH, "inn"),
]

CREATE_TMP = "CREATE TEMP TABLE {name} AS\n{body}\nDISTRIBUTED BY ({dist})"
# Запасной вариант DDL: DISTRIBUTED BY — расширение Greenplum, и на обычном
# PostgreSQL (открытый контур) оператор с ним не разбирается вовсе.
CREATE_TMP_PLAIN = "CREATE TEMP TABLE {name} AS\n{body}"
ANALYZE_TMP = "ANALYZE {name}"
DROP_TMP = "DROP TABLE IF EXISTS {name}"


# --------------------------------------------------------------------------- #
# Лестница причин: получатели (тройки)
# --------------------------------------------------------------------------- #
#
# Каждый потерянный получатель получает РОВНО ОДНУ причину — первую сработавшую
# сверху вниз. Аддитивность не пожелание, а условие осмысленности: если тройка
# попадёт в две ветки, «из чего состоит падение» перестанет складываться в
# падение, и читатель не сможет проверить ни одну цифру.
#
# Порядок ветвей значим и обоснован так:
#   1. организация ликвидирована — она не «ушла в другой банк», её просто нет;
#   2. ИНН исчез из ведомостей целиком — это про организацию, а не про человека;
#   3. человека нет в ведомостях банка ВООБЩЕ и не было в предыдущие месяцы —
#      единственный НАСТОЯЩИЙ уход физлица;
#   4. его нет в отчётном месяце, но он был только что — перерыв, а не уход;
#   5. зачисления в этой же тройке есть, коды нужные, сумма не дотянула — порог;
#   6. зачисления есть, а коды не те — кодировка;
#   7. тот же ИНН, другое подразделение — перевод внутри организации;
#   8. человека в сегменте больше нет — сменил работодателя;
#   9. в базе у него было несколько организаций, стало меньше — совместительство;
#  10. остальное: перешёл в другую бюджетную организацию.
#
# Ветви 4-7, 9 и 10 выглядят оттоком в любом агрегате, но оттоком не являются.
# Ради них разбор и опускается до физического лица.
_LOST_CASE = """
    CASE
      WHEN s.is_liquidated                            THEN 'liquidated'
      WHEN si.inn IS NULL                             THEN 'inn_gone'
      WHEN se.epk_id IS NULL AND rc.epk_id IS NULL    THEN 'left_bank'
      WHEN se.epk_id IS NULL                          THEN 'gap_only'
      WHEN t.amt_codes > 0                            THEN 'below_threshold'
      WHEN t.amt_all > 0                              THEN 'code_out_of_list'
      WHEN cg.epk_id IS NOT NULL                      THEN 'gosb_moved'
      WHEN ce.epk_id IS NULL                          THEN 'left_rgs'
      WHEN be.n_inn > 1                               THEN 'multi_collapsed'
      ELSE 'moved_within_rgs'
    END
"""

# Общая часть запросов по потерянным получателям. Вынесена в кусок, чтобы итог и
# разрезы считались ПО ОДНОМУ И ТОМУ ЖЕ определению — разъехавшись, они дали бы
# разные ответы на один вопрос.
#
# Ветки «сумма ниже порога» и «код вне списка» не требуют отдельной проверки
# `t.epk_id IS NOT NULL`: если зачислений в этой тройке нет вовсе, обе суммы
# приходят NULL, оба условия ложны, и разбор идёт дальше сам.
_LOST_BASE = """
  SELECT b.epk_id, b.inn, b.gosb_id, b.tb_id, b.amt,
         """ + _LOST_CASE + """ AS cause
  FROM t_pairs b
  LEFT JOIN t_pairs c ON c.report_dt = CAST(:d_cur AS date)
                     AND c.epk_id = b.epk_id AND c.inn = b.inn
                     AND c.gosb_id = b.gosb_id
  LEFT JOIN t_seg s      ON s.inn = b.inn
  LEFT JOIN t_inn_seen si ON si.inn = b.inn
                         AND si.report_dt = CAST(:d_cur AS date)
  LEFT JOIN t_seen_epk se ON se.epk_id = b.epk_id
                         AND se.report_dt = CAST(:d_cur AS date)
  LEFT JOIN t_seen t     ON t.epk_id = b.epk_id AND t.inn = b.inn
                        AND t.gosb_id = b.gosb_id
                        AND t.report_dt = CAST(:d_cur AS date)
  LEFT JOIN t_recent rc  ON rc.epk_id = b.epk_id
  LEFT JOIN (SELECT DISTINCT epk_id, inn FROM t_pairs
             WHERE report_dt = CAST(:d_cur AS date)) cg
                         ON cg.epk_id = b.epk_id AND cg.inn = b.inn
  LEFT JOIN t_epk_month ce ON ce.epk_id = b.epk_id
                          AND ce.report_dt = CAST(:d_cur AS date)
  LEFT JOIN t_epk_month be ON be.epk_id = b.epk_id
                          AND be.report_dt = CAST(:d_base AS date)
  WHERE b.report_dt = CAST(:d_base AS date) AND c.epk_id IS NULL
"""

# Зеркальная лестница по ПРИШЕДШИМ получателям.
#
# Зеркальная не ради симметрии отчёта: без неё видно только половину движения, и
# «пришло меньше, чем раньше» не отличить от «ушло больше». Главное же — без
# раскладки прихода нельзя ответить на вопрос, ради которого всё считается:
# ВЫРОСЛИ ЛИ МЫ, если не брать методологию счёта. Вычитать из полного прихода
# только настоящие потери — арифметика, не значащая ничего.
#
# Методологические ветки проверяются РАНЬШЕ «новых»: человек, который в базовом
# месяце получал здесь же, но ниже порога, — это порог, а не новый сотрудник, и
# назвать его новым значило бы завысить рост ровно на ту величину, которую мы и
# вычитаем со стороны потерь.
_GAINED_CASE = """
    CASE
      WHEN bt.amt_codes > 0        THEN 'crossed_threshold'
      WHEN bt.amt_all > 0          THEN 'code_came_into_list'
      WHEN bg.epk_id IS NOT NULL   THEN 'gosb_moved_in'
      WHEN bse.epk_id IS NULL      THEN 'person_new_to_bank'
      WHEN bp.epk_id IS NULL       THEN 'returned_to_rgs'
      WHEN bi.inn IS NULL          THEN 'inn_new'
      WHEN ce.n_inn > bp.n_inn     THEN 'multi_new'
      ELSE 'moved_in'
    END
"""

_GAINED_BASE = """
  SELECT c.epk_id, c.inn, c.gosb_id, c.tb_id, c.amt,
         """ + _GAINED_CASE + """ AS cause
  FROM t_pairs c
  LEFT JOIN t_pairs b ON b.report_dt = CAST(:d_base AS date)
                     AND b.epk_id = c.epk_id AND b.inn = c.inn
                     AND b.gosb_id = c.gosb_id
  LEFT JOIN t_seen bt     ON bt.epk_id = c.epk_id AND bt.inn = c.inn
                         AND bt.gosb_id = c.gosb_id
                         AND bt.report_dt = CAST(:d_base AS date)
  LEFT JOIN (SELECT DISTINCT epk_id, inn FROM t_pairs
             WHERE report_dt = CAST(:d_base AS date)) bg
                         ON bg.epk_id = c.epk_id AND bg.inn = c.inn
  LEFT JOIN t_seen_epk bse ON bse.epk_id = c.epk_id
                          AND bse.report_dt = CAST(:d_base AS date)
  LEFT JOIN t_epk_month bp ON bp.epk_id = c.epk_id
                          AND bp.report_dt = CAST(:d_base AS date)
  LEFT JOIN t_epk_month ce ON ce.epk_id = c.epk_id
                          AND ce.report_dt = CAST(:d_cur AS date)
  LEFT JOIN t_inn_month bi ON bi.inn = c.inn
                          AND bi.report_dt = CAST(:d_base AS date)
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

# Потери в разрезе организации — с составом причин. Именно это агрегаты показать
# не могут: «утекло образование» без состава причин остаётся утверждением без
# содержания. Возвращаются ВСЕ бюджетные ИНН с потерями; обрезать выборку в SQL
# нельзя — итог по разрезам перестал бы сходиться с общим итогом.
LOST_BY_INN = """
WITH lost AS (""" + _LOST_BASE + """)
SELECT l.inn, l.cause, min(l.gosb_id) AS gosb_id, min(l.tb_id) AS tb_id,
       count(*) AS n_triples
FROM lost l GROUP BY l.inn, l.cause
"""

# Приход в разрезе организации. Без него таблица крупнейших потерь врёт:
# организация, потерявшая двести тысяч получателей и набравшая столько же, в ней
# выглядит катастрофой, хотя не потеряла ничего.
GAINED_BY_INN = """
WITH gained AS (""" + _GAINED_BASE + """)
SELECT g.inn, count(*) AS n_triples,
       sum(CASE WHEN g.cause IN ('person_new_to_bank', 'returned_to_rgs',
                                 'inn_new') THEN 1 ELSE 0 END) AS n_real
FROM gained g GROUP BY g.inn
"""


# --------------------------------------------------------------------------- #
# Лестница причин: ЛЮДИ
# --------------------------------------------------------------------------- #
#
# Второй разбор, на грейне человека, со своим тождеством:
#     людей_база − потеряно_людей + пришло_людей = людей_отчёт
#
# Веток здесь меньше, и это главное: «схлопнулось совместительство», «сменил
# организацию» и «переведён в другое подразделение» на уровне человека НЕ
# СУЩЕСТВУЮТ — там не потерян никто. Разница между двумя лестницами и есть цена
# методологии счёта, выраженная в людях.
#
# Колонка `n_epk` в лестнице получателей на этот вопрос не отвечает и отвечать не
# может: один человек попадает в несколько её веток, складывать её нельзя.
_LOST_EPK_CASE = """
    CASE
      WHEN se.epk_id IS NULL AND rc.epk_id IS NULL THEN 'left_bank'
      WHEN se.epk_id IS NULL                       THEN 'gap_only'
      WHEN sg.amt_codes > 0                        THEN 'below_threshold'
      WHEN sg.amt_all > 0                          THEN 'code_out_of_list'
      ELSE 'left_rgs'
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
  LEFT JOIN t_recent rc   ON rc.epk_id = b.epk_id
  WHERE b.report_dt = CAST(:d_base AS date) AND c.epk_id IS NULL
"""

_GAINED_EPK_CASE = """
    CASE
      WHEN bsg.amt_codes > 0   THEN 'crossed_threshold'
      WHEN bsg.amt_all > 0     THEN 'code_came_into_list'
      WHEN bse.epk_id IS NULL  THEN 'person_new_to_bank'
      ELSE 'returned_to_rgs'
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

# Стаж ушедших: сколько месяцев из всего ряда человек был в сегменте до ухода.
#
# Ответ, который она даёт: уходят недавно пришедшие (ротация, сезонники) или
# старожилы (потеря ядра). Это разные диагнозы с разными решениями, и по одному
# числу «ушло N человек» они неразличимы.
#
# Возвращаются КОРЗИНЫ, а не люди: список из полутора миллионов строк в тетрадку
# не поедет, а корзины отвечают на вопрос целиком.
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


# --------------------------------------------------------------------------- #
# Итоги, разрезы, ряды
# --------------------------------------------------------------------------- #

# Итоги опорных месяцев: те самые числа, которые сравнивают с отчётностью.
# Получатели И люди сразу — разница между ними и есть вклад совместительства.
MONTH_TOTALS = """
SELECT report_dt,
       count(*)                  AS n_triples,
       count(DISTINCT epk_id)    AS n_epk,
       count(DISTINCT inn)       AS n_inn,
       sum(amt)                  AS amt
FROM t_pairs GROUP BY report_dt ORDER BY report_dt
"""

# Атрибуты бюджетных организаций — для разрезов. Отдельным запросом, а не джойном
# в каждый разрез: справочник маленький, а джойн в тяжёлый запрос стоит дорого и
# рискует задвоить строки.
SEG_ATTRS = """
SELECT * FROM t_seg
"""

# Помесячный ряд бюджетной сферы. Отвечает на «КОГДА»: обрыв в одном месяце — это
# событие, плавное снижение — текучесть. По двум точкам года они неразличимы.
#
# Отношение получателей к людям — коэффициент совместительства. Его падение и
# есть та часть минуса, где не потерян ни один человек.
MONTHLY = """
WITH pairs AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn, p.gosb_id,
         sum(p.amt) AS amt,
         sum(sum(p.amt)) OVER (PARTITION BY p.report_dt, p.epk_id,
                                            CAST(p.inn AS bigint)) AS amt_inn
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt >= CAST(:d_from AS date)
    AND p.report_dt <= CAST(:d_to AS date)
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.gosb_id
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

# Полнота загрузки ПО ВСЕМУ БАНКУ, без фильтра сегмента и порога. Месяц с
# аномально низким числом строк — недогруженная партиция, и любой вывод по нему
# будет выводом про загрузку, а не про людей. Проверяется ДО объяснений.
#
# Только счёт строк и сумма: `count(DISTINCT epk_id)` по всему банку за 25 месяцев
# — сортировка миллиардов значений ради числа, которого нет ни в одном разделе.
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

# Кривая дожития когорты базового месяца: сколько её получателей живо в каждом
# следующем месяце. Когорта берётся из рабочего набора, ряд — из витрины.
SURVIVAL = """
WITH cohort AS (
  SELECT epk_id, inn, gosb_id FROM t_pairs
  WHERE report_dt = CAST(:d_base AS date)
),
pairs AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn, p.gosb_id,
         sum(p.amt) AS amt,
         sum(sum(p.amt)) OVER (PARTITION BY p.report_dt, p.epk_id,
                                            CAST(p.inn AS bigint)) AS amt_inn
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt >= CAST(:d_base AS date)
    AND p.report_dt <= CAST(:d_cur AS date)
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.gosb_id
)
SELECT x.report_dt, count(*) AS n_alive, count(DISTINCT x.epk_id) AS n_epk_alive
FROM pairs x JOIN cohort c ON c.epk_id = x.epk_id AND c.inn = x.inn
                          AND c.gosb_id = x.gosb_id
WHERE {amt_cond}
GROUP BY x.report_dt
ORDER BY x.report_dt
"""

# Чувствительность к порогу получателя. Порог фиксирован, а зарплаты индексируются
# — сам по себе он должен год к году ДОБАВЛЯТЬ получателей, а не убавлять. Если
# падение сохраняется при пороге 0, порог ни при чём; если исчезает — объяснение
# найдено. Проверяется в лоб, а не рассуждением.
THRESHOLD_SENS = """
WITH pairs AS (
  SELECT p.report_dt, p.epk_id, CAST(p.inn AS bigint) AS inn, p.gosb_id,
         sum(sum(p.amt)) OVER (PARTITION BY p.report_dt, p.epk_id,
                                            CAST(p.inn AS bigint)) AS amt_inn
  FROM {schema}.uzp_data_payroll_m p
  WHERE p.report_dt = ANY(CAST(:months AS date[]))
    AND p.{code_col} = ANY(:codes)
    AND p.epk_id IS NOT NULL
    AND """ + INN_OK + """
  GROUP BY p.report_dt, p.epk_id, CAST(p.inn AS bigint), p.gosb_id
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

# Зарплатные коды против всех остальных.
#
# Перечень кодов по одному бесполезен: его верх занимают массовые социальные
# коды, которых метрика и не считала, и читатель делает из них вывод про метрику
# — ровно эта ошибка и случилась при первом прогоне. Значение имеет ОДНО
# отношение: держится ли объём зарплатных кодов и какова его доля. Если она
# стабильна, перемены среди прочих кодов метрику не задевают по построению.
CODE_SPLIT = """
SELECT p.report_dt,
       CASE WHEN p.{code_col} = ANY(:codes) THEN 'in' ELSE 'out' END AS grp,
       count(DISTINCT p.epk_id) AS n_epk,
       count(*)                 AS n_rows,
       sum(p.amt)               AS amt
FROM {schema}.uzp_data_payroll_m p
JOIN t_seg s ON s.inn = CAST(p.inn AS bigint)
WHERE p.report_dt = ANY(CAST(:months AS date[]))
  AND """ + INN_OK + """
GROUP BY p.report_dt, 2
ORDER BY p.report_dt, 2
"""

# Коды по отдельности — СПРАВКА, а не объяснение. Нужна затем, чтобы увидеть коды,
# исчезнувшие целиком: это признак смены кодировки в витрине. На метрику коды вне
# списка не влияют по построению, и отчёт обязан говорить это прямо.
CODE_MIX = """
SELECT p.report_dt,
       p.{code_col}                          AS code,
       min(p.enrollment_transcription)       AS code_name,
       count(DISTINCT p.epk_id)              AS n_epk,
       sum(p.amt)                            AS amt
FROM {schema}.uzp_data_payroll_m p
JOIN t_seg s ON s.inn = CAST(p.inn AS bigint)
WHERE p.report_dt = ANY(CAST(:months AS date[]))
  AND """ + INN_OK + """
GROUP BY p.report_dt, p.{code_col}
ORDER BY p.report_dt, p.{code_col}
"""

# Миграция ИНН: куда переехали люди, потерявшие свою организацию.
#
# Единственная проверка, отличающая переоформление от оттока: если заметная доля
# людей одного ИНН оказалась в одном и том же новом ИНН — организацию
# переоформили, люди никуда не уходили. Ни один агрегат по холдингам этого не
# покажет, потому что новый ИНН к старому холдингу ещё не привязан.
#
# Приёмник ищется по ВСЕЙ витрине отчётного месяца, без фильтра сегмента: при
# переоформлении новый ИНН часто ещё не размечен как бюджетный.
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
WHERE b2.epk_id IS NULL                      -- приёмник НОВЫЙ для этого человека
GROUP BY l.inn_from, t.inn
HAVING count(DISTINCT l.epk_id) >= :min_movers
ORDER BY count(DISTINCT l.epk_id) DESC
"""

# Территория: ТБ берётся ПРЯМО ИЗ ВЕДОМОСТЕЙ (`tb_id`), а не выводится через
# справочник ГОСБ. На проме первый прогон показал, что `gosb_id` ведомостей со
# справочником не сошёлся вовсе, и весь территориальный разрез схлопнулся в одну
# строку «ТБ неизвестен». В ведомостях `tb_id` заполнен на 100% и принимает
# двенадцать значений — этого достаточно, чтобы разрез был, даже когда ГОСБ не
# опознан.
TB_DIM = """
SELECT d.tb_id, min(d.tb_short_name) AS tb_short_name
FROM {schema}.uzp_dim_gosb d
WHERE """ + Q._NO_CA + """
GROUP BY d.tb_id
"""

# Справочник ГОСБ. Каким ключом он джойнится с ведомостями, решает разведка:
# `old_gosb_id` и `new_gosb_id` — разные колонки с разной мощностью, и угадывать
# нельзя. Запрос отдаёт оба, выбор делается по покрытию.
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

# Сколько ГОСБ ведомостей опознаётся каждым ключом справочника. Разведка выбирает
# тот, что покрывает больше; не покрыл ни один — территориальный разрез честно
# отключается, а не показывает заглушку под видом подразделения.
GOSB_MATCH = """
WITH used AS (SELECT DISTINCT gosb_id FROM t_pairs),
dim AS (
  SELECT old_gosb_id, new_gosb_id FROM {schema}.uzp_dim_gosb
  WHERE """ + Q._NO_CA + """
)
SELECT count(*)                                                      AS n_used,
       count(*) FILTER (WHERE u.gosb_id IN (SELECT old_gosb_id FROM dim)) AS n_old,
       count(*) FILTER (WHERE u.gosb_id IN (SELECT new_gosb_id FROM dim)) AS n_new
FROM used u
"""
