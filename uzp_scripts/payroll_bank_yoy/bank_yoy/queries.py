"""Весь SQL отчёта. Ни одной строки SQL за пределами этого файла.

Плейсхолдеры `{schema}` и `{code_col}` подставляет `db.render` заменой по имени;
значения — именованными параметрами `:name`.

ПЯТЬ ЛОВУШЕК, на которых такой разбор ломается тихо
---------------------------------------------------

1. **Партиция.** `uzp_data_payroll_m` партиционирована по `report_dt`. Запрос без
   `report_dt` в WHERE читает всю историю всего банка — на проме это «никогда».
   Каждое обращение к витрине ниже ограничено ОДНИМ месяцем (`= CAST(:m AS date)`),
   и это проверяет самопроверка `check_partition_filter`.
2. **Тип ИНН.** В ведомостях `inn` — text длиной 1–12, в справочнике ЕПК — bigint.
   `CAST` на значении «ИНН123» роняет ВЕСЬ запрос, поэтому приведение — только под
   маской `INN_OK`, а в условии соединения — внутри `CASE`.
3. **Диалект 9.4.** Никаких `make_interval(months => n)`, `ON CONFLICT`, `GROUPING
   SETS`. Предыдущий месяц передаётся параметром, а не вычисляется в SQL.
4. **Список кодов** уезжает ОДНИМ параметром-массивом и сравнивается `= ANY(:codes)`.
   `IN :codes` через `text()` подставил бы кортеж одним значением.
5. **NULL в ключе тройки.** `sys_gosb_id` бывает пуст; NULL не равен NULL, и
   тройка с пустым ГОСБ «исчезала» бы каждый месяц. Поэтому `COALESCE(..., -1)`.

Грейн и порог
-------------
Получатель — ТРОЙКА (epk_id, ИНН, ГОСБ). Засчитывается, если сумма по зарплатным
кодам за месяц В ИНН (не в тройке!) больше порога. Порог и ключ счёта живут на
разных грейнах, поэтому сумма по ИНН — оконная функция поверх группировки по
тройке: HAVING умеет фильтровать только свою группу.
"""
from __future__ import annotations

from . import segments as S

CODES = (1, 2, 16, 18, 19, 26, 28, 33, 38, 39, 40, 42, 49, 82, 87, 88, 94, 95)
AMT_MIN = 2500
THRESHOLDS = (0, 1000, 5000, 10000)     # плюс AMT_MIN — чувствительность к порогу

INN_OK = "p.inn ~ '^[0-9]{1,12}$'"
INN_OK_R = "r.inn ~ '^[0-9]{1,12}$'"


# --------------------------------------------------------------------------- #
# Разведка
# --------------------------------------------------------------------------- #
PROBE_COLUMNS = """
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = :schema AND table_name = :table
"""

PROBE_EPK = """
SELECT count(*)                                                AS n_rows,
       count(DISTINCT e.inn)                                   AS n_inn,
       count(*) FILTER (WHERE COALESCE(e.segment_name, '') = '') AS n_no_seg,
       max(e.modified_dttm)                                    AS snapshot
FROM {schema}.uzp_data_epk_consolidation e
WHERE e.inn IS NOT NULL
"""

PROBE_TEMP = "CREATE TEMP TABLE t_probe_tmp AS SELECT 1 AS x"
DROP_PROBE_TEMP = "DROP TABLE IF EXISTS t_probe_tmp"


# --------------------------------------------------------------------------- #
# Рабочий набор. Строится ПОМЕСЯЧНО: CREATE по первому месяцу, INSERT по
# остальным. Один оператор = одна партиция, и каждый укладывается в
# statement_timeout даже на объёме всего банка.
# --------------------------------------------------------------------------- #

# ЕДИНСТВЕННОЕ обращение к ведомостям во всём отчёте. Узкая копия витрины за все
# нужные месяцы (ряд, месяцы набора, месяц после отчётного) — одна временная
# таблица, заполняется оператором на партицию (каждый — в statement_timeout).
# Всё остальное — полнота, ряд, коды, тройки, присутствие ФЛ — считается из неё.
# Название кода берётся только у зарплатных кодов: длинный текст по каждой
# строке всего банка таблица не тащит.
T_RAW_MONTH = """
SELECT p.report_dt,
       p.epk_id,
       p.inn,
       p.sys_gosb_id,
       p.sys_tb_id,
       p.{code_col}                                    AS code,
       CASE WHEN p.{code_col} = ANY(:codes)
            THEN p.enrollment_transcription END        AS code_name,
       p.amt
FROM {schema}.uzp_data_payroll_m p
WHERE p.report_dt = CAST(:m AS date)
"""

# Полнота загрузки и пригодность номеров организаций — по копии, все месяцы сразу.
# Месяц без строк в копии — месяца нет в витрине.
LOAD_FROM_RAW = """
SELECT r.report_dt,
       count(*)                                     AS n_rows,
       count(*) FILTER (WHERE """ + INN_OK_R + """) AS n_inn_ok,
       count(*) FILTER (WHERE r.epk_id IS NULL)     AS n_no_epk,
       sum(r.amt)                                   AS amt
FROM t_raw r
GROUP BY r.report_dt
ORDER BY r.report_dt
"""

# Организации справочника ЕПК, свёрнутые до ИНН. Свёртка обязательна: у одного ИНН
# бывает несколько ЕПК, и соединение строкой справочника задвоило бы ВЕДОМОСТИ.
# Ликвидация — «нет НИ ОДНОЙ активной записи», одна мёртвая строка при живой
# соседней ничего не значит. Сегмент при нескольких записях — min() по короткому
# имени: детерминированно, и расхождение печатается разведкой.
T_ORG = """
SELECT e.inn,
       min(""" + S.seg_case("e.segment_name") + """)            AS seg,
       count(DISTINCT """ + S.seg_case("e.segment_name") + """) AS n_seg,
       min(e.company_name)                                       AS company_name,
       min(e.holding_name)                                       AS holding_name,
       min(e.industry_name)                                      AS industry_name,
       NOT bool_or(COALESCE(e.status_name, '') = 'Активна')      AS is_liquidated
FROM {schema}.uzp_data_epk_consolidation e
WHERE e.inn IS NOT NULL
GROUP BY e.inn
"""

# ВСЕ зарплатные тройки ОДНОГО месяца с суммой по ИНН — без порога, из копии
# витрины. Из неё — и тройки-получатели (`t_pairs`, порог), и строка ряда по
# сегментам при всех порогах (`SERIES_FROM_STAGE`).
# Сегмент денормализуется сразу: дальше он нужен в каждом запросе. ИНН вне
# справочника НЕ отбрасывается: банк — это все получатели, такие ИНН идут
# строкой «Не в справочнике».
T_STAGE_MONTH = """
SELECT x.report_dt, x.epk_id, x.inn, x.gosb_id, x.tb_id, x.amt, x.amt_inn,
       COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg
FROM (
  SELECT r.report_dt,
         r.epk_id,
         CAST(r.inn AS bigint)              AS inn,
         COALESCE(r.sys_gosb_id, -1)        AS gosb_id,
         min(r.sys_tb_id)                   AS tb_id,
         sum(r.amt)                         AS amt,
         sum(sum(r.amt)) OVER (PARTITION BY r.report_dt, r.epk_id,
                                            CAST(r.inn AS bigint)) AS amt_inn
  FROM t_raw r
  WHERE r.report_dt = CAST(:m AS date)
    AND r.code = ANY(:codes)
    AND r.epk_id IS NOT NULL
    AND """ + INN_OK_R + """
  GROUP BY r.report_dt, r.epk_id, CAST(r.inn AS bigint), COALESCE(r.sys_gosb_id, -1)
) x
LEFT JOIN t_org o ON o.inn = x.inn
"""

# Получатели: тройки месяца выше порога по ИНН.
PAIRS_FROM_STAGE = """
SELECT report_dt, epk_id, inn, gosb_id, tb_id, amt, amt_inn, seg
FROM t_stage
WHERE amt_inn > :amt_min
"""

# То же одним запросом по копии — для ПОКАЗА читателю (определение одно).
T_PAIRS_MONTH = "SELECT s.* FROM (" + T_STAGE_MONTH + ") s\nWHERE s.amt_inn > :amt_min\n"

# ФЛ-месяц поверх троек: сколько ИНН и троек у человека и его ОСНОВНОЙ сегмент —
# сегмент ИНН с наибольшей суммой. Основной сегмент один на человека, поэтому
# численность ФЛ по сегментам складывается в банк, а матрица перетоков замкнута.
# count(DISTINCT) в оконной функции на 9.4 нет — отсюда два подзапроса.
T_EPK = """
SELECT a.report_dt, a.epk_id, a.n_inn, a.n_triples,
       m.seg AS main_seg, m.tb_id AS main_tb
FROM (
  SELECT report_dt, epk_id, count(DISTINCT inn) AS n_inn, count(*) AS n_triples
  FROM t_pairs GROUP BY report_dt, epk_id
) a
JOIN (
  SELECT report_dt, epk_id, seg, tb_id,
         row_number() OVER (PARTITION BY report_dt, epk_id
                            ORDER BY amt_inn DESC, inn, gosb_id) AS rn
  FROM t_pairs
) m ON m.report_dt = a.report_dt AND m.epk_id = a.epk_id AND m.rn = 1
"""

# В каких сегментах человек получает в месяце (для лестницы по сегменту:
# «остался в своём сегменте» против «ушёл в другой»).
T_EPK_SEG = """
SELECT DISTINCT report_dt, epk_id, seg FROM t_pairs
"""

# Все ФЛ, бывшие получателями хоть в одном месяце набора. Только про них разбор
# и спрашивает; витрина за месяц — это клиенты всего банка, включая пенсионеров.
T_KEYS = """
SELECT DISTINCT epk_id FROM t_epk
"""

# Присутствие ФЛ в ведомостях БЕЗ фильтра кода и порога — за ОДИН месяц. Без него
# не отличить «нет зачислений в банке» от «зарплата ниже порога» и «только
# незарплатные выплаты» — три разных диагноза.
# Зарплатная сумма берётся только по пригодным ИНН: зарплата на ИНН, который не
# сопоставить, получателя не делает (это видно в полноте загрузки).
T_PERSON_MONTH = """
SELECT r.report_dt,
       r.epk_id,
       sum(r.amt) AS amt_all,
       sum(CASE WHEN r.code = ANY(:codes) AND """ + INN_OK_R + """
                THEN r.amt ELSE 0 END) AS amt_codes
FROM t_raw r
JOIN t_keys k ON k.epk_id = r.epk_id
WHERE r.report_dt = CAST(:m AS date)
  AND r.epk_id IS NOT NULL
GROUP BY r.report_dt, r.epk_id
"""

CREATE_TMP = "CREATE TEMP TABLE {name} AS\n{body}\nDISTRIBUTED BY ({dist})"
# DISTRIBUTED BY — расширение Greenplum; обычный PostgreSQL его не разбирает.
CREATE_TMP_PLAIN = "CREATE TEMP TABLE {name} AS\n{body}"
INSERT_TMP = "INSERT INTO {name}\n{body}"
ANALYZE_TMP = "ANALYZE {name}"
DROP_TMP = "DROP TABLE IF EXISTS {name}"

# Порядок значим: каждая следующая таблица читает предыдущие.
WORKSET_ORDER = ["t_org", "t_raw", "t_pairs", "t_epk", "t_epk_seg", "t_keys", "t_person"]
# Порядок для ПОКАЗА: плюс временные таблицы отдельных шагов (t_tflow).
SHOW_ORDER = ["t_org", "t_raw", "t_stage", "t_pairs", "t_epk", "t_epk_seg", "t_keys",
              "t_person", "t_tflow", "t_oflow"]
DIST = {"t_org": "inn", "t_raw": "epk_id", "t_pairs": "epk_id", "t_epk": "epk_id",
        "t_epk_seg": "epk_id", "t_keys": "epk_id", "t_person": "epk_id",
        "t_tflow": "epk_id", "t_oflow": "epk_id", "t_stage": "epk_id"}

# Определения для ПОКАЗА читателю: запрос у блока должен выполняться как есть, а
# `FROM t_pairs` выполнить негде — таблица жила в чужой сессии. Поэтому к
# показанному запросу приклеиваются эти определения как CTE (по всем месяцам).
_ALL = "report_dt = ANY(CAST(:months AS date[]))"
SHOW_DEFS = {
    "t_org": T_ORG,
    "t_raw": T_RAW_MONTH.replace("p.report_dt = CAST(:m AS date)", "p." + _ALL),
    "t_stage": T_STAGE_MONTH,
    "t_pairs": T_PAIRS_MONTH.replace("r.report_dt = CAST(:m AS date)", "r." + _ALL),
    "t_epk": T_EPK,
    "t_epk_seg": T_EPK_SEG,
    "t_keys": T_KEYS,
    "t_person": T_PERSON_MONTH.replace("r.report_dt = CAST(:m AS date)", "r." + _ALL),
}


# --------------------------------------------------------------------------- #
# Ряд и зарплатные коды — по копии витрины
# --------------------------------------------------------------------------- #

# Численность месяца по сегментам и при нескольких порогах. Строка '__ALL__' —
# банк целиком: ФЛ с работой в двух сегментах в сумме по сегментам посчитался бы
# дважды. Считается по `t_stage` каждого месяца ряда — то есть по копии витрины.
_SERIES_AGG = """
SELECT seg,
       count(*) FILTER (WHERE amt_inn > :amt_min)                AS n_triples,
       count(DISTINCT epk_id) FILTER (WHERE amt_inn > :amt_min)  AS n_epk,
       count(DISTINCT inn) FILTER (WHERE amt_inn > :amt_min)     AS n_inn,
       count(*) FILTER (WHERE amt_inn > 0)                       AS t0,
       count(*) FILTER (WHERE amt_inn > 1000)                    AS t1000,
       count(*) FILTER (WHERE amt_inn > 5000)                    AS t5000,
       count(*) FILTER (WHERE amt_inn > 10000)                   AS t10000
FROM __SRC__ GROUP BY seg
UNION ALL
SELECT '__ALL__',
       count(*) FILTER (WHERE amt_inn > :amt_min),
       count(DISTINCT epk_id) FILTER (WHERE amt_inn > :amt_min),
       count(DISTINCT inn) FILTER (WHERE amt_inn > :amt_min),
       count(*) FILTER (WHERE amt_inn > 0),
       count(*) FILTER (WHERE amt_inn > 1000),
       count(*) FILTER (WHERE amt_inn > 5000),
       count(*) FILTER (WHERE amt_inn > 10000)
FROM __SRC__
"""
SERIES_FROM_STAGE = _SERIES_AGG.replace("__SRC__", "t_stage")

# Зарплатные коды месяца по сегментам: КАКОЙ вид выплаты просел (отпускные,
# премия, аванс). Только коды метрики — коды вне списка на неё не влияют, а в
# таблице заняли бы верх массовыми соцвыплатами (это уже сбивало вывод).
CODE_MONTH = """
SELECT r.report_dt,
       r.code                                AS code,
       min(r.code_name)                      AS code_name,
       COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg,
       count(DISTINCT r.epk_id)              AS n_epk,
       sum(r.amt)                            AS amt
FROM t_raw r
LEFT JOIN t_org o ON o.inn = CASE WHEN """ + INN_OK_R + """ THEN CAST(r.inn AS bigint) END
WHERE r.report_dt = ANY(CAST(:code_months AS date[]))
  AND r.code = ANY(:codes)
  AND r.epk_id IS NOT NULL
GROUP BY r.report_dt, r.code, COALESCE(o.seg, '""" + S.NO_DIM + """')
"""


# --------------------------------------------------------------------------- #
# Разложение изменения между двумя месяцами набора: b (база) → c (сравнение).
# Одни и те же запросы обслуживают и год к году (b = c − 12), и месяц к месяцу
# (b = c − 1): разница разниц собирается из месячных переходов.
#
# Параметры: :b, :c — месяцы; :bp1, :bp2 — один и два месяца до b; :cn1 — месяц
# после c. :has_bp1, :has_bp2, :has_cn1 — есть ли эти месяцы в наборе. Если нет,
# метка — NULL («неизвестно»), а не «новичок»: отсутствие данных не то же самое,
# что отсутствие человека.
# --------------------------------------------------------------------------- #

# Ситуация ФЛ, которого НЕТ среди получателей в месяце X, по присутствию в X.
# Ровно одна, первая сработавшая.
def _gone_case(ps: str) -> str:
    return (f"CASE WHEN {ps}.epk_id IS NULL OR COALESCE({ps}.amt_all, 0) <= 0 "
            f"THEN 'left_bank' "
            f"WHEN {ps}.amt_codes > 0 THEN 'below_threshold' "
            f"ELSE 'other_codes' END")


def _come_case(ps: str) -> str:
    return (f"CASE WHEN {ps}.epk_id IS NULL OR COALESCE({ps}.amt_all, 0) <= 0 "
            f"THEN 'new_to_bank' "
            f"WHEN {ps}.amt_codes > 0 THEN 'above_threshold' "
            f"ELSE 'back_to_codes' END")


# Метка стажа ушедшего: 'new1' — пришёл в месяце b (не было в b−1), 'new2' — пришёл
# в b−1 (был в b−1, не было в b−2), 'old' — был и там, и там.
def _tenure(e1: str, e2: str) -> str:
    return (f"CASE WHEN NOT CAST(:has_bp1 AS boolean) THEN NULL "
            f"WHEN {e1}.epk_id IS NULL THEN 'new1' "
            f"WHEN NOT CAST(:has_bp2 AS boolean) THEN NULL "
            f"WHEN {e2}.epk_id IS NULL THEN 'new2' ELSE 'old' END")


def _flag(alias: str, has: str) -> str:
    return (f"CASE WHEN NOT CAST(:{has} AS boolean) THEN NULL "
            f"WHEN {alias}.epk_id IS NULL THEN 0 ELSE 1 END")


# ПО ФЛ. Полное внешнее соединение получателей двух месяцев: каждый ФЛ — ровно
# одна строка «откуда (основной сегмент в b) → куда (основной сегмент в c)».
# Отсюда сразу:
#   * матрица перетоков между сегментами (замкнута: сумма строк = было, столбцов = стало);
#   * разложение по ФЛ: перестали / начали / продолжают;
#   * совместительство: Σ(троек в c − троек в b) у продолжающих = третья строка
#     разложения по получателям, а та же разность по ИНН и ГОСБ делит его на
#     «меньше организаций» и «меньше ГОСБ в одной организации».
FL_FLOW = """
WITH b AS (SELECT * FROM t_epk WHERE report_dt = CAST(:b AS date)),
     c AS (SELECT * FROM t_epk WHERE report_dt = CAST(:c AS date)),
     f AS (
  SELECT COALESCE(b.epk_id, c.epk_id) AS epk_id,
         b.main_seg AS seg_from, c.main_seg AS seg_to,
         b.n_inn AS inn_b, b.n_triples AS tr_b,
         c.n_inn AS inn_c, c.n_triples AS tr_c
  FROM b FULL OUTER JOIN c ON c.epk_id = b.epk_id
)
SELECT COALESCE(f.seg_from, '—') AS seg_from,
       COALESCE(f.seg_to, '—')   AS seg_to,
       CASE WHEN f.seg_from IS NULL THEN """ + _come_case("pb") + """
            WHEN f.seg_to IS NULL   THEN """ + _gone_case("pc") + """
            ELSE 'stay' END        AS cause,
       CASE WHEN f.seg_from IS NULL THEN NULL
            ELSE """ + _tenure("e1", "e2") + """ END AS tenure,
       CASE WHEN f.seg_from IS NULL THEN """ + _flag("e1", "has_bp1") + """
            ELSE NULL END          AS was_bp1,
       """ + _flag("n1", "has_cn1") + """ AS in_cn1,
       count(*)                    AS n_epk,
       COALESCE(sum(f.tr_b), 0)    AS tr_b,
       COALESCE(sum(f.tr_c), 0)    AS tr_c,
       COALESCE(sum(f.inn_b), 0)   AS inn_b,
       COALESCE(sum(f.inn_c), 0)   AS inn_c
FROM f
LEFT JOIN t_person pc ON pc.report_dt = CAST(:c AS date) AND pc.epk_id = f.epk_id
LEFT JOIN t_person pb ON pb.report_dt = CAST(:b AS date) AND pb.epk_id = f.epk_id
LEFT JOIN t_epk e1 ON e1.report_dt = CAST(:bp1 AS date) AND e1.epk_id = f.epk_id
LEFT JOIN t_epk e2 ON e2.report_dt = CAST(:bp2 AS date) AND e2.epk_id = f.epk_id
LEFT JOIN t_epk n1 ON n1.report_dt = CAST(:cn1 AS date) AND n1.epk_id = f.epk_id
GROUP BY 1, 2, 3, 4, 5, 6
"""

# ПО ПОЛУЧАТЕЛЯМ (тройкам), по сегменту ТРОЙКИ. Два шага.
#
# Шаг 1 — `t_tflow`: исчезнувшие (side='lost') и появившиеся (side='gained')
# тройки перехода b → c, временной таблицей со статистикой. Отдельный шаг — не
# украшение: тройки двух месяцев почти совпадают, и планировщик оценивает
# анти-соединение в ОДНУ строку, после чего строит вложенные циклы по остальным
# соединениям. На синтетике это 370 секунд вместо одной; на объёме банка — никогда.
# С материализованным набором и ANALYZE оценка честная.
T_TFLOW = """
SELECT 'lost' AS side, b.seg, b.epk_id
FROM (SELECT * FROM t_pairs WHERE report_dt = CAST(:b AS date)) b
LEFT JOIN (SELECT * FROM t_pairs WHERE report_dt = CAST(:c AS date)) c
       ON c.epk_id = b.epk_id AND c.inn = b.inn AND c.gosb_id = b.gosb_id
WHERE c.epk_id IS NULL
UNION ALL
SELECT 'gained', c.seg, c.epk_id
FROM (SELECT * FROM t_pairs WHERE report_dt = CAST(:c AS date)) c
LEFT JOIN (SELECT * FROM t_pairs WHERE report_dt = CAST(:b AS date)) b
       ON b.epk_id = c.epk_id AND b.inn = c.inn AND b.gosb_id = c.gosb_id
WHERE b.epk_id IS NULL
"""

# Шаг 2 — ситуация каждой тройки, ровно одна:
#   inside      — ФЛ в другом месяце получает в ЭТОМ ЖЕ сегменте (сменил ИНН/ГОСБ
#                 или стал получать в меньшем/большем числе мест);
#   other_seg   — ФЛ получает в банке, но только в ДРУГИХ сегментах;
#   left_bank / below_threshold / other_codes (и зеркальные для появившихся) —
#                 ФЛ не получатель нигде.
# «Другой месяц» для исчезнувших — c, для появившихся — b. Сумма по сегментам =
# банк; на уровне банка inside + other_seg = совместительство.
TRIPLE_FLOW = """
SELECT f.side, f.seg,
       CASE WHEN os.epk_id IS NOT NULL THEN 'inside'
            WHEN oe.epk_id IS NOT NULL THEN 'other_seg'
            WHEN f.side = 'lost' THEN """ + _gone_case("ps") + """
            ELSE """ + _come_case("ps") + """ END AS cause,
       CASE WHEN f.side = 'lost' THEN """ + _tenure("e1", "e2") + """ END AS tenure,
       """ + _flag("n1", "has_cn1") + """ AS in_cn1,
       count(*) AS n_triples, count(DISTINCT f.epk_id) AS n_epk
FROM t_tflow f
LEFT JOIN t_epk_seg os ON os.epk_id = f.epk_id AND os.seg = f.seg
     AND os.report_dt = CASE WHEN f.side = 'lost' THEN CAST(:c AS date) ELSE CAST(:b AS date) END
LEFT JOIN t_epk oe ON oe.epk_id = f.epk_id
     AND oe.report_dt = CASE WHEN f.side = 'lost' THEN CAST(:c AS date) ELSE CAST(:b AS date) END
LEFT JOIN t_person ps ON ps.epk_id = f.epk_id
     AND ps.report_dt = CASE WHEN f.side = 'lost' THEN CAST(:c AS date) ELSE CAST(:b AS date) END
LEFT JOIN t_epk e1 ON e1.report_dt = CAST(:bp1 AS date) AND e1.epk_id = f.epk_id
LEFT JOIN t_epk e2 ON e2.report_dt = CAST(:bp2 AS date) AND e2.epk_id = f.epk_id
LEFT JOIN t_epk n1 ON n1.report_dt = CAST(:cn1 AS date) AND n1.epk_id = f.epk_id
GROUP BY 1, 2, 3, 4, 5
"""


# --------------------------------------------------------------------------- #
# Итоги месяцев набора: банк, сегменты, совместительство, территория
# --------------------------------------------------------------------------- #

# Совместительство по сегментам: «лишние» получатели = тройки − ФЛ. Делятся на
# несколько ИНН у ФЛ (Σ(n_inn − 1)) и одну организацию через несколько ГОСБ
# (Σ(n_triples − n_inn)). Второе — чистый эффект счёта: человек ничего не менял.
MULTI_SEG = """
SELECT report_dt, seg,
       count(*)               AS n_epk,
       sum(n_tr)              AS n_triples,
       sum(n_inn)             AS n_pairs,
       sum(n_inn - 1)         AS extra_inn,
       sum(n_tr - n_inn)      AS extra_gosb
FROM (
  SELECT report_dt, seg, epk_id, count(DISTINCT inn) AS n_inn, count(*) AS n_tr
  FROM t_pairs GROUP BY report_dt, seg, epk_id
) x
GROUP BY report_dt, seg
"""

# То же по банку: ФЛ с работой в двух сегментах даёт ещё и «межсегментных» лишних.
MULTI_BANK = """
SELECT report_dt,
       count(*)                                   AS n_epk,
       sum(n_triples)                             AS n_triples,
       sum(n_inn)                                 AS n_pairs,
       sum(n_inn - 1)                             AS extra_inn,
       sum(n_triples - n_inn)                     AS extra_gosb,
       count(*) FILTER (WHERE n_inn = 1)          AS epk_inn1,
       count(*) FILTER (WHERE n_inn = 2)          AS epk_inn2,
       count(*) FILTER (WHERE n_inn >= 3)         AS epk_inn3,
       count(*) FILTER (WHERE n_triples > n_inn)  AS epk_multi_gosb
FROM t_epk
GROUP BY report_dt
"""

TB_SEG = """
SELECT report_dt, tb_id, seg, count(*) AS n_triples
FROM t_pairs
GROUP BY report_dt, tb_id, seg
"""

TB_DIM = """
SELECT d.tb_id, min(d.tb_short_name) AS tb_short_name
FROM {schema}.uzp_dim_gosb d
WHERE d.tb_id IS NOT NULL
GROUP BY d.tb_id
"""


# --------------------------------------------------------------------------- #
# Когорты, сезонность
# --------------------------------------------------------------------------- #

# Когорта пришедших: ФЛ — получатели в :k и НЕ получатели в :kp. Сколько из них
# остаются получателями в каждом следующем месяце набора. По основному сегменту
# в месяце прихода. kp = k−1 — пришедшие за месяц; kp = k−12 — новые год к году.
COHORT = """
WITH k AS (
  SELECT c.epk_id, c.main_seg AS seg
  FROM t_epk c
  LEFT JOIN t_epk p ON p.report_dt = CAST(:kp AS date) AND p.epk_id = c.epk_id
  WHERE c.report_dt = CAST(:k AS date) AND p.epk_id IS NULL
)
SELECT k.seg, j.report_dt, count(*) AS n_alive
FROM k JOIN t_epk j ON j.epk_id = k.epk_id AND j.report_dt >= CAST(:k AS date)
GROUP BY k.seg, j.report_dt
"""

# Сезонность — только при ПОВТОРЕ. «Получал в июле, не получает в августе» ещё не
# сезон: доказать, что человек вернётся, нечем. Сезонным поведение становится,
# когда повторяется: получал в предыдущем месяце ОБОИХ лет и не получал в отчётном
# ОБОИХ лет. И отдельно — вернулся ли в следующем месяце (если он загружен).
SEASONAL = """
WITH bp AS (SELECT epk_id, main_seg FROM t_epk WHERE report_dt = CAST(:b_prev AS date)),
     bm AS (SELECT epk_id FROM t_epk WHERE report_dt = CAST(:b AS date)),
     bn AS (SELECT epk_id FROM t_epk WHERE report_dt = CAST(:b_next AS date)),
     cp AS (SELECT epk_id FROM t_epk WHERE report_dt = CAST(:c_prev AS date)),
     cm AS (SELECT epk_id FROM t_epk WHERE report_dt = CAST(:c AS date)),
     cn AS (SELECT epk_id FROM t_epk WHERE report_dt = CAST(:c_next AS date))
SELECT bp.main_seg AS seg,
       count(*)                                                   AS n_prev_both,
       count(*) FILTER (WHERE bm.epk_id IS NULL)                  AS n_gone_base,
       count(*) FILTER (WHERE cm.epk_id IS NULL)                  AS n_gone_cur,
       count(*) FILTER (WHERE bm.epk_id IS NULL AND cm.epk_id IS NULL) AS n_seasonal,
       count(*) FILTER (WHERE bm.epk_id IS NULL AND bn.epk_id IS NOT NULL) AS n_back_base,
       count(*) FILTER (WHERE cm.epk_id IS NULL AND cn.epk_id IS NOT NULL) AS n_back_cur
FROM bp
JOIN cp ON cp.epk_id = bp.epk_id
LEFT JOIN bm ON bm.epk_id = bp.epk_id
LEFT JOIN cm ON cm.epk_id = bp.epk_id
LEFT JOIN bn ON bn.epk_id = bp.epk_id
LEFT JOIN cn ON cn.epk_id = bp.epk_id
GROUP BY bp.main_seg
"""


# --------------------------------------------------------------------------- #
# Организации: где численность СОКРАТИЛАСЬ на самом деле
# --------------------------------------------------------------------------- #
#
# Грейн — ФЛ внутри ИНН (пара epk × ИНН): перевод между ГОСБ одной организации
# сюда не попадает по построению. Сравнение b → c (год к году).
#
# Ушедшие из ИНН (были в b, нет в c) — ровно одна ситуация:
#   reorg      — ушёл в ИНН-приёмник: туда переехало не меньше :reorg_min_movers
#                ФЛ и не меньше :reorg_min_share ушедших этого ИНН. Переоформление,
#                а не сокращение — исключается;
#   moved      — получает ЗП в банке в другой организации: ПЕРЕТОК. Часть снижения
#                организации, но не потеря для Сбера — отдельная колонка;
#   back       — в c не получатель нигде, но снова получатель в c+1 (если загружен):
#                пропуск месяца (перенос выплаты, отпуск), а не уход — отдельная
#                колонка, в сокращение не идёт. Иначе организация, заплатившая за
#                август в сентябре, выглядела бы «ушедшей из Сбера целиком»;
#   left_bank / below_threshold / other_codes — ПЕРЕСТАЛ получать ЗП в Сбере.
#                Это и есть основная метрика.
# Пришедшие (нет в b, есть в c): reorg (из ИНН-предшественника), moved (был
# получателем в банке), new (не был).
#
# Отбор (в SQL, в ядро едет только список):
#   нетто без реорганизации и пропуска месяца < 0 — численность действительно
#                                      упала: «5 ушли, 5 пришли» сюда не попадает;
#   реальное = min(перестали в Сбере, −нетто без реорганизации) — какая часть
#                                      падения объяснена уходом из Сбера;
#   база ≥ :min_base, реальное ≥ :min_real и ≥ :min_share · база.
#
# Ушедшие и пришедшие пары материализуются ОТДЕЛЬНЫМ шагом (`t_oflow`) — по той же
# причине, что и `t_tflow`: анти-соединение почти совпадающих месяцев планировщик
# оценивает в одну строку.
T_OFLOW = """
SELECT 'lv' AS side, b.epk_id, b.inn
FROM (SELECT DISTINCT epk_id, inn FROM t_pairs WHERE report_dt = CAST(:b AS date)) b
LEFT JOIN (SELECT DISTINCT epk_id, inn FROM t_pairs WHERE report_dt = CAST(:c AS date)) c
       ON c.epk_id = b.epk_id AND c.inn = b.inn
WHERE c.epk_id IS NULL
UNION ALL
SELECT 'jn', c.epk_id, c.inn
FROM (SELECT DISTINCT epk_id, inn FROM t_pairs WHERE report_dt = CAST(:c AS date)) c
LEFT JOIN (SELECT DISTINCT epk_id, inn FROM t_pairs WHERE report_dt = CAST(:b AS date)) b
       ON b.epk_id = c.epk_id AND b.inn = c.inn
WHERE b.epk_id IS NULL
"""

_ORG_HEAD = """
WITH bp AS (SELECT DISTINCT epk_id, inn FROM t_pairs WHERE report_dt = CAST(:b AS date)),
cp AS (SELECT DISTINCT epk_id, inn FROM t_pairs WHERE report_dt = CAST(:c AS date)),
lv AS (SELECT epk_id, inn FROM t_oflow WHERE side = 'lv'),
jn AS (SELECT epk_id, inn FROM t_oflow WHERE side = 'jn'),
mv AS (
  SELECT lv.inn AS inn_from, cp.inn AS inn_to, lv.epk_id
  FROM lv JOIN cp ON cp.epk_id = lv.epk_id
),
lvn AS (SELECT inn, count(*) AS n_lv FROM lv GROUP BY inn),
pairs_mv AS (
  SELECT inn_from, inn_to, count(DISTINCT epk_id) AS n_mv FROM mv GROUP BY inn_from, inn_to
),
succ AS (
  SELECT m.inn_from, m.inn_to, m.n_mv, l.n_lv
  FROM pairs_mv m JOIN lvn l ON l.inn = m.inn_from
  WHERE m.n_mv >= :reorg_min_movers AND m.n_mv >= :reorg_min_share * l.n_lv
),
reorg_out AS (
  SELECT DISTINCT mv.inn_from AS inn, mv.epk_id
  FROM mv JOIN succ s ON s.inn_from = mv.inn_from AND s.inn_to = mv.inn_to
),
reorg_in AS (
  SELECT DISTINCT mv.inn_to AS inn, mv.epk_id
  FROM mv JOIN succ s ON s.inn_from = mv.inn_from AND s.inn_to = mv.inn_to
),
moved_out AS (SELECT DISTINCT inn_from AS inn, epk_id FROM mv),
lv_cls AS (
  SELECT lv.inn,
         CASE WHEN r.epk_id IS NOT NULL THEN 'reorg'
              WHEN m.epk_id IS NOT NULL THEN 'moved'
              WHEN CAST(:has_cn1 AS boolean) AND nx.epk_id IS NOT NULL THEN 'back'
              ELSE """ + _gone_case("ps") + """ END AS cls
  FROM lv
  LEFT JOIN reorg_out r ON r.inn = lv.inn AND r.epk_id = lv.epk_id
  LEFT JOIN moved_out m ON m.inn = lv.inn AND m.epk_id = lv.epk_id
  LEFT JOIN t_epk nx ON nx.report_dt = CAST(:cn1 AS date) AND nx.epk_id = lv.epk_id
  LEFT JOIN t_person ps ON ps.report_dt = CAST(:c AS date) AND ps.epk_id = lv.epk_id
),
jn_cls AS (
  SELECT jn.inn,
         CASE WHEN r.epk_id IS NOT NULL THEN 'reorg'
              WHEN e.epk_id IS NOT NULL THEN 'moved'
              ELSE 'new' END AS cls
  FROM jn
  LEFT JOIN reorg_in r ON r.inn = jn.inn AND r.epk_id = jn.epk_id
  LEFT JOIN t_epk e ON e.report_dt = CAST(:b AS date) AND e.epk_id = jn.epk_id
),
base AS (SELECT inn, count(*) AS base_fl FROM bp GROUP BY inn),
cur AS (SELECT inn, count(*) AS cur_fl FROM cp GROUP BY inn),
lo AS (
  SELECT inn,
         count(*) FILTER (WHERE cls = 'left_bank')       AS out_left_bank,
         count(*) FILTER (WHERE cls = 'below_threshold') AS out_below,
         count(*) FILTER (WHERE cls = 'other_codes')     AS out_other_codes,
         count(*) FILTER (WHERE cls = 'moved')           AS out_moved,
         count(*) FILTER (WHERE cls = 'reorg')           AS out_reorg,
         count(*) FILTER (WHERE cls = 'back')            AS out_back
  FROM lv_cls GROUP BY inn
),
ji AS (
  SELECT inn,
         count(*) FILTER (WHERE cls = 'new')   AS in_new,
         count(*) FILTER (WHERE cls = 'moved') AS in_moved,
         count(*) FILTER (WHERE cls = 'reorg') AS in_reorg
  FROM jn_cls GROUP BY inn
),
org AS (
  SELECT b.inn, b.base_fl,
         COALESCE(c.cur_fl, 0)                AS cur_fl,
         COALESCE(lo.out_left_bank, 0)        AS out_left_bank,
         COALESCE(lo.out_below, 0)            AS out_below,
         COALESCE(lo.out_other_codes, 0)      AS out_other_codes,
         COALESCE(lo.out_moved, 0)            AS out_moved,
         COALESCE(lo.out_reorg, 0)            AS out_reorg,
         COALESCE(lo.out_back, 0)             AS out_back,
         COALESCE(ji.in_new, 0)               AS in_new,
         COALESCE(ji.in_moved, 0)             AS in_moved,
         COALESCE(ji.in_reorg, 0)             AS in_reorg
  FROM base b
  LEFT JOIN cur c ON c.inn = b.inn
  LEFT JOIN lo ON lo.inn = b.inn
  LEFT JOIN ji ON ji.inn = b.inn
),
calc AS (
  SELECT o.*,
         o.out_left_bank + o.out_below + o.out_other_codes          AS out_stopped,
         o.cur_fl - o.base_fl                                        AS net,
         -- Вернувшиеся в следующем месяце считаются присутствующими: пропуск
         -- месяца не падение численности.
         o.cur_fl - o.base_fl + o.out_reorg - o.in_reorg + o.out_back AS net_ex_reorg
  FROM org o
),
sel AS (
  SELECT c.*,
         CASE WHEN c.net_ex_reorg < 0
              THEN LEAST(c.out_stopped, -c.net_ex_reorg) ELSE 0 END AS real_cut
  FROM calc c
)"""

# Список организаций с реальным сокращением + итоги по ВСЕМ отобранным (окнами,
# до LIMIT): «показано N из M» и сумма по всем считаются в SQL, а не по показанным.
ORG_LIST = _ORG_HEAD + """,
picked AS (
  SELECT s.* FROM sel s
  WHERE s.base_fl >= :min_base
    AND s.net_ex_reorg < 0
    AND s.real_cut >= :min_real
    AND s.real_cut >= :min_share * s.base_fl
),
tbm AS (
  SELECT inn, tb_id FROM (
    SELECT inn, tb_id, row_number() OVER (PARTITION BY inn ORDER BY count(*) DESC, tb_id) AS rn
    FROM t_pairs WHERE report_dt = CAST(:b AS date) GROUP BY inn, tb_id
  ) t WHERE rn = 1
),
dest AS (
  SELECT inn_from AS inn, inn_to, n_mv FROM (
    SELECT inn_from, inn_to, n_mv,
           row_number() OVER (PARTITION BY inn_from ORDER BY n_mv DESC, inn_to) AS rn
    FROM pairs_mv
  ) d WHERE rn = 1
)
SELECT p.*,
       COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg,
       o.company_name, o.holding_name, o.industry_name,
       COALESCE(o.is_liquidated, false)        AS is_liquidated,
       t.tb_id,
       d.inn_to AS top_dest_inn, d.n_mv AS top_dest_n,
       count(*) OVER ()           AS n_picked,
       sum(p.real_cut) OVER ()    AS sum_real_picked,
       sum(p.out_stopped) OVER () AS sum_stopped_picked
FROM picked p
LEFT JOIN t_org o ON o.inn = p.inn
LEFT JOIN tbm t ON t.inn = p.inn
LEFT JOIN dest d ON d.inn = p.inn
ORDER BY p.real_cut DESC, p.inn
LIMIT :max_rows
"""

# Сводка по ВСЕМ организациям базы: сколько и каких — чтобы список не висел в
# воздухе («показано 300 из 12 000 сократившихся; 4 000 — нетто ноль»).
ORG_SUMMARY = _ORG_HEAD + """
SELECT CASE WHEN s.base_fl < :min_base                     THEN 'small'
            WHEN s.net_ex_reorg < 0
             AND s.real_cut >= :min_real
             AND s.real_cut >= :min_share * s.base_fl      THEN 'cut'
            WHEN s.out_reorg > 0                           THEN 'reorg'
            WHEN s.net_ex_reorg > 0                        THEN 'grew'
            WHEN s.net_ex_reorg = 0                        THEN 'flat'
            WHEN s.out_stopped = 0                         THEN 'down_moved'
            ELSE 'down_small' END                AS cls,
       COALESCE(o.seg, '""" + S.NO_DIM + """')  AS seg,
       count(*)                AS n_orgs,
       sum(s.base_fl)          AS base_fl,
       sum(s.cur_fl)           AS cur_fl,
       sum(s.out_stopped)      AS out_stopped,
       sum(s.out_moved)        AS out_moved,
       sum(s.out_reorg)        AS out_reorg,
       sum(s.out_back)         AS out_back,
       sum(s.in_new)           AS in_new,
       sum(s.in_moved)         AS in_moved,
       sum(s.real_cut)         AS real_cut
FROM sel s LEFT JOIN t_org o ON o.inn = s.inn
GROUP BY 1, 2
"""

# Реорганизации, исключённые из списка: откуда → куда, сколько и какая доля.
ORG_REORG = _ORG_HEAD + """
SELECT s.inn_from, s.inn_to, s.n_mv, s.n_lv,
       COALESCE(f.seg, '""" + S.NO_DIM + """') AS seg_from,
       f.company_name AS name_from, t.company_name AS name_to,
       COALESCE(f.is_liquidated, false) AS from_liquidated
FROM succ s
LEFT JOIN t_org f ON f.inn = s.inn_from
LEFT JOIN t_org t ON t.inn = s.inn_to
ORDER BY s.n_mv DESC, s.inn_from
LIMIT :max_rows
"""


# --------------------------------------------------------------------------- #
# Август: потеря или перенос в следующий месяц
# --------------------------------------------------------------------------- #

# Организации, пропустившие месяц: в :m_prev у ИНН не меньше :hole_min_base
# получателей, в :m — не больше половины, в :m_next — снова не меньше 80% от
# :m_prev. Признак переноса даты выплаты организацией, а не ухода людей.
# Итоги — по всем ИНН окнами, в ядро едет только верх списка.
ORG_HOLE = """
WITH x AS (
  SELECT inn,
         count(*) FILTER (WHERE report_dt = CAST(:m_prev AS date)) AS n_prev,
         count(*) FILTER (WHERE report_dt = CAST(:m AS date))      AS n_cur,
         count(*) FILTER (WHERE report_dt = CAST(:m_next AS date)) AS n_next
  FROM t_pairs
  WHERE report_dt IN (CAST(:m_prev AS date), CAST(:m AS date), CAST(:m_next AS date))
  GROUP BY inn
),
h AS (
  SELECT x.*, x.n_prev - x.n_cur AS hole
  FROM x
  WHERE x.n_prev >= :hole_min_base
    AND x.n_cur <= 0.5 * x.n_prev
    AND x.n_next >= 0.8 * x.n_prev
)
SELECT h.inn, h.n_prev, h.n_cur, h.n_next, h.hole,
       COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg, o.company_name,
       count(*) OVER ()    AS n_orgs,
       sum(h.hole) OVER () AS sum_hole
FROM h LEFT JOIN t_org o ON o.inn = h.inn
ORDER BY h.hole DESC, h.inn
LIMIT :max_rows
"""

# Подпись переноса выплаты: у ФЛ, пропавших в :m и вернувшихся в :m_next, —
# отношение зарплатной суммы :m_next / :m_prev. ≈2 — выплату за пропущенный месяц
# перенесли; ≈1 — человек просто не получал месяц. Рядом — получавшие все три.
RETURN_PAY = """
WITH g AS (
  SELECT p.epk_id,
         CASE WHEN c.epk_id IS NULL THEN 'gap' ELSE 'steady' END AS grp
  FROM t_epk p
  JOIN t_epk n ON n.epk_id = p.epk_id AND n.report_dt = CAST(:m_next AS date)
  LEFT JOIN t_epk c ON c.epk_id = p.epk_id AND c.report_dt = CAST(:m AS date)
  WHERE p.report_dt = CAST(:m_prev AS date)
),
r AS (
  SELECT g.grp, a.amt_codes AS a_prev, b.amt_codes AS a_next
  FROM g
  JOIN t_person a ON a.epk_id = g.epk_id AND a.report_dt = CAST(:m_prev AS date)
  JOIN t_person b ON b.epk_id = g.epk_id AND b.report_dt = CAST(:m_next AS date)
  WHERE a.amt_codes > 0
)
SELECT grp,
       count(*)                                                     AS n_epk,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY a_next / a_prev) AS median_ratio,
       avg(CASE WHEN a_next / a_prev >= 1.6 THEN 1.0 ELSE 0 END)    AS share_double,
       avg(CASE WHEN a_next / a_prev BETWEEN 0.7 AND 1.4 THEN 1.0 ELSE 0 END) AS share_single
FROM r GROUP BY grp
"""

SHOW_DEFS["t_tflow"] = T_TFLOW
SHOW_DEFS["t_oflow"] = T_OFLOW


# Все запросы файла — для самопроверок (партиция, диалект, маска ИНН).
def all_sql() -> dict[str, str]:
    return {k: v for k, v in globals().items()
            if k.isupper() and isinstance(v, str) and ("SELECT" in v or "CREATE" in v)}
