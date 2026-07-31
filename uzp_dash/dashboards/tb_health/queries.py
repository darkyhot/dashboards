"""SQL для дэша tb_health. {schema} / {schema_t} подставляются в db.read_sql,
значения — :params.

Дэш строится на ТЕКУЩИЙ (незакрытый) месяц :ref_cur — по прогнозу, потому что
окончательная ЗП-ведомость есть только за закрытый месяц. Опорные даты:
  :ref_cur    — прогнозный месяц (конец месяца), из uzp_dwh_day_outflow;
  :act_dt     — по какую дату в нём есть факт зачислений;
  :ref_closed — предыдущий, ЗАКРЫТЫЙ месяц: база прогноза (факт метрик оттуда);
  :ref_funnel — конец окна воронки, совпадает с :ref_cur.
Метрики фильтруются по end_dt = <нужный месяц> (а не по max(end_dt) — там бывают
будущие плановые месяцы). Активности — окно 3 мес до :ref_funnel.
"""

METRIC_FOT = 1000164          # Общий ФОТ, млн ₽
METRIC_RECIPIENTS = 12400196  # Количество уникальных получателей до ИНН
# Метрика витрины премирования: «Новые получатели b2b» — факт привлечения по сделкам
METRIC_NEW_RECIPIENTS_B2B = 1000636
# В факт идут только зачтённые строки: остальное — фрод и «для информации, не
# участвует в расчёте kpi», реального привлечения они не означают
MOTIV_COUNTED = "учтено"

# Закрытый месяц: последний день месяца из report_dt операционных витрин.
# Используется как ФОЛБЭК опорной даты, если ежедневная витрина пуста.
REF_DATE = """
SELECT max(report_dt) AS ref FROM {schema}.uzp_dwh_company_holding_metric
"""

# Опорные даты дэша. Ежедневная витрина живёт ТОЛЬКО за текущий месяц (один
# report_dt и один act_dt), поэтому именно она задаёт «сегодня»: прогнозный месяц
# и дату, по которую есть факт зачислений.
REF_CUR = """
SELECT r.ref_cur,
       (SELECT max(d.act_dt) FROM {schema}.uzp_dwh_day_outflow d
        WHERE d.report_dt = r.ref_cur) AS act_dt
FROM (SELECT max(report_dt) AS ref_cur FROM {schema}.uzp_dwh_day_outflow) r
WHERE r.ref_cur IS NOT NULL
"""

# Резолв ТБ по короткому имени -> tb_id, полное имя
TB_RESOLVE = """
SELECT DISTINCT tb_id, tb_short_name, tb_full_name
FROM {schema}.uzp_dim_gosb
WHERE tb_short_name = :tb
"""

# Вердикт по обеим метрикам за текущий месяц (:ref) + ранг ТБ среди всех (лучший = 1)
TB_VERDICT = """
WITH tb AS (SELECT DISTINCT tb_id, tb_short_name FROM {schema}.uzp_dim_gosb)
SELECT m.metric_id, m.level_id AS tb_id, tb.tb_short_name, m.end_dt,
       m.plan_amt, m.fact_amt, m.execution_percent,
       rank() OVER (PARTITION BY m.metric_id ORDER BY m.execution_percent DESC) AS rnk,
       count(*) OVER (PARTITION BY m.metric_id) AS n_tb
FROM {schema}.uzp_dwh_metrics m
JOIN tb ON tb.tb_id=m.level_id
WHERE m.level_name='tb' AND m.period_type='m' AND COALESCE(m.extended_dim_1,1)=1
  AND m.end_dt = :ref AND m.metric_id IN (:m_fot, :m_rcp)
"""

# Грейн дэша по ГОСБ — new_gosb_id (реальный ГОСБ). Метрики лежат на old_gosb_id,
# агрегируем old_gosb_id -> new_gosb_id (несколько old могут мапиться в один new).
_GMAP = """SELECT old_gosb_id, min(tb_id) AS tb_id, min(new_gosb_id) AS new_gosb_id,
                  min(new_gosb_name) AS gosb_name
           FROM {schema}.uzp_dim_gosb GROUP BY old_gosb_id"""

# Матрица ГОСБ×сегмент по получателям (макс. месяц), агрегат по new_gosb_id
GOSB_SEG = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, g.gosb_name,
       m.extended_dim_1 AS seg_id,
       sum(m.plan_amt) AS plan_amt, sum(m.fact_amt) AS fact_amt,
       sum(m.fact_amt) / NULLIF(sum(m.plan_amt), 0) AS execution_percent,
       sum(m.plan_amt - m.fact_amt) AS nedobor
FROM {schema}.uzp_dwh_metrics m
JOIN gmap g ON g.old_gosb_id=m.level_id
WHERE m.level_name='gosb' AND m.period_type='m' AND m.metric_id=:m_rcp
  AND m.end_dt = :ref AND g.tb_id=:tb_id AND m.extended_dim_1 <> 1
GROUP BY g.new_gosb_id, g.gosb_name, m.extended_dim_1
"""

# Итоги по ГОСБ (все сегменты, агрегат по new_gosb_id) — разрыв по ГОСБ
GOSB_TOTALS = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, g.gosb_name,
       sum(m.plan_amt) AS plan_amt, sum(m.fact_amt) AS fact_amt,
       sum(m.fact_amt) / NULLIF(sum(m.plan_amt), 0) AS execution_percent,
       sum(m.plan_amt - m.fact_amt) AS nedobor
FROM {schema}.uzp_dwh_metrics m
JOIN gmap g ON g.old_gosb_id=m.level_id
WHERE m.level_name='gosb' AND m.period_type='m' AND m.metric_id=:m_rcp
  AND m.end_dt = :ref AND g.tb_id=:tb_id AND COALESCE(m.extended_dim_1,1)=1
GROUP BY g.new_gosb_id, g.gosb_name
ORDER BY nedobor DESC
"""

# Организации ТБ (витрина) с сегментом и средней ЗП
ORGS = """
WITH gmap AS (""" + _GMAP + """)
SELECT c.org_id AS inn, dc.company_name, c.level_id AS gosb_id, g.new_gosb_id, g.gosb_name,
       dc.segment_name AS segment_big,
       c.current_fl_qty, c.current_fot_amt, c.zp_fl_perc, c.total_emp_qty, c.fl_y_1_diff_qty,
       c.emp_potential_qty, c.fot_potential_amt, c.fl_outflow_qty, c.fot_outflow_amt,
       CASE WHEN c.current_fl_qty>0 THEN c.current_fot_amt/c.current_fl_qty END AS avg_salary
FROM {schema}.uzp_dwh_company_holding_metric c
JOIN gmap g ON g.old_gosb_id=c.level_id
LEFT JOIN {schema}.uzp_dim_company dc ON dc.inn=c.org_id
WHERE g.tb_id=:tb_id AND c.report_dt = :ref
  AND c.org_type = 'inn'   -- только организации по ИНН (не holding/head_holding)
  -- работать можно только с закреплёнными в эталонной базе (грейн ГОСБ+ИНН).
  -- EXISTS, а не JOIN: в базе несколько срезов actual_dt на одну пару.
  AND EXISTS (SELECT 1 FROM {schema}.uzp_dim_mzp_reference_base rb
              WHERE rb.inn = c.org_id AND rb.gosb_id = g.new_gosb_id)
"""

# Сколько пар (ГОСБ, ИНН) закреплено в эталонной базе — для прогресса в тетрадке
ORGS_REF_STATS = """
WITH gmap AS (""" + _GMAP + """),
pairs AS (
  SELECT DISTINCT g.new_gosb_id, c.org_id AS inn
  FROM {schema}.uzp_dwh_company_holding_metric c
  JOIN gmap g ON g.old_gosb_id=c.level_id
  WHERE g.tb_id=:tb_id AND c.report_dt = :ref AND c.org_type = 'inn'
)
SELECT count(*) AS n_all,
       count(*) FILTER (
         WHERE EXISTS (SELECT 1 FROM {schema}.uzp_dim_mzp_reference_base rb
                       WHERE rb.inn = pairs.inn AND rb.gosb_id = pairs.new_gosb_id)
       ) AS n_ref
FROM pairs
"""

# Окно активностей: три КАЛЕНДАРНЫХ месяца — от первого дня месяца T-2 до конца
# месяца T (:ref_funnel). Задачи по метрикам идут месяцем позже метрик, поэтому
# :ref_funnel = :ref + 1 месяц. Пример: ref_funnel = 31.07 -> май, июнь, июль.
#
# Сделки оцениваются по ДАТЕ СОЗДАНИЯ СДЕЛКИ (deal_create_dttm), а не задачи:
# сделка, заведённая в последние два месяца (>= :fresh_from), считается свежей —
# получатели ещё не успели прийти, судить о недоработке рано.
# Ещё два признака, без которых задача выглядит недоработанной, хотя это не так:
#   is_in_progress — задача ЕЩЁ НЕ ЗАКРЫТА (статус «Новая»/«В работе»). Если она при
#     этом не просрочена, требовать результата рано: это нормальный ход работы.
#   deal_expected  — по задаче В ПРИНЦИПЕ ожидается сделка. Определяем ПО ДАННЫМ
#     (план сделки > 0 либо заведён deal_code), а не по вокабуляру task_type: у задач
#     типа «Задача» сделки нет и не будет, спрашивать по ним факт зачислений нельзя.
# Просрочку ловим по вхождению «просроч» (ILIKE), а не точным литералом статуса —
# формулировки статусов в АС различаются.
_FUNNEL_BASE = """
base AS (
  SELECT g.new_gosb_id, f.inn, f.role_code, f.task_type, f.last_active_type,
         f.task_text_status, f.is_task_closed_success, f.last_active_dttm,
         COALESCE(f.plan_staff_deal_qty, 0)      AS plan_staff_deal_qty,
         COALESCE(f.fact_staff_deal_qty, 0)      AS fact_staff_deal_qty,
         COALESCE(f.unrealized_deal_potential,0) AS unrealized_deal_potential,
         f.deal_create_dttm,
         (COALESCE(f.plan_staff_deal_qty, 0) > 0
          AND f.deal_create_dttm >= CAST(:fresh_from AS date)) AS is_fresh_deal,
         COALESCE(f.is_task_in_progress, NOT COALESCE(f.is_task_closed, false))
                                                              AS is_in_progress,
         COALESCE(f.task_text_status ILIKE '%просроч%', false) AS is_overdue,
         (COALESCE(f.plan_staff_deal_qty, 0) > 0
          OR f.deal_code IS NOT NULL)                         AS deal_expected,
         (COALESCE(btrim(f.task_comment), '') <> ''
          OR COALESCE(btrim(f.task_questionnaire), '') <> '') AS has_text
  FROM {schema}.uzp_dwh_sale_funnel_task f
  LEFT JOIN gmap g ON g.old_gosb_id = f.gosb_id
  WHERE f.tb_id = :tb_id
    AND f.task_create_dt >= CAST(:funnel_from AS date)
    AND f.task_create_dt <= CAST(:ref_funnel  AS date)
)"""

# Агрегат по (ГОСБ, ИНН) — по ВСЕМ активностям за 3 мес (не по последней задаче).
# Тяжёлые тексты не тянем: только флаг has_text.
FUNNEL_AGG = """
WITH gmap AS (""" + _GMAP + """),
""" + _FUNNEL_BASE + """
SELECT new_gosb_id, inn,
       count(*)                                                        AS n_tasks,
       sum(CASE WHEN last_active_type='Звонок'  THEN 1 ELSE 0 END)      AS n_calls,
       sum(CASE WHEN last_active_type='Встреча' THEN 1 ELSE 0 END)      AS n_meetings,
       sum(CASE WHEN is_task_closed_success THEN 1 ELSE 0 END)          AS n_success,
       bool_or(is_task_closed_success)                                  AS any_success,
       sum(CASE WHEN is_overdue THEN 1 ELSE 0 END)                      AS n_overdue,
       -- ещё в работе и НЕ просрочены: по таким требовать результата рано
       sum(CASE WHEN is_in_progress AND NOT is_overdue THEN 1 ELSE 0 END) AS n_in_progress,
       sum(CASE WHEN NOT is_in_progress THEN 1 ELSE 0 END)              AS n_closed,
       bool_or(deal_expected)                                           AS deal_expected,
       sum(CASE WHEN task_type='Отток' THEN 1 ELSE 0 END)               AS n_outflow,
       sum(plan_staff_deal_qty)                                         AS plan_deal,
       sum(fact_staff_deal_qty)                                         AS fact_deal,
       -- недоработку считаем ТОЛЬКО по сделкам, созданным до :fresh_from
       sum(CASE WHEN NOT is_fresh_deal THEN plan_staff_deal_qty ELSE 0 END) AS plan_deal_old,
       sum(CASE WHEN NOT is_fresh_deal THEN fact_staff_deal_qty ELSE 0 END) AS fact_deal_old,
       bool_or(is_fresh_deal)                                           AS has_fresh_deal,
       max(CASE WHEN is_fresh_deal THEN deal_create_dttm END)           AS fresh_deal_dt,
       sum(unrealized_deal_potential)                                   AS unrealized,
       bool_or(has_text)                                                AS any_text,
       max(last_active_dttm)                                            AS last_active
FROM base
GROUP BY new_gosb_id, inn
"""

# Итоги активностей по ТБ (для блока «Активности за 3 месяца»)
ACTIVITY_TOTALS = """
WITH gmap AS (""" + _GMAP + """),
""" + _FUNNEL_BASE + """
SELECT count(*) AS n, count(DISTINCT inn) AS orgs,
       sum(CASE WHEN last_active_type='Звонок'  THEN 1 ELSE 0 END) AS calls,
       sum(CASE WHEN last_active_type='Встреча' THEN 1 ELSE 0 END) AS meetings,
       avg(CASE WHEN is_task_closed_success THEN 1.0 ELSE 0.0 END) AS success_rate,
       sum(CASE WHEN is_overdue THEN 1 ELSE 0 END) AS overdue,
       sum(plan_staff_deal_qty) AS plan_deal,
       sum(fact_staff_deal_qty) AS fact_deal,
       sum(unrealized_deal_potential) AS unrealized
FROM base
"""

# Разрезы активностей: по ролям / типам задач / статусам отработки
ACTIVITY_BREAKDOWN = """
WITH gmap AS (""" + _GMAP + """),
""" + _FUNNEL_BASE + """
SELECT 'role' AS dim, COALESCE(role_code,'—') AS k, count(*) AS n FROM base GROUP BY 2
UNION ALL
SELECT 'type', COALESCE(task_type,'—'), count(*) FROM base GROUP BY 2
UNION ALL
SELECT 'status', COALESCE(task_text_status,'—'), count(*) FROM base GROUP BY 2
"""

# Свободный текст ТОЛЬКО по приоритетным ИНН — все содержательные активности
# каждой пары (ГОСБ, ИНН) за 3 месяца (не одна последняя).
#
# Для хронологии и поиска противоречий тянем АВТОРА (табельный isu_struct_saphr_id —
# ФИО НЕ тянем: для «противоречий между сотрудниками» достаточно РАЗЛИЧАТЬ авторов),
# роль, дату создания задачи, факт закрытия и признак успеха. Числа по сделкам/
# потенциалу/оттоку здесь не нужны — они берутся из FUNNEL_AGG/ORGS (уже с *_old).
# Плюс признак «задача ещё в работе» и «по задаче есть/ожидается сделка» — чтобы в
# хронологии было видно, с какой задачи вообще правомерно спрашивать результат.
# NULLS LAST: задачи без активности не должны всплывать первыми и занимать лимит.
FUNNEL_TEXT = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, f.inn, f.task_type, f.task_subtype, f.task_text_status,
       f.task_comment, f.task_questionnaire, f.last_active_dttm,
       f.isu_struct_saphr_id AS author_id, f.role_code,
       f.task_create_dt, f.fact_close_task_dttm, f.is_task_closed_success,
       COALESCE(f.is_task_in_progress, NOT COALESCE(f.is_task_closed, false))
                                                             AS is_in_progress,
       COALESCE(f.task_text_status ILIKE '%просроч%', false)  AS is_overdue,
       (COALESCE(f.plan_staff_deal_qty, 0) > 0
        OR f.deal_code IS NOT NULL)                           AS deal_expected
FROM {schema}.uzp_dwh_sale_funnel_task f
LEFT JOIN gmap g ON g.old_gosb_id = f.gosb_id
WHERE f.tb_id = :tb_id
  AND f.inn = ANY(:inns)
  AND f.task_create_dt >= CAST(:funnel_from AS date)
  AND f.task_create_dt <= CAST(:ref_funnel  AS date)
  AND (COALESCE(btrim(f.task_comment), '') <> ''
       OR COALESCE(btrim(f.task_questionnaire), '') <> '')
ORDER BY f.inn, g.new_gosb_id, f.last_active_dttm DESC NULLS LAST
"""

# Справка по организациям ТБ для детализации прогноза: название, тренд год к году
# и признак «закреплена в эталонной базе».
#
# Это ORGS БЕЗ фильтра эталонной базы — намеренно: в ожидаемый отток входят и
# организации вне базы (на тестовом ТБ это ~12% оттока), и без них детализация
# не сойдётся с водопадом. Флаг in_ref показывает, можно ли с парой работать.
ORG_DETAIL = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, c.org_id AS inn, dc.company_name,
       COALESCE(c.fl_y_1_diff_qty, 0) AS fl_yoy,
       COALESCE(c.current_fl_qty, 0)  AS current_fl_qty,
       EXISTS (SELECT 1 FROM {schema}.uzp_dim_mzp_reference_base rb
               WHERE rb.inn = c.org_id AND rb.gosb_id = g.new_gosb_id) AS in_ref
FROM {schema}.uzp_dwh_company_holding_metric c
JOIN gmap g ON g.old_gosb_id = c.level_id
LEFT JOIN {schema}.uzp_dim_company dc ON dc.inn = c.org_id
WHERE g.tb_id = :tb_id AND c.report_dt = :ref_closed AND c.org_type = 'inn'
"""

# ==================== Прогноз на текущий месяц ============================== #

# Ежедневный отток: сколько получателей прошлого месяца ещё НЕ зачислились, хотя
# их выплатная дата уже прошла. Грейн (ГОСБ, ИНН).
#
# min(outflow_unpaid_m_qty) — по требованию бизнеса: у организации в месяце
# обычно две выплаты (аванс + основная), и отток надо брать ПО ИТОГУ обеих: если
# сотрудник получил хотя бы на одну из дат, он не отток.
#
# paid_mtd / fl_prev_m нужны для стыковки с прогнозным оттоком: доля уже
# зачислившихся показывает, сколько риска месяца уже отыграно (см. forecast.reconcile).
# segment_name здесь КОРОТКИЙ (ММБ/КСБ/…) — это основной источник сегмента
# организации, справочник uzp_dim_company идёт фолбэком.
DAY_OUTFLOW = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, d.org_inn AS inn,
       max(d.segment_name)                        AS seg_day,
       min(d.outflow_unpaid_m_qty)                AS out_observed,
       max(d.fact_fl_qty)                         AS paid_mtd,
       max(d.fl_prev_m_qty)                       AS fl_prev_m,
       max(d.m_avg_salary_amt)                    AS avg_salary_m,
       count(DISTINCT d.payment_order_num)        AS n_payments
FROM {schema}.uzp_dwh_day_outflow d
LEFT JOIN gmap g ON g.old_gosb_id = d.gosb_id
WHERE d.report_dt = :ref_cur AND d.act_dt = :act_dt AND d.tb_id = :tb_id
GROUP BY g.new_gosb_id, d.org_inn
"""

# История витрины по (ГОСБ, ИНН) за :hist_from … :ref_closed — под модель оттока
# (устойчивый отток два закрытых месяца подряд + сезонность год к году).
# Тянем только то, что нужно модели: сам отток и численность получателей.
OUTFLOW_HISTORY = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, c.org_id AS inn, c.report_dt,
       COALESCE(c.fl_outflow_qty, 0) AS fl_outflow_qty,
       COALESCE(c.current_fl_qty, 0) AS current_fl_qty
FROM {schema}.uzp_dwh_company_holding_metric c
JOIN gmap g ON g.old_gosb_id = c.level_id
WHERE g.tb_id = :tb_id AND c.org_type = 'inn'
  AND c.report_dt > CAST(:hist_from AS date)
  AND c.report_dt <= CAST(:ref_closed AS date)
"""

# Пайплайн: сколько НП сотрудник запланировал ИМЕННО на текущий месяц.
#
# В uzp_dwh_sale_funnel_task.plan_staff_deal_qty план размазан на все 3 месяца
# жизни сделки; помесячная разбивка есть только в yva_pl_task_deal_code, ключ —
# coalesce(deal_code, task_code).
#
# CTE codes ОБЯЗАТЕЛЬНА: один и тот же deal_code встречается в нескольких строках
# воронки (несколько задач по сделке), и join напрямую задвоил бы план.
# Окно воронки то же, что у активностей: сделка живёт 3 месяца, поэтому запланировать
# текущий месяц могли только сделки этого окна.
# План пайплайна ПО МЕСЯЦАМ на грейне (ГОСБ, ИНН, СОТРУДНИК).
#
# Грейн именно такой, потому что сравнивать факт надо с АГРЕГАТОМ планов: сотрудник
# мог завести две сделки по одной организации и обе запланировать на один месяц
# (10 и 15) — факт месяца относится к их сумме (25), а не к каждой сделке отдельно.
#
# Восстановление ГОДА. В pl_month_num лежит только НОМЕР месяца. Сделка живёт три
# месяца, поэтому планировать она может лишь месяц создания m0, m0+1 или m0+2:
#     off = (pl_month_num − месяц(m0) + 12) % 12,   строка годна при off <= 2
# и тогда план относится к месяцу m0 + off. Строки с off > 2 несогласованы и
# отбрасываются (их доля печатается в прогресс).
PIPELINE_PLAN_M = """
WITH gmap AS (""" + _GMAP + """),
codes AS (
  SELECT COALESCE(f.deal_code, f.task_code) AS code,
         f.inn,
         min(g.new_gosb_id)            AS new_gosb_id,
         min(f.isu_struct_saphr_id)    AS saphr_id,
         min(f.segment_name)           AS seg_funnel,
         min(date_trunc('month', COALESCE(f.deal_create_dttm,
                                          CAST(f.task_create_dt AS timestamp)))) AS m0
  FROM {schema}.uzp_dwh_sale_funnel_task f
  LEFT JOIN gmap g ON g.old_gosb_id = f.gosb_id
  WHERE f.tb_id = :tb_id
    AND f.task_create_dt >= CAST(:plan_from AS date)
    AND f.task_create_dt <= CAST(:ref_funnel AS date)
    AND COALESCE(f.deal_code, f.task_code) IS NOT NULL
  GROUP BY 1, 2
),
resolved AS (
  SELECT c.new_gosb_id, c.inn, c.saphr_id, c.seg_funnel, c.code,
         -- КОНЕЦ месяца: report_dt в витрине факта тоже конец месяца, иначе не сойдётся.
         -- Сдвиг через умножение интервала, а НЕ make_interval(months => …): именованные
         -- аргументы через «=>» появились только в PostgreSQL 9.5, а Greenplum стоит на
         -- ядре 9.4 и разбирает «=>» как оператор («column months does not exist»).
         CAST(c.m0
              + ((p.pl_month_num - CAST(EXTRACT(MONTH FROM c.m0) AS int) + 12) % 12)
                * interval '1 month'
              + interval '1 month' - interval '1 day' AS date)        AS plan_month,
         ((p.pl_month_num - CAST(EXTRACT(MONTH FROM c.m0) AS int) + 12) % 12) AS off_m,
         COALESCE(p.pl_plan_np_amt, 0)  AS plan_np,
         COALESCE(p.pl_plan_fot_amt, 0) AS plan_fot
  FROM codes c
  JOIN {schema_t}.yva_pl_task_deal_code p ON p.pl_task_deal_code = c.code
)
SELECT new_gosb_id, inn, saphr_id, plan_month,
       min(seg_funnel)       AS seg_funnel,
       sum(plan_np)          AS plan_np,
       sum(plan_fot)         AS plan_fot,
       count(DISTINCT code)  AS n_deals
FROM resolved
WHERE off_m <= 2
GROUP BY new_gosb_id, inn, saphr_id, plan_month
"""

# Доля строк пайплайна, у которых месяц плана не попадает в 3 месяца жизни сделки:
# считать их нельзя (год не восстанавливается), но молчать о них тоже нельзя.
PIPELINE_PLAN_STATS = """
WITH gmap AS (""" + _GMAP + """),
codes AS (
  SELECT COALESCE(f.deal_code, f.task_code) AS code,
         min(date_trunc('month', COALESCE(f.deal_create_dttm,
                                          CAST(f.task_create_dt AS timestamp)))) AS m0
  FROM {schema}.uzp_dwh_sale_funnel_task f
  LEFT JOIN gmap g ON g.old_gosb_id = f.gosb_id
  WHERE f.tb_id = :tb_id
    AND f.task_create_dt >= CAST(:plan_from AS date)
    AND f.task_create_dt <= CAST(:ref_funnel AS date)
    AND COALESCE(f.deal_code, f.task_code) IS NOT NULL
  GROUP BY 1
)
SELECT count(*) AS n_all,
       count(*) FILTER (
         WHERE ((p.pl_month_num - CAST(EXTRACT(MONTH FROM c.m0) AS int) + 12) % 12) > 2
       ) AS n_bad
FROM codes c
JOIN {schema_t}.yva_pl_task_deal_code p ON p.pl_task_deal_code = c.code
"""

# Сколько НП по сделкам реально пришло — ФАКТ, помесячно, тот же грейн.
# report_dt здесь — месяц ИЗ ПАЙПЛАЙНА, на который сделка обещала привлечение.
# Берём только зачтённые строки: фрод и «для информации» реального привлечения
# не означают. metric_id — «Новые получатели b2b».
PIPELINE_FACT_M = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, m.inn, m.saphr_id,
       CAST(date_trunc('month', m.report_dt)
            + interval '1 month' - interval '1 day' AS date) AS plan_month,
       sum(COALESCE(m.sales_amt, 0)) AS fact_np
FROM {schema}.uzp_data_mzp_motivation_detail_corr m
LEFT JOIN gmap g ON g.old_gosb_id = m.gosb_id
WHERE m.tb_id = :tb_id
  AND m.metric_id = :m_np
  AND m.product_cmnt = :counted
  AND m.report_dt >= CAST(:plan_from AS date)
  AND m.report_dt <= CAST(:ref_cur   AS date)
GROUP BY g.new_gosb_id, m.inn, m.saphr_id, 4
"""

# Сколько строк отсекается фильтром «учтено» — для прогресса (доверие к цифре)
PIPELINE_FACT_STATS = """
SELECT count(*) AS n_all,
       count(*) FILTER (WHERE product_cmnt = :counted) AS n_counted,
       sum(COALESCE(sales_amt, 0)) AS amt_all,
       sum(COALESCE(sales_amt, 0)) FILTER (WHERE product_cmnt = :counted) AS amt_counted
FROM {schema}.uzp_data_mzp_motivation_detail_corr
WHERE tb_id = :tb_id AND metric_id = :m_np
  AND report_dt >= CAST(:plan_from AS date) AND report_dt <= CAST(:ref_cur AS date)
"""
