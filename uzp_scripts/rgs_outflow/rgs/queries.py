"""SQL отчёта. Все запросы — здесь, ни одной строки SQL в расчётах.

Плейсхолдеры `{schema}` / `{schema_t}` подставляет `uzp_dash.db.read_sql`, значения
передаются именованными параметрами `:name`.

ДВЕ ЛОВУШКИ, на которых код ломается тихо:

1. **Диалект.** Greenplum на ядре PostgreSQL 9.4. `make_interval(months => n)` там
   не работает — «=>» разбирается как оператор; сдвиг на месяцы пишется умножением
   интервала: `n * interval '1 month'`.
2. **Фигурные скобки.** `read_sql` прогоняет текст через `str.format()`, поэтому
   квантификатор регулярки `{2}` обязан быть записан как `{{2}}`. Иначе запрос
   падает с `IndexError: Replacement index 2 out of range` — сообщение, по
   которому про SQL не догадаешься.

Соответствие ГОСБ и словарь сегментов берутся ИЗ ДЭША: вторая копия разъедется
молча, и отчёт начнёт мерить не тот банк и не тот сегмент.
"""
from __future__ import annotations

from uzp_dash.dashboards.tb_health import queries as Q
from uzp_dash.dashboards.tb_health import segments as S

_GMAP = Q._GMAP     # noqa: SLF001 — единственный источник соответствия ГОСБ в проекте

# Бюджетная сфера в трёх разных словарях витрин. Разные написания одного и того же
# сегмента — самая дорогая ловушка этого отчёта: фильтр большим именем по витрине
# оттока вернул бы ноль строк, и это выглядело бы как «оттока в бюджетной сфере нет».
SEG_CODE = 22                          # uzp_dwh_metrics.extended_dim_1
SEG_SHORT = S.SHORT[SEG_CODE]          # 'РГС'  — uzp_dwh_fact_outflow.segment_name
SEG_BIG = "Рег. госсектор"             # uzp_dim_company.segment_name
assert S.BIG_TO_CODE[SEG_BIG] == SEG_CODE, "словарь сегментов дэша разъехался с отчётом"

# Метрики плана/факта — те же, что в дэше.
METRIC_FOT = Q.METRIC_FOT
METRIC_RECIPIENTS = Q.METRIC_RECIPIENTS

# Колонки, смотрящие в БУДУЩЕЕ. Прогнозу их читать нельзя: это не прогноз, а чтение
# ответа. Список проверяется тестом `check_no_lookahead` — он ищет эти имена во ВСЕХ
# запросах файла, поэтому упоминать их в SQL нельзя даже в комментарии.
FORBIDDEN_COLUMNS = (
    "next_m_fl_val",
    "next_m_avg_salary_amt",
    "new_fl_cnt_next_month",
    "np_cnt_next_month",
)


# --------------------------------------------------------------------------- #
# Разведка: что вообще есть в витринах
# --------------------------------------------------------------------------- #

# Глубина истории и заполненность витрины оттока.
#
# Зачем нужен именно такой набор счётчиков: в профиле пром-витрины у неё РОВНО ОДНА
# отчётная дата, а `other_inn_emp_perc` и `prev_m_overflow_qty` полностью пусты.
# Значит блок тенденций может остаться без истории, а два признака «куда ушли» —
# без данных. Узнать это надо в начале прогона, а не по пустому разделу в отчёте.
PROBE_FACT = """
SELECT count(*)                                    AS n_rows,
       count(DISTINCT inn)                         AS n_inn,
       count(DISTINCT report_dt)                   AS n_months,
       min(report_dt)                              AS d_min,
       max(report_dt)                              AS d_max,
       count(*) FILTER (WHERE segment_name = :seg) AS n_seg_rows,
       count(DISTINCT inn) FILTER (WHERE segment_name = :seg) AS n_seg_inn,
       count(*) FILTER (WHERE COALESCE(other_inn_emp_perc, 0) > 0) AS n_other_inn,
       count(*) FILTER (WHERE COALESCE(prev_m_overflow_qty, 0) > 0) AS n_overflow,
       count(*) FILTER (WHERE length(COALESCE(oktmo, '')) = 11)     AS n_oktmo_full,
       count(*) FILTER (WHERE COALESCE(oktmo_subject_code, '') ~ '^[0-9]{{2}}$')
                                                   AS n_subject_code_ok
FROM {schema}.uzp_dwh_fact_outflow
"""

# Помесячные объёмы бюджетной сферы в витрине оттока — из них берётся глубина ряда.
PROBE_FACT_MONTHS = """
SELECT report_dt, count(*) AS n_rows, sum(outflow_qty) AS out_qty
FROM {schema}.uzp_dwh_fact_outflow
WHERE segment_name = :seg
GROUP BY report_dt
ORDER BY report_dt
"""

PROBE_RETURN = """
SELECT count(*) AS n_rows, count(DISTINCT report_dt) AS n_months,
       min(report_dt) AS d_min, max(report_dt) AS d_max,
       sum(outflow_qty) AS out_qty, sum(return_qty) AS ret_qty,
       count(*) FILTER (WHERE segment_name = :seg) AS n_seg_rows
FROM {schema}.uzp_data_outflow_return_detail
"""

# Глубина истории витрины организаций — ЗАПАСНОЙ источник ряда, если в витрине
# оттока месяцев не хватает.
PROBE_HOLDING = """
SELECT count(DISTINCT report_dt) AS n_months,
       min(report_dt) AS d_min, max(report_dt) AS d_max
FROM {schema}.uzp_dwh_company_holding_metric
WHERE level_name = 'gosb' AND org_type = 'inn'
"""

# Покрытие витрины ключевых клиентов. Считается ОБЯЗАТЕЛЬНО: витрина знает про
# конкурентов только у ключевых клиентов, и без доли покрытия блок читается как
# «конкурентов почти нет», хотя про остальных просто не спрашивали.
# Считается ТОЛЬКО по организациям бюджетной сферы из витрины оттока — тем самым,
# которые разбирает отчёт. Счёт по всей витрине дал бы покрытие больше 100%:
# в числителе оказались бы ключевые клиенты всех сегментов, а в знаменателе —
# только бюджетные. Ошибка не гипотетическая: ровно так и вышло при первом прогоне.
PROBE_KEY_CLIENT = """
SELECT count(*) AS n_rows, count(DISTINCT inn) AS n_inn,
       max(report_dt) AS d_max,
       count(*) FILTER (WHERE COALESCE(bank_competitor, '') <> '') AS n_competitor,
       count(*) FILTER (WHERE COALESCE(captive_bank_name, '') <> '') AS n_captive
FROM {schema}.uzp_data_key_client_info_add_attr
WHERE report_dt <= CAST(:d_to AS date)
  AND inn IN (
        SELECT DISTINCT inn FROM {schema}.uzp_dwh_fact_outflow
        WHERE segment_name = :seg
          AND report_dt >= CAST(:d_from AS date)
          AND report_dt <= CAST(:d_to AS date))
"""


# --------------------------------------------------------------------------- #
# Основная выгрузка
# --------------------------------------------------------------------------- #

# Возвраты сворачиваются ОТДЕЛЬНОЙ CTE, а не join-ом напрямую к витрине оттока.
# Ключ (report_dt, gosb_id, inn) объявлен первичным, но не «pk_exact»: если по
# какой-то паре придут две строки, прямой join задвоил бы ОТТОК — не возврат, а
# именно отток, потому что дублируется левая сторона. Ошибка тихая: цифры вырастут
# правдоподобно. Свёртка до join-а делает её невозможной.
_RETURNS = """
  SELECT report_dt, gosb_id, inn, sum(COALESCE(return_qty, 0)) AS ret_qty
  FROM {schema}.uzp_data_outflow_return_detail
  GROUP BY report_dt, gosb_id, inn
"""

# Месячный отток бюджетной сферы на грейне (месяц, ГОСБ, организация).
#
# `GREATEST(..., 0)`: в витрине возврат может превысить отток того же месяца
# (вернулись ушедшие раньше). Отрицательный «невозврат» смысла не имеет.
#
# Код субъекта РФ берётся из `substr(oktmo, 1, 2)`, а НЕ из `oktmo_subject_code`:
# последний на проме негоден — длиной 0-2 символа, среди значений «"0», «М», «П»,
# «tr». Это мусор загрузки, и разрез по субъектам, построенный на нём, рассыпается.
# Проверка длины обязательна: короткий ОКТМО первых двух знаков субъекта не несёт.
RGS_FACT = """
WITH gmap AS (""" + _GMAP + """),
ret AS (""" + _RETURNS + """)
SELECT f.report_dt,
       g.new_gosb_id,
       g.tb_id,
       f.inn,
       sum(f.outflow_qty)                                             AS out_qty,
       sum(COALESCE(r.ret_qty, 0))                                    AS ret_qty,
       sum(GREATEST(f.outflow_qty - COALESCE(r.ret_qty, 0), 0))       AS out_kept,
       max(f.calc_fl_qty)                                             AS calc_fl,
       max(f.prev_m_fl_val)                                           AS fl_prev_m,
       max(f.fact_payee_qty)                                          AS fact_payee,
       max(f.plan_payee_qty)                                          AS plan_payee,
       max(f.m_avg_salary_amt)                                        AS salary_m,
       max(f.prev_m_avg_salary_amt)                                   AS salary_prev_m,
       max(COALESCE(f.other_inn_emp_perc, 0))                         AS other_inn_perc,
       sum(COALESCE(f.prev_m_overflow_qty, 0))                        AS overflow_qty,
       bool_or(COALESCE(f.is_force, false))                           AS is_force,
       bool_or(COALESCE(f.is_task, false))                            AS is_task,
       max(CASE WHEN length(COALESCE(f.oktmo, '')) = 11
                THEN substr(f.oktmo, 1, 2) END)                       AS subject_code,
       max(CASE WHEN length(COALESCE(f.oktmo, '')) = 11
                THEN substr(f.oktmo, 1, 5) END)                       AS district_code
FROM {schema}.uzp_dwh_fact_outflow f
JOIN gmap g ON g.old_gosb_id = f.gosb_id
LEFT JOIN ret r ON r.report_dt = f.report_dt AND r.gosb_id = f.gosb_id
                AND r.inn = f.inn
WHERE f.segment_name = :seg
  AND f.report_dt >= CAST(:d_from AS date)
  AND f.report_dt <= CAST(:d_to AS date)
GROUP BY f.report_dt, g.new_gosb_id, g.tb_id, f.inn
"""

# Наименования организаций — сырьё классификатора ведомств.
#
# Отбор идёт по ИНН ИЗ ВИТРИНЫ ОТТОКА, а не по сегменту справочника компаний.
# Сегменты в этих двух витринах записаны разными словарями и обновляются в разное
# время; фильтр по справочнику отдал бы другой набор организаций, часть оттока
# осталась бы без имени, и ведомство ей не определилось бы. Так же честно видно,
# скольким организациям имени в справочнике не нашлось вовсе.
RGS_NAMES = """
SELECT c.inn,
       min(c.company_name) AS company_name,
       min(c.holding_name) AS holding_name
FROM {schema}.uzp_dim_company c
WHERE c.inn IN (
        SELECT DISTINCT inn FROM {schema}.uzp_dwh_fact_outflow
        WHERE segment_name = :seg
          AND report_dt >= CAST(:d_from AS date)
          AND report_dt <= CAST(:d_to AS date))
GROUP BY c.inn
"""

# Помесячная панель по бюджетным организациям: численность получателей, ФОТ, ШТАТ.
#
# Штат (`total_emp_qty`) — ключевая колонка всего разбора причин: по тому, идёт ли
# штат за получателями или стоит на месте, отличается сокращение штата от ухода
# к конкуренту. Проникновение (`zp_fl_perc`) здесь НЕ берётся: усреднять готовую
# долю по организациям нельзя, она считается из сумм уже в расчётах.
#
# Чанкование по остатку от деления ИНН безопасно: выборка сгруппирована по ИНН,
# поэтому организация целиком попадает ровно в одну часть.
RGS_PANEL = """
WITH gmap AS (""" + _GMAP + """),
seg AS (
  SELECT DISTINCT inn FROM {schema}.uzp_dwh_fact_outflow
  WHERE segment_name = :seg
    AND report_dt >= CAST(:d_from AS date)
    AND report_dt <= CAST(:d_to AS date)
)
SELECT CAST(date_trunc('month', c.report_dt) AS date) AS ym,
       g.new_gosb_id,
       g.tb_id,
       c.org_id                              AS inn,
       sum(COALESCE(c.current_fl_qty, 0))    AS fl,
       sum(COALESCE(c.fl_outflow_qty, 0))    AS out_q,
       sum(COALESCE(c.current_fot_amt, 0))   AS fot,
       sum(COALESCE(c.total_emp_qty, 0))     AS emp,
       sum(COALESCE(c.emp_potential_qty, 0)) AS pot,
       sum(COALESCE(c.new_fl_cnt, 0))        AS new_fl,
       sum(COALESCE(c.np_cnt, 0))            AS np
FROM {schema}.uzp_dwh_company_holding_metric c
JOIN gmap g ON g.old_gosb_id = c.level_id
JOIN seg   s ON s.inn = c.org_id
WHERE c.level_name = 'gosb' AND c.org_type = 'inn'
  AND c.report_dt >= CAST(:p_from AS date)
  AND c.report_dt <= CAST(:d_to AS date)
  AND mod(abs(c.org_id), :n_parts) = :part
GROUP BY 1, 2, 3, 4
"""

# Сколько строк ждать от панели — чтобы выбрать число частей заранее, а не упереться
# в лимит уже после того, как тяжёлый запрос отработал.
RGS_PANEL_COUNT = """
WITH seg AS (
  SELECT DISTINCT inn FROM {schema}.uzp_dwh_fact_outflow
  WHERE segment_name = :seg
    AND report_dt >= CAST(:d_from AS date)
    AND report_dt <= CAST(:d_to AS date)
)
SELECT count(*) AS n_rows
FROM {schema}.uzp_dwh_company_holding_metric c
JOIN seg s ON s.inn = c.org_id
WHERE c.level_name = 'gosb' AND c.org_type = 'inn'
  AND c.report_dt >= CAST(:p_from AS date)
  AND c.report_dt <= CAST(:d_to AS date)
"""

# Банки-конкуренты и стратегия по организации.
#
# Берётся ОДИН срез — последний, не позже отчётной даты. На проме в витрине лежат
# и БУДУЩИЕ отчётные даты; без верхней границы отчёт молча взял бы срез из будущего.
RGS_COMPETITORS = """
SELECT k.inn,
       min(k.bank_competitor)       AS bank_competitor,
       min(k.captive_bank_name)     AS captive_bank_name,
       min(k.holding_strategy_name) AS strategy_name,
       min(k.industry_name)         AS industry_name,
       min(k.holding_name)          AS holding_name
FROM {schema}.uzp_data_key_client_info_add_attr k
WHERE k.report_dt = (SELECT max(report_dt)
                     FROM {schema}.uzp_data_key_client_info_add_attr
                     WHERE report_dt <= CAST(:d_to AS date))
  AND k.inn IN (
        SELECT DISTINCT inn FROM {schema}.uzp_dwh_fact_outflow
        WHERE segment_name = :seg
          AND report_dt >= CAST(:d_from AS date)
          AND report_dt <= CAST(:d_to AS date))
GROUP BY k.inn
"""

# Территория: ГОСБ -> ТБ -> регион. Регион нужен как самостоятельный разрез: один
# регион может обслуживаться несколькими ГОСБ, и по ГОСБ картина региона не видна.
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

# План и факт по бюджетному сегменту в разрезе ГОСБ — прокси «как идут зарплатные
# проекты в регионе». Это витрина ПЛАНОВ банка, а не государственная статистика,
# и в отчёте подписывается именно так.
SEG_METRICS = """
SELECT m.level_id                AS old_gosb_id,
       m.end_dt,
       m.metric_id,
       sum(m.plan_amt)           AS plan_amt,
       sum(m.fact_amt)           AS fact_amt
FROM {schema}.uzp_dwh_metrics m
WHERE m.level_name = 'gosb' AND m.period_type = 'm'
  AND m.metric_id IN (:m_rcp, :m_fot)
  AND COALESCE(m.extended_dim_1, 1) = :seg_code
  AND m.end_dt >= CAST(:p_from AS date)
  AND m.end_dt <= CAST(:d_to AS date)
GROUP BY m.level_id, m.end_dt, m.metric_id
"""
