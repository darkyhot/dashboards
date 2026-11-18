# Design

## Context

Greenplum (ядро 9.4), plpgsql-функции, запуск — потоки информатики. Сначала всё создаётся в
лаборатории `s_grnplm_ld_salesntwrk_pcap_sn_t_uzp`.

Данные источников, важные для реализации:

| Источник | Что важно |
| --- | --- |
| `uzp_dwh_company_holding_metric` | пара — `org_id` + `level_id`; `level_name` = 'gosb', `org_type` = 'inn' |
| навигатор | `inn` — varchar |
| `vsp_data_salary_detail_report` | `fact_inn`, `gu_inn` — bigint; `urf_code` = '<ТБ>_<ГОСБ>_<ВСП>'; `credential_transferred_flag` — тип заявления |
| `uzp_dim_gosb` | `tb_id` + `old_gosb_id` → один `new_gosb_id` |
| `uzp_data_epk_consolidation` | несколько ЕПК на ИНН; `status_name`, `is_parent` бывают null |
| `mis_data_payroll_d` | партиции по `report_dt` помесячно |
| `jupiter_data_agrmnt_num` | `inn`, `start_dt`, `end_dt` |

## Goals / Non-Goals

**Goals:**
- Одна таблица, две функции: ежемесячная и еженедельная.
- Функция пересчитывает только свои триггеры за свой период.

**Non-Goals:**
- Триггеры с пометкой «ограничение».
- Прод и потоки информатики.

## Decisions

**Таблица.** Состав полей — в [спеке витрины](specs/trigger-mart/spec.md#requirement-состав-полей).
Время расчёта хранится в `modified_dttm`. Хранение:
- `appendonly=true, compresstype=zstd, compresslevel=1`;
- `partition by range(start_dt)` помесячно, 2026-01-01…2031-01-01, без default;
- `distributed by (inn)`: по ИНН идут соединения, строк меньше 10 млн;
- `distributed by` стоит перед `partition by` — так требует Greenplum.

Партиция по `start_dt`: неделя целиком попадает в партицию понедельника.

**Функции.** `uzp_dwh_task_trigger_month_calc`, `uzp_dwh_task_trigger_week_calc`.
Параметры — `p_etl_load_id`, `p_start_dttm`, `p_end_dttm`; `p_start_dttm` не используется.
- Месяц: `end_dt` = `last_day(p_end_dttm)`, `start_dt` — 1-е число.
- Неделя: `end_dt` = `date_trunc('week', p_end_dttm)::date + 6`, `start_dt` = `end_dt − 6`.

Дата не в конце месяца или не в воскресенье не даёт ошибку: период строится сам.

**Даты месяца.** `last_day`, `date_trunc` и `interval` (`sql-style.md`). Переменные —
только для фильтров по источникам: T, 1-е число T, T-2, T-23. В `tmp_hm` и `tmp_app` колонка
`month_ago` — месяцев от T (0 — месяц T); триггеры сравнивают с ней.

Деления — через `case` с проверкой знаменателя на 0.

**Шаги функции:**

| Шаг | Действие |
| --- | --- |
| `01` | старт, статус 1 |
| `02` | наличие данных источников за период; нет — `98`, статус 98, `return` |
| `10.NN` | `tmp_hm`, `tmp_industry`, `tmp_agrmnt`, `tmp_app`, `tmp_enrl`, `tmp_app_pair`; 0 строк после чтения источника — `98`, статус 98, `return` |
| `20.NN` | вставка каждого триггера в `tmp_stg_uzp_dwh_task_trigger` |
| `21` | стейджинг не пуст и ключ уникален; иначе `98`, статус 98, `return` |
| `30` | `delete` за период по `trigger_name` своей функции, `insert` из стейджинга |
| `40` | `sys_table_analyze` |
| `99` | успех, статус 99; ошибка — `exception`, статус `-1` |

Проверка шага `02`:
- месяц: hm за T; навигатор за `report_dt` = T и `version_plan_dt` = T; заявления за
  `report_m_dt` = T; `fact_outflow` за T; `payroll_d` за месяц T; остальные
  источники не пусты;
- неделя: заявления за неделю; `payroll_d` с `start_dt`; справочники и договоры не пусты.

На шаге `30` функция удаляет строки за точный интервал `start_dt`…`end_dt` и только по своему
списку `trigger_name`, затем записывает новый результат. Так соблюдается правило
[перерасчёта](specs/trigger-mart/spec.md#requirement-перерасчёт).

**Отрасль.** `row_number() over (partition by inn order by coalesce(status_name = 'Активна', false) desc,
(industry_name is not null) desc, coalesce(is_parent, false) desc, epk_create_dttm desc, epk_id desc) = 1`.
`coalesce` — null при `desc` идёт первым. `epk_id` — для однозначности.

**ГОСБ заявления.** Соединение с `uzp_dim_gosb`: `tb_id = split_part(urf_code, '_', 1)::int`,
`old_gosb_id = split_part(urf_code, '_', 2)::int`. Без ТБ связь неоднозначна (1002, 1009).

**Зачисления.** Один проход по `payroll_d` от первой даты заявления до `current_date` с
предикатом по `report_dt`. Соединение с `uzp_dim_enrollment_type` по `is_fot_enrollment`.
`tmp_enrl` хранит `max(report_dt)` по `epk_id`. Заявление с зачислением, если
`max(report_dt)` ≥ даты заявления. `exists` на каждое заявление читал бы таблицу много раз.

**JSON.** `json_build_object`. Даты — строки `ГГГГ-ММ-ДД`, числовые параметры — числа.
Ключи и правила расчёта описаны в спеках [месячной](specs/monthly-triggers/spec.md)
и [недельной](specs/weekly-triggers/spec.md) функций.

**ИНН навигатора.** `try_cast` varchar → int8. Не число — строка не берётся.

## Risks / Trade-offs

- [Нет одного источника — все триггеры функции не считаются] → поток ставить после самого
  позднего источника.
- [Договоры и зачисления берутся на `current_date`: перерасчёт старого периода даст другой
  результат] → так задано интентом; `current_date` пишется в лог.

## Migration Plan

1. Лаборатория: DDL, функции, проверки из tasks.
2. Прод и потоки — отдельным решением. Откат — `drop function`, `drop table`.
