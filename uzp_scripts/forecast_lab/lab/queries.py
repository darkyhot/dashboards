"""SQL для forecast_lab. {schema} / {schema_t} подставляет uzp_dash.db.read_sql.

Все запросы читают ТОЛЬКО ЗАКРЫТЫЕ периоды: лаборатория проверяет, насколько точно
можно предсказать месяц M по данным месяцев < M. Колонки, смотрящие вперёд
(`new_fl_cnt_next_month`, `np_cnt_next_month`, `fl_next_m_qty`, `next_m_fl_val`),
не выбираются НИГДЕ — это решение постановки, и нарушить его случайной правкой
нельзя: список запрещённых имён проверяется тестом `lab/selfcheck.py`.

Грейн панели — (new_gosb_id, ИНН, месяц), тот же, что у дэша. Свёртка old -> new
берётся из общего CTE дэша (`tb_health.queries._GMAP`): второй копии соответствия
ГОСБ быть не должно — разъедутся молча, и лаборатория начнёт мерить не тот банк.
"""
from __future__ import annotations

from uzp_dash.dashboards.tb_health import queries as Q

# Метрики витрины — те же, что в отчёте (получатели основные, ФОТ проверкой).
METRIC_FOT = Q.METRIC_FOT
METRIC_RECIPIENTS = Q.METRIC_RECIPIENTS

# Колонки, которые лаборатории брать запрещено: они описывают БУДУЩИЙ месяц и
# превратили бы «прогноз» в чтение ответа. Список проверяется тестом
# `check_forbidden_columns` — он ищет эти имена во ВСЕХ запросах файла.
#
# prediction_amt в список НЕ входит намеренно: собственный прогноз витрины —
# не признак модели, а отдельный конкурент в таблице лидеров. Признаком он не
# становится нигде: читает его только `backtest.baselines`.
FORBIDDEN_COLUMNS = (
    "new_fl_cnt_next_month",
    "np_cnt_next_month",
    "fl_next_m_qty",
    "next_m_fl_val",
    "next_m_avg_salary_amt",
)

_GMAP = Q._GMAP     # noqa: SLF001 — единственный источник соответствия ГОСБ в проекте


# --------------------------------------------------------------------------- #
# Справочники и разметка выгрузки
# --------------------------------------------------------------------------- #

# Соответствие new_gosb_id -> список old_gosb_id и ТБ. Чанки выгрузки режутся по
# ГОСБ, а фильтровать выборку надо по СТАРОМУ id: он лежит в самой витрине
# (level_id), и фильтр по нему база применяет до join'а.
GOSB_MAP = """
WITH gmap AS (""" + _GMAP + """)
SELECT old_gosb_id, new_gosb_id, tb_id, gosb_name FROM gmap
"""

# Сколько строк даст каждый ГОСБ за всё окно. Нужно ДО выгрузки: чанки собираются
# так, чтобы ни один запрос не превысил лимит строк (правило 12 Datalab), а узнать
# это иначе можно только уронив тяжёлый запрос.
PANEL_COUNTS = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, count(*) AS n_rows
FROM {schema}.uzp_dwh_company_holding_metric c
JOIN gmap g ON g.old_gosb_id = c.level_id
WHERE c.level_name = 'gosb' AND c.org_type = 'inn'
  AND c.report_dt >= CAST(:d_from AS date) AND c.report_dt <= CAST(:d_to AS date)
GROUP BY g.new_gosb_id
"""


# --------------------------------------------------------------------------- #
# Панель: месячная история по организациям
# --------------------------------------------------------------------------- #

# Одна строка на (ГОСБ, ИНН, месяц). Свёртка old -> new суммированием — ровно как в
# ORGS_ALL и OUTFLOW_HIST_AGG дэша: несколько бывших отделений одного ГОСБ
# обслуживают РАЗНЫХ людей.
#
# Фильтр по :old_ids — список СТАРЫХ id чанка (см. GOSB_MAP). Он же гарантирует,
# что размер выборки предсказуем.
#
# Второй фильтр, по остатку от деления ИНН, нужен для ОДНОГО случая: ГОСБ, который
# сам по себе даёт больше лимита строк, режется на части. При :n_parts = 1 условие
# истинно всегда и на план запроса не влияет.
PANEL = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, g.tb_id, c.org_id AS inn,
       CAST(date_trunc('month', c.report_dt) AS date) AS ym,
       sum(COALESCE(c.current_fl_qty, 0))    AS fl,
       sum(COALESCE(c.fl_outflow_qty, 0))    AS out_q,
       sum(COALESCE(c.new_fl_cnt, 0))        AS new_fl,
       sum(COALESCE(c.np_cnt, 0))            AS np,
       sum(COALESCE(c.current_fot_amt, 0))   AS fot,
       sum(COALESCE(c.total_emp_qty, 0))     AS emp,
       sum(COALESCE(c.emp_potential_qty, 0)) AS pot
FROM {schema}.uzp_dwh_company_holding_metric c
JOIN gmap g ON g.old_gosb_id = c.level_id
WHERE c.level_name = 'gosb' AND c.org_type = 'inn'
  AND c.level_id = ANY(:old_ids)
  AND mod(abs(c.org_id), :n_parts) = :part
  AND c.report_dt >= CAST(:d_from AS date) AND c.report_dt <= CAST(:d_to AS date)
GROUP BY g.new_gosb_id, g.tb_id, c.org_id,
         CAST(date_trunc('month', c.report_dt) AS date)
"""

# Месячный факт оттока из ОТДЕЛЬНОЙ витрины. Дэш её не использует вовсе, а по
# методологии (§12.6) отток в company_holding_metric есть лишь у ~0.8% пар — если
# здесь он заполнен лучше, модель оттока имеет смысл строить именно отсюда.
# Таблицы может не быть вовсе (её нет и в синтетике) — вызывающий код ловит ошибку
# и помечает семейство как недоступное, см. fetch.fact_outflow_available().
FACT_OUTFLOW = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, f.inn,
       CAST(date_trunc('month', f.report_dt) AS date) AS ym,
       sum(COALESCE(f.outflow_qty, 0))     AS fo_out,
       sum(COALESCE(f.plan_payee_qty, 0))  AS fo_plan,
       sum(COALESCE(f.fact_payee_qty, 0))  AS fo_fact,
       sum(COALESCE(f.calc_fl_qty, 0))     AS fo_calc
FROM {schema}.uzp_dwh_fact_outflow f
JOIN gmap g ON g.old_gosb_id = f.gosb_id
WHERE f.gosb_id = ANY(:old_ids)
  AND mod(abs(f.inn), :n_parts) = :part
  AND f.report_dt >= CAST(:d_from AS date) AND f.report_dt <= CAST(:d_to AS date)
GROUP BY g.new_gosb_id, f.inn, CAST(date_trunc('month', f.report_dt) AS date)
"""

FACT_OUTFLOW_PROBE = """
SELECT count(*) AS n_rows, count(DISTINCT inn) AS n_inn,
       min(report_dt) AS d_min, max(report_dt) AS d_max
FROM {schema}.uzp_dwh_fact_outflow
"""

# Сколько вообще организаций в справочнике — чтобы заранее выбрать число частей.
# Иначе размер частей приходится угадывать, и на проме выборка упирается в лимит
# строк уже после того, как запрос отработал.
ORG_SEG_COUNT = """
SELECT count(DISTINCT inn) AS n FROM {schema}.uzp_dim_company WHERE inn IS NOT NULL
"""

# Сегмент организации. Берётся из справочника, а не из дневной витрины: дневная
# есть не за все месяцы, а сегмент нужен на КАЖДЫЙ оцениваемый месяц, чтобы
# сопоставить организации с плановой матрицей «единица × сегмент».
#
# Чанкование по остатку от деления ИНН безопасно: выборка сгруппирована по ИНН,
# поэтому организация целиком попадает ровно в одну часть.
ORG_SEG = """
SELECT dc.inn, min(dc.segment_name) AS segment_big
FROM {schema}.uzp_dim_company dc
WHERE dc.inn IS NOT NULL AND mod(abs(dc.inn), :n_parts) = :part
GROUP BY dc.inn
"""


# --------------------------------------------------------------------------- #
# Единицы разбора: план, факт и собственный прогноз витрины
# --------------------------------------------------------------------------- #

# Уровни gosb / tb / sb за все месяцы окна, обе метрики, с сегментом и без.
# Выборка крошечная (сотни строк на месяц), чанковать нечего.
#
# prediction_amt тянется НЕ как признак, а как отдельный конкурент в таблице
# лидеров: витрина публикует собственный прогноз, и его никто ни разу не сверял
# с фактом. Если он выигрывает — это и есть ответ на вопрос пользователя.
UNITS = """
SELECT m.level_name, m.level_id, m.metric_id, m.end_dt,
       COALESCE(m.extended_dim_1, 1) AS seg_id,
       sum(m.plan_amt)       AS plan_amt,
       sum(m.fact_amt)       AS fact_amt,
       sum(m.prediction_amt) AS pred_amt
FROM {schema}.uzp_dwh_metrics m
WHERE m.period_type = 'm'
  AND m.metric_id IN (:m_fot, :m_rcp)
  AND m.level_name IN ('gosb', 'tb', 'sb')
  AND m.end_dt >= CAST(:d_from AS date) AND m.end_dt <= CAST(:d_to AS date)
GROUP BY m.level_name, m.level_id, m.metric_id, m.end_dt,
         COALESCE(m.extended_dim_1, 1)
"""


# --------------------------------------------------------------------------- #
# Пайплайн сделок
# --------------------------------------------------------------------------- #

METRIC_NEW_RECIPIENTS_B2B = Q.METRIC_NEW_RECIPIENTS_B2B
MOTIV_COUNTED = Q.MOTIV_COUNTED

# План привлечения по месяцам с ЯВНЫМ месяцем создания сделки.
#
# Дэш месяц создания не возвращает, а лаборатории он нужен: прогноз строится
# 1–5 числа месяца M, и сделка, заведённая в самом M, к этому моменту ещё не
# существует. Поэтому план фильтруется условием «создана РАНЬШЕ планируемого
# месяца» — иначе перебор пользовался бы сведениями из будущего.
#
# Восстановление года из pl_month_num — как в дэше (окно жизни сделки 3 месяца);
# сдвиг умножением интервала, а не make_interval(months => …): Greenplum на ядре
# 9.4 разбирает «=>» как оператор.
PIPELINE_PLAN = """
WITH gmap AS (""" + _GMAP + """),
codes AS (
  SELECT COALESCE(f.deal_code, f.task_code) AS code,
         min(f.inn)         AS inn,
         min(g.new_gosb_id) AS new_gosb_id,
         min(date_trunc('month', COALESCE(f.deal_create_dttm,
                                          CAST(f.task_create_dt AS timestamp)))) AS m0
  FROM {schema}.uzp_dwh_sale_funnel_task f
  LEFT JOIN gmap g ON g.old_gosb_id = f.gosb_id
  WHERE f.inn IS NOT NULL
    AND f.task_create_dt <= CAST(:d_to AS date)
    AND COALESCE(f.deal_code, f.task_code) IS NOT NULL
  GROUP BY 1
),
resolved AS (
  SELECT c.new_gosb_id, c.inn,
         CAST(date_trunc('month', c.m0) AS date) AS m0_month,
         CAST(c.m0
              + ((p.pl_month_num - CAST(EXTRACT(MONTH FROM c.m0) AS int) + 12) % 12)
                * interval '1 month' AS date)                         AS plan_month,
         ((p.pl_month_num - CAST(EXTRACT(MONTH FROM c.m0) AS int) + 12) % 12) AS off_m,
         COALESCE(p.pl_plan_np_amt, 0)                                AS plan_np
  FROM codes c
  JOIN {schema_t}.yva_pl_task_deal_code p ON p.pl_task_deal_code = c.code
  WHERE c.new_gosb_id IS NOT NULL
)
SELECT new_gosb_id, inn, m0_month, plan_month, sum(plan_np) AS plan_np
FROM resolved
WHERE off_m <= 2 AND plan_month >= CAST(:d_from AS date)
GROUP BY new_gosb_id, inn, m0_month, plan_month
"""

# Сколько НП по сделкам реально пришло — ИТОГОМ по месяцу. По организациям не
# нужно: факт пайплайна участвует только в оценке доли реализуемости, а она
# пулированная (одна на банк за закрытые месяцы).
PIPELINE_FACT_TOTAL = """
SELECT CAST(date_trunc('month', report_dt) AS date) AS ym,
       sum(COALESCE(sales_amt, 0)) AS fact_np
FROM {schema}.uzp_data_mzp_motivation_detail_corr
WHERE metric_id = :m_np AND product_cmnt = :counted
  AND report_dt >= CAST(:d_from AS date) AND report_dt <= CAST(:d_to AS date)
GROUP BY CAST(date_trunc('month', report_dt) AS date)
ORDER BY 1
"""


# --------------------------------------------------------------------------- #
# Разведка данных (probe)
# --------------------------------------------------------------------------- #

# Глубина истории и заполненность признаков по месяцам. Отдельный вопрос — ЛАГ
# ЗАГРУЗКИ: отчёт строится 1–5 числа месяца M, и если закрытый месяц M−1 к этому
# моменту ещё не загружен, базой прогноза служит M−2. Меряем по modified_dttm.
PROBE_HISTORY = """
SELECT CAST(date_trunc('month', report_dt) AS date) AS ym,
       count(*)                                  AS n_rows,
       count(DISTINCT org_id)                    AS n_orgs,
       count(DISTINCT level_id)                  AS n_gosb,
       sum(CASE WHEN COALESCE(fl_outflow_qty,0) > 0 THEN 1 ELSE 0 END) AS n_out,
       sum(CASE WHEN COALESCE(new_fl_cnt,0)     > 0 THEN 1 ELSE 0 END) AS n_new_fl,
       sum(CASE WHEN COALESCE(np_cnt,0)         > 0 THEN 1 ELSE 0 END) AS n_np,
       sum(CASE WHEN COALESCE(total_emp_qty,0)  > 0 THEN 1 ELSE 0 END) AS n_emp,
       sum(CASE WHEN COALESCE(emp_potential_qty,0) > 0 THEN 1 ELSE 0 END) AS n_pot,
       sum(COALESCE(current_fl_qty, 0))          AS sum_fl,
       sum(COALESCE(fl_outflow_qty, 0))          AS sum_out,
       sum(COALESCE(new_fl_cnt, 0))              AS sum_new_fl,
       max(modified_dttm)                        AS loaded_at
FROM {schema}.uzp_dwh_company_holding_metric
WHERE level_name = 'gosb' AND org_type = 'inn'
GROUP BY CAST(date_trunc('month', report_dt) AS date)
ORDER BY 1
"""

# Сходимость уровней: сумма организаций против строки витрины метрик. Это ПОТОЛОК
# точности любого подхода «база единицы + сумма дельт по организациям»: если
# уровни расходятся на проценты, никакая модель на грейне организаций этого не
# перекроет, и правильный ответ — считать прогноз прямо на уровне единицы.
PROBE_LEVELS = """
WITH gmap AS (""" + _GMAP + """),
orgs AS (
  SELECT g.tb_id,
         CAST(date_trunc('month', c.report_dt) AS date) AS ym,
         sum(COALESCE(c.current_fl_qty, 0)) AS fl_orgs
  FROM {schema}.uzp_dwh_company_holding_metric c
  JOIN gmap g ON g.old_gosb_id = c.level_id
  WHERE c.level_name = 'gosb' AND c.org_type = 'inn'
  GROUP BY g.tb_id, CAST(date_trunc('month', c.report_dt) AS date)
),
mtr AS (
  SELECT level_id AS tb_id,
         CAST(date_trunc('month', end_dt) AS date) AS ym,
         sum(fact_amt) AS fl_metrics
  FROM {schema}.uzp_dwh_metrics
  WHERE period_type = 'm' AND level_name = 'tb' AND metric_id = :m_rcp
    AND COALESCE(extended_dim_1, 1) = 1
  GROUP BY level_id, CAST(date_trunc('month', end_dt) AS date)
)
SELECT o.ym, o.tb_id, o.fl_orgs, m.fl_metrics
FROM orgs o LEFT JOIN mtr m ON m.tb_id = o.tb_id AND m.ym = o.ym
ORDER BY o.ym, o.tb_id
"""

# Есть ли ВНУТРИМЕСЯЧНЫЕ срезы дневной витрины за прошлые месяцы. На главный
# горизонт (1–5 число) это не влияет — дневной витрины там ещё нет, — но от
# ответа зависит, можно ли вообще построить дополнительную кривую по дням.
PROBE_DAY = """
SELECT report_dt,
       count(DISTINCT act_dt) AS n_act_dt,
       min(act_dt)            AS act_min,
       max(act_dt)            AS act_max,
       count(*)               AS n_rows
FROM {schema}.uzp_dwh_day_outflow
GROUP BY report_dt
ORDER BY report_dt
"""

# Заполненность собственного прогноза витрины и глубина плана/факта по метрикам.
PROBE_METRICS = """
SELECT metric_id, level_name,
       CAST(date_trunc('month', end_dt) AS date) AS ym,
       count(*)                AS n_rows,
       count(plan_amt)         AS n_plan,
       count(fact_amt)         AS n_fact,
       count(prediction_amt)   AS n_pred
FROM {schema}.uzp_dwh_metrics
WHERE period_type = 'm' AND metric_id IN (:m_fot, :m_rcp)
  AND level_name IN ('gosb', 'tb', 'sb')
GROUP BY metric_id, level_name, CAST(date_trunc('month', end_dt) AS date)
ORDER BY 1, 2, 3
"""
