"""SQL для дэша tb_health. {schema} подставляется в db.read_sql, значения — :params.

Опорная точка — макс. месяц метрик (текущая ситуация). Активности — окно 3 мес
от макс. отчётной даты воронки. Ранги ТБ считаются window-функцией.
"""

METRIC_FOT = 1000164          # Общий ФОТ, млн ₽
METRIC_RECIPIENTS = 12400196  # Количество уникальных получателей до ИНН

# Резолв ТБ по короткому имени -> tb_id, полное имя
TB_RESOLVE = """
SELECT DISTINCT tb_id, tb_short_name, tb_full_name
FROM {schema}.uzp_dim_gosb
WHERE tb_short_name = :tb
"""

# Вердикт по обеим метрикам за макс. месяц + ранг ТБ среди всех (лучший = 1)
TB_VERDICT = """
WITH mx AS (
  SELECT metric_id, max(end_dt) AS end_dt
  FROM {schema}.uzp_dwh_metrics
  WHERE level_name='tb' AND period_type='m' AND metric_id IN (:m_fot, :m_rcp)
  GROUP BY metric_id
),
tb AS (SELECT DISTINCT tb_id, tb_short_name FROM {schema}.uzp_dim_gosb)
SELECT m.metric_id, m.level_id AS tb_id, tb.tb_short_name, m.end_dt,
       m.plan_amt, m.fact_amt, m.execution_percent,
       rank() OVER (PARTITION BY m.metric_id ORDER BY m.execution_percent DESC) AS rnk,
       count(*) OVER (PARTITION BY m.metric_id) AS n_tb
FROM {schema}.uzp_dwh_metrics m
JOIN mx ON mx.metric_id=m.metric_id AND mx.end_dt=m.end_dt
JOIN tb ON tb.tb_id=m.level_id
WHERE m.level_name='tb' AND m.period_type='m' AND COALESCE(m.extended_dim_1,1)=1
"""

# Грейн дэша по ГОСБ — new_gosb_id (реальный ГОСБ). Метрики лежат на old_gosb_id,
# агрегируем old_gosb_id -> new_gosb_id (несколько old могут мапиться в один new).
_GMAP = """SELECT old_gosb_id, min(tb_id) AS tb_id, min(new_gosb_id) AS new_gosb_id,
                  min(new_gosb_name) AS gosb_name
           FROM {schema}.uzp_dim_gosb GROUP BY old_gosb_id"""

# Матрица ГОСБ×сегмент по получателям (макс. месяц), агрегат по new_gosb_id
GOSB_SEG = """
WITH gmap AS (""" + _GMAP + """),
mx AS (SELECT max(end_dt) d FROM {schema}.uzp_dwh_metrics
       WHERE level_name='gosb' AND period_type='m' AND metric_id=:m_rcp)
SELECT g.new_gosb_id, g.gosb_name,
       m.extended_dim_1 AS seg_id,
       sum(m.plan_amt) AS plan_amt, sum(m.fact_amt) AS fact_amt,
       sum(m.fact_amt) / NULLIF(sum(m.plan_amt), 0) AS execution_percent,
       sum(m.plan_amt - m.fact_amt) AS nedobor
FROM {schema}.uzp_dwh_metrics m
JOIN gmap g ON g.old_gosb_id=m.level_id
JOIN mx ON mx.d=m.end_dt
WHERE m.level_name='gosb' AND m.period_type='m' AND m.metric_id=:m_rcp
  AND g.tb_id=:tb_id AND m.extended_dim_1 <> 1
GROUP BY g.new_gosb_id, g.gosb_name, m.extended_dim_1
"""

# Итоги по ГОСБ (все сегменты, агрегат по new_gosb_id) — разрыв по ГОСБ
GOSB_TOTALS = """
WITH gmap AS (""" + _GMAP + """),
mx AS (SELECT max(end_dt) d FROM {schema}.uzp_dwh_metrics
       WHERE level_name='gosb' AND period_type='m' AND metric_id=:m_rcp)
SELECT g.new_gosb_id, g.gosb_name,
       sum(m.plan_amt) AS plan_amt, sum(m.fact_amt) AS fact_amt,
       sum(m.fact_amt) / NULLIF(sum(m.plan_amt), 0) AS execution_percent,
       sum(m.plan_amt - m.fact_amt) AS nedobor
FROM {schema}.uzp_dwh_metrics m
JOIN gmap g ON g.old_gosb_id=m.level_id
JOIN mx ON mx.d=m.end_dt
WHERE m.level_name='gosb' AND m.period_type='m' AND m.metric_id=:m_rcp
  AND g.tb_id=:tb_id AND COALESCE(m.extended_dim_1,1)=1
GROUP BY g.new_gosb_id, g.gosb_name
ORDER BY nedobor DESC
"""

# Организации ТБ (витрина) с сегментом и средней ЗП
ORGS = """
WITH gmap AS (""" + _GMAP + """),
mx AS (SELECT max(report_dt) d FROM {schema}.uzp_dwh_company_holding_metric)
SELECT c.org_id AS inn, dc.company_name, c.level_id AS gosb_id, g.new_gosb_id, g.gosb_name,
       dc.segment_name AS segment_big,
       c.current_fl_qty, c.current_fot_amt, c.zp_fl_perc, c.total_emp_qty, c.fl_y_1_diff_qty,
       c.emp_potential_qty, c.fot_potential_amt, c.fl_outflow_qty, c.fot_outflow_amt,
       CASE WHEN c.current_fl_qty>0 THEN c.current_fot_amt/c.current_fl_qty END AS avg_salary
FROM {schema}.uzp_dwh_company_holding_metric c
JOIN gmap g ON g.old_gosb_id=c.level_id
LEFT JOIN {schema}.uzp_dim_company dc ON dc.inn=c.org_id
JOIN mx ON mx.d=c.report_dt
WHERE g.tb_id=:tb_id
"""

# Активности воронки за последние 3 месяца по ТБ (со свободным текстом).
# gosb_name — из справочника (грейн дэша = имя ГОСБ).
FUNNEL_TB = """
WITH gmap AS (""" + _GMAP + """)
SELECT f.inn, f.company_name, f.gosb_id, g.new_gosb_id, g.gosb_name, f.segment_name, f.role_code, f.task_type,
       f.last_active_type, f.task_text_status, f.is_task_closed, f.is_task_closed_success,
       f.task_create_dt, f.last_active_dttm, f.unrealized_deal_potential,
       f.plan_staff_deal_qty, f.fact_staff_deal_qty, f.task_comment, f.task_questionnaire
FROM {schema}.uzp_dwh_sale_funnel_task f
LEFT JOIN gmap g ON g.old_gosb_id=f.gosb_id
WHERE f.tb_id=:tb_id
  AND f.task_create_dt >= (
      SELECT max(report_dt) FROM {schema}.uzp_dwh_sale_funnel_task
  ) - INTERVAL '3 months'
"""
