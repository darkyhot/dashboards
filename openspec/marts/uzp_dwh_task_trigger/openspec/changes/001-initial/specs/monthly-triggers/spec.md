# Spec Delta

## Purpose

Правила и JSON ежемесячных триггеров витрины `uzp_dwh_task_trigger`. T — конец месяца `end_dt`,
T-k — конец месяца на k раньше. hm — `uzp_dwh_company_holding_metric` (пара — `org_id` + `level_id`).
Ключ JSON `month` = T, если он есть в интенте. Портфель — получатели ЗП на карту банка
(`current_fl_qty`), численность — все сотрудники (`total_emp_qty`), потенциал — их разность
(`emp_potential_qty`).

## ADDED Requirements

### Requirement: ЕПП флаг
'ЕПП флаг' SHALL срабатывать, если в навигаторе за `report_dt` = T и `version_plan_dt` = T у пары только `sales_flag` = 'Единичное платежное поручение'. JSON: `epp_cnt` = `fl_count`.

#### Scenario: Только ЕПП
- **WHEN** у пары одна строка: ЕПП, `fl_count` = 14
- **THEN** строка с `epp_cnt` = 14

#### Scenario: ЕПП и реестр
- **WHEN** у пары есть `sales_flag` = 'Реестр + Единичное платежное поручение'
- **THEN** строки нет

### Requirement: Отрасль
Отрасль ИНН SHALL браться из `uzp_data_epk_consolidation.industry_name`, одна строка на ИНН. Порядок выбора: активная (`status_name` = 'Активна'), с заполненной отраслью, головная (`is_parent`), самая новая `epk_create_dttm`.

#### Scenario: Активная важнее головной
- **WHEN** у ИНН неактивная головная ЕПК и активная неголовная
- **THEN** отрасль — из активной неголовной

#### Scenario: Пустая отрасль у головной
- **WHEN** у ИНН две активные ЕПК: головная без отрасли, неголовная с отраслью
- **THEN** отрасль — из неголовной

### Requirement: Ср чек ниже среднего
'Ср чек ниже среднего' SHALL срабатывать, если `client_salary` ≤ 0,8 × `avg_salary`. Обе величины — сумма `current_fot_amt` / сумма `current_fl_qty` за T-5…T при `current_fl_qty` <> 0: `client_salary` — по паре, `avg_salary` — по всем парам ГОСБ + отрасли. Пары без отрасли не проверяются.

#### Scenario: Ниже на 40%
- **WHEN** `avg_salary` = 55000, `client_salary` = 33000
- **THEN** строка с этими значениями

#### Scenario: Ниже на 9%
- **WHEN** 55000 и 50000
- **THEN** строки нет

### Requirement: Отрицательный потенциал
'Отрицательный потенциал' SHALL срабатывать, если в hm за T `np_cnt > emp_potential_qty`. JSON: `np_cnt`.

#### Scenario: НП больше потенциала
- **WHEN** `np_cnt` = 55, `emp_potential_qty` = 40
- **THEN** строка с `np_cnt` = 55

#### Scenario: НП равен потенциалу
- **WHEN** `np_cnt` = 40, `emp_potential_qty` = 40
- **THEN** строки нет

### Requirement: Кп первое зачисление
'Кп первое зачисление' SHALL срабатывать, если в hm у пары `current_fot_amt > 0` за T и `current_fot_amt = 0` за каждый месяц T-12…T-1. JSON: `fl_cnt` = `current_fl_qty` за T.

#### Scenario: Первое за год
- **WHEN** ФОТ > 0 за T, ФОТ = 0 за T-12…T-1, получателей 12
- **THEN** строка с `fl_cnt` = 12

#### Scenario: Было зачисление
- **WHEN** ФОТ > 0 за T и за T-7
- **THEN** строки нет

### Requirement: Сезонный тип деятельности
'Сезонный тип деятельности' SHALL срабатывать, если портфель `current_fl_qty` вырос на 10% и более из T+1 в T+2 год назад и два года назад: `(T+2−12)/(T+1−12) − 1 ≥ 0,1` и `(T+2−24)/(T+1−24) − 1 ≥ 0,1`. База 0 — роста нет. JSON: `fl_uplift` — средний за 2 года прирост портфеля.

#### Scenario: Рост в обоих годах
- **WHEN** T = 2026-10-31; портфель ноя-25 = 100, дек-25 = 112; ноя-24 = 100, дек-24 = 112
- **THEN** строка с `fl_uplift` = 12

#### Scenario: Рост в одном году
- **WHEN** портфель дек-25 к ноя-25 = +12%, дек-24 к ноя-24 = +5%
- **THEN** строки нет

### Requirement: Высокая текучка
'Высокая текучка' SHALL срабатывать, если за T `new_fl_cnt > 0`, `np_cnt < 0,2 × new_fl_cnt` и портфель `current_fl_qty` за T меньше, чем за T-1. JSON: `nfl_cnt` = `new_fl_cnt`, `np_cnt`, `portf_last_m` и `portf_cur_m` = `current_fl_qty` за T-1 и T.

#### Scenario: Текучка
- **WHEN** `new_fl_cnt` = 12, `np_cnt` = 2, портфель 145 → 140
- **THEN** строка, `portf_last_m` = 145, `portf_cur_m` = 140

#### Scenario: Портфель вырос
- **WHEN** портфель 140 → 145
- **THEN** строки нет

### Requirement: Действующий договор
У ИНН SHALL быть действующий договор, если в `jupiter_data_agrmnt_num` есть строка с `end_dt is null or end_dt >= current_date`.

#### Scenario: Договор закрыт
- **WHEN** у ИНН один договор, `end_dt` < `current_date`
- **THEN** действующего договора нет

### Requirement: Есть договор нет зачисления
'Есть договор нет зачисления' SHALL срабатывать по паре hm за T, если у ИНН действующий договор с `start_dt <= current_date - 6 мес` и `current_fot_amt = 0` за каждый месяц T-5…T. JSON: только `month`.

#### Scenario: Полгода без зачислений
- **WHEN** договор с `start_dt` год назад, ФОТ = 0 за T-5…T
- **THEN** строка

#### Scenario: Договор моложе 6 месяцев
- **WHEN** `start_dt` = `current_date` − 2 мес
- **THEN** строки нет

### Requirement: Контактная политика 4 месяца
'Контактная политика 4 месяца' SHALL срабатывать по паре hm за T, если в `uzp_dwh_sale_funnel_task` по паре нет задачи: `is_task_closed`, `role_code` = 'МЗП', `src_task_as_code not in ('ИСУ', 'ПРБР')`, `fact_close_task_dttm` ≥ 1-го числа месяца T-3. Верхней границы нет. JSON: `task_close_date` — последняя такая задача за всё время, иначе `null`.

#### Scenario: Последняя задача в декабре
- **WHEN** T = 2026-04-30, задача 2025-12-20
- **THEN** строка

#### Scenario: Задача в январе
- **WHEN** T = 2026-04-30, задача 2026-01-15
- **THEN** строки нет

#### Scenario: Задача после T
- **WHEN** T = 2026-04-30, задача 2026-05-05
- **THEN** строки нет

#### Scenario: Задач не было
- **WHEN** у пары нет закрытых задач
- **THEN** строка с `task_close_date` = null

### Requirement: Заявление ВСП
Заявление SHALL браться из `vsp_data_salary_detail_report` с `is_asm` = 1. ИНН = `coalesce(fact_inn, gu_inn)`, пустой — заявление не берётся. ГОСБ = `uzp_dim_gosb.new_gosb_id` по `tb_id` и `old_gosb_id` из 1-й и 2-й части `urf_code`. Дата — `report_dt`. Дубли не убираются. Тип: электронное — `credential_transferred_flag` = 1, бумажное — остальные.

#### Scenario: ГОСБ
- **WHEN** `urf_code` = '044_8558_062'
- **THEN** ГОСБ — `new_gosb_id` при `tb_id` = 44, `old_gosb_id` = 8558

#### Scenario: Тип заявления
- **WHEN** `credential_transferred_flag` = 1
- **THEN** заявление электронное
- **WHEN** `credential_transferred_flag` = 0 или null
- **THEN** заявление бумажное

### Requirement: Зачисление по заявлению
Заявление SHALL считаться с зачислением, если в `mis_data_payroll_d` у его `epk_id` есть строка с `report_dt` ≥ даты заявления и ЗП-кодом (`uzp_dim_enrollment_type.is_fot_enrollment`). Организация любая, порога нет.

#### Scenario: Другая организация
- **WHEN** после заявления пришло ЗП-зачисление 1 500 руб. от другого ИНН
- **THEN** заявление с зачислением

### Requirement: Заявления за прошлый месяц
'Бумажные заявления из ВСП или МП СБОЛ за прошлый месяц' и 'Электронные заявления из ВСП или МП СБОЛ за прошлый месяц' SHALL срабатывать, если у пары есть заявления этого типа с `report_m_dt` = T. JSON: `app_cnt` — число заявлений, `app_with_zachisl_cnt` — из них с зачислением, `first_app_date` — первая дата заявления.

#### Scenario: 3 из 10
- **WHEN** 10 бумажных заявлений за T, первое 2026-09-01, 3 с зачислением
- **THEN** строка 'Бумажные…' с `app_cnt` = 10, `app_with_zachisl_cnt` = 3, `first_app_date` = '2026-09-01'

#### Scenario: 0 с зачислением
- **WHEN** 4 электронных заявления за T, все без зачислений
- **THEN** строка 'Электронные…' с `app_cnt` = 4, `app_with_zachisl_cnt` = 0

#### Scenario: Оба типа
- **WHEN** у пары за T есть бумажные и электронные заявления
- **THEN** две строки, в каждой — заявления своего типа

### Requirement: Заявления за три месяца
'Бумажные заявления из ВСП или МП СБОЛ за последние три месяца' и 'Электронные заявления из ВСП или МП СБОЛ за последние три месяца' SHALL срабатывать, если у пары есть заявления этого типа с `report_m_dt` в T-2…T. JSON: `app_cnt_3m`, `app_with_zachisl_cnt_3m`, `first_app_date`.

#### Scenario: 9 из 27
- **WHEN** 27 бумажных заявлений за T-2…T, первое 2026-07-04, 9 с зачислением
- **THEN** строка 'Бумажные…' с `app_cnt_3m` = 27, `app_with_zachisl_cnt_3m` = 9, `first_app_date` = '2026-07-04'

### Requirement: Отток за месяц
'По клиенту был отток в прошлом месяце, численность' SHALL срабатывать, если в `uzp_dwh_fact_outflow` за T `outflow_qty ≥ 3` и `fact_payee_qty < prev_m_fl_val`. JSON: `portf_last_m` = `prev_m_fl_val`, `portf_cur_m` = `fact_payee_qty`, `churn_cnt` = их разность, `churn_pct` = `churn_cnt / prev_m_fl_val`.

#### Scenario: 400 → 388
- **WHEN** `outflow_qty` = 15, `prev_m_fl_val` = 400, `fact_payee_qty` = 388
- **THEN** `churn_cnt` = 12, `churn_pct` = 0.03

#### Scenario: Отток 2
- **WHEN** `outflow_qty` = 2
- **THEN** строки нет

### Requirement: Отток за три месяца
'По клиенту был отток в последние три месяца' SHALL срабатывать, если условие оттока за месяц выполнено за T-2, T-1 и T. JSON: `portf_3m_ago` = `prev_m_fl_val` за T-2, `portf_cur_m` = `fact_payee_qty` за T, `churn_pct_3m` = `(portf_3m_ago − portf_cur_m) / portf_3m_ago`.

#### Scenario: Три месяца подряд
- **WHEN** условие выполнено за T-2…T, 430 → 388
- **THEN** `churn_pct_3m` = 0.1 (42 / 430, округлено до 2 знаков)

#### Scenario: Перерыв
- **WHEN** условие не выполнено за T-1
- **THEN** строки нет

### Requirement: Доля по клиенту больше 90%
'Доля по клиенту больше 90%' SHALL срабатывать, если в hm за T `zp_fl_perc > 0,9`. JSON: `fl_cnt` = `current_fl_qty` за T, `fl_dolay` = `zp_fl_perc` за T (дробь, не проценты).

#### Scenario: 93%
- **WHEN** `current_fl_qty` = 12, `zp_fl_perc` = 0.93
- **THEN** строка, `fl_cnt` = 12, `fl_dolay` = 0.93

#### Scenario: 90%
- **WHEN** `zp_fl_perc` = 0.9
- **THEN** строки нет
