---
name: pcap-data
description: "Как корректно считать по таблицам УЗП под бизнес-задачи: портфель (получатели ЗП) и ФОТ по mis_data_payroll_m, параметры организации по ИНН (сегмент, холдинг, отрасль, ликвидация) по uzp_data_epk_consolidation, работа только через временные таблицы, SQL под Greenplum 9.4. Используй при любом расчёте по ведомостям, портфелю, ФОТ и организациям."
---

# Данные УЗП: как считать

Если расчёт расходится с этим документом, ошибка в расчёте.

---

## Общие правила

1. **Только временные таблицы.** Большую витрину (`mis_data_payroll_m` прежде всего)
   читать **один раз** — в узкую временную копию с нужными колонками за все нужные
   месяцы. Всё остальное считать из копии. CTE вместо временных таблиц не использовать:
   каждый запрос будет пересчитывать всё заново.
2. **Витрину — помесячно.** `mis_data_payroll_m` партиционирована по `report_dt`.
   Копию заполнять оператором на месяц: `CREATE ... WHERE report_dt = '<месяц>'` по
   первому, `INSERT ... WHERE report_dt = '<месяц>'` по остальным. Запрос без фильтра по
   `report_dt` (или с `>=` на годы) читает всю историю банка и не укладывается в таймаут.
3. **Из копии — одним запросом за все месяцы.** У временной таблицы партиций нет:
   фильтр по одному месяцу читает её целиком, цикл по месяцам — это N полных проходов.
   Группировать по `report_dt`.
4. **Перекодировки и признаки — сразу при заполнении копии**, дальше не пересчитывать.
5. **`DISTRIBUTED BY` по ключу соединения** (`epk_id` или `inn`) и `ANALYZE` после
   создания каждой временной таблицы. Анти-соединения (кто был в одном месяце и пропал в
   другом) — материализовать: без статистики планировщик оценивает их в одну строку и
   строит вложенные циклы, запрос идёт в сотни раз дольше.
6. **`count(DISTINCT)` на больших объёмах не использовать** — в два шага: сначала
   `GROUP BY` до ключа, потом `count(*)`.

---

## Задача: посчитать портфель (получателей ЗП) или ФОТ

Таблицы: `mis_data_payroll_m` (ведомости), `uzp_dim_enrollment_type` (виды зачислений),
`uzp_dim_education_organization` и `uzp_data_epk_consolidation` (организации без порога).

### Эталон (логика заказчика, дословно)

```sql
-- Шаг 1. Временная таблица со строками payroll_m
drop table if exists all_payroll_m_rows cascade;
create temp table all_payroll_m_rows as
select
    acc_num,
    acc_open_dt,
    actual_client_tid,
    amt,
    company_name,
    agrmnt_dt,
    agrmnt_num,
    enrollment_transcription,
    enrollment_type,
    epk_id,
    case
        when sys_tb_id = 38 then 9038
        when sys_gosb_id = 1009 then 8557
        when sys_gosb_id = 0 and sys_tb_id = 40 then 9040
        when segment_name in ('СКБ-Средние','СКБ-Прочее','СКБ-Крупные','Средний бизнес','Крупный бизнес','ККСБ - прочие', 'ККСБ - Прочие')
             and sys_gosb_id = 8591 then 8646
        else sys_gosb_id
    end as sys_gosb_id,
    case
        when inn = 'Не определено' then '0'
        else inn
    end as inn,
    ipt_name,
    sys_osb_id,
    card_type,
    modified_dttm,
    report_dt,
    sys_tb_id,
    transaction_qty,
    sys_vsp_id,
    report_id,
    is_security_force,
    case
        when segment_name in ('СКБ-Средние','СКБ-Прочее','СКБ-Крупные','Средний бизнес','Крупный бизнес','ККСБ - прочие', 'ККСБ - Прочие') then 'КСБ'
        when segment_name in ('ОПК','Клиенты машиностроения','СКМ','Прочие','УРКМ') then 'СКМ'
        when segment_name in ('Малый бизнес','Микробизнес','БМО','УМБ-Малые','УМБ-Микро') then 'ММБ'
        when segment_name in ('Крупнейший бизнес','CIB') then 'КФИ'
        when segment_name in ('Государственный сектор') then 'РГС'
        else segment_name
    end as segment_name,
    enrollment_kind_descr,
    market_share_flag_name,
    src_system_name
from
    s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_m m
where
    m.report_dt >= '2023-11-01';

-- Шаг 2. Джойны к tmp-таблице + предикат relev
--        + оконные суммы по релевантным portfolio/ФОТ-строкам.
drop table if exists enrollments cascade;
create temp table enrollments as
select
    x.*,
    sum(case when x.is_portfolio_enrollment and x.relev = 1 then x.amt else 0 end)
        over (partition by x.report_dt, x.epk_id, x.sys_gosb_id, x.inn) as sum_pf,
    sum(case when x.is_fot_enrollment and x.relev = 1 then x.amt else 0 end)
        over (partition by x.report_dt, x.epk_id, x.sys_gosb_id, x.inn) as sum_fot
from (
    select
        e.*,
        case
            when ((e.enrollment_kind_descr = 'Основные' and e.report_dt >= '2024-01-01')
                    or (e.report_dt between '2023-11-01' and '2023-12-31'))
                 and ((e.market_share_flag_name <> 'ИП 1 чел.' and e.report_dt >= '2024-01-01')
                    or (e.report_dt between '2023-11-01' and '2023-12-31'))
                 and e.inn not in ('7707083893', 'Не определено', '0', '-1')
            then 1 else 0
        end as relev,
        dim.enrollment_type_id,
        dim.is_fot_enrollment,
        dim.is_portfolio_enrollment,
        exc.inn_exc
    from
        all_payroll_m_rows e
    left join
        s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_enrollment_type dim
            on dim.enrollment_type_id = e.enrollment_type
    left join
        (
            select inn_exc
            from (
                select distinct inn::int8 as inn_exc
                from s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_education_organization
                union
                select distinct inn::int8 as inn_exc
                from s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_epk_consolidation
                where reference_holding_name = 'МИНОБОРОНЫ'
            ) t
        ) exc
            on exc.inn_exc = e.inn::int8
) x;

-- Шаг 3. Все поля исходного payroll_m + 2 признака.
--        Признак = 1, если relev=1 и подпадает хотя бы под одно условие.
drop table if exists x1 cascade;
create temp table x1 as
select
    acc_num,
    acc_open_dt,
    actual_client_tid,
    amt,
    company_name,
    agrmnt_dt,
    agrmnt_num,
    enrollment_transcription,
    enrollment_type,
    epk_id,
    sys_gosb_id,
    inn,
    ipt_name,
    sys_osb_id,
    card_type,
    modified_dttm,
    report_dt,
    sys_tb_id,
    transaction_qty,
    sys_vsp_id,
    report_id,
    is_security_force,
    segment_name,
    enrollment_kind_descr,
    market_share_flag_name,
    src_system_name,
    case
        when relev = 1 and amt > 0 and (
                (is_portfolio_enrollment and sum_pf > 2500)              -- усл 1
                or enrollment_type_id = 2                                -- усл 2
                or (is_portfolio_enrollment and inn_exc is not null)     -- усл 3
            ) then 1 else 0
    end as is_salary_client,
    case
        when relev = 1 and amt > 0 and (
                (is_fot_enrollment and sum_fot > 2500)                   -- усл 1
                or enrollment_type_id = 2                                -- усл 2
                or (is_fot_enrollment and inn_exc is not null)           -- усл 3
            ) then 1 else 0
    end as is_salary_fot
from
    enrollments;

-- Портфель и ФОТ по ГОСБ × организации
select report_dt, sys_gosb_id, inn,
       count(distinct epk_id) filter (where is_salary_client = 1) as portfolio,
       sum(amt)               filter (where is_salary_fot = 1)    as fot
from x1
group by 1, 2, 3;
```

### Правила

* **Ведомости — `mis_data_payroll_m`**, не `uzp_data_payroll_m`.
* **Перекодировка ГОСБ** (порядок веток важен): ТБ 38 → 9038; ГОСБ 1009 → 8557; ГОСБ 0
  в ТБ 40 → 9040; сырой сегмент КСБ в ГОСБ 8591 → 8646. Первые три правила применять
  и к другим таблицам с ГОСБ (НФЛ, воронка продаж), если их соединяют с ведомостями,
  иначе не сойдутся по ГОСБ. Правило КСБ — только где есть сегмент ведомостей.
* **Территория — `sys_gosb_id` / `sys_tb_id`.** Старые `gosb_id` / `tb_id` со
  справочником `uzp_dim_gosb` не сходятся.
* **«Не определено» → `'0'`** в номере организации.
* **`relev`** — вид «Основные» и доля рынка не «ИП 1 чел.» (с января 2024; ноябрь–декабрь
  2023 — без этих условий); организация не Сбер (`7707083893`) и не заглушка
  (`Не определено` / `0` / `-1`). Раньше ноября 2023 эта логика не определена.
* **Портфельные и ФОТ-виды — только по справочнику** `uzp_dim_enrollment_type`
  (`is_portfolio_enrollment`, `is_fot_enrollment`). Списки кодов руками не писать.
  Колонка в ведомостях — `enrollment_type`, в справочнике — `enrollment_type_id`.
* **Порог 2500 ₽ — к сумме за месяц по тройке** (месяц, `epk_id`, `sys_gosb_id`, `inn`),
  а не к строке: аванс и зарплата — разные строки.
* **Строка засчитывается** при `relev = 1`, `amt > 0` и любом из трёх условий: сумма
  тройки > 2500; вид зачисления 2; организация без порога (образовательная или
  `reference_holding_name = 'МИНОБОРОНЫ'` в ЕПК).
* **Портфель** = число ФЛ (`epk_id`) по строкам с `is_salary_client = 1`.
  **ФОТ** = сумма `amt` по строкам с `is_salary_fot = 1` (та же схема, но
  `is_fot_enrollment` и `sum_fot`). ФОТ — в рублях.
* **Единица счёта — тройка** `(epk_id, inn, sys_gosb_id)`. Совместитель в двух
  организациях — два получателя; организация, платящая через два ГОСБ, — тоже два.
  Число ФЛ банка и сумма получателей по организациям — разные числа.

### Как исполнять эталон на объёме банка

Эталон задаёт логику. Чтобы он отработал на проме:

1. **Шаг 1 — помесячно** (общее правило 2), только нужные колонки.
2. **Номер организации — под маской цифр.** В ведомостях `inn` — text (бывает «ID123»),
   в справочниках — bigint. `inn::int8` на нецифровом значении роняет **весь** запрос.
   Приводить только так, в том числе в условии соединения:
   `CASE WHEN inn ~ '^[0-9]{1,12}$' THEN CAST(inn AS bigint) END`.
   В `uzp_dim_education_organization` номер тоже текстовый — там тоже маска.
3. **Итог — без `count(DISTINCT)`**: `GROUP BY report_dt, sys_gosb_id, inn, epk_id`,
   затем `count(*)`.
4. **Пустой ГОСБ → `COALESCE(sys_gosb_id, -1)`**, если тройки сравниваются между
   месяцами: `NULL` не равен `NULL`, и тройка «исчезает». Строки без `epk_id` в портфель
   не идут.

---

## Задача: параметры организации по ИНН

Сегмент, холдинг, отрасль, название, ликвидация — из `uzp_data_epk_consolidation`.

```sql
CREATE TEMP TABLE t_org AS
SELECT e.inn,
       min(CASE e.segment_name
             WHEN 'Средние'                THEN 'КСБ'
             WHEN 'Крупные'                THEN 'КСБ'
             WHEN 'Крупнейшие'             THEN 'КСБ'
             WHEN 'Рег. госсектор'         THEN 'РГС'
             WHEN 'Клиенты машиностроения' THEN 'СКМ'
             WHEN 'Фин.институты'          THEN 'КФИ'
             WHEN 'SBI'                    THEN 'БМО'
             WHEN 'Микро'                  THEN 'ММБ'
             WHEN 'Малые'                  THEN 'ММБ'
             ELSE COALESCE(NULLIF(btrim(e.segment_name), ''), 'Сегмент не указан')
           END)                                                AS seg,
       min(e.company_name)                                     AS org_name,
       min(NULLIF(btrim(e.holding_name), ''))                  AS holding_name,
       min(e.industry_name)                                    AS industry_name,
       NOT bool_or(COALESCE(e.status_name, '') = 'Активна')    AS is_liquidated
FROM s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_epk_consolidation e
WHERE e.inn IS NOT NULL
GROUP BY e.inn
DISTRIBUTED BY (inn);
ANALYZE t_org;
```

* **Свернуть до ИНН перед любым соединением.** У одного ИНН бывает несколько записей ЕПК;
  соединение со строками справочника **задваивает ведомости** (и портфель, и ФОТ).
* **Ликвидирована — нет НИ ОДНОЙ активной записи.** Одна мёртвая запись при живой
  соседней ничего не значит.
* **Сегмент — большое имя → короткое** (как в `CASE` выше). Неизвестное имя оставлять
  как есть, не прятать в «прочие». Несколько разных сегментов у ИНН — `min()`
  (детерминированно); таких ИНН бывает немного, но они есть.
* **Это текущий срез**: отчётной даты в справочнике нет. Для сравнения лет разметка
  одна — переклассификация организации между годами не видна.
* **Холдинг:** для разрезов и фильтров — `holding_name`; для организаций без порога в
  портфеле — `reference_holding_name` (так у заказчика). Это разные поля.
* **Присоединять к ведомостям `LEFT JOIN` с маской ИНН:**
  `LEFT JOIN t_org o ON o.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END`.
  ИНН вне справочника не отбрасывать — это тоже получатели.
* `segment_name` в ведомостях бывает пуст — сегмент организации брать отсюда.

---

## Диалект: Greenplum на ядре PostgreSQL 9.4

| Не работает | Чем заменить |
|---|---|
| `make_interval(months => n)` | `n * interval '1 month'` |
| `last_day(d)` (функция заказчика, в ядре нет) | `CAST(date_trunc('month', d) + interval '1 month' - interval '1 day' AS date)` |
| `GROUPING SETS` / `ROLLUP` / `CUBE` | несколько `GROUP BY` через `UNION ALL` |
| `INSERT ... ON CONFLICT` | `INSERT ... SELECT ... WHERE NOT EXISTS` |

Работает: `FILTER (WHERE …)` у агрегатов, оконные функции, `date_trunc`, `CAST`,
`WITH`, `bool_or`, `FULL OUTER JOIN`, `= ANY(массив)`, регулярки `~`.

* **Дата текстом** (например, `data_timestamp` в кликах СБОЛ): сначала
  `CAST(col AS date)`, потом сравнения и `date_trunc`.
* **Литералы дат — с типом:** `DATE '2024-01-01'`.
* **Разница в месяцах:**
  `(EXTRACT(YEAR FROM d2) * 12 + EXTRACT(MONTH FROM d2)) - (EXTRACT(YEAR FROM d1) * 12 + EXTRACT(MONTH FROM d1))`.
