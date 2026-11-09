"""SQL для дэша tb_health. {schema} / {schema_t} подставляются в db.read_sql,
значения — :params.

Дэш строится на ТЕКУЩИЙ (незакрытый) месяц :ref_cur — по ПРОГНОЗУ ВИТРИНЫ
(uzp_dwh_metrics.prediction_amt), потому что окончательная ЗП-ведомость есть только
за закрытый месяц. Свой прогноз дэш не считает. Опорные даты:
  :ref_cur    — прогнозный месяц (конец месяца): самый свежий с prediction_amt;
  :ref_closed — предыдущий, ЗАКРЫТЫЙ месяц: портфель-база (факт метрик оттуда);
  :m_out1..3  — три закрытых месяца (T-1, T-2, T-3) под фактический отток;
  :ref_funnel — конец окна воронки, совпадает с :ref_cur.
Метрики фильтруются по end_dt = <нужный месяц> (а не по max(end_dt) — там бывают
будущие плановые месяцы). Активности — окно 3 мес до :ref_funnel.

ВСЕ запросы идут ПО ВСЕМУ БАНКУ: отчёт строится одним проходом, а разрез по ТБ
делается уже в pandas. Поэтому :tb_id здесь нет нигде, зато почти везде есть
колонка tb_id — по ней уровень отчёта режет готовые кадры.

Уровень витрины. И uzp_dwh_metrics, и uzp_dwh_company_holding_metric хранят
НЕСКОЛЬКО уровней в одной таблице (level_name = 'sb' / 'tb' / 'gosb'), а level_id —
это номер соответствующей единицы. Фильтр по level_name ОБЯЗАТЕЛЕН в каждом
запросе: номера ТБ (2, 17, 38, 52 …) встречаются и среди old_gosb_id, поэтому без
него join по old_gosb_id = level_id затягивает агрегатные строки уровня ТБ в
разбор обычного ГОСБ — числа раздуваются в разы.
"""

# Две бизнес-метрики отчёта — версии ПО БАЛАНСУ (uzp_dim_metric). Раньше читались
# 1000164 «Общий ФОТ» и 12400196 «Количество уникальных получателей до ИНН».
# Меняются ТОЛЬКО здесь: в SQL id уходят параметрами :m_fot / :m_rcp, а синтетика
# импортирует эти же константы — разъехаться им негде.
METRIC_FOT = 1020389          # Обьем ФОТ(по балансу), в рублях (в отчёте ÷ 1e6)
METRIC_RECIPIENTS = 1020377   # Портфель зарплатных ФЛ до ИНН(по балансу), чел
# Метрика витрины премирования: «Новые получатели b2b» — факт привлечения по сделкам
METRIC_NEW_RECIPIENTS_B2B = 1000636
# В факт идут только зачтённые строки: остальное — фрод и «для информации, не
# участвует в расчёте kpi», реального привлечения они не означают
MOTIV_COUNTED = "учтено"

# Центральный аппарат — не территориальный банк и не продающая единица сети: плана
# по ФОТ на него не ставят, и как единица разбора он смысла не имеет. Исключается
# ЕДИНОЖДЫ здесь, во всех запросах, которые перечисляют ТБ и подразделения, — тогда
# про него не знают ни список вкладок, ни ранг ТБ, ни свод уровня СБ, ни карточки.
# Вердикт банка при этом по-прежнему берётся строкой level_name='sb', где ЦА внутри:
# уровни витрины считаются независимо, и сумма единиц ей и так не равна (раздел 11).
EXCLUDE_TB = ("ЦА",)
_NO_CA = "tb_short_name NOT IN (" + ", ".join(f"'{t}'" for t in EXCLUDE_TB) + ")"

# Закрытый месяц: последний день месяца из report_dt операционных витрин.
# Используется как ФОЛБЭК опорной даты, если ежедневная витрина пуста.
REF_DATE = """
SELECT max(report_dt) AS ref FROM {schema}.uzp_dwh_company_holding_metric
"""

# Опорный месяц отчёта по умолчанию — САМЫЙ СВЕЖИЙ месяц, на который витрина уже
# посчитала ПРОГНОЗ. Отчёт теперь целиком про витринный прогноз, и привязываться
# логично к его наличию: месяц без prediction_amt показывать нечем.
# Раньше якорем была ежедневная витрина оттока — она из дэша убрана.
REF_CUR = """
SELECT max(end_dt) AS ref_cur
FROM {schema}.uzp_dwh_metrics
WHERE period_type='m' AND prediction_amt IS NOT NULL
  AND metric_id = :m_rcp AND level_name = 'sb'
"""

# Все ТБ — по вкладке на каждый плюс сводный уровень СБ
TB_LIST = """
SELECT DISTINCT tb_id, tb_short_name, tb_full_name
FROM {schema}.uzp_dim_gosb
WHERE tb_short_name IS NOT NULL AND """ + _NO_CA + """
ORDER BY tb_short_name
"""

# Вердикты ВСЕХ уровней области разбора за ВСЕ нужные месяцы — одним запросом.
# Раньше это было шесть отдельных чтений на каждый ТБ (текущий месяц, закрытый,
# год назад × уровень) — при 12 ТБ порядка сорока запросов на одну и ту же таблицу.
#
# Уровень банка живёт ТОЛЬКО здесь, как level_name='sb': план и факт СБ читаются
# отсюда, а НЕ складываются из ТБ (сумма ТБ витрине не равна — см. раздел 11
# методологии). Ранга у банка нет: сравнивать его не с кем, поэтому окно ранга
# разбито и по level_name — у 'sb' в нём одна строка.
#
# tb_short_name приклеивается LEFT JOIN-ом (у строки 'sb' его нет), а отбор
# «level_id действительно является ТБ» вынесен в EXISTS: раньше это делал INNER
# JOIN, и без него в ранг попали бы строки уровня tb с чужим level_id.
METRICS_VERDICT = """
WITH tb AS (SELECT DISTINCT tb_id, tb_short_name FROM {schema}.uzp_dim_gosb
            WHERE tb_short_name IS NOT NULL AND """ + _NO_CA + """)
SELECT m.level_name, m.metric_id, m.level_id, tb.tb_short_name, m.end_dt,
       m.plan_amt, m.fact_amt, m.execution_percent,
       -- ПРОГНОЗ витрины: дэш его больше не считает, а берёт готовым
       m.prediction_amt AS pred_amt, m.prediction_percent AS pred_pct,
       rank() OVER (PARTITION BY m.metric_id, m.end_dt, m.level_name
                    ORDER BY m.execution_percent DESC)               AS rnk,
       count(*) OVER (PARTITION BY m.metric_id, m.end_dt, m.level_name) AS n_tb
FROM {schema}.uzp_dwh_metrics m
LEFT JOIN tb ON tb.tb_id = m.level_id AND m.level_name = 'tb'
WHERE m.period_type='m' AND COALESCE(m.extended_dim_1,1)=1
  AND m.end_dt IN (:ref_cur, :ref_closed, :ref_yoy)
  AND m.metric_id IN (:m_fot, :m_rcp)
  AND (m.level_name = 'sb'
       OR (m.level_name = 'tb'
           AND EXISTS (SELECT 1 FROM tb WHERE tb.tb_id = m.level_id)))
"""

# Грейн дэша по ГОСБ — new_gosb_id (реальный ГОСБ). Метрики лежат на old_gosb_id,
# агрегируем old_gosb_id -> new_gosb_id (несколько old могут мапиться в один new).
#
# Здесь же отсекается ЦА — и этого достаточно на весь отчёт: общий CTE используют
# почти все запросы, и подразделения ЦА просто перестают существовать в грейне.
# Строки с пустым tb_short_name фильтр не затрагивает: их в справочнике нет.
_GMAP = """SELECT old_gosb_id, min(tb_id) AS tb_id, min(new_gosb_id) AS new_gosb_id,
                  min(new_gosb_name) AS gosb_name
           FROM {schema}.uzp_dim_gosb WHERE """ + _NO_CA + """
           GROUP BY old_gosb_id"""

# Состав подразделений: к какому ТБ относится и сколько их всего в этом ТБ.
# Число нужно для правила аппарата: аппарат исключается из разбора, НО если он
# единственное подразделение своего ТБ (Московский банк), исключать нельзя — ТБ
# обнулился бы. Считаем в SQL, чтобы правило не зависело от того, какие ТБ грузятся.
#
# Этот же запрос — ЕДИНСТВЕННЫЙ источник соответствия ГОСБ → ТБ для всего отчёта.
# Один ГОСБ обязан принадлежать ровно одному ТБ, иначе его организации попали бы в
# разбор двух ТБ сразу и свод по банку задвоился бы.
GOSB_FLAGS = """
WITH gmap AS (""" + _GMAP + """),
u AS (SELECT new_gosb_id, min(tb_id) AS tb_id, min(gosb_name) AS gosb_name
      FROM gmap GROUP BY new_gosb_id)
SELECT u.new_gosb_id, u.tb_id, u.gosb_name,
       count(*) OVER (PARTITION BY u.tb_id) AS n_gosb
FROM u
"""

# Матрица «единица × сегмент» по получателям и итоги единиц — ПО ВСЕМУ БАНКУ, СРАЗУ
# ЗА ОБА месяца (:ref_closed — база, :ref_cur — план) и СРАЗУ ЗА ОБА УРОВНЯ.
# Месяц и уровень остаются колонками (end_dt, level_name): раньше это были четыре
# отдельных запроса на каждый ТБ.
#
# ДВА УРОВНЯ В ОДНОМ ЗАПРОСЕ, и это принципиально:
#   * level_name='gosb' — единицы отчёта по ТБ. Здесь аппараты из разбора убираются
#     (карточки им не строятся), поэтому сумма карточек итогу ТБ не равна;
#   * level_name='tb'   — единицы отчёта по банку. Берутся КАК ЕСТЬ, вместе с
#     аппаратом: план сегмента нередко стоит именно на аппарате ТБ, и свёртка
#     ГОСБ-строк без него теряла до 90% плана — выполнение улетало в тысячи процентов.
#     Уровни витрины считаются независимо, и складывать ГОСБ ради ТБ незачем: строка
#     ТБ в витрине уже есть.
#
# Имя единицы для уровня ТБ берётся из справочника (tb_short_name), для ГОСБ — из
# gmap. ЦА отсекается в обоих случаях: у ГОСБ через gmap, у ТБ через тот же _NO_CA.
_UNITS = """
WITH gmap AS (""" + _GMAP + """),
tb AS (SELECT DISTINCT tb_id, tb_short_name FROM {schema}.uzp_dim_gosb
       WHERE tb_short_name IS NOT NULL AND """ + _NO_CA + """)
SELECT 'gosb' AS level_name, g.new_gosb_id AS unit_id, g.gosb_name AS unit_name,
       g.tb_id, m.end_dt, {seg_col}
       sum(m.plan_amt) AS plan_amt, sum(m.fact_amt) AS fact_amt,
       sum(m.prediction_amt) AS pred_amt,
       sum(m.plan_amt - m.fact_amt) AS nedobor
FROM {schema}.uzp_dwh_metrics m
JOIN gmap g ON g.old_gosb_id=m.level_id
WHERE m.level_name='gosb' AND m.period_type='m' AND m.metric_id=:m_rcp
  AND m.end_dt IN (:ref_cur, :ref_closed) AND {seg_filter}
GROUP BY g.new_gosb_id, g.gosb_name, g.tb_id, m.end_dt{seg_group}
UNION ALL
SELECT 'tb' AS level_name, m.level_id AS unit_id, tb.tb_short_name AS unit_name,
       m.level_id AS tb_id, m.end_dt, {seg_col}
       sum(m.plan_amt) AS plan_amt, sum(m.fact_amt) AS fact_amt,
       sum(m.prediction_amt) AS pred_amt,
       sum(m.plan_amt - m.fact_amt) AS nedobor
FROM {schema}.uzp_dwh_metrics m
JOIN tb ON tb.tb_id = m.level_id
WHERE m.level_name='tb' AND m.period_type='m' AND m.metric_id=:m_rcp
  AND m.end_dt IN (:ref_cur, :ref_closed) AND {seg_filter}
GROUP BY m.level_id, tb.tb_short_name, m.end_dt{seg_group}
"""

UNIT_SEG = _UNITS.replace("{seg_col}", "m.extended_dim_1 AS seg_id,") \
                 .replace("{seg_filter}", "m.extended_dim_1 <> 1") \
                 .replace("{seg_group}", ", m.extended_dim_1")

UNIT_TOTALS = _UNITS.replace("{seg_col}", "") \
                    .replace("{seg_filter}", "COALESCE(m.extended_dim_1,1)=1") \
                    .replace("{seg_group}", "")

# Организации ПО ВСЕМУ БАНКУ на грейне (ГОСБ, ИНН): витринные метрики, сегмент,
# средняя ЗП, годовой тренд и признак «закреплена в эталонной базе».
#
# Раньше это было ДВА запроса (ORGS с фильтром эталонной базы и ORG_DETAIL без него),
# причём второй выполнялся дважды на каждый ТБ. Фильтр эталонной базы перенесён в
# pandas: выборка одна и та же, а нужны оба среза — работать можно только с
# закреплёнными парами, но в ожидаемый отток входят и организации вне базы
# (на тестовом ТБ это ~12% оттока), и без них детализация не сойдётся с итогом.
#
# EXISTS, а не JOIN: в эталонной базе несколько срезов actual_dt на одну пару.
#
# ГРЕЙН РЕЗУЛЬТАТА — РОВНО (new_gosb_id, ИНН). Метрики лежат на old_gosb_id, а в
# справочнике 29 из 105 реальных ГОСБ склеены из нескольких старых: без свёртки одна
# организация приезжала бы в отчёт несколькими строками ОДНОГО И ТОГО ЖЕ ГОСБ — и в
# списке к работе, и в блоке годового тренда. Суммируем (а не берём max), потому что
# так же обходится со слиянием ГОСБ и матрица метрик: два бывших отделения — это одна
# клиентская связь с общей численностью.
ORGS_ALL = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, g.gosb_name, g.tb_id, c.org_id AS inn,
       min(dc.company_name)                        AS company_name,
       min(dc.segment_name)                        AS segment_big,
       min(c.level_id)                             AS gosb_id,
       count(*)                                    AS n_src_rows,
       sum(COALESCE(c.current_fl_qty, 0))          AS current_fl_qty,
       sum(COALESCE(c.current_fot_amt, 0))         AS current_fot_amt,
       sum(COALESCE(c.total_emp_qty, 0))           AS total_emp_qty,
       -- имя колонки то же, что у среза уровня ТБ: детализация прогноза читает оба
       -- кадра одним кодом, разница между уровнями — только в ключе единицы
       sum(COALESCE(c.fl_y_1_diff_qty, 0))         AS fl_yoy,
       sum(COALESCE(c.emp_potential_qty, 0))       AS emp_potential_qty,
       sum(COALESCE(c.fot_potential_amt, 0))       AS fot_potential_amt,
       sum(COALESCE(c.fl_outflow_qty, 0))          AS fl_outflow_qty,
       sum(COALESCE(c.fot_outflow_amt, 0))         AS fot_outflow_amt,
       -- доли и средние пересчитываются ПОСЛЕ свёртки, иначе они не про эту пару
       sum(COALESCE(c.current_fl_qty, 0))
           / NULLIF(sum(COALESCE(c.total_emp_qty, 0)), 0)   AS zp_fl_perc,
       sum(COALESCE(c.current_fot_amt, 0))
           / NULLIF(sum(COALESCE(c.current_fl_qty, 0)), 0)  AS avg_salary,
       -- без агрегата: обе колонки условия входят в GROUP BY, поэтому EXISTS
       -- вычисляется на группу. Оборачивать его в bool_or() и не нужно, и рискованно —
       -- коррелированный подзапрос внутри агрегата Greenplum поддерживает не везде
       EXISTS (SELECT 1 FROM {schema}.uzp_dim_mzp_reference_base rb
               WHERE rb.inn = c.org_id AND rb.gosb_id = g.new_gosb_id) AS in_ref
FROM {schema}.uzp_dwh_company_holding_metric c
JOIN gmap g ON g.old_gosb_id=c.level_id
LEFT JOIN {schema}.uzp_dim_company dc ON dc.inn=c.org_id
WHERE c.level_name = 'gosb'
  AND c.report_dt = :ref_closed
  AND c.org_type = 'inn'   -- только организации по ИНН (не holding/head_holding)
GROUP BY g.new_gosb_id, g.gosb_name, g.tb_id, c.org_id
"""

# Организации на грейне (ТБ, ИНН) — строки того же справочника уровнем выше.
#
# Нужны блоку «Портфель год к году» на уровне СБ. Складывать туда ГОСБ-строки нельзя
# дважды: организация обслуживается в нескольких ГОСБ одного ТБ, и её годовая дельта
# при свёртке повторилась бы у каждого ГОСБ. Витрина такую свёртку уже сделала —
# берём её строку уровня ТБ, а не считаем заново.
ORGS_TB = """
SELECT c.level_id AS tb_id, c.org_id AS inn,
       COALESCE(c.fl_y_1_diff_qty, 0) AS fl_yoy,
       COALESCE(c.current_fl_qty, 0)  AS current_fl_qty
FROM {schema}.uzp_dwh_company_holding_metric c
WHERE c.level_name = 'tb'
  AND c.report_dt = :ref_closed
  AND c.org_type = 'inn'
"""

# Закреплённый за организацией сотрудник — на грейне (ГОСБ, ИНН), по всему банку.
#
# Цепочка: закрепление (uzp_data_emp_epk_assignment) → ЕПК (uzp_data_epk_consolidation,
# оттуда ИНН) → штатка (uzp_dwh_sap_staff_emp, оттуда ФИО по табельному).
#
# Берутся ДЕЙСТВУЮЩИЕ закрепления: end_dttm IS NULL. Заполненный end_dttm означает,
# что закрепление уже закрыто, — по такому в отчёт приехало бы ФИО бывшего менеджера.
#
# Дедуп ИМЕННО на паре (new_gosb_id, ИНН), а не на epk_id: несколько ЕПК могут
# указывать на один ИНН, и организация приехала бы в список двумя строками с разными
# ФИО. Из нескольких закреплений пары берём ПОСЛЕДНЕЕ по start_dttm; второй ключ
# сортировки (saphr_id) нужен только ради детерминированности при равных датах.
#
# ЕПК группируется по epk_id со взятием min(inn) — страховка от размножения строк,
# если витрина придёт с историей на один ЕПК. Ключ в профиле уникален, но джойн,
# способный задвоить список к работе, лучше обезвредить в самом запросе.
#
# row_number() вместо DISTINCT ON и коррелированных max(): Greenplum стоит на ядре
# PostgreSQL 9.4 — оконные функции там есть, а диалектными конструкциями рисковать
# незачем.
#
# ФИО берётся за МАКСИМАЛЬНУЮ дату штатки, где оно заполнено: в свежем срезе ФИО
# бывает пустым, и сортировка без фильтра вернула бы пустую строку вместо имени.
ROLE_ATTACHED = 14            # role_id закрепления «за сотрудником ведётся клиент»

ORG_MANAGER = """
WITH gmap AS (""" + _GMAP + """),
asg AS (
  SELECT g.new_gosb_id, a.epk_id, a.saphr_id, a.start_dttm
  FROM {schema}.uzp_data_emp_epk_assignment a
  JOIN gmap g ON g.old_gosb_id = a.gosb_id
  WHERE a.role_id = :role AND a.end_dttm IS NULL
),
epk AS (
  SELECT epk_id, min(inn) AS inn
  FROM {schema}.uzp_data_epk_consolidation
  WHERE inn IS NOT NULL
  GROUP BY epk_id
),
fio AS (
  SELECT saphr_id, fio FROM (
    SELECT s.saphr_id, s.fio,
           row_number() OVER (PARTITION BY s.saphr_id
                              ORDER BY s.report_dt DESC) AS rn
    FROM {schema}.uzp_dwh_sap_staff_emp s
    WHERE s.fio IS NOT NULL
  ) t WHERE t.rn = 1
),
pair AS (
  SELECT a.new_gosb_id, e.inn, a.saphr_id,
         row_number() OVER (PARTITION BY a.new_gosb_id, e.inn
                            ORDER BY a.start_dttm DESC, a.saphr_id DESC) AS rn
  FROM asg a
  JOIN epk e ON e.epk_id = a.epk_id
)
SELECT p.new_gosb_id, p.inn, p.saphr_id, f.fio
FROM pair p
LEFT JOIN fio f ON f.saphr_id = p.saphr_id
WHERE p.rn = 1
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
#
# f.inn IS NOT NULL — обязательное условие: ИНН в воронке бывает пустым, а вся
# работа с клиентом идёт на грейне (ГОСБ, ИНН). Такие строки отбрасываем ЯВНО;
# раньше они молча исчезали в pandas, а в агрегате давали отдельную группу с
# ключом NaN.
_FUNNEL_BASE = """
base AS (
  SELECT g.new_gosb_id, f.tb_id, f.inn, f.role_code, f.task_type, f.last_active_type,
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
  WHERE f.inn IS NOT NULL
    AND f.task_create_dt >= CAST(:funnel_from AS date)
    AND f.task_create_dt <= CAST(:ref_funnel  AS date)
)"""

# Сколько строк воронки отбрасывает фильтр по ИНН — в прогресс. Молча терять строки
# нельзя: по ним не видно ни активности, ни пайплайна.
FUNNEL_INN_STATS = """
SELECT count(*) AS n_all,
       count(*) FILTER (WHERE inn IS NULL) AS n_null_inn
FROM {schema}.uzp_dwh_sale_funnel_task
WHERE task_create_dt >= CAST(:plan_from AS date)
  AND task_create_dt <= CAST(:ref_funnel AS date)
"""

# Активности ПО МЕСЯЦАМ за длинный горизонт — под вопрос «отрабатывали ли отток
# тогда, когда он случился». Основное окно воронки (FUNNEL_AGG) — 3 месяца, а годовое
# падение портфеля обычно относится к оттоку 6–12 месяцев назад: по трёхмесячной
# выборке про него нельзя сказать ничего, и молчаливое «не отрабатывали» было бы
# неправдой, а не незнанием.
#
# Запрос намеренно лёгкий: агрегация в БД, тексты не тянем (только флаг), строки
# появляются лишь там, где задачи БЫЛИ, — выборка разрежённая. Отдельный запрос, а не
# расширение окна FUNNEL_AGG: тот тянет полтора десятка колонок, включая суммы по
# сделкам, и растягивать его на 13 месяцев незачем.
FUNNEL_MONTHS = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, f.tb_id, f.inn,
       CAST(date_trunc('month', f.task_create_dt) AS date)          AS ym,
       count(*)                                                     AS n_tasks,
       sum(CASE WHEN f.is_task_closed_success THEN 1 ELSE 0 END)    AS n_success,
       sum(CASE WHEN f.task_type='Отток' THEN 1 ELSE 0 END)         AS n_outflow,
       -- УСПЕШНО закрытая задача ИМЕННО ПО ОТТОКУ. Отдельная колонка, а не пересечение
       -- двух предыдущих: успешная задача о привлечении и проваленная об оттоке дали бы
       -- «отработано», хотя отток как раз не отработан. По ней решается рычаг «Вернуть».
       sum(CASE WHEN f.task_type='Отток' AND f.is_task_closed_success
                THEN 1 ELSE 0 END)                                  AS n_out_success,
       bool_or(COALESCE(btrim(f.task_comment), '') <> ''
               OR COALESCE(btrim(f.task_questionnaire), '') <> '')  AS has_text
FROM {schema}.uzp_dwh_sale_funnel_task f
LEFT JOIN gmap g ON g.old_gosb_id = f.gosb_id
WHERE f.inn IS NOT NULL
  AND f.task_create_dt >= CAST(:months_from AS date)
  AND f.task_create_dt <= CAST(:ref_funnel AS date)
GROUP BY g.new_gosb_id, f.tb_id, f.inn,
         CAST(date_trunc('month', f.task_create_dt) AS date)
"""

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

# Итоги активностей по КАЖДОМУ ТБ (для блока «Активности за 3 месяца»). Уровень СБ
# складывает эти же строки: банк — это сумма задач своих ТБ, отдельной строки
# активностей в витрине нет.
ACTIVITY_TOTALS = """
WITH gmap AS (""" + _GMAP + """),
""" + _FUNNEL_BASE + """
SELECT tb_id, count(*) AS n, count(DISTINCT inn) AS orgs,
       sum(CASE WHEN last_active_type='Звонок'  THEN 1 ELSE 0 END) AS calls,
       sum(CASE WHEN last_active_type='Встреча' THEN 1 ELSE 0 END) AS meetings,
       sum(CASE WHEN is_task_closed_success THEN 1 ELSE 0 END) AS n_success,
       sum(CASE WHEN is_overdue THEN 1 ELSE 0 END) AS overdue,
       sum(plan_staff_deal_qty) AS plan_deal,
       sum(fact_staff_deal_qty) AS fact_deal,
       sum(unrealized_deal_potential) AS unrealized
FROM base GROUP BY tb_id
"""

# Разрезы активностей: по ролям / типам задач / статусам отработки, по каждому ТБ
ACTIVITY_BREAKDOWN = """
WITH gmap AS (""" + _GMAP + """),
""" + _FUNNEL_BASE + """
SELECT tb_id, 'role' AS dim, COALESCE(role_code,'—') AS k, count(*) AS n
FROM base GROUP BY 1, 3
UNION ALL
SELECT tb_id, 'type', COALESCE(task_type,'—'), count(*) FROM base GROUP BY 1, 3
UNION ALL
SELECT tb_id, 'status', COALESCE(task_text_status,'—'), count(*) FROM base GROUP BY 1, 3
"""

# Свободный текст ТОЛЬКО по приоритетным ИНН — все содержательные активности
# каждой пары (ГОСБ, ИНН) за 3 месяца (не одна последняя). Список ИНН собирается
# по ВСЕМ уровням сразу, поэтому запрос один на отчёт, а не по одному на ТБ.
#
# Для хронологии и поиска противоречий тянем АВТОРА (табельный isu_struct_saphr_id —
# ФИО НЕ тянем: для «противоречий между сотрудниками» достаточно РАЗЛИЧАТЬ авторов),
# роль, дату создания задачи, факт закрытия и признак успеха. Числа по сделкам/
# потенциалу/оттоку здесь не нужны — они берутся из FUNNEL_AGG/ORGS_ALL (уже с *_old).
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
WHERE f.inn = ANY(:inns)
  AND f.task_create_dt >= CAST(:funnel_from AS date)
  AND f.task_create_dt <= CAST(:ref_funnel  AS date)
  AND (COALESCE(btrim(f.task_comment), '') <> ''
       OR COALESCE(btrim(f.task_questionnaire), '') <> '')
ORDER BY f.inn, g.new_gosb_id, f.last_active_dttm DESC NULLS LAST
"""

# ==================== Отток и пайплайн ===================================== #

# ФАКТИЧЕСКИЙ отток, который НЕ ВЕРНУЛСЯ, за три ЗАКРЫТЫХ месяца (T-1, T-2, T-3).
#
# Заменяет собой и ежедневную витрину, и модель истории: отчёт больше ничего не
# предсказывает — он показывает, сколько людей уже ушло и не вернулось.
#
# Порог `outflow_qty >= :out_min` — требование бизнеса: уход одного-двух человек это
# текучка, а не потеря клиента, и разбирать её поимённо смысла нет.
#
# Возвраты приклеиваются LEFT JOIN-ом по (report_dt, gosb_id, inn) — тому же грейну,
# на котором лежит сам отток. LEFT, а не INNER: строка без возврата означает «никто
# не вернулся», и терять её нельзя — это как раз худший случай.
#
# GREATEST(..., 0): в витрине возврат может превысить отток (вернулись ушедшие ранее
# окна). Отрицательный «невозврат» смысла не имеет и всё равно обнулялся бы в pandas —
# лучше сделать это явно и в одном месте.
#
# Грейн результата — (ГОСБ, ИНН, месяц): помесячная разбивка нужна блоку «крупнейшие
# оттоки», который группирует организации по месяцу ухода.
FACT_OUTFLOW = """
WITH gmap AS (""" + _GMAP + """)
SELECT g.new_gosb_id, f.inn, f.report_dt,
       max(f.segment_name)                                         AS seg_fact,
       sum(f.outflow_qty)                                          AS out_qty,
       sum(COALESCE(r.return_qty, 0))                              AS ret_qty,
       sum(GREATEST(f.outflow_qty - COALESCE(r.return_qty, 0), 0)) AS out_kept,
       max(f.m_avg_salary_amt)                                     AS avg_salary_m,
       max(f.prev_m_fl_val)                                        AS fl_prev_m
FROM {schema}.uzp_dwh_fact_outflow f
JOIN gmap g ON g.old_gosb_id = f.gosb_id
LEFT JOIN {schema}.uzp_data_outflow_return_detail r
       ON r.report_dt = f.report_dt AND r.gosb_id = f.gosb_id AND r.inn = f.inn
WHERE f.outflow_qty >= :out_min
  AND f.report_dt IN (:m_out1, :m_out2, :m_out3)
GROUP BY g.new_gosb_id, f.inn, f.report_dt
"""

# Знаменатель к FACT_OUTFLOW: сколько строк оттока есть всего и сколько отсекает
# порог. Без этого «оттока мало» не отличить от «порог съел почти всё».
FACT_OUTFLOW_STATS = """
SELECT count(*)                                                AS n_all,
       count(*) FILTER (WHERE outflow_qty >= :out_min)         AS n_kept,
       sum(outflow_qty)                                        AS qty_all,
       sum(outflow_qty) FILTER (WHERE outflow_qty >= :out_min) AS qty_kept
FROM {schema}.uzp_dwh_fact_outflow
WHERE report_dt IN (:m_out1, :m_out2, :m_out3)
"""

# Пайплайн: сколько НП сотрудник запланировал ИМЕННО на текущий месяц.
#
# В uzp_dwh_sale_funnel_task.plan_staff_deal_qty план размазан на все 3 месяца
# жизни сделки; помесячная разбивка есть только в yva_pl_task_deal_code, ключ —
# coalesce(deal_code, task_code).
#
# CTE codes ОБЯЗАТЕЛЬНА и группируется ТОЛЬКО по коду: один и тот же deal_code
# встречается в нескольких строках воронки (несколько задач по сделке), и join
# напрямую задвоил бы план. Раньше группировка шла по паре (код, ИНН) — и если один
# код встречался с двумя разными ИНН, план входил в расчёт дважды. Организацию для
# кода выбираем детерминированно (min), а сколько кодов вообще имеют больше одного
# ИНН — печатается в прогресс запросом PIPELINE_PLAN_STATS.
#
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
         min(f.inn)                    AS inn,
         min(g.new_gosb_id)            AS new_gosb_id,
         min(f.isu_struct_saphr_id)    AS saphr_id,
         min(f.segment_name)           AS seg_funnel,
         min(date_trunc('month', COALESCE(f.deal_create_dttm,
                                          CAST(f.task_create_dt AS timestamp)))) AS m0
  FROM {schema}.uzp_dwh_sale_funnel_task f
  LEFT JOIN gmap g ON g.old_gosb_id = f.gosb_id
  WHERE f.inn IS NOT NULL
    AND f.task_create_dt >= CAST(:plan_from AS date)
    AND f.task_create_dt <= CAST(:ref_funnel AS date)
    AND COALESCE(f.deal_code, f.task_code) IS NOT NULL
  GROUP BY 1
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

# Две доли, о которых нельзя молчать: месяц плана вне 3 месяцев жизни сделки (год не
# восстановить, строка в расчёт не идёт) и коды, встречающиеся сразу с несколькими
# ИНН (раньше такой код задваивал план).
PIPELINE_PLAN_STATS = """
WITH gmap AS (""" + _GMAP + """),
codes AS (
  SELECT COALESCE(f.deal_code, f.task_code) AS code,
         count(DISTINCT f.inn) AS n_inn,
         min(date_trunc('month', COALESCE(f.deal_create_dttm,
                                          CAST(f.task_create_dt AS timestamp)))) AS m0
  FROM {schema}.uzp_dwh_sale_funnel_task f
  LEFT JOIN gmap g ON g.old_gosb_id = f.gosb_id
  WHERE f.inn IS NOT NULL
    AND f.task_create_dt >= CAST(:plan_from AS date)
    AND f.task_create_dt <= CAST(:ref_funnel AS date)
    AND COALESCE(f.deal_code, f.task_code) IS NOT NULL
  GROUP BY 1
)
SELECT count(*) AS n_all,
       count(*) FILTER (
         WHERE ((p.pl_month_num - CAST(EXTRACT(MONTH FROM c.m0) AS int) + 12) % 12) > 2
       ) AS n_bad,
       count(DISTINCT c.code) FILTER (WHERE c.n_inn > 1) AS n_multi_inn
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
WHERE m.metric_id = :m_np
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
WHERE metric_id = :m_np
  AND report_dt >= CAST(:plan_from AS date) AND report_dt <= CAST(:ref_cur AS date)
"""
