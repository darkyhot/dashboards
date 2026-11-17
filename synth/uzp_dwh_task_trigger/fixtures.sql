-- Тестовые данные под сценарии спек витрины uzp_dwh_task_trigger (интент 001).
-- Месяц T = 2026-10-31, неделя 2026-11-09…2026-11-15. ИНН пар — 9900000000NN.
-- Файл идемпотентен: сначала удаляет свои строки.

delete from s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_company_holding_metric where org_id between 990000000001 and 990000000099;
delete from s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_fl_navigator_planning_base where inn like '9900000000%';
delete from s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_fact_outflow where inn between 990000000001 and 990000000099;
delete from s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_sale_funnel_task where inn between 990000000001 and 990000000099;
delete from s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_epk_consolidation where inn between 990000000001 and 990000000099;
delete from s_grnplm_ld_salesntwrk_pcap_sn_uzp.jupiter_data_agrmnt_num where inn between 990000000001 and 990000000099;
delete from s_grnplm_ld_salesntwrk_pcap_sn_uzp.vsp_data_salary_detail_report where coalesce(fact_inn, gu_inn) between 990000000001 and 990000000099 or urf_code = '099_9999_001';
delete from s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_d where epk_id between 9900000000 and 9900099999;

-- holding_metric: inn, gosb, report_dt, fot, fl, np, potential, total_emp, new_fl, zp_fl_perc
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_company_holding_metric
	(report_dt, level_name, level_id, org_type, org_id, current_fot_amt, current_fl_qty, np_cnt,
	 emp_potential_qty, total_emp_qty, new_fl_cnt, zp_fl_perc)
select v.dt, 'gosb', v.gosb, 'inn', v.inn, v.fot, v.fl, v.np, v.pot, v.emp, v.nfl, v.perc
from (values
	-- отрицательный потенциал: 001 да, 002 нет
	(990000000001, 9001, date '2026-10-31', 100::numeric, 1, 55, 40::numeric, 10::numeric, 0, 0::numeric),
	(990000000002, 9001, date '2026-10-31', 100, 1, 40, 40, 10, 0, 0),
	-- кп первое зачисление: 004 — зачисление в T-7
	(990000000003, 9001, date '2026-10-31', 1000, 12, 0, 50, 20, 0, 0),
	(990000000004, 9001, date '2026-10-31', 1000, 12, 0, 50, 20, 0, 0),
	(990000000004, 9001, date '2026-03-31', 500, 5, 0, 50, 20, 0, 0),
	-- сезонный (портфель fl; численность emp не растёт): 005 рост 12% в оба года, 006 — 12% и 5%
	(990000000005, 9001, date '2025-11-30', 100, 100, 0, 0, 500, 0, 0),
	(990000000005, 9001, date '2025-12-31', 100, 112, 0, 0, 500, 0, 0),
	(990000000005, 9001, date '2024-11-30', 100, 100, 0, 0, 500, 0, 0),
	(990000000005, 9001, date '2024-12-31', 100, 112, 0, 0, 500, 0, 0),
	(990000000006, 9001, date '2025-11-30', 100, 100, 0, 0, 500, 0, 0),
	(990000000006, 9001, date '2025-12-31', 100, 112, 0, 0, 500, 0, 0),
	(990000000006, 9001, date '2024-11-30', 100, 100, 0, 0, 500, 0, 0),
	(990000000006, 9001, date '2024-12-31', 100, 105, 0, 0, 500, 0, 0),
	-- высокая текучка (портфель fl; численность emp растёт): 007 145 → 140, 008 140 → 145
	(990000000007, 9001, date '2026-09-30', 100, 145, 0, 0, 500, 0, 0),
	(990000000007, 9001, date '2026-10-31', 100, 140, 2, 0, 510, 12, 0),
	(990000000008, 9001, date '2026-09-30', 100, 140, 0, 0, 510, 0, 0),
	(990000000008, 9001, date '2026-10-31', 100, 145, 2, 0, 500, 12, 0),
	-- контактная политика: пары за T
	(990000000012, 9001, date '2026-10-31', 100, 1, 0, 0, 10, 0, 0),
	(990000000013, 9001, date '2026-10-31', 100, 1, 0, 0, 10, 0, 0),
	(990000000014, 9001, date '2026-10-31', 100, 1, 0, 0, 10, 0, 0),
	(990000000015, 9001, date '2026-10-31', 100, 1, 0, 0, 10, 0, 0),
	-- доля: 019 0.93, 020 0.9
	(990000000019, 9001, date '2026-10-31', 100, 12, 0, 0, 10, 0, 0.93),
	(990000000020, 9001, date '2026-10-31', 100, 1, 0, 0, 10, 0, 0.9)
) v (inn, gosb, dt, fot, fl, np, pot, emp, nfl, perc);

-- кп первое зачисление: строки с ФОТ = 0 за T-12…T-1 (003 — 12 месяцев, 004 — без T-7)
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_company_holding_metric
	(report_dt, level_name, level_id, org_type, org_id, current_fot_amt, current_fl_qty, np_cnt,
	 emp_potential_qty, total_emp_qty, new_fl_cnt, zp_fl_perc)
select (date '2026-10-01' - (g || ' month')::interval - interval '1 day')::date, 'gosb', 9001, 'inn', p.inn,
	0, 0, 0, 0, 20, 0, 0
from generate_series(0, 11) g
cross join (values (990000000003), (990000000004)) p (inn)
where not (p.inn = 990000000004 and g = 6);

-- есть договор нет зачисления: ФОТ = 0 за T-5…T у 009, 010, 011
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_company_holding_metric
	(report_dt, level_name, level_id, org_type, org_id, current_fot_amt, current_fl_qty, np_cnt,
	 emp_potential_qty, total_emp_qty, new_fl_cnt, zp_fl_perc)
select (date '2026-11-01' - (g || ' month')::interval - interval '1 day')::date, 'gosb', 9001, 'inn', p.inn,
	0, 0, 0, 0, 10, 0, 0
from generate_series(0, 5) g
cross join (values (990000000009), (990000000010), (990000000011)) p (inn);

-- ср чек: ГОСБ 9002, одна отрасль; 016 — 33 000, 017 — 77 000, среднее 55 000
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_company_holding_metric
	(report_dt, level_name, level_id, org_type, org_id, current_fot_amt, current_fl_qty, np_cnt,
	 emp_potential_qty, total_emp_qty, new_fl_cnt, zp_fl_perc)
select (date '2026-11-01' - (g || ' month')::interval - interval '1 day')::date, 'gosb', 9002, 'inn', p.inn,
	p.fot, 10, 0, 0, 10, 0, 0
from generate_series(0, 5) g
cross join (values (990000000016, 330000), (990000000017, 770000)) p (inn, fot);

-- отрасль: 016 — неактивная головная (позже) и активная неголовная;
-- 017 — две активные: головная без отрасли и неголовная с отраслью
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_epk_consolidation
	(epk_id, epk_create_dttm, inn, industry_name, status_name, is_parent)
values
	(9900000016001, '2025-01-01', 990000000016, 'Другая отрасль', 'Ликвидирована', true),
	(9900000016002, '2020-01-01', 990000000016, 'Тестовая отрасль', 'Активна', false),
	(9900000017001, '2025-01-01', 990000000017, null, 'Активна', true),
	(9900000017002, '2020-01-01', 990000000017, 'Тестовая отрасль', 'Активна', false);

-- договоры: 009 — год назад, 010 — 2 месяца назад, 011 — закрыт; 033, 034 — действуют
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.jupiter_data_agrmnt_num (inn, start_dt, end_dt)
values
	(990000000009, current_date - interval '1 year', null),
	(990000000010, current_date - interval '2 month', null),
	(990000000011, '2020-01-01', '2026-01-01'),
	(990000000033, '2020-01-01', null),
	(990000000034, '2020-01-01', current_date + interval '1 year');

-- задачи МЗП: 012 — 2026-06-20, 013 — 2026-07-15, 014 — 2026-11-05 (после T), 015 — нет;
-- задача ИСУ у 012 позже — не учитывается
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_sale_funnel_task
	(task_code, inn, gosb_id, is_task_closed, role_code, src_task_as_code, fact_close_task_dttm)
values
	('fx-012', 990000000012, 9001, true, 'МЗП', 'КМ', '2026-06-20 10:00'),
	('fx-012-isu', 990000000012, 9001, true, 'МЗП', 'ИСУ', '2026-09-20 10:00'),
	('fx-013', 990000000013, 9001, true, 'МЗП', 'КМ', '2026-07-15 10:00'),
	('fx-014', 990000000014, 9001, true, 'МЗП', 'КМ', '2026-11-05 10:00');

-- отток: 021 за T, 022 — отток 2, 023 — три месяца подряд, 024 — перерыв в T-1
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_fact_outflow
	(report_dt, gosb_id, inn, outflow_qty, prev_m_fl_val, fact_payee_qty)
values
	('2026-10-31', 9001, 990000000021, 15, 400, 388),
	('2026-10-31', 9001, 990000000022, 2, 400, 388),
	('2026-08-31', 9001, 990000000023, 5, 430, 420),
	('2026-09-30', 9001, 990000000023, 4, 420, 400),
	('2026-10-31', 9001, 990000000023, 3, 400, 388),
	('2026-08-31', 9001, 990000000024, 5, 430, 420),
	('2026-09-30', 9001, 990000000024, 1, 420, 419),
	('2026-10-31', 9001, 990000000024, 3, 419, 400);

-- навигатор: 025 — только ЕПП, 026 — ЕПП и реестр
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_fl_navigator_planning_base
	(report_dt, version_plan_dt, inn, gosb_id, sales_flag, fl_count)
values
	('2026-10-31', '2026-10-31', '990000000025', 9001, 'Единичное платежное поручение', 14),
	('2026-10-31', '2026-10-31', '990000000026', 9001, 'Единичное платежное поручение', 5),
	('2026-10-31', '2026-10-31', '990000000026', 9001, 'Реестр + Единичное платежное поручение', 7);

-- заявления за месяц: 027 — 10 за T (с 2026-10-01) и 17 за T-2…T-1 (с 2026-08-04),
-- 028 — 4 за T без зачислений. epk_id = 99000NNKKK.
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.vsp_data_salary_detail_report
	(report_m_dt, report_dt, urf_code, epk_id, is_asm, fact_inn, gu_inn)
select date '2026-10-31', date '2026-10-01' + g, '044_8558_062', 9900027000 + g, 1, 990000000027, null::int8
from generate_series(0, 9) g
union all
select date '2026-09-30', date '2026-09-01' + g, '044_8558_062', 9900027100 + g, 1, null::int8, 990000000027
from generate_series(0, 7) g
union all
select date '2026-08-31', date '2026-08-04' + g, '044_8558_062', 9900027200 + g, 1, 990000000027, null::int8
from generate_series(0, 8) g
union all
select date '2026-10-31', date '2026-10-05' + g, '044_8558_062', 9900028000 + g, 1, 990000000028, null::int8
from generate_series(0, 3) g
union all
-- не берутся: не АСМ, ГОСБ не найден
select date '2026-10-31', date '2026-10-10', '044_8558_062', 9900029000, 0, 990000000029, null
union all
select date '2026-10-31', date '2026-10-10', '099_9999_001', 9900029001, 1, 990000000029, null;

-- зачисления 027: за T — 3 из 10 (ЗП-код 1 после заявления); ещё одно до заявления
-- и одно с не-ЗП кодом 3 — не считаются. За T-2…T-1 — 6 из 17.
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_d (epk_id, report_dt, enrollment_type, amt)
values
	(9900027000, '2026-10-20', 1, 1500),
	(9900027001, '2026-10-20', 1, 1500),
	(9900027002, '2026-10-25', 1, 50000),
	(9900027003, '2026-09-15', 1, 50000),
	(9900027004, '2026-10-25', 3, 50000),
	(9900027100, '2026-09-20', 1, 1000),
	(9900027101, '2026-09-20', 1, 1000),
	(9900027102, '2026-10-20', 1, 1000),
	(9900027200, '2026-08-20', 1, 1000),
	(9900027201, '2026-08-20', 1, 1000),
	(9900027202, '2026-09-20', 1, 1000);

-- неделя 2026-11-09…15: 031 — нет договора, 10 заявлений, 3 с зачислением;
-- 032 — нет договора, все с зачислением; 033 — договор, 3 из 10; 034 — договор, все с зачислением;
-- 035 — заявление 2026-11-08, вне недели; 036 — зачисление за день до заявления.
insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.vsp_data_salary_detail_report
	(report_m_dt, report_dt, urf_code, epk_id, is_asm, fact_inn, gu_inn)
select date '2026-11-30', date '2026-11-09' + (g % 7), '044_8558_062', 9900000000 + p.inn % 1000 * 1000 + g, 1, p.inn, null::int8
from (values (990000000031, 10), (990000000032, 2), (990000000033, 10), (990000000034, 2)) p (inn, n)
cross join generate_series(0, 9) g
where g < p.n
union all
select date '2026-11-30', date '2026-11-08', '044_8558_062', 9900035000, 1, 990000000035, null
union all
select date '2026-11-30', date '2026-11-10', '044_8558_062', 9900036000, 1, 990000000036, null;

insert into s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_d (epk_id, report_dt, enrollment_type, amt)
values
	(9900031000, '2026-11-15', 1, 1000),
	(9900031001, '2026-11-15', 1, 1000),
	(9900031002, '2026-11-15', 1, 1000),
	(9900032000, '2026-11-15', 1, 1000),
	(9900032001, '2026-11-15', 1, 1000),
	(9900033000, '2026-11-15', 1, 1000),
	(9900033001, '2026-11-15', 1, 1000),
	(9900033002, '2026-11-15', 1, 1000),
	(9900034000, '2026-11-15', 1, 1000),
	(9900034001, '2026-11-15', 1, 1000),
	(9900036000, '2026-11-09', 1, 1000);
