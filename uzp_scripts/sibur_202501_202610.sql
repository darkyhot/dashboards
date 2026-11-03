-- СИБУР: январь 2025 — октябрь 2026 включительно. Greenplum / PostgreSQL 9.4.
-- Выполнить весь файл в одной сессии. Рабочие таблицы временные, без COMMIT DROP.
-- sn_t_uzp раскрыта до промышленного имени схемы из portfolio_core/ptf/config.py.
-- Источники списков: yva_inn_sibur(inn), yva_epk_sibur(epk_id).
-- Индивидуальные данные остаются во временных таблицах текущей сессии.
-- Результаты: SELECT 1а, 1б, 2, 3а, 3б, 4 и контроль в конце файла.
-- Порог СТРОГО > 2500 по месяцу × ЕПК × ИНН × исправленному ГОСБ;
-- ровно 2500 исключается и показано отдельно от < 2500.
-- Суммы для порога включают отрицательные корректировки. В ФОТ входят положительные
-- строки, как в portfolio_core. Для ФЛ и ФОТ используются РАЗНЫЕ флаги справочника.
-- Вид 2 и организации-исключения учитываются без порога.
-- ФЛ уникальны внутри строки результата; между видами выплат ФЛ не складываются.
-- Нет данных по другим банкам: одна выплата в Сбер — только кандидат на проверку.
-- Проверено на синтетических данных; промышленная БД недоступна локально.

SET search_path TO pg_temp, s_grnplm_ld_salesntwrk_pcap_sn_t_uzp,
                   s_grnplm_ld_salesntwrk_pcap_sn_uzp, public;

DROP TABLE IF EXISTS pg_temp.sb_months;
CREATE TEMP TABLE sb_months AS
SELECT CAST(date_trunc('month', DATE '2025-01-01' + n * INTERVAL '1 month')
            + INTERVAL '1 month' - INTERVAL '1 day' AS date) AS report_dt
FROM generate_series(0, 21) n
DISTRIBUTED RANDOMLY;

DROP TABLE IF EXISTS pg_temp.sb_inn;
CREATE TEMP TABLE sb_inn AS
SELECT DISTINCT CASE WHEN btrim(CAST(inn AS text)) ~ '^[0-9]{1,12}$'
                     THEN CAST(btrim(CAST(inn AS text)) AS bigint) END AS inn
FROM s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.yva_inn_sibur
WHERE btrim(CAST(inn AS text)) ~ '^[0-9]{1,12}$'
DISTRIBUTED BY (inn);

DROP TABLE IF EXISTS pg_temp.sb_epk;
CREATE TEMP TABLE sb_epk AS
SELECT DISTINCT epk_id
FROM s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.yva_epk_sibur
WHERE epk_id IS NOT NULL
DISTRIBUTED BY (epk_id);

DROP TABLE IF EXISTS pg_temp.sb_tik;
CREATE TEMP TABLE sb_tik AS
SELECT DISTINCT inn FROM s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.yva_tik_cik
WHERE is_tik_cik IS TRUE AND inn IS NOT NULL
DISTRIBUTED BY (inn);

DROP TABLE IF EXISTS pg_temp.sb_exc;
CREATE TEMP TABLE sb_exc AS
SELECT CASE WHEN CAST(inn AS text) ~ '^[0-9]{1,12}$'
            THEN CAST(CAST(inn AS text) AS bigint) END AS inn
FROM s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_education_organization
WHERE CAST(inn AS text) ~ '^[0-9]{1,12}$'
UNION
SELECT inn FROM s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_epk_consolidation
WHERE reference_holding_name = 'МИНОБОРОНЫ' AND inn IS NOT NULL
DISTRIBUTED BY (inn);

DROP TABLE IF EXISTS pg_temp.sb_types;
CREATE TEMP TABLE sb_types AS
SELECT enrollment_type_id, min(enrollment_type_name) AS enrollment_type_name,
       bool_or(COALESCE(is_portfolio_enrollment, false)) AS is_pf,
       bool_or(COALESCE(is_fot_enrollment, false)) AS is_fot
FROM s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_enrollment_type
GROUP BY enrollment_type_id
DISTRIBUTED BY (enrollment_type_id);

ANALYZE sb_inn;
ANALYZE sb_epk;
ANALYZE sb_tik;
ANALYZE sb_exc;
ANALYZE sb_types;

-- Каждый INSERT читает одну месячную партицию напрямую в TEMP TABLE sb_raw.
-- Все создаваемые объекты — только временные таблицы текущей сессии.
-- Премиальные ЕПК проверяются по всем отправителям, включая отправителей вне холдинга.
-- Нерелевантные строки сохранены для диагностики, но в официальный ФЛ/ФОТ не входят.
DROP TABLE IF EXISTS pg_temp.sb_raw;
CREATE TEMP TABLE sb_raw (
    report_dt date, epk_id bigint, inn bigint, gosb_id integer, code integer,
    is_pf boolean, is_fot boolean, relev boolean, in_holding boolean,
    in_premium_list boolean, exc boolean, amt_net numeric, amt_pos numeric,
    n_pay numeric, n_unknown_qty bigint, n_rows bigint
) DISTRIBUTED BY (epk_id);

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-01-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-02-28'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-03-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-04-30'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-05-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-06-30'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-07-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-08-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-09-30'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-10-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-11-30'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2025-12-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2026-01-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2026-02-28'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2026-03-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2026-04-30'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2026-05-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2026-06-30'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2026-07-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2026-08-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2026-09-30'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

INSERT INTO sb_raw
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
    LEFT JOIN sb_types d ON d.enrollment_type_id = p.enrollment_type
    LEFT JOIN sb_inn i ON i.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_epk e ON e.epk_id = p.epk_id
    LEFT JOIN sb_exc x ON x.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    LEFT JOIN sb_tik k ON k.inn = CASE WHEN p.inn ~ '^[0-9]{1,12}$' THEN CAST(p.inn AS bigint) END
    WHERE p.report_dt = DATE '2026-10-31'
      AND (i.inn IS NOT NULL OR e.epk_id IS NOT NULL)
) src
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

ANALYZE sb_raw;

-- Расчёт по тройке до свёртки ГОСБ и видов выплат. Отсечка не применяется к строке
-- или отдельному виду: 1500 ЗП + 1500 аванс проходят, 2500 суммарно — не проходят.
DROP TABLE IF EXISTS pg_temp.sb_calc;
CREATE TEMP TABLE sb_calc AS
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
    FROM sb_raw r
) w
DISTRIBUTED BY (epk_id);
ANALYZE sb_calc;

-- 1. Уникальный ФЛ на ИНН × месяц × вид; итог по ИНН считается отдельно,
-- чтобы один человек с ЗП/авансом или несколькими ГОСБ не был посчитан дважды.
DROP TABLE IF EXISTS pg_temp.sb_person_kind;
CREATE TEMP TABLE sb_person_kind AS
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
FROM sb_calc WHERE in_holding
GROUP BY report_dt, inn, epk_id, code
DISTRIBUTED BY (inn);
ANALYZE sb_person_kind;

-- 1а. Все ИНН × все 22 месяца × виды выплат (включая виды без ФОТ/портфеля).
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
    FROM sb_person_kind GROUP BY report_dt, inn, code
), codes AS (
    SELECT enrollment_type_id AS code, enrollment_type_name FROM sb_types
    UNION ALL
    SELECT code, 'Нет в справочнике'::text FROM sb_raw r
    WHERE NOT EXISTS (SELECT 1 FROM sb_types t WHERE t.enrollment_type_id = r.code)
    GROUP BY code
)
SELECT m.report_dt, i.inn, d.code, d.enrollment_type_name,
       COALESCE(a.fl_all, 0) AS fl_all, COALESCE(a.fl_counted, 0) AS fl_counted,
       COALESCE(a.fl_excluded_threshold, 0) AS fl_excluded_threshold,
       COALESCE(a.fl_excluded_lt2500, 0) AS fl_excluded_lt2500,
       COALESCE(a.fl_excluded_eq2500, 0) AS fl_excluded_eq2500,
       COALESCE(a.fl_with_excluded_part, 0) AS fl_with_excluded_part,
       COALESCE(a.fot_counted, 0) AS fot_counted,
       COALESCE(a.fot_excluded_threshold, 0) AS fot_excluded_threshold,
       COALESCE(a.fot_excluded_lt2500, 0) AS fot_excluded_lt2500,
       COALESCE(a.fot_excluded_eq2500, 0) AS fot_excluded_eq2500,
       COALESCE(a.fot_before_threshold, 0) AS fot_before_threshold,
       COALESCE(a.amt_positive_all, 0) AS amt_positive_all,
       COALESCE(a.amt_not_relevant, 0) AS amt_not_relevant
FROM sb_months m CROSS JOIN sb_inn i CROSS JOIN codes d
LEFT JOIN a ON a.report_dt = m.report_dt AND a.inn = i.inn AND a.code IS NOT DISTINCT FROM d.code
ORDER BY i.inn, m.report_dt, d.code;

-- 1б. Итого по ИНН без задвоения ФЛ между видами выплат и ГОСБ.
WITH persons AS (
    SELECT report_dt, inn, epk_id, bool_or(counted_fl) AS counted_fl,
           bool_or(has_excluded_fl_part) AS has_excluded_fl_part,
           bool_or(has_fl_lt2500_part) AS has_fl_lt2500_part,
           bool_or(has_fl_eq2500_part) AS has_fl_eq2500_part,
           sum(fot_counted) AS fot_counted,
           sum(fot_excluded_threshold) AS fot_excluded_threshold
    FROM sb_person_kind GROUP BY report_dt, inn, epk_id
), a AS (
    SELECT report_dt, inn,
           sum(CASE WHEN counted_fl THEN 1 ELSE 0 END) AS fl_counted,
           sum(CASE WHEN has_excluded_fl_part AND NOT counted_fl THEN 1 ELSE 0 END) AS fl_excluded_threshold,
           sum(CASE WHEN has_fl_lt2500_part AND NOT counted_fl THEN 1 ELSE 0 END) AS fl_excluded_lt2500,
           sum(CASE WHEN has_fl_eq2500_part AND NOT counted_fl THEN 1 ELSE 0 END) AS fl_excluded_eq2500,
           sum(fot_counted) AS fot_counted, sum(fot_excluded_threshold) AS fot_excluded_threshold
    FROM persons GROUP BY report_dt, inn
)
SELECT m.report_dt, i.inn, COALESCE(a.fl_counted, 0) AS fl_counted,
       COALESCE(a.fl_excluded_threshold, 0) AS fl_excluded_threshold,
       COALESCE(a.fl_excluded_lt2500, 0) AS fl_excluded_lt2500,
       COALESCE(a.fl_excluded_eq2500, 0) AS fl_excluded_eq2500,
       COALESCE(a.fot_counted, 0) AS fot_counted,
       COALESCE(a.fot_excluded_threshold, 0) AS fot_excluded_threshold
FROM sb_months m CROSS JOIN sb_inn i
LEFT JOIN a ON a.report_dt = m.report_dt AND a.inn = i.inn
ORDER BY i.inn, m.report_dt;

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
    JOIN sb_inn i ON i.inn = c.org_id
    WHERE c.report_dt >= DATE '2024-12-31' AND c.report_dt <= DATE '2026-10-31'
      AND c.org_type = 'inn' AND c.level_name = 'sb'
    GROUP BY c.report_dt, c.org_id
), calendar AS (
    SELECT report_dt FROM sb_months UNION ALL SELECT DATE '2024-12-31'
), series AS (
    SELECT m.report_dt, i.inn, c.emp_potential_qty,
           COALESCE(c.source_rows, 0) AS source_rows, c.potential_min, c.potential_max,
           lag(c.emp_potential_qty) OVER (PARTITION BY i.inn ORDER BY m.report_dt) AS prev_potential
    FROM calendar m CROSS JOIN sb_inn i
    LEFT JOIN metrics c ON c.report_dt = m.report_dt AND c.inn = i.inn
)
SELECT report_dt, inn, emp_potential_qty, prev_potential,
       emp_potential_qty - prev_potential AS potential_change,
       100.0 * (emp_potential_qty - prev_potential) / NULLIF(prev_potential, 0) AS potential_change_pct,
       source_rows, potential_min, potential_max
FROM series WHERE report_dt >= DATE '2025-01-31'
ORDER BY inn, report_dt;

-- 3а. Каждый ЕПК списка × каждый месяц, в том числе месяцы без зачислений.
-- Наличие премиального ПУ задаётся предоставленным списком, не выводится из выплат.
-- has_payroll_* — портфель по правилам; has_zp_* — именно ЗП/аванс (коды 1/16).
WITH a AS (
    SELECT report_dt, epk_id,
           bool_or(amt_pos > 0) AS has_any_payment,
           bool_or(counted_fl) AS has_payroll_sber,
           bool_or(counted_fl AND in_holding) AS has_payroll_holding,
           bool_or(relev AND code IN (1, 16) AND amt_pos > 0) AS has_zp_before_threshold,
           bool_or(counted_fl AND code IN (1, 16)) AS has_zp_sber,
           bool_or(counted_fl AND code IN (1, 16) AND in_holding) AS has_zp_holding,
           bool_or(excluded_fl_threshold) AS has_below_threshold,
           sum(fot_counted) AS fot_sber,
           sum(CASE WHEN in_holding THEN fot_counted ELSE 0 END) AS fot_holding,
           sum(fot_excluded_threshold) AS fot_excluded_threshold
    FROM sb_calc WHERE in_premium_list GROUP BY report_dt, epk_id
)
SELECT m.report_dt, e.epk_id,
       COALESCE(a.has_any_payment, false) AS has_any_payment,
       COALESCE(a.has_payroll_sber, false) AS has_payroll_sber,
       COALESCE(a.has_payroll_holding, false) AS has_payroll_holding,
       COALESCE(a.has_zp_before_threshold, false) AS has_zp_before_threshold,
       COALESCE(a.has_zp_sber, false) AS has_zp_sber,
       COALESCE(a.has_zp_holding, false) AS has_zp_holding,
       COALESCE(a.has_below_threshold, false) AS has_below_threshold,
       COALESCE(a.fot_sber, 0) AS fot_sber, COALESCE(a.fot_holding, 0) AS fot_holding,
       COALESCE(a.fot_excluded_threshold, 0) AS fot_excluded_threshold
FROM sb_months m CROSS JOIN sb_epk e
LEFT JOIN a ON a.report_dt = m.report_dt AND a.epk_id = e.epk_id
ORDER BY e.epk_id, m.report_dt;

-- 3б. Отправители и виды выплат по премиальным ЕПК, включая выплаты вне холдинга.
SELECT c.report_dt, c.epk_id, c.inn, c.in_holding, c.code, d.enrollment_type_name,
       sum(c.amt_pos) AS amt_positive_all, sum(c.fot_counted) AS fot_counted,
       bool_or(c.counted_fl) AS counted_fl,
       sum(c.fot_excluded_threshold) AS fot_excluded_threshold,
       sum(c.n_pay) AS transaction_qty_estimate, sum(c.n_unknown_qty) AS rows_unknown_qty
FROM sb_calc c LEFT JOIN sb_types d ON d.enrollment_type_id = c.code
WHERE c.in_premium_list AND c.amt_pos > 0
GROUP BY c.report_dt, c.epk_id, c.inn, c.in_holding, c.code, d.enrollment_type_name
ORDER BY c.epk_id, c.report_dt, c.inn, c.code;

-- 4. ФЛ с одной ЗП/авансом или одной портфельной выплатой за месяц от холдинга.
-- Частота — SUM(transaction_qty), НЕ count строк и НЕ число видов выплат.
-- Считаем до отсечки, чтобы вторая выплата < 2500 не сделала две выплаты одной.
-- Частоту считаем по всем ГОСБ и всем ИНН холдинга вместе; затем выводим ИНН плательщика.
-- Фильтр relev соответствует portfolio_core; признаки отсечки показаны отдельно.
-- Коды 1/16 — именно ЗП/аванс; премии/отпускные не подменяют регулярную выплату.
-- Неизвестная transaction_qty: оценка NULL -> 1, но доказанная частота = NULL.
WITH by_org AS (
    SELECT report_dt, epk_id, inn,
           sum(CASE WHEN is_pf OR code = 2 THEN n_pay ELSE 0 END) AS portfolio_pay_qty,
           sum(CASE WHEN is_pf OR code = 2 THEN n_unknown_qty ELSE 0 END) AS pf_unknown_qty,
           sum(CASE WHEN code IN (1, 16) THEN n_pay ELSE 0 END) AS zp_pay_qty,
           sum(CASE WHEN code IN (1, 16) THEN n_unknown_qty ELSE 0 END) AS zp_unknown_qty,
           sum(CASE WHEN code = 1 THEN amt_pos ELSE 0 END) AS salary_amt,
           sum(CASE WHEN code = 16 THEN amt_pos ELSE 0 END) AS advance_amt,
           sum(fot_counted) AS fot_counted,
           bool_or(counted_fl) AS counted_fl,
           bool_or(excluded_fl_threshold) AS has_excluded_fl_part
    FROM sb_calc WHERE in_holding AND relev AND amt_pos > 0
    GROUP BY report_dt, epk_id, inn
), by_person AS (
    SELECT report_dt, epk_id, sum(portfolio_pay_qty) AS holding_portfolio_pay_qty,
           sum(pf_unknown_qty) AS pf_unknown_qty,
           sum(zp_pay_qty) AS holding_zp_pay_qty, sum(zp_unknown_qty) AS zp_unknown_qty
    FROM by_org GROUP BY report_dt, epk_id
)
SELECT o.report_dt, o.epk_id, o.inn AS payer_inn,
       o.zp_pay_qty AS payer_zp_pay_qty_estimate,
       p.holding_zp_pay_qty AS holding_zp_pay_qty_estimate,
       CASE WHEN p.zp_unknown_qty = 0 THEN p.holding_zp_pay_qty END AS holding_zp_pay_qty_confirmed,
       p.holding_portfolio_pay_qty AS holding_portfolio_pay_qty_estimate,
       CASE WHEN p.pf_unknown_qty = 0 THEN p.holding_portfolio_pay_qty END AS holding_portfolio_pay_qty_confirmed,
       p.holding_zp_pay_qty = 1 AS one_zp_payment_estimate,
       p.holding_portfolio_pay_qty = 1 AS one_portfolio_payment_estimate,
       o.salary_amt, o.advance_amt, o.fot_counted, o.counted_fl, o.has_excluded_fl_part,
       p.zp_unknown_qty AS rows_unknown_zp_qty,
       CASE WHEN p.zp_unknown_qty > 0 OR (p.holding_portfolio_pay_qty = 1 AND p.pf_unknown_qty > 0)
                 THEN 'Частота требует проверки: неизвестно количество транзакций'
            WHEN p.holding_zp_pay_qty = 0 THEN 'Одна портфельная выплата иного вида; проверить регулярные ЗП/аванс'
            WHEN o.advance_amt > 0 THEN 'Одна выплата: аванс; проверить перечисление ЗП в другой банк'
            ELSE 'Одна выплата: ЗП; проверить перечисление аванса в другой банк' END AS check_reason,
       'Нет данных о зачислениях в другие банки; разделение не подтверждено'::text AS other_bank_status
FROM by_org o JOIN by_person p ON p.report_dt = o.report_dt AND p.epk_id = o.epk_id
WHERE (p.holding_zp_pay_qty = 1 AND o.zp_pay_qty > 0)
   OR (p.holding_portfolio_pay_qty = 1 AND o.portfolio_pay_qty > 0)
ORDER BY o.epk_id, o.report_dt, o.inn;

-- Контроль 1. Полнота источника по месяцам рабочего набора: отсутствие строк
-- не доказывает отсутствие выплат (может быть недогруз промышленной витрины).
SELECT m.report_dt, COALESCE(sum(r.n_rows), 0) AS source_rows_in_scope,
       COALESCE(sum(CASE WHEN r.in_holding THEN r.n_rows ELSE 0 END), 0) AS holding_source_rows,
       COALESCE(sum(CASE WHEN r.in_premium_list THEN r.n_rows ELSE 0 END), 0) AS premium_source_rows,
       COALESCE(sum(CASE WHEN r.epk_id IS NULL THEN r.amt_pos ELSE 0 END), 0) AS amt_without_epk,
       COALESCE(sum(CASE WHEN r.code IS NULL OR d.enrollment_type_id IS NULL THEN r.n_rows ELSE 0 END), 0) AS rows_unknown_type,
       COALESCE(sum(r.n_unknown_qty), 0) AS rows_unknown_transaction_qty
FROM sb_months m LEFT JOIN sb_raw r ON r.report_dt = m.report_dt
LEFT JOIN sb_types d ON d.enrollment_type_id = r.code
GROUP BY m.report_dt ORDER BY m.report_dt;

-- Контроль 2. Должно быть 0: ФОТ до отсечки = учтённый + исключённый по порогу.
-- Вид 2 отдельно: он учитывается независимо от справочного флага is_fot.
SELECT report_dt, inn,
       sum(CASE WHEN relev AND (is_fot OR code = 2) THEN amt_pos ELSE 0 END)
           - sum(fot_counted) - sum(fot_excluded_threshold) AS fot_balance_diff
FROM sb_calc WHERE in_holding
GROUP BY report_dt, inn
HAVING sum(CASE WHEN relev AND (is_fot OR code = 2) THEN amt_pos ELSE 0 END)
           - sum(fot_counted) - sum(fot_excluded_threshold) <> 0
ORDER BY inn, report_dt;
