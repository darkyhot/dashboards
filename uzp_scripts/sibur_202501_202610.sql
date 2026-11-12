-- СИБУР: январь 2025 — октябрь 2026 включительно. Greenplum / PostgreSQL 9.4.
-- Выполнить весь файл в одной сессии. Рабочие таблицы временные, без COMMIT DROP.
-- sn_t_uzp раскрыта до промышленного имени схемы из portfolio_core/ptf/config.py.
-- Источники списков: yva_inn_sibur(inn), yva_epk_sibur(epk_id).
-- Индивидуальные данные остаются во временных таблицах текущей сессии.
-- Четыре выгрузки: ФОТ/ФЛ по видам выплат; Росстат; премиальные ЕПК; частота выплат.
-- Порог СТРОГО > 2500 по месяцу × ЕПК × ИНН × исправленному ГОСБ;
-- ровно 2500 исключается и показано отдельно от < 2500.
-- Суммы для порога включают отрицательные корректировки. В ФОТ входят положительные
-- строки, как в portfolio_core. Для ФЛ и ФОТ используются РАЗНЫЕ флаги справочника.
-- Вид 2 и организации-исключения учитываются без порога.
-- ФЛ уникальны внутри строки результата; между видами выплат ФЛ не складываются.
-- Нет данных по другим банкам: одна выплата в Сбер — только кандидат на проверку.
-- Проверено на синтетических данных; промышленная БД недоступна локально.

DROP TABLE IF EXISTS tmp_sibur_months;
CREATE TEMP TABLE tmp_sibur_months AS
SELECT CAST(date_trunc('month', DATE '2025-01-01' + n * INTERVAL '1 month')
            + INTERVAL '1 month' - INTERVAL '1 day' AS date) AS report_dt
FROM generate_series(0, 21) n
DISTRIBUTED RANDOMLY;

DROP TABLE IF EXISTS tmp_sibur_inn;
CREATE TEMP TABLE tmp_sibur_inn AS
SELECT DISTINCT CASE WHEN btrim(CAST(inn AS text)) ~ '^[0-9]{1,12}$'
                     THEN CAST(btrim(CAST(inn AS text)) AS bigint) END AS inn
FROM s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.yva_inn_sibur
WHERE btrim(CAST(inn AS text)) ~ '^[0-9]{1,12}$'
DISTRIBUTED BY (inn);

DROP TABLE IF EXISTS tmp_sibur_epk;
CREATE TEMP TABLE tmp_sibur_epk AS
SELECT DISTINCT epk_id
FROM s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.yva_epk_sibur
WHERE epk_id IS NOT NULL
DISTRIBUTED BY (epk_id);

DROP TABLE IF EXISTS tmp_sibur_tik;
CREATE TEMP TABLE tmp_sibur_tik AS
SELECT DISTINCT inn FROM s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.yva_tik_cik
WHERE is_tik_cik IS TRUE AND inn IS NOT NULL
DISTRIBUTED BY (inn);

DROP TABLE IF EXISTS tmp_sibur_exc;
CREATE TEMP TABLE tmp_sibur_exc AS
SELECT CASE WHEN CAST(inn AS text) ~ '^[0-9]{1,12}$'
            THEN CAST(CAST(inn AS text) AS bigint) END AS inn
FROM s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_education_organization
WHERE CAST(inn AS text) ~ '^[0-9]{1,12}$'
UNION
SELECT inn FROM s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_epk_consolidation
WHERE reference_holding_name = 'МИНОБОРОНЫ' AND inn IS NOT NULL
DISTRIBUTED BY (inn);

DROP TABLE IF EXISTS tmp_sibur_types;
CREATE TEMP TABLE tmp_sibur_types AS
SELECT enrollment_type_id, min(enrollment_type_name) AS enrollment_type_name,
       bool_or(COALESCE(is_portfolio_enrollment, false)) AS is_pf,
       bool_or(COALESCE(is_fot_enrollment, false)) AS is_fot
FROM s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_enrollment_type
GROUP BY enrollment_type_id
DISTRIBUTED BY (enrollment_type_id);

ANALYZE tmp_sibur_inn;
ANALYZE tmp_sibur_epk;
ANALYZE tmp_sibur_tik;
ANALYZE tmp_sibur_exc;
ANALYZE tmp_sibur_types;

-- Цикл помесячно читает ведомости в одну временную таблицу.
-- Все создаваемые объекты — только временные таблицы текущей сессии.
-- Премиальные ЕПК проверяются по всем отправителям, включая отправителей вне холдинга.
-- Нерелевантные строки сохранены для диагностики, но в официальный ФЛ/ФОТ не входят.
DROP TABLE IF EXISTS tmp_sibur_raw;
CREATE TEMP TABLE tmp_sibur_raw (
    report_dt date, epk_id bigint, inn bigint, gosb_id integer, code integer,
    is_pf boolean, is_fot boolean, relev boolean, in_holding boolean,
    in_premium_list boolean, exc boolean, amt_net numeric, amt_pos numeric,
    n_pay numeric, n_unknown_qty bigint, n_rows bigint
) DISTRIBUTED BY (epk_id);

DO $load$
DECLARE
    v_report_dt date;
BEGIN
    FOR v_report_dt IN SELECT report_dt FROM tmp_sibur_months ORDER BY report_dt
    LOOP
        -- Литерал даты в динамическом SQL обеспечивает чтение одной партиции.
        EXECUTE format($month$
INSERT INTO tmp_sibur_raw
SELECT report_dt, epk_id, inn, gosb_id, code,
       COALESCE(is_pf, false), COALESCE(is_fot, false), relev,
       in_holding, in_premium_list, exc, sum(COALESCE(amt, 0)),
       sum(CASE WHEN amt > 0 THEN amt ELSE 0 END),
       sum(CASE WHEN amt > 0 THEN COALESCE(transaction_qty, 1) ELSE 0 END),
       sum(CASE WHEN amt > 0 AND (transaction_qty IS NULL OR transaction_qty <= 0)
                THEN 1 ELSE 0 END), count(*)
FROM (
    SELECT p.report_dt, p.epk_id,
           CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END AS inn,
           COALESCE(CASE WHEN p.sys_tb_id = 38 THEN 9038
                         WHEN p.sys_gosb_id = 1009 THEN 8557
                         WHEN p.sys_gosb_id = 0 AND p.sys_tb_id = 40 THEN 9040
                         WHEN p.segment_name IN ('СКБ-Средние', 'СКБ-Прочее', 'СКБ-Крупные',
                              'Средний бизнес', 'Крупный бизнес', 'ККСБ - прочие', 'ККСБ - Прочие')
                              AND p.sys_gosb_id = 8591 THEN 8646
                         ELSE p.sys_gosb_id END, -1) AS gosb_id,
           p.enrollment_type AS code, d.is_pf, d.is_fot,
           COALESCE(p.epk_id IS NOT NULL
                    AND p.inn ~ '^[0-9]{1,12}$'
                    AND p.inn NOT IN ('7707083893', 'Не определено', '0', '-1')
                    AND p.enrollment_kind_descr = 'Основные'
                    AND p.market_share_flag_name <> 'ИП 1 чел.'
                    AND k.inn IS NULL, false) AS relev,
           i.inn IS NOT NULL AS in_holding, e.epk_id IS NOT NULL AS in_premium_list,
           x.inn IS NOT NULL AS exc, p.amt, p.transaction_qty
    FROM s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_m p
    LEFT JOIN tmp_sibur_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN tmp_sibur_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN tmp_sibur_epk e ON e.epk_id = p.epk_id
    LEFT JOIN tmp_sibur_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN tmp_sibur_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE %L
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;
$month$, v_report_dt);
        RAISE NOTICE 'Загружен месяц %', v_report_dt;
    END LOOP;
END;
$load$;

ANALYZE tmp_sibur_raw;

-- Расчёт по тройке до свёртки ГОСБ и видов выплат. Отсечка не применяется к строке
-- или отдельному виду: 1500 ЗП + 1500 аванс проходят, 2500 суммарно — не проходят.
DROP TABLE IF EXISTS tmp_sibur_calc;
CREATE TEMP TABLE tmp_sibur_calc AS
SELECT w.*,
       relev AND amt_pos > 0
           AND (code = 2 OR (is_pf AND (exc OR sum_pf > 2500))) AS counted_fl,
       CASE WHEN relev AND (code = 2 OR (is_fot AND (exc OR sum_fot > 2500)))
            THEN amt_pos ELSE 0 END AS fot_counted,
       relev AND amt_pos > 0 AND is_pf AND code <> 2 AND NOT exc
           AND sum_pf <= 2500 AS excluded_fl_threshold,
       CASE WHEN relev AND is_fot AND code <> 2 AND NOT exc AND sum_fot <= 2500
            THEN amt_pos ELSE 0 END AS fot_excluded_threshold
FROM (
    SELECT r.*,
           sum(CASE WHEN relev AND is_pf THEN amt_net ELSE 0 END)
               OVER (PARTITION BY report_dt, epk_id, inn, gosb_id) AS sum_pf,
           sum(CASE WHEN relev AND is_fot THEN amt_net ELSE 0 END)
               OVER (PARTITION BY report_dt, epk_id, inn, gosb_id) AS sum_fot
    FROM tmp_sibur_raw r
) w
DISTRIBUTED BY (epk_id);
ANALYZE tmp_sibur_calc;

-- Уникальный ФЛ на ИНН × месяц × вид, с объединением ГОСБ перед подсчётом.
DROP TABLE IF EXISTS tmp_sibur_person_kind;
CREATE TEMP TABLE tmp_sibur_person_kind AS
SELECT report_dt, inn, epk_id, code,
       bool_or(amt_pos > 0) AS has_payment,
       bool_or(counted_fl) AS counted_fl,
       bool_or(excluded_fl_threshold) AS has_excluded_fl_part,
       bool_or(excluded_fl_threshold AND sum_pf < 2500) AS has_fl_lt2500_part,
       bool_or(excluded_fl_threshold AND sum_pf = 2500) AS has_fl_eq2500_part,
       sum(amt_pos) AS amt_positive_all,
       sum(CASE WHEN NOT relev THEN amt_pos ELSE 0 END) AS amt_not_relevant,
       sum(CASE WHEN relev AND is_fot THEN amt_pos ELSE 0 END) AS fot_before_threshold,
       sum(fot_counted) AS fot_counted,
       sum(fot_excluded_threshold) AS fot_excluded_threshold,
       sum(CASE WHEN sum_fot < 2500 THEN fot_excluded_threshold ELSE 0 END) AS fot_lt2500,
       sum(CASE WHEN sum_fot = 2500 THEN fot_excluded_threshold ELSE 0 END) AS fot_eq2500
FROM tmp_sibur_calc WHERE in_holding
GROUP BY report_dt, inn, epk_id, code
DISTRIBUTED BY (inn);
ANALYZE tmp_sibur_person_kind;

-- 1. Все ИНН × все 22 месяца × виды выплат (включая виды без ФОТ/портфеля).
-- fl_excluded_threshold — полностью неучтённые ФЛ внутри данного вида.
-- fl_with_excluded_part — включая частично учтённых в другом ГОСБ.
-- fl_excluded_lt2500 и fl_excluded_eq2500 могут пересекаться между ГОСБ.
-- Для epk_id IS NULL деньги сохранены, ФЛ не считается.
WITH a AS (
    SELECT report_dt, inn, code,
           sum(CASE WHEN epk_id IS NOT NULL AND has_payment THEN 1 ELSE 0 END) AS fl_all,
           sum(CASE WHEN counted_fl THEN 1 ELSE 0 END) AS fl_counted,
           sum(CASE WHEN has_excluded_fl_part AND NOT counted_fl THEN 1 ELSE 0 END) AS fl_excluded_threshold,
           sum(CASE WHEN has_fl_lt2500_part AND NOT counted_fl THEN 1 ELSE 0 END) AS fl_excluded_lt2500,
           sum(CASE WHEN has_fl_eq2500_part AND NOT counted_fl THEN 1 ELSE 0 END) AS fl_excluded_eq2500,
           sum(CASE WHEN has_excluded_fl_part THEN 1 ELSE 0 END) AS fl_with_excluded_part,
           sum(amt_positive_all) AS amt_positive_all, sum(amt_not_relevant) AS amt_not_relevant,
           sum(fot_before_threshold) AS fot_before_threshold, sum(fot_counted) AS fot_counted,
           sum(fot_excluded_threshold) AS fot_excluded_threshold,
           sum(fot_lt2500) AS fot_excluded_lt2500, sum(fot_eq2500) AS fot_excluded_eq2500
    FROM tmp_sibur_person_kind GROUP BY report_dt, inn, code
), codes AS (
    SELECT enrollment_type_id AS code, enrollment_type_name FROM tmp_sibur_types
    UNION ALL
    SELECT code, 'Нет в справочнике'::text FROM tmp_sibur_raw r
    WHERE NOT EXISTS (SELECT 1 FROM tmp_sibur_types t WHERE t.enrollment_type_id = r.code)
    GROUP BY code
)
SELECT
    m.report_dt AS "Отчетный месяц",
    i.inn AS "ИНН",
    d.code AS "Код выплаты",
    d.enrollment_type_name AS "Вид выплаты",
    COALESCE(a.fl_all, 0) AS "ФЛ с выплатами",
    COALESCE(a.fl_counted, 0) AS "Учтенные ФЛ",
    COALESCE(a.fl_excluded_threshold, 0) AS "Неучтенные ФЛ по порогу",
    COALESCE(a.fl_excluded_lt2500, 0) AS "Неучтенные ФЛ менее 2500",
    COALESCE(a.fl_excluded_eq2500, 0) AS "Неучтенные ФЛ ровно 2500",
    COALESCE(a.fl_with_excluded_part, 0) AS "ФЛ с отсеченными выплатами",
    COALESCE(a.fot_counted, 0) AS "Учтенный ФОТ, руб.",
    COALESCE(a.fot_excluded_threshold, 0) AS "Отсеченный ФОТ, руб.",
    COALESCE(a.fot_excluded_lt2500, 0) AS "ФОТ менее 2500, руб.",
    COALESCE(a.fot_excluded_eq2500, 0) AS "ФОТ ровно 2500, руб.",
    COALESCE(a.fot_before_threshold, 0) AS "ФОТ до отсечки, руб.",
    COALESCE(a.amt_positive_all, 0) AS "Все выплаты, руб.",
    COALESCE(a.amt_not_relevant, 0) AS "Нерелевантные выплаты, руб."
FROM tmp_sibur_months m CROSS JOIN tmp_sibur_inn i CROSS JOIN codes d
LEFT JOIN a ON a.report_dt = m.report_dt AND a.inn = i.inn AND a.code IS NOT DISTINCT FROM d.code
ORDER BY i.inn, m.report_dt, d.code;

-- 2. Потенциал из company_holding_metric, только ИНН и уровень СБ ('sb').
-- Не суммировать одинаковый потенциал по ГОСБ. Конфликтующие дубли возвращают NULL.
-- Пропуск месяца остаётся NULL, а не 0; lag применяется к полному календарю.
-- Для изменения января 2025 дополнительно читается декабрь 2024.
WITH metrics AS (
    SELECT c.report_dt, c.org_id AS inn, count(*) AS source_rows,
           min(c.emp_potential_qty) AS potential_min,
           max(c.emp_potential_qty) AS potential_max,
           CASE WHEN count(c.emp_potential_qty) = count(*)
                     AND min(c.emp_potential_qty) = max(c.emp_potential_qty)
                THEN max(c.emp_potential_qty) END AS emp_potential_qty
    FROM s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_company_holding_metric c
    JOIN tmp_sibur_inn i ON i.inn = c.org_id
    WHERE c.report_dt >= DATE '2024-12-31' AND c.report_dt <= DATE '2026-10-31'
      AND c.org_type = 'inn' AND c.level_name = 'sb'
    GROUP BY c.report_dt, c.org_id
), calendar AS (
    SELECT report_dt FROM tmp_sibur_months UNION ALL SELECT DATE '2024-12-31'
), series AS (
    SELECT m.report_dt, i.inn, c.emp_potential_qty,
           COALESCE(c.source_rows, 0) AS source_rows, c.potential_min, c.potential_max,
           lag(c.emp_potential_qty) OVER (PARTITION BY i.inn ORDER BY m.report_dt) AS prev_potential
    FROM calendar m CROSS JOIN tmp_sibur_inn i
    LEFT JOIN metrics c ON c.report_dt = m.report_dt AND c.inn = i.inn
)
SELECT
    report_dt AS "Отчетный месяц",
    inn AS "ИНН",
    emp_potential_qty AS "Потенциал по Росстату",
    prev_potential AS "Потенциал прошлого месяца",
    emp_potential_qty - prev_potential AS "Изменение численности",
    100.0 * (emp_potential_qty - prev_potential) / NULLIF(prev_potential, 0) AS "Изменение численности, %",
    source_rows AS "Строк в источнике",
    potential_min AS "Минимальный потенциал",
    potential_max AS "Максимальный потенциал"
FROM series WHERE report_dt >= DATE '2025-01-31'
ORDER BY inn, report_dt;

-- 3. Премиальные ЕПК: каждый месяц, включая месяцы без выплат.
-- ЗП-выплаты — портфельные виды из справочника, включая вид 2.
-- Суммы до отсечки включают все положительные выплаты соответствующих видов.
-- Учтённые ЗП-выплаты и признак получателя — по правилам portfolio_core.
WITH payments AS (
    SELECT report_dt, epk_id,
           bool_or(counted_fl) AS is_recipient,
           sum(CASE WHEN is_pf OR code = 2 THEN amt_pos ELSE 0 END) AS salary_amt,
           sum(CASE WHEN counted_fl THEN amt_pos ELSE 0 END) AS counted_salary_amt,
           sum(CASE WHEN is_pf OR code = 2 THEN 0 ELSE amt_pos END) AS other_amt
    FROM tmp_sibur_calc
    WHERE in_premium_list
    GROUP BY report_dt, epk_id
)
SELECT
    m.report_dt AS "Дата",
    e.epk_id AS "ЕПК ИД",
    CASE WHEN COALESCE(p.is_recipient, false)
         THEN 'Является' ELSE 'Не является' END AS "Получатель ЗП",
    COALESCE(p.salary_amt, 0) AS "Сумма ЗП выплат",
    COALESCE(p.counted_salary_amt, 0) AS "Учтенные ЗП выплаты",
    COALESCE(p.other_amt, 0) AS "Сумма не ЗП выплат"
FROM tmp_sibur_months m
CROSS JOIN tmp_sibur_epk e
LEFT JOIN payments p ON p.report_dt = m.report_dt AND p.epk_id = e.epk_id
ORDER BY e.epk_id, m.report_dt;

-- 4. ФЛ из «Учтенные ФЛ» запроса 1 по коду 1 с единственной выплатой ЗП.
-- База получателей берётся из той же tmp-таблицы, что и в запросе 1.
-- Частоту проверяем до отсечки по всем ГОСБ данного ИНН:
-- ровно одна транзакция кода 1, без аванса (код 16).
-- Вторую выплату менее 2500 не отбрасываем при подсчёте частоты.
-- Неизвестное количество транзакций исключается из счётчика.
WITH persons AS (
    SELECT r.report_dt, r.inn, r.epk_id
    FROM tmp_sibur_person_kind k
    JOIN tmp_sibur_raw r ON r.report_dt = k.report_dt
                        AND r.inn = k.inn AND r.epk_id = k.epk_id
    WHERE k.code = 1 AND k.counted_fl
      AND r.in_holding AND r.relev AND r.amt_pos > 0 AND r.code IN (1, 16)
    GROUP BY r.report_dt, r.inn, r.epk_id
    HAVING sum(r.n_pay) = 1
       AND sum(CASE WHEN r.code = 1 THEN r.n_pay ELSE 0 END) = 1
       AND sum(r.n_unknown_qty) = 0
)
SELECT
    m.report_dt AS "Дата",
    i.inn AS "ИНН",
    count(p.epk_id) AS "ФЛ с одной выплатой ЗП"
FROM tmp_sibur_months m
CROSS JOIN tmp_sibur_inn i
LEFT JOIN persons p ON p.report_dt = m.report_dt AND p.inn = i.inn
GROUP BY m.report_dt, i.inn
ORDER BY i.inn, m.report_dt;
