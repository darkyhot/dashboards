"""Весь SQL отчёта. Ни одной строки SQL за пределами этого файла.

Плейсхолдеры `{schema}` и `{code_col}` подставляет `db.render` заменой по имени;
значения — именованными параметрами `:name`.

ПЯТЬ ЛОВУШЕК, на которых такой разбор ломается тихо
---------------------------------------------------

1. **Партиция.** `mis_data_payroll_m` партиционирована по `report_dt`. Запрос без
   `report_dt` в WHERE читает всю историю всего банка — на проме это «никогда».
   Каждое обращение к витрине ниже ограничено ОДНИМ месяцем (`= CAST(:m AS date)`),
   и это проверяет самопроверка `check_partition_filter`.
2. **Тип номера организации.** В ведомостях `inn` — text длиной 1–12, в справочнике ЕПК — bigint.
   `CAST` на значении «ID123» роняет ВЕСЬ запрос, поэтому приведение — только под
   маской `INN_OK`, а в условии соединения — внутри `CASE`.
3. **Диалект 9.4.** Никаких `make_interval(months => n)`, `ON CONFLICT`, `GROUPING
   SETS`. Предыдущий месяц передаётся параметром, а не вычисляется в SQL.
4. **Списки значений** (`adv_codes`, `sal_codes`) уезжают ОДНИМ параметром-массивом и
   сравниваются `= ANY(:x)`. `IN :x` через `text()` подставил бы кортеж одним значением.
5. **NULL в ключе тройки.** `sys_gosb_id` бывает пуст; NULL не равен NULL, и
   тройка с пустым ГОСБ «исчезала» бы каждый месяц. Поэтому `COALESCE(..., -1)`.

Отбор зачислений и порог — логика заказчика (портфель, is_salary_client)
------------------------------------------------------------------------
Ведомости — `mis_data_payroll_m`, с перекодировками заказчика: ГОСБ (`gosb_fix`) и
«Не определено» → '0'. Строка релевантна (`_RELEV`): вид «Основные» и не «ИП 1 чел.»
(с января 2024; ноябрь–декабрь 2023 — без условий), id орг не Сбер и не заглушка.
Портфельные виды — по справочнику `uzp_dim_enrollment_type.is_portfolio_enrollment`,
а не списком кодов.

Получатель — ТРОЙКА (epk_id, inn, ГОСБ). Сумма портфельных зачислений `amt_pf`
считается по ТРОЙКЕ (месяц, ФЛ, ГОСБ, id орг). Тройка — получатель, если у неё есть
релевантная строка с суммой > 0 и выполнено одно из трёх (`rec`):
  1) портфельная строка и `amt_pf` > порога;
  2) вид зачисления 2;
  3) портфельная строка и id орг без порога (`t_exc`: образовательные и холдинг
     МИНОБОРОНЫ по `reference_holding_name`).
"""
from __future__ import annotations

from . import segments as S

AMT_MIN = 2500
THRESHOLDS = (0, 1000, 5000, 10000)     # плюс AMT_MIN — чувствительность к порогу
EXC_HOLDING = "МИНОБОРОНЫ"              # холдинг без порога (условие 3 заказчика)
BAD_INNS = "('7707083893', 'Не определено', '0', '-1')"
KSB_RAW = ("('СКБ-Средние','СКБ-Прочее','СКБ-Крупные','Средний бизнес','Крупный бизнес',"
           "'ККСБ - прочие', 'ККСБ - Прочие')")
MASK = "'^[0-9]{1,12}$'"


def gosb_fix(gosb: str, tb: str, seg: str) -> str:
    """Перекодировка ГОСБ заказчика (сегмент — сырой, из ведомостей)."""
    return (f"CASE WHEN {tb} = 38 THEN 9038\n         WHEN {gosb} = 1009 THEN 8557\n"
            f"         WHEN {gosb} = 0 AND {tb} = 40 THEN 9040\n"
            f"         WHEN {seg} IN {KSB_RAW} AND {gosb} = 8591 THEN 8646\n"
            f"         ELSE {gosb} END")


# Признак relev заказчика (дословно). id орг — после замены «Не определено» → '0';
# NULL в любом поле даёт «не релевантна» (COALESCE снаружи).
_RELEV = ("""((p.enrollment_kind_descr = 'Основные' AND p.report_dt >= DATE '2024-01-01')
          OR (p.report_dt BETWEEN DATE '2023-11-01' AND DATE '2023-12-31'))
      AND ((p.market_share_flag_name <> 'ИП 1 чел.' AND p.report_dt >= DATE '2024-01-01')
          OR (p.report_dt BETWEEN DATE '2023-11-01' AND DATE '2023-12-31'))
      AND (CASE WHEN p.inn = 'Не определено' THEN '0' ELSE p.inn END) NOT IN """ + BAD_INNS)
_PF = "COALESCE(" + _RELEV + ", false) AND COALESCE(d.is_portfolio_enrollment, false)"
_T2 = "COALESCE(" + _RELEV + ", false) AND COALESCE(d.enrollment_type_id = 2, false)"


def rec(t: str, a: str = "") -> str:
    """Тройка t_stage (алиас a) — получатель при пороге t (три условия заказчика)."""
    a = f"{a}." if a else ""
    return f"({a}t2 OR ({a}pos_pf AND ({a}exc OR {a}amt_pf > {t})))"


REC = rec(":amt_min")

INN_OK = "p.inn ~ '^[0-9]{1,12}$'"
INN_OK_R = "r.inn ~ '^[0-9]{1,12}$'"
_INN_R = "CASE WHEN " + INN_OK_R + " THEN CAST(r.inn AS bigint) END"
VALID = "epk_id IS NOT NULL AND inn IS NOT NULL"     # пригодная тройка t_stage


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
# Перекодировки заказчика (ГОСБ, «Не определено») и отбор строки делаются здесь
# же: в копию едут готовые признаки `pf` (релевантная портфельная строка) и `t2`
# (релевантная строка вида 2), а не поля, из которых они считаются. Название вида
# берётся только у портфельных строк: объёмный текст по каждой строке всего банка
# таблица не тащит.
#
# Прочие строки (пенсии, пособия, «Дополнительные», «ИП 1 чел.») нужны только
# месяцам рабочего набора — по ним отличается «нет зачислений в банке» от «только
# прочие зачисления». Месяцам, которые нужны лишь ряду, достаточно строк, которые
# могут сделать получателя: `:all_rows` = false, и копия этих месяцев в разы меньше.
T_RAW_MONTH = """
SELECT p.report_dt,
       p.epk_id,
       CASE WHEN p.inn = 'Не определено' THEN '0' ELSE p.inn END AS inn,
       """ + gosb_fix("p.sys_gosb_id", "p.sys_tb_id", "p.segment_name") + """ AS sys_gosb_id,
       p.sys_tb_id,
       p.{code_col}                                    AS code,
       """ + _PF + """ AS pf,
       """ + _T2 + """ AS t2,
       CASE WHEN """ + _PF + """
            THEN p.enrollment_transcription END        AS code_name,
       p.amt
FROM {schema}.mis_data_payroll_m p
LEFT JOIN {schema}.uzp_dim_enrollment_type d ON d.enrollment_type_id = p.{code_col}
WHERE p.report_dt = CAST(:m AS date)
  AND (CAST(:all_rows AS boolean)
       OR (""" + _PF + """) OR (""" + _T2 + """))
"""

# id орг, которые идут в портфель БЕЗ порога (условие 3 заказчика): образовательные
# и холдинг :exc_holding (по `reference_holding_name`, как у заказчика). Номер — под
# маской: в справочнике образовательных он текстовый.
T_EXC = """
SELECT inn_exc FROM (
  SELECT CAST(CAST(d.inn AS text) AS bigint) AS inn_exc
  FROM {schema}.uzp_dim_education_organization d
  WHERE CAST(d.inn AS text) ~ """ + MASK + """
  UNION
  SELECT c.inn
  FROM {schema}.uzp_data_epk_consolidation c
  WHERE c.reference_holding_name = :exc_holding AND c.inn IS NOT NULL
) t
"""

# Организации справочника ЕПК, свёрнутые до id орг. Свёртка обязательна: у одного id орг
# бывает несколько ЕПК, и соединение строкой справочника задвоило бы ВЕДОМОСТИ.
# Ликвидация — «нет НИ ОДНОЙ активной записи», одна мёртвая строка при живой
# соседней ничего не значит. Сегмент при нескольких записях — min() по короткому
# имени: детерминированно, и расхождение печатается разведкой.
#
# Названия организаций и холдингов берутся, но колонки называются ТОЛЬКО
# `org_name` / `holding_name`: каждая выборка проходит маску `names.mask_frame`,
# и название с ФИО (у ИП, главы КФХ — фамилия, имя и отчество человека) дальше
# выборки не уходит — вместо него показывается id орг.
# `focus` — организация выделенного холдинга (отдельная вкладка отчёта).
T_ORG = """
SELECT e.inn,
       min(""" + S.seg_case("e.segment_name") + """)            AS seg,
       count(DISTINCT """ + S.seg_case("e.segment_name") + """) AS n_seg,
       min(e.company_name)                                       AS org_name,
       min(NULLIF(btrim(e.holding_name), ''))                    AS holding_name,
       min(e.industry_name)                                      AS industry_name,
       bool_or(COALESCE(btrim(e.holding_name), '') = :focus_holding) AS focus,
       NOT bool_or(COALESCE(e.status_name, '') = 'Активна')      AS is_liquidated
FROM {schema}.uzp_data_epk_consolidation e
WHERE e.inn IS NOT NULL
GROUP BY e.inn
"""

# ВСЕ портфельные тройки ВСЕХ месяцев копии с признаками отбора — без порога, ОДНИМ
# проходом по t_raw. Из неё — и ряд по сегментам при всех порогах (`SERIES_ALL`),
# и тройки-получатели месяцев набора (`t_pairs`). Получатель — `REC` (см. шапку):
#   amt_pf — сумма релевантных портфельных зачислений тройки (sum_pf заказчика);
#   pos_pf — есть релевантная портфельная строка с суммой > 0;
#   t2     — есть релевантная строка вида 2 с суммой > 0;
#   exc    — id орг без порога (t_exc).
#
# Строки с пустым ФЛ или непригодным номером организации (inn = NULL) в t_stage
# ОСТАЮТСЯ — со счётчиком строк `n_rows`: из них считается полнота загрузки, и
# отдельного прохода по копии ради неё не нужно. Получатели и ряд берут только
# пригодные тройки (`VALID`).
#
# Именно ОДНИМ запросом, а не циклом по месяцам: у временной таблицы нет партиций,
# и фильтр `report_dt = :m` по t_raw читает её ЦЕЛИКОМ — цикл по 26 месяцам был
# 26 полными проходами по копии всего банка, и копия ничего не ускоряла.
# Сегмент денормализуется сразу: дальше он нужен в каждом запросе. id орг вне
# справочника НЕ отбрасывается: банк — это все получатели, такие id орг идут
# строкой «Не в справочнике».
#
# Виды выплат (`has_adv` — аванс, `has_sal` — заработная плата) — по номерам
# кодов, которые классифицированы по названиям ОДИН раз (`CODE_NAMES`): сравнение
# чисел дешевле регулярки по каждой строке банка.
T_STAGE = """
SELECT x.report_dt, x.epk_id, x.inn, x.gosb_id, x.tb_id, x.amt_pf, x.pos_pf, x.t2, x.n_rows,
       x.has_adv, x.has_sal,
       (e.inn_exc IS NOT NULL)            AS exc,
       COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg,
       COALESCE(o.focus, false)           AS focus
FROM (
  SELECT r.report_dt,
         r.epk_id,
         """ + _INN_R + """                 AS inn,
         COALESCE(r.sys_gosb_id, -1)        AS gosb_id,
         min(r.sys_tb_id)                   AS tb_id,
         sum(CASE WHEN r.pf THEN r.amt ELSE 0 END) AS amt_pf,
         bool_or(r.pf AND r.amt > 0)        AS pos_pf,
         bool_or(r.t2 AND r.amt > 0)        AS t2,
         count(*)                           AS n_rows,
         bool_or(r.pf AND r.code = ANY(:adv_codes)) AS has_adv,
         bool_or(r.pf AND r.code = ANY(:sal_codes)) AS has_sal
  FROM t_raw r
  WHERE r.pf OR r.t2
  GROUP BY r.report_dt, r.epk_id, """ + _INN_R + """, COALESCE(r.sys_gosb_id, -1)
) x
LEFT JOIN t_exc e ON e.inn_exc = x.inn
LEFT JOIN t_org o ON o.inn = x.inn
"""

# Получатели: тройки месяцев набора, прошедшие отбор, — одним запросом.
PAIRS_FROM_STAGE = """
SELECT report_dt, epk_id, inn, gosb_id, tb_id, amt_pf, seg, focus, has_adv, has_sal
FROM t_stage
WHERE """ + REC + """
  AND """ + VALID + """
  AND report_dt = ANY(CAST(:months AS date[]))
"""

# То же одним запросом по копии — для ПОКАЗА читателю (определение одно).
T_PAIRS = ("SELECT s.* FROM (" + T_STAGE + ") s\n"
           "WHERE " + rec(":amt_min", "s") + "\n"
           "  AND s.epk_id IS NOT NULL AND s.inn IS NOT NULL\n"
           "  AND s.report_dt = ANY(CAST(:months AS date[]))\n")

# ФЛ-месяц поверх троек: сколько id орг и троек у человека и его ОСНОВНОЙ сегмент —
# сегмент id орг с наибольшей суммой. Основной сегмент один на человека, поэтому
# численность ФЛ по сегментам складывается в банк, а матрица перетоков замкнута.
# count(DISTINCT) здесь нет нигде: на Greenplum уникальный подсчёт по таблице
# всего банка — самая дорогая операция, она уже снимала прогон по таймауту.
# Уникальные считаются двухступенчатой группировкой: сначала до нужного ключа,
# потом count(*).
T_EPK = """
SELECT a.report_dt, a.epk_id, a.n_inn, a.n_triples,
       m.seg AS main_seg, m.tb_id AS main_tb, m.inn AS main_inn
FROM (
  SELECT report_dt, epk_id, count(*) AS n_inn, sum(n_tr) AS n_triples
  FROM (SELECT report_dt, epk_id, inn, count(*) AS n_tr
        FROM t_pairs GROUP BY report_dt, epk_id, inn) i
  GROUP BY report_dt, epk_id
) a
JOIN (
  SELECT report_dt, epk_id, seg, tb_id, inn,
         row_number() OVER (PARTITION BY report_dt, epk_id
                            ORDER BY amt_pf DESC, inn, gosb_id) AS rn
  FROM t_pairs
) m ON m.report_dt = a.report_dt AND m.epk_id = a.epk_id AND m.rn = 1
"""

# В каких сегментах человек получает в месяце (для лестницы по сегменту:
# «остался в своём сегменте» против «ушёл в другой»).
T_EPK_SEG = """
SELECT DISTINCT report_dt, epk_id, seg FROM t_pairs
"""

# ФЛ-месяц выделенного холдинга: получает ли ФЛ в его организациях. Маленькая.
T_EPK_FOC = """
SELECT DISTINCT report_dt, epk_id FROM t_pairs WHERE focus
"""

# Все ФЛ, бывшие получателями хоть в одном месяце набора. Только про них разбор
# и спрашивает; витрина за месяц — это клиенты всего банка, включая пенсионеров.
T_KEYS = """
SELECT DISTINCT epk_id FROM t_epk
"""

# Присутствие ФЛ в ведомостях БЕЗ фильтра кода и порога — все месяцы набора ОДНИМ
# запросом (тот же довод, что у t_stage: цикл по месяцам = полный проход на месяц). Без него
# не отличить «нет зачислений в банке» от «зарплата ниже порога» и «только
# прочие зачисления» — три разных диагноза.
# Зарплатная сумма берётся только по пригодным id орг: зарплата на id орг, который не
# сопоставить, получателя не делает (это видно в полноте загрузки).
T_PERSON = """
SELECT r.report_dt,
       r.epk_id,
       sum(r.amt) AS amt_all,
       sum(CASE WHEN r.pf AND """ + INN_OK_R + """
                THEN r.amt ELSE 0 END) AS amt_codes
FROM t_raw r
JOIN t_keys k ON k.epk_id = r.epk_id
WHERE r.report_dt = ANY(CAST(:months AS date[]))
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
WORKSET_ORDER = ["t_org", "t_exc", "t_raw", "t_pairs", "t_epk", "t_epk_seg", "t_epk_foc", "t_keys", "t_person"]
# Порядок для ПОКАЗА: плюс временные таблицы отдельных шагов (t_tflow).
SHOW_ORDER = ["t_org", "t_exc", "t_raw", "t_stage", "t_ptype", "t_pairs", "t_epk", "t_epk_seg", "t_epk_foc",
              "t_keys", "t_person", "t_tflow", "t_oflow", "t_mv", "t_succ", "t_orgsel"]
DIST = {"t_org": "inn", "t_exc": "inn_exc", "t_raw": "epk_id", "t_pairs": "epk_id", "t_epk": "epk_id",
        "t_epk_seg": "epk_id", "t_keys": "epk_id", "t_person": "epk_id",
        "t_tflow": "epk_id", "t_oflow": "epk_id", "t_stage": "epk_id",
        "t_mv": "epk_id", "t_succ": "inn_from", "t_orgsel": "inn",
        "t_epk_foc": "epk_id", "t_ptype": "epk_id"}

# Определения для ПОКАЗА читателю: запрос у блока должен выполняться как есть, а
# `FROM t_pairs` выполнить негде — таблица жила в чужой сессии. Поэтому к
# показанному запросу приклеиваются эти определения как CTE (по всем месяцам).
_ALL = "report_dt = ANY(CAST(:months AS date[]))"
SHOW_DEFS = {
    "t_org": T_ORG,
    "t_exc": T_EXC,
    "t_raw": T_RAW_MONTH.replace("p.report_dt = CAST(:m AS date)", "p." + _ALL),
    "t_stage": T_STAGE,
    "t_pairs": T_PAIRS,
    "t_epk": T_EPK,
    "t_epk_seg": T_EPK_SEG,
    "t_epk_foc": T_EPK_FOC,
    "t_keys": T_KEYS,
    "t_person": T_PERSON,
}


# --------------------------------------------------------------------------- #
# Ряд и портфельные виды — по копии витрины
# --------------------------------------------------------------------------- #

# Численность месяца по сегментам и при нескольких порогах. Строка '__ALL__' —
# банк целиком: ФЛ с работой в двух сегментах в сумме по сегментам посчитался бы
# дважды. Все месяцы ряда — ОДНИМ запросом по t_stage. ФЛ — двухступенчатой
# группировкой (до ФЛ, потом счёт), без count(DISTINCT): он на объёме банка
# не уложился в таймаут. Число организаций в ряду не считается — не используется.
SERIES_ALL = """
WITH e AS (
  SELECT report_dt, seg, epk_id,
         count(*) FILTER (WHERE """ + REC + """) AS n_tr,
         count(*) FILTER (WHERE """ + rec("0") + """)     AS t0,
         count(*) FILTER (WHERE """ + rec("1000") + """)  AS t1000,
         count(*) FILTER (WHERE """ + rec("5000") + """)  AS t5000,
         count(*) FILTER (WHERE """ + rec("10000") + """) AS t10000
  FROM t_stage WHERE """ + VALID + """
  GROUP BY report_dt, seg, epk_id
)
SELECT report_dt, seg,
       sum(n_tr)                              AS n_triples,
       count(*) FILTER (WHERE n_tr > 0)       AS n_epk,
       CAST(NULL AS bigint)                   AS n_inn,
       sum(t0) AS t0, sum(t1000) AS t1000, sum(t5000) AS t5000, sum(t10000) AS t10000
FROM e GROUP BY report_dt, seg
UNION ALL
SELECT report_dt, '__ALL__',
       sum(n_tr), count(*) FILTER (WHERE n_tr > 0), CAST(NULL AS bigint),
       sum(t0), sum(t1000), sum(t5000), sum(t10000)
FROM (SELECT report_dt, epk_id, sum(n_tr) AS n_tr, sum(t0) AS t0, sum(t1000) AS t1000,
             sum(t5000) AS t5000, sum(t10000) AS t10000
      FROM e GROUP BY report_dt, epk_id) a
GROUP BY report_dt
"""

# Полнота загрузки — по t_stage (портфельные строки, в т.ч. непригодные): сколько
# строк, сколько с пригодным номером организации, сколько без ФЛ. Месяцы ряда в
# копии только строки отбора — полнота везде считается по одним и тем же строкам.
LOAD_FROM_STAGE = """
SELECT report_dt,
       sum(n_rows)                                     AS n_rows,
       sum(n_rows) FILTER (WHERE inn IS NOT NULL)      AS n_inn_ok,
       sum(n_rows) FILTER (WHERE epk_id IS NULL)       AS n_no_epk
FROM t_stage
GROUP BY report_dt
"""

# Ряд выделенного холдинга — отдельным оператором (свой таймаут), из t_stage.
SERIES_FOCUS = """
SELECT report_dt, sum(n_tr) AS n_triples, count(*) FILTER (WHERE n_tr > 0) AS n_epk
FROM (SELECT report_dt, epk_id, count(*) FILTER (WHERE """ + REC + """) AS n_tr
      FROM t_stage WHERE """ + VALID + """ AND focus
      GROUP BY report_dt, epk_id) e
GROUP BY report_dt
"""

# Названия портфельных видов — по ПЕРВОМУ скопированному месяцу (одна партиция
# в копии, проход дешёвый). Из них в Python — какие коды аванс, какие зарплата.
CODE_NAMES = """
SELECT code, min(code_name) AS code_name, count(*) AS n_rows
FROM t_raw
WHERE pf
GROUP BY code
"""

# Виды выплат пары ФЛ × организация в месяцах переходов: был ли аванс, была ли
# зарплата. Временной таблицей из t_stage (в t_pairs нет тех, кто ниже порога, а
# «потерял аванс и упал ниже порога» — ровно тот случай, который ищем).
T_PTYPE = """
SELECT report_dt, epk_id, inn, min(seg) AS seg, bool_or(focus) AS focus,
       bool_or(has_adv) AS has_adv, bool_or(has_sal) AS has_sal,
       bool_or(""" + REC + """) AS rec,
       CAST(date_trunc('month', report_dt) + interval '2 month' - interval '1 day' AS date) AS dt_next
FROM t_stage
WHERE """ + VALID + """ AND report_dt = ANY(CAST(:pt_months AS date[]))
GROUP BY report_dt, epk_id, inn
"""


def _kind(a: str) -> str:
    return (f"CASE WHEN {a}.has_adv AND {a}.has_sal THEN 'both' WHEN {a}.has_sal THEN 'sal' "
            f"WHEN {a}.has_adv THEN 'adv' ELSE 'other' END")


# Пара — получатель в месяце b: какие виды выплат были в b и что стало в
# следующем месяце (оба / только один / нет выплат от этой организации) и
# осталась ли пара получателем.
PAYTYPE = """
SELECT b.report_dt AS b_dt, b.seg, b.focus,
       """ + _kind("b") + """ AS kind_b,
       CASE WHEN c.epk_id IS NULL THEN 'none' ELSE """ + _kind("c") + """ END AS kind_c,
       CASE WHEN c.rec THEN 1 ELSE 0 END AS rec_c,
       count(*) AS n_pairs
FROM t_ptype b
LEFT JOIN t_ptype c ON c.epk_id = b.epk_id AND c.inn = b.inn AND c.report_dt = b.dt_next
WHERE b.rec AND b.report_dt = ANY(CAST(:pt_b AS date[]))
GROUP BY 1, 2, 3, 4, 5, 6
"""

# Зарплатные коды по месяцам и сегментам: КАКОЙ вид выплаты просел (отпускные,
# премия, аванс). Только коды метрики и только месяцы сравнения — проход по
# копии с фильтром, без справочника для остальных месяцев.
CODE_MONTH = """
SELECT report_dt, code, min(code_name) AS code_name, seg,
       count(*) AS n_epk, sum(amt) AS amt
FROM (
SELECT r.report_dt,
       r.code                                AS code,
       r.epk_id,
       min(r.code_name)                      AS code_name,
       COALESCE(o.seg, '""" + S.NO_DIM + """')       AS seg,
       sum(r.amt)                            AS amt
FROM t_raw r
LEFT JOIN t_org o ON o.inn = """ + _INN_R + """
WHERE r.report_dt = ANY(CAST(:code_months AS date[]))
  AND r.pf
  AND r.epk_id IS NOT NULL
GROUP BY r.report_dt, r.code, r.epk_id, COALESCE(o.seg, '""" + S.NO_DIM + """')
) x
GROUP BY report_dt, code, seg
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
#     разложения по получателям, а та же разность по организациям и ГОСБ делит его на
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
SELECT 'lost' AS side, b.seg, b.epk_id, b.inn, b.focus
FROM (SELECT * FROM t_pairs WHERE report_dt = CAST(:b AS date)) b
LEFT JOIN (SELECT * FROM t_pairs WHERE report_dt = CAST(:c AS date)) c
       ON c.epk_id = b.epk_id AND c.inn = b.inn AND c.gosb_id = b.gosb_id
WHERE c.epk_id IS NULL
UNION ALL
SELECT 'gained', c.seg, c.epk_id, c.inn, c.focus
FROM (SELECT * FROM t_pairs WHERE report_dt = CAST(:c AS date)) c
LEFT JOIN (SELECT * FROM t_pairs WHERE report_dt = CAST(:b AS date)) b
       ON b.epk_id = c.epk_id AND b.inn = c.inn AND b.gosb_id = c.gosb_id
WHERE b.epk_id IS NULL
"""

# Шаг 2 — ситуация каждой тройки, ровно одна:
#   inside      — ФЛ в другом месяце получает в ЭТОМ ЖЕ сегменте (сменил организацию/ГОСБ
#                 или стал получать в меньшем/большем числе мест);
#   other_seg   — ФЛ получает в банке, но только в ДРУГИХ сегментах;
#   left_bank / below_threshold / other_codes (и зеркальные для появившихся) —
#                 ФЛ не получатель нигде.
# «Другой месяц» для исчезнувших — c, для появившихся — b. Сумма по сегментам =
# банк; на уровне банка inside + other_seg = совместительство.
TRIPLE_FLOW = """
SELECT side, seg, cause, tenure, in_cn1,
       sum(n) AS n_triples, count(*) AS n_epk
FROM (
SELECT f.side, f.seg,
       CASE WHEN os.epk_id IS NOT NULL THEN 'inside'
            WHEN oe.epk_id IS NOT NULL THEN 'other_seg'
            WHEN f.side = 'lost' THEN """ + _gone_case("ps") + """
            ELSE """ + _come_case("ps") + """ END AS cause,
       CASE WHEN f.side = 'lost' THEN """ + _tenure("e1", "e2") + """ END AS tenure,
       """ + _flag("n1", "has_cn1") + """ AS in_cn1,
       f.epk_id, count(*) AS n
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
GROUP BY 1, 2, 3, 4, 5, 6
) x
GROUP BY side, seg, cause, tenure, in_cn1
"""

# Отток B2C и B2B: переставшие получать ЗП в Сбере, по организации, из которой
# ушли. Сколько ФЛ перестали получать из ОДНОЙ организации за переход: 1–2 —
# отток B2C (люди уходят поодиночке), 3 и больше — B2B (уходит организация или
# её часть). Ступени внутри B2B — для формы распределения.
# Грейн — ФЛ × организация: совместитель, переставший получать в двух
# организациях, считается в обеих. Получатели (тройки) складываются точно в
# «перестали получать» разложения — это проверяется.
STOP_SIZE = """
WITH s AS (
  SELECT f.inn, f.epk_id, min(f.seg) AS seg, bool_or(f.focus) AS focus, count(*) AS n_tr
  FROM t_tflow f
  LEFT JOIN t_epk oe ON oe.epk_id = f.epk_id AND oe.report_dt = CAST(:c AS date)
  WHERE f.side = 'lost' AND oe.epk_id IS NULL
  GROUP BY f.inn, f.epk_id
),
o AS (
  SELECT inn, min(seg) AS seg, bool_or(focus) AS focus, count(*) AS n_fl, sum(n_tr) AS n_tr
  FROM s GROUP BY inn
)
SELECT seg, focus,
       CASE WHEN n_fl = 1 THEN '1' WHEN n_fl = 2 THEN '2' WHEN n_fl < 10 THEN '3-9'
            WHEN n_fl < 50 THEN '10-49' ELSE '50+' END AS bucket,
       count(*) AS n_orgs, sum(n_fl) AS n_fl, sum(n_tr) AS n_tr
FROM o
GROUP BY 1, 2, 3
"""

# Выделенный холдинг: то же разложение по получателям, где «свой сегмент» —
# организации холдинга. Ситуация исчезнувшей/появившейся тройки холдинга:
#   inside    — ФЛ в другом месяце получает в организациях холдинга;
#   other_org — получает в банке, но вне холдинга (переток);
#   left_bank / below_threshold / other_codes (и зеркальные) — нигде не получатель.
FOCUS_FLOW = """
SELECT side, cause, tenure, in_cn1, sum(n) AS n_triples, count(*) AS n_epk
FROM (
SELECT f.side,
       CASE WHEN fo.epk_id IS NOT NULL THEN 'inside'
            WHEN oe.epk_id IS NOT NULL THEN 'other_org'
            WHEN f.side = 'lost' THEN """ + _gone_case("ps") + """
            ELSE """ + _come_case("ps") + """ END AS cause,
       CASE WHEN f.side = 'lost' THEN """ + _tenure("e1", "e2") + """ END AS tenure,
       """ + _flag("n1", "has_cn1") + """ AS in_cn1,
       f.epk_id, count(*) AS n
FROM t_tflow f
LEFT JOIN t_epk_foc fo ON fo.epk_id = f.epk_id
     AND fo.report_dt = CASE WHEN f.side = 'lost' THEN CAST(:c AS date) ELSE CAST(:b AS date) END
LEFT JOIN t_epk oe ON oe.epk_id = f.epk_id
     AND oe.report_dt = CASE WHEN f.side = 'lost' THEN CAST(:c AS date) ELSE CAST(:b AS date) END
LEFT JOIN t_person ps ON ps.epk_id = f.epk_id
     AND ps.report_dt = CASE WHEN f.side = 'lost' THEN CAST(:c AS date) ELSE CAST(:b AS date) END
LEFT JOIN t_epk e1 ON e1.report_dt = CAST(:bp1 AS date) AND e1.epk_id = f.epk_id
LEFT JOIN t_epk e2 ON e2.report_dt = CAST(:bp2 AS date) AND e2.epk_id = f.epk_id
LEFT JOIN t_epk n1 ON n1.report_dt = CAST(:cn1 AS date) AND n1.epk_id = f.epk_id
WHERE f.focus
GROUP BY 1, 2, 3, 4, 5
) x
GROUP BY side, cause, tenure, in_cn1
"""

# Итоги холдинга по месяцам набора: получатели, пары ФЛ × организация, ФЛ, организации.
FOCUS_TOT = """
WITH x AS (
  SELECT report_dt, epk_id, inn, count(*) AS n FROM t_pairs WHERE focus GROUP BY 1, 2, 3
)
SELECT p.report_dt, p.n_triples, p.n_pairs, e.n_epk, o.n_orgs
FROM (SELECT report_dt, sum(n) AS n_triples, count(*) AS n_pairs FROM x GROUP BY 1) p
JOIN (SELECT report_dt, count(*) AS n_epk
      FROM (SELECT report_dt, epk_id FROM x GROUP BY 1, 2) y GROUP BY 1) e ON e.report_dt = p.report_dt
JOIN (SELECT report_dt, count(*) AS n_orgs
      FROM (SELECT report_dt, inn FROM x GROUP BY 1, 2) y GROUP BY 1) o ON o.report_dt = p.report_dt
"""


# --------------------------------------------------------------------------- #
# Итоги месяцев набора: банк, сегменты, совместительство, территория
# --------------------------------------------------------------------------- #

# Совместительство по сегментам: «лишние» получатели = тройки − ФЛ. Делятся на
# несколько организаций у ФЛ (Σ(n_inn − 1)) и одну организацию через несколько ГОСБ
# (Σ(n_triples − n_inn)). Второе — чистый эффект счёта: человек ничего не менял.
MULTI_SEG = """
SELECT report_dt, seg,
       count(*)               AS n_epk,
       sum(n_tr)              AS n_triples,
       sum(n_inn)             AS n_pairs,
       sum(n_inn - 1)         AS extra_inn,
       sum(n_tr - n_inn)      AS extra_gosb
FROM (
  SELECT report_dt, seg, epk_id, count(*) AS n_inn, sum(n_tr) AS n_tr
  FROM (SELECT report_dt, seg, epk_id, inn, count(*) AS n_tr
        FROM t_pairs GROUP BY report_dt, seg, epk_id, inn) i
  GROUP BY report_dt, seg, epk_id
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

# Растворились пришедшие — ГДЕ: когорта прихода :k (не было в :kp) по основной
# организации в месяце прихода, и что с каждым в :t — получает (alive) или почему
# нет. Разрезы — сегмент, холдинг, отрасль, выделенный холдинг. Холдинг едет
# колонкой `holding_name` — под маску названий.
COHORT_DIM = """
WITH k AS (
  SELECT c.epk_id, c.main_inn
  FROM t_epk c
  LEFT JOIN t_epk p ON p.report_dt = CAST(:kp AS date) AND p.epk_id = c.epk_id
  WHERE c.report_dt = CAST(:k AS date) AND p.epk_id IS NULL
),
a AS (
  SELECT k.main_inn,
         CASE WHEN j.epk_id IS NOT NULL THEN 'alive' ELSE """ + _gone_case("ps") + """ END AS st,
         count(*) AS n
  FROM k
  LEFT JOIN t_epk j ON j.epk_id = k.epk_id AND j.report_dt = CAST(:t AS date)
  LEFT JOIN t_person ps ON ps.epk_id = k.epk_id AND ps.report_dt = CAST(:t AS date)
  GROUP BY 1, 2
),
d AS (
  SELECT a.st, a.n, COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg, o.holding_name, o.industry_name,
         COALESCE(o.focus, false) AS focus
  FROM a LEFT JOIN t_org o ON o.inn = a.main_inn
)
SELECT 'seg' AS dim, seg AS key, CAST(NULL AS text) AS holding_name, st, sum(n) AS n
FROM d GROUP BY seg, st
UNION ALL
SELECT 'holding', NULL, holding_name, st, sum(n) FROM d WHERE holding_name IS NOT NULL
GROUP BY holding_name, st
UNION ALL
SELECT 'industry', industry_name, NULL, st, sum(n) FROM d GROUP BY industry_name, st
UNION ALL
SELECT 'focus', 'focus', NULL, st, sum(n) FROM d WHERE focus GROUP BY st
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
# Грейн — ФЛ внутри id орг (пара epk × организация): перевод между ГОСБ одной организации
# сюда не попадает по построению. Сравнение b → c (год к году).
#
# Ушедшие из id орг (были в b, нет в c) — ровно одна ситуация:
#   reorg      — ушёл в организацию-приёмник: туда переехало не меньше :reorg_min_movers
#                ФЛ и не меньше :reorg_min_share ушедших этого id орг. Переоформление,
#                а не сокращение — исключается;
#   moved      — получает ЗП в банке в другой организации: ПЕРЕТОК. Часть снижения
#                организации, но не потеря для Сбера — отдельная колонка;
#   back       — в c не получатель нигде, но снова получатель в c+1 (если загружен):
#                пропуск месяца (перенос выплаты, отпуск), а не уход — отдельная
#                колонка, в сокращение не идёт. Иначе организация, заплатившая за
#                август в сентябре, выглядела бы «ушедшей из Сбера целиком»;
#   left_bank / below_threshold / other_codes — ПЕРЕСТАЛ получать ЗП в Сбере.
#                Это и есть основная метрика.
# Пришедшие (нет в b, есть в c): reorg (из организации-предшественника), moved (был
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

# Тяжёлая цепочка (ушедшие → куда ушли → приёмники → ситуации → итог по id орг)
# считается ОДИН раз на сравнение — тремя временными таблицами шага, — а список,
# сводка и реорганизации читают готовое. Раньше каждый из трёх запросов
# пересчитывал всю цепочку заново.

# Ушедшие, получающие в c в другой организации: откуда → куда, по ФЛ. Строка
# уникальна по (откуда, куда, ФЛ): lv и cp — различные пары, поэтому дальше
# count(*), а не count(DISTINCT).
T_MV = """
SELECT lv.inn AS inn_from, cp.inn AS inn_to, lv.epk_id
FROM (SELECT epk_id, inn FROM t_oflow WHERE side = 'lv') lv
JOIN (SELECT DISTINCT epk_id, inn FROM t_pairs WHERE report_dt = CAST(:c AS date)) cp
  ON cp.epk_id = lv.epk_id
"""

# Приёмники реорганизации: в одну организацию ушло ≥ :reorg_min_movers ФЛ и ≥
# :reorg_min_share ушедших.
T_SUCC = """
WITH lvn AS (SELECT inn, count(*) AS n_lv FROM t_oflow WHERE side = 'lv' GROUP BY inn),
pairs_mv AS (
  SELECT inn_from, inn_to, count(*) AS n_mv FROM t_mv GROUP BY inn_from, inn_to
)
SELECT m.inn_from, m.inn_to, m.n_mv, l.n_lv
FROM pairs_mv m JOIN lvn l ON l.inn = m.inn_from
WHERE m.n_mv >= :reorg_min_movers AND m.n_mv >= :reorg_min_share * l.n_lv
"""

# Итог по каждой организации базы: было, стало, ситуации ушедших и пришедших,
# нетто без реорганизации и пропуска месяца, реальное сокращение.
T_ORGSEL = """
WITH bp AS (SELECT DISTINCT epk_id, inn FROM t_pairs WHERE report_dt = CAST(:b AS date)),
cp AS (SELECT DISTINCT epk_id, inn FROM t_pairs WHERE report_dt = CAST(:c AS date)),
lv AS (SELECT epk_id, inn FROM t_oflow WHERE side = 'lv'),
jn AS (SELECT epk_id, inn FROM t_oflow WHERE side = 'jn'),
reorg_out AS (
  SELECT DISTINCT mv.inn_from AS inn, mv.epk_id
  FROM t_mv mv JOIN t_succ s ON s.inn_from = mv.inn_from AND s.inn_to = mv.inn_to
),
reorg_in AS (
  SELECT DISTINCT mv.inn_to AS inn, mv.epk_id
  FROM t_mv mv JOIN t_succ s ON s.inn_from = mv.inn_from AND s.inn_to = mv.inn_to
),
moved_out AS (SELECT DISTINCT inn_from AS inn, epk_id FROM t_mv),
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
)
SELECT * FROM sel
"""

# Список организаций с реальным сокращением + итоги по ВСЕМ отобранным (окнами,
# до LIMIT): «показано N из M» и сумма по всем считаются в SQL, а не по показанным.
ORG_LIST = """
WITH picked AS (
  SELECT s.* FROM t_orgsel s
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
    SELECT inn_from, inn_to, count(*) AS n_mv,
           row_number() OVER (PARTITION BY inn_from
                              ORDER BY count(*) DESC, inn_to) AS rn
    FROM t_mv GROUP BY inn_from, inn_to
  ) d WHERE rn = 1
)
SELECT p.*,
       COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg,
       o.org_name,
       o.holding_name,
       o.industry_name,
       COALESCE(o.focus, false)                AS focus,
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
ORG_SUMMARY = """
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
FROM t_orgsel s LEFT JOIN t_org o ON o.inn = s.inn
GROUP BY 1, 2
"""

# Разрезы организаций с реальным сокращением (по ВСЕМ отобранным, а не по
# показанным): сегмент, холдинг, отрасль. Рядом — вся база разреза: какая доля
# его численности пришлась на реальное сокращение. Холдингов и отраслей — верх
# по реальному сокращению (:break_max), выделенный холдинг — всегда.
_ORG_AGG = """count(*) AS n_orgs_all, sum(base_fl) AS base_all, sum(cur_fl) AS cur_all,
       sum(picked) AS n_orgs, sum(base_fl * picked) AS base_fl, sum(real_cut * picked) AS real_cut,
       sum(out_stopped * picked) AS out_stopped, sum(out_moved * picked) AS out_moved,
       sum(out_back * picked) AS out_back, sum(in_new * picked) AS in_new,
       sum(out_stopped) AS out_stopped_all, bool_or(focus) AS focus"""
ORG_BREAK = """
WITH s AS (
  SELECT s.*, COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg, o.holding_name, o.industry_name,
         COALESCE(o.focus, false) AS focus,
         CASE WHEN s.base_fl >= :min_base AND s.net_ex_reorg < 0 AND s.real_cut >= :min_real
               AND s.real_cut >= :min_share * s.base_fl THEN 1 ELSE 0 END AS picked
  FROM t_orgsel s LEFT JOIN t_org o ON o.inn = s.inn
),
g AS (
  SELECT 'seg' AS dim, seg AS key, CAST(NULL AS text) AS holding_name, """ + _ORG_AGG + """
  FROM s GROUP BY seg
  UNION ALL
  SELECT 'holding', NULL, holding_name, """ + _ORG_AGG + """
  FROM s WHERE holding_name IS NOT NULL GROUP BY holding_name
  UNION ALL
  SELECT 'industry', industry_name, NULL, """ + _ORG_AGG + """
  FROM s GROUP BY industry_name
)
SELECT * FROM (
  SELECT g.*, row_number() OVER (PARTITION BY dim ORDER BY real_cut DESC, n_orgs_all DESC) AS rn
  FROM g
) x
WHERE rn <= :break_max OR dim = 'seg' OR focus
"""

# Все организации выделенного холдинга (без порогов): было, стало, куда ушли.
ORG_FOCUS = """
WITH tbm AS (
  SELECT inn, tb_id FROM (
    SELECT inn, tb_id, row_number() OVER (PARTITION BY inn ORDER BY count(*) DESC, tb_id) AS rn
    FROM t_pairs WHERE report_dt = CAST(:b AS date) AND focus GROUP BY inn, tb_id
  ) t WHERE rn = 1
)
SELECT s.*, t.tb_id, COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg, o.org_name, o.holding_name,
       o.industry_name,
       COALESCE(o.is_liquidated, false) AS is_liquidated,
       CASE WHEN s.base_fl >= :min_base AND s.net_ex_reorg < 0 AND s.real_cut >= :min_real
             AND s.real_cut >= :min_share * s.base_fl THEN 1 ELSE 0 END AS picked
FROM t_orgsel s JOIN t_org o ON o.inn = s.inn AND o.focus
LEFT JOIN tbm t ON t.inn = s.inn
ORDER BY s.net_ex_reorg, s.inn
LIMIT :max_rows
"""

# Реорганизации, исключённые из списка: откуда → куда, сколько и какая доля.
ORG_REORG = """
SELECT s.inn_from, s.inn_to, s.n_mv, s.n_lv,
       f.org_name, t.org_name AS org_name_to,
       COALESCE(f.seg, '""" + S.NO_DIM + """') AS seg_from,
       COALESCE(f.is_liquidated, false) AS from_liquidated
FROM t_succ s
LEFT JOIN t_org f ON f.inn = s.inn_from
LEFT JOIN t_org t ON t.inn = s.inn_to
ORDER BY s.n_mv DESC, s.inn_from
LIMIT :max_rows
"""


# --------------------------------------------------------------------------- #
# Август: потеря или перенос в следующий месяц
# --------------------------------------------------------------------------- #

# Организации, пропустившие месяц: в :m_prev у id орг не меньше :hole_min_base
# получателей, в :m — не больше половины, в :m_next — снова не меньше 80% от
# :m_prev. Признак переноса даты выплаты организацией, а не ухода людей.
# Итоги — по всем id орг окнами, в ядро едет только верх списка.
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
SELECT * FROM (
  SELECT h.inn, h.n_prev, h.n_cur, h.n_next, h.hole,
         COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg,
         o.org_name, o.holding_name, o.industry_name,
         COALESCE(o.focus, false) AS focus,
         count(*) OVER ()    AS n_orgs,
         sum(h.hole) OVER () AS sum_hole,
         row_number() OVER (ORDER BY h.hole DESC, h.inn) AS rn
  FROM h LEFT JOIN t_org o ON o.inn = h.inn
) z
WHERE rn <= :max_rows OR focus
ORDER BY rn
"""

# Пропустившие месяц — по разрезам: сегмент, холдинг, отрасль. Знаменатель —
# все организации разреза с базой ≥ :hole_min_base в :m_prev: «пропустили 12 из
# 40» говорит о закономерности, «пропустили 12» — нет.
_HOLE_AGG = """count(*) AS n_orgs_base, sum(n_prev) AS n_prev_all, sum(hit) AS n_orgs,
       sum(hit * (n_prev - n_cur)) AS hole, bool_or(focus) AS focus"""
ORG_HOLE_DIM = """
WITH x AS (
  SELECT inn,
         count(*) FILTER (WHERE report_dt = CAST(:m_prev AS date)) AS n_prev,
         count(*) FILTER (WHERE report_dt = CAST(:m AS date))      AS n_cur,
         count(*) FILTER (WHERE report_dt = CAST(:m_next AS date)) AS n_next
  FROM t_pairs
  WHERE report_dt IN (CAST(:m_prev AS date), CAST(:m AS date), CAST(:m_next AS date))
  GROUP BY inn
),
s AS (
  SELECT x.*,
         CASE WHEN x.n_cur <= 0.5 * x.n_prev AND x.n_next >= 0.8 * x.n_prev THEN 1 ELSE 0 END AS hit,
         COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg, o.holding_name, o.industry_name,
         COALESCE(o.focus, false) AS focus
  FROM x LEFT JOIN t_org o ON o.inn = x.inn
  WHERE x.n_prev >= :hole_min_base
),
g AS (
  SELECT 'seg' AS dim, seg AS key, CAST(NULL AS text) AS holding_name, """ + _HOLE_AGG + """
  FROM s GROUP BY seg
  UNION ALL
  SELECT 'holding', NULL, holding_name, """ + _HOLE_AGG + """
  FROM s WHERE holding_name IS NOT NULL GROUP BY holding_name
  UNION ALL
  SELECT 'industry', industry_name, NULL, """ + _HOLE_AGG + """
  FROM s GROUP BY industry_name
)
SELECT * FROM (
  SELECT g.*, row_number() OVER (PARTITION BY dim ORDER BY hole DESC, n_orgs_base DESC) AS rn
  FROM g
) y
WHERE rn <= :break_max OR dim = 'seg' OR focus
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
SHOW_DEFS["t_ptype"] = T_PTYPE
SHOW_DEFS["t_oflow"] = T_OFLOW
SHOW_DEFS["t_mv"] = T_MV
SHOW_DEFS["t_succ"] = T_SUCC
SHOW_DEFS["t_orgsel"] = T_ORGSEL


# Все запросы файла — для самопроверок (партиция, диалект, маска номера организации).
def all_sql() -> dict[str, str]:
    return {k: v for k, v in globals().items()
            if k.isupper() and isinstance(v, str) and ("SELECT" in v or "CREATE" in v)}
