-- Ежемесячные триггеры для задач УЗП.
-- Интент 001 (UZPPZPS1-422), спеки — marts/uzp_dwh_task_trigger/openspec/specs/
-- (trigger-mart, monthly-triggers). Формулы триггеров — только оттуда.

create or replace function s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger_month_calc(
		p_etl_load_id int8,
		p_start_dttm timestamp,
		p_end_dttm timestamp
)
	returns void
	language plpgsql
	volatile
as $$

/*
 * Описание:
 *      Расчет ежемесячных триггеров витрины uzp_dwh_task_trigger за месяц p_end_dttm
 *      (T — последний день месяца). Пересчитывает только свои 15 триггеров за этот месяц.
 *
 * Параметры:
 *      p_etl_load_id - уникальный идентификатор загрузки
 *      p_start_dttm - дата начала периода (не используется)
 *      p_end_dttm - любая дата расчетного месяца
 *
 * Изменения:
 *      2026-11-16 <ФИО>. Функция создана. (<номер задачи>; интент 001)
 */

declare
	v_function_name text := 'uzp_dwh_task_trigger_month_calc';
	v_etl_load_id int8 := p_etl_load_id;
	v_row_count int8 := 0;

	v_start_dt date;
	v_end_dt date;
	v_month_txt text;
	v_t2_dt date;
	v_hm_from_dt date;
	v_min_app_dt date;

	v_missing_src text;
	v_missing_tbl text;
	v_missing_col text;
	v_fact_dt text;
	v_dup_qty int8;

	-- Триггеры этой функции: только их строки заменяются при перерасчёте.
	v_trigger_names text[] := array[
		'ЕПП флаг',
		'ср чек ниже среднего',
		'отрицательный потенциал',
		'кп первое зачисление',
		'сезонный тип деятельности',
		'высокая текучка',
		'есть договор нет зачисления',
		'контактная политика 4 месяца',
		'Подано заявлений в ВСП или МП СБОЛ за прошлый месяц',
		'Заявлений с зачислениями из ВСП или МП СБОЛ за прошлый месяц',
		'Подано заявлений в ВСП или МП СБОЛ за последние три месяца',
		'Заявлений с зачислениями из ВСП или МП СБОЛ за последние три месяца',
		'По клиенту был отток в прошлом месяце, численность',
		'По клиенту был отток в последние три месяца',
		'Доля по клиенту больше 90%'
	];

	v_error_state text;
	v_error_msg text;
	v_error_detail text;
	v_error_hint text;
	v_error_context text;
	v_error_column text;
	v_error_constraint text;
	v_error_datatype text;
	v_error_table text;
	v_error_schema text;
	v_error_message text;
begin
	begin
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
			p_etl_load_id := v_etl_load_id,
			p_state_id := 1::int2
		);

		-- 01. Период — месяц p_end_dttm, T = v_end_dt. Отдельно считаем только даты
		-- для фильтров по источникам: T-2 (заявления, отток) и T-23 (сезонность).
		v_end_dt := s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.last_day(p_end_dttm::date);
		v_start_dt := s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.trunc(v_end_dt, 'mm');
		v_t2_dt := s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.last_day((v_end_dt - interval '2 month')::date);
		v_hm_from_dt := s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.last_day((v_end_dt - interval '23 month')::date);
		v_month_txt := to_char(v_end_dt, 'YYYY-MM-DD');

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '01 - Период: ' || v_start_dt::text || ' - ' || v_end_dt::text
				|| ', дата расчета: ' || current_date::text
		);

		-- 02. Проверка ККД: у каждого источника есть данные за период.
		-- Берём первый источник без данных; tbl_name и dt_col нужны, чтобы показать
		-- в логе фактическую последнюю дату источника.
		select
			chk.src_name,
			chk.tbl_name,
			chk.dt_col
		into
			v_missing_src,
			v_missing_tbl,
			v_missing_col
		from (
				select
					1 as ord,
					'uzp_dwh_company_holding_metric, ожидалась дата ' || v_end_dt::text as src_name,
					's_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_company_holding_metric' as tbl_name,
					'report_dt' as dt_col,
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_company_holding_metric hm
						where
							hm.report_dt = v_end_dt
							and hm.level_name = 'gosb'
							and hm.org_type = 'inn'
					) as is_exists
				union all
				select
					2,
					'mis_data_payroll_fl_navigator_planning_base, ожидалась дата и версия ' || v_end_dt::text,
					's_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_fl_navigator_planning_base',
					'report_dt',
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_fl_navigator_planning_base nav
						where
							nav.report_dt = v_end_dt
							and nav.version_plan_dt = v_end_dt
					)
				union all
				select
					3,
					'vsp_data_salary_detail_report, ожидался месяц ' || v_end_dt::text,
					's_grnplm_ld_salesntwrk_pcap_sn_uzp.vsp_data_salary_detail_report',
					'report_m_dt',
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.vsp_data_salary_detail_report vsp
						where
							vsp.report_m_dt = v_end_dt
					)
				union all
				select
					4,
					'uzp_dwh_fact_outflow, ожидалась дата ' || v_end_dt::text,
					's_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_fact_outflow',
					'report_dt',
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_fact_outflow fo
						where
							fo.report_dt = v_end_dt
					)
				union all
				select
					5,
					'mis_data_payroll_d, ожидались зачисления с ' || v_start_dt::text,
					's_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_d',
					'report_dt',
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_d pd
						where
							pd.report_dt >= v_start_dt
					)
				union all
				select
					6,
					'uzp_dwh_sale_funnel_task пуста',
					null,
					null,
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_sale_funnel_task tsk
					)
				union all
				select
					7,
					'uzp_data_epk_consolidation пуста',
					null,
					null,
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_epk_consolidation epk
					)
				union all
				select
					8,
					'jupiter_data_agrmnt_num пуста',
					null,
					null,
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.jupiter_data_agrmnt_num agr
					)
				union all
				select
					9,
					'uzp_dim_gosb пуст',
					null,
					null,
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_gosb gosb
					)
				union all
				select
					10,
					'uzp_dim_enrollment_type пуст',
					null,
					null,
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_enrollment_type et
					)
			) chk
		where
			not chk.is_exists
		order by
			chk.ord
		limit 1;

		if v_missing_src is not null then
			-- Фактическая последняя дата — только для источников с датой.
			if v_missing_col is not null then
				execute format('select max(%s)::text from %s', v_missing_col, v_missing_tbl)
				into v_fact_dt;
			end if;

			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
				p_etl_load_id := v_etl_load_id,
				p_function_name := v_function_name,
				p_log_message := '98 - Нет данных источника: ' || v_missing_src
					|| coalesce('; последняя дата в источнике: ' || v_fact_dt, '')
			);
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
				p_etl_load_id := v_etl_load_id,
				p_state_id := 98::int2
			);
			return;
		end if;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '02 - Проверка ККД: данные всех источников есть'
		);
	exception
		when others then
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
				p_etl_load_id := v_etl_load_id,
				p_function_name := v_function_name,
				p_log_message := '02 - Ошибка проверки данных источников: ' || sqlerrm,
				p_error_code := sqlstate
			);
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
				p_etl_load_id := v_etl_load_id,
				p_state_id := -1::int2
			);
			return;
	end;

	begin
		-- 10.01. Пары holding_metric за T-23…T. month_ago — сколько месяцев от T
		-- (0 — месяц T): дальше триггеры сравнивают с ним, а не с датами.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.01 - Заполнение tmp_hm'
		);

		drop table if exists tmp_hm;
		create temp table tmp_hm (
			gosb_id int4,
			inn int8,
			month_ago int4,
			current_fot_amt numeric,
			current_fl_qty int8,
			np_cnt int8,
			new_fl_cnt int8,
			emp_potential_qty numeric,
			total_emp_qty numeric,
			zp_fl_perc numeric
		)
		with (
			appendonly=true,
			compresstype=none
		)
		distributed by (inn);

		insert into tmp_hm (
			gosb_id,
			inn,
			month_ago,
			current_fot_amt,
			current_fl_qty,
			np_cnt,
			new_fl_cnt,
			emp_potential_qty,
			total_emp_qty,
			zp_fl_perc
		)
		select
			hm.level_id,
			hm.org_id,
			((date_part('year', v_end_dt) - date_part('year', hm.report_dt)) * 12
				+ date_part('month', v_end_dt) - date_part('month', hm.report_dt))::int4,
			hm.current_fot_amt,
			hm.current_fl_qty,
			hm.np_cnt,
			hm.new_fl_cnt,
			hm.emp_potential_qty,
			hm.total_emp_qty,
			hm.zp_fl_perc
		from
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_company_holding_metric hm
		where
			hm.level_name = 'gosb'
			and hm.org_type = 'inn'
			and hm.report_dt between v_hm_from_dt and v_end_dt;

		get diagnostics v_row_count = row_count;
		analyze tmp_hm;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.01 - Вставлено строк в tmp_hm: ' || v_row_count::text
		);

		-- 10.02. Отрасль ИНН: одна ЕПК на ИНН — активная, с заполненной отраслью,
		-- головная, самая новая. coalesce и nulls last: null при desc идёт первым.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.02 - Заполнение tmp_industry'
		);

		drop table if exists tmp_industry;
		create temp table tmp_industry (
			inn int8,
			industry_name varchar
		)
		with (
			appendonly=true,
			compresstype=none
		)
		distributed by (inn);

		insert into tmp_industry (
			inn,
			industry_name
		)
		select
			ind.inn,
			ind.industry_name
		from (
				select
					epk.inn,
					epk.industry_name,
					row_number() over(
						partition by
							epk.inn
						order by
							coalesce(epk.status_name = 'Активна', false) desc,
							(epk.industry_name is not null) desc,
							coalesce(epk.is_parent, false) desc,
							epk.epk_create_dttm desc nulls last,
							epk.epk_id desc
					) as rn
				from
					s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_epk_consolidation epk
				where
					epk.inn is not null
			) ind
		where
			ind.rn = 1
			and ind.industry_name is not null;

		get diagnostics v_row_count = row_count;
		analyze tmp_industry;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.02 - Вставлено строк в tmp_industry: ' || v_row_count::text
		);

		-- 10.03. ИНН с действующим договором и датой начала самого раннего из них.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.03 - Заполнение tmp_agrmnt'
		);

		drop table if exists tmp_agrmnt;
		create temp table tmp_agrmnt (
			inn int8,
			first_start_dt date
		)
		with (
			appendonly=true,
			compresstype=none
		)
		distributed by (inn);

		insert into tmp_agrmnt (
			inn,
			first_start_dt
		)
		select
			agr.inn,
			min(agr.start_dt)
		from
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.jupiter_data_agrmnt_num agr
		where
			agr.inn is not null
			and (
				agr.end_dt is null
				or agr.end_dt >= current_date
			)
		group by
			agr.inn;

		get diagnostics v_row_count = row_count;
		analyze tmp_agrmnt;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.03 - Вставлено строк в tmp_agrmnt: ' || v_row_count::text
		);

		-- 10.04. Заявления ВСП / МП СБОЛ за T-2…T. ГОСБ — по ТБ и номеру ГОСБ из urf_code:
		-- по номеру без ТБ справочник неоднозначен. Заявление без ГОСБ не берётся.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.04 - Заполнение tmp_app'
		);

		drop table if exists tmp_app;
		create temp table tmp_app (
			gosb_id int4,
			inn int8,
			epk_id int8,
			app_dt date,
			month_ago int4
		)
		with (
			appendonly=true,
			compresstype=none
		)
		distributed by (epk_id);

		insert into tmp_app (
			gosb_id,
			inn,
			epk_id,
			app_dt,
			month_ago
		)
		select
			gosb.new_gosb_id,
			coalesce(vsp.fact_inn, vsp.gu_inn),
			vsp.epk_id,
			vsp.report_dt,
			((date_part('year', v_end_dt) - date_part('year', vsp.report_m_dt)) * 12
				+ date_part('month', v_end_dt) - date_part('month', vsp.report_m_dt))::int4
		from
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.vsp_data_salary_detail_report vsp
		inner join
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_gosb gosb
				on gosb.tb_id = s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.try_cast(
					split_part(vsp.urf_code, '_', 1), null::int4)
				and gosb.old_gosb_id = s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.try_cast(
					split_part(vsp.urf_code, '_', 2), null::int4)
		where
			vsp.report_m_dt between v_t2_dt and v_end_dt
			and vsp.is_asm = 1
			and coalesce(vsp.fact_inn, vsp.gu_inn) is not null
			and gosb.new_gosb_id is not null;

		get diagnostics v_row_count = row_count;
		analyze tmp_app;

		select min(app.app_dt) into v_min_app_dt from tmp_app app;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.04 - Вставлено строк в tmp_app: ' || v_row_count::text
				|| ', первая дата заявления: ' || coalesce(v_min_app_dt::text, 'нет')
		);

		-- 10.05. Последнее ЗП-зачисление человека с первой даты заявления до даты расчета.
		-- Один проход по payroll_d: заявление с зачислением, если last_enrl_dt >= даты заявления.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.05 - Заполнение tmp_enrl'
		);

		drop table if exists tmp_enrl;
		create temp table tmp_enrl (
			epk_id int8,
			last_enrl_dt date
		)
		with (
			appendonly=true,
			compresstype=none
		)
		distributed by (epk_id);

		insert into tmp_enrl (
			epk_id,
			last_enrl_dt
		)
		select
			pd.epk_id,
			max(pd.report_dt)
		from
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_d pd
		inner join
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_enrollment_type et
				on et.enrollment_type_id = pd.enrollment_type
		where
			pd.report_dt between v_min_app_dt and current_date
			and et.is_fot_enrollment
			-- Только люди с заявлениями: остальные зачисления не нужны.
			and pd.epk_id in (
				select
					app.epk_id
				from
					tmp_app app
			)
		group by
			pd.epk_id;

		get diagnostics v_row_count = row_count;
		analyze tmp_enrl;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.05 - Вставлено строк в tmp_enrl: ' || v_row_count::text
		);

		-- 10.06. Заявления по паре: за месяц T и за T-2…T, всего и с зачислением.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.06 - Заполнение tmp_app_pair'
		);

		drop table if exists tmp_app_pair;
		create temp table tmp_app_pair (
			gosb_id int4,
			inn int8,
			app_cnt_1m int8,
			first_app_dt_1m date,
			app_with_enrl_cnt_1m int8,
			app_cnt_3m int8,
			first_app_dt_3m date,
			app_with_enrl_cnt_3m int8
		)
		with (
			appendonly=true,
			compresstype=none
		)
		distributed by (inn);

		insert into tmp_app_pair (
			gosb_id,
			inn,
			app_cnt_1m,
			first_app_dt_1m,
			app_with_enrl_cnt_1m,
			app_cnt_3m,
			first_app_dt_3m,
			app_with_enrl_cnt_3m
		)
		select
			app.gosb_id,
			app.inn,
			sum(case when app.month_ago = 0 then 1 else 0 end),
			min(case when app.month_ago = 0 then app.app_dt end),
			sum(
				case
					when app.month_ago = 0
						and enrl.last_enrl_dt >= app.app_dt
						then 1
					else 0
				end
			),
			count(*),
			min(app.app_dt),
			sum(case when enrl.last_enrl_dt >= app.app_dt then 1 else 0 end)
		from
			tmp_app app
		-- Левое соединение: заявление без зачисления тоже считается.
		left join
			tmp_enrl enrl
				on enrl.epk_id = app.epk_id
		group by
			app.gosb_id,
			app.inn;

		get diagnostics v_row_count = row_count;
		analyze tmp_app_pair;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.06 - Вставлено строк в tmp_app_pair: ' || v_row_count::text
		);
	exception
		when others then
			get stacked diagnostics
				v_error_state = returned_sqlstate,
				v_error_msg = message_text,
				v_error_detail = pg_exception_detail,
				v_error_hint = pg_exception_hint,
				v_error_context = pg_exception_context,
				v_error_column = column_name,
				v_error_constraint = constraint_name,
				v_error_datatype = pg_datatype_name,
				v_error_table = table_name,
				v_error_schema = schema_name;

			select
				array_to_string(array[
					case when v_error_state <> '' then 'sqlstate:' || v_error_state end,
					case when v_error_msg <> '' then 'message: ' || v_error_msg end,
					case when v_error_detail <> '' then 'detail: ' || v_error_detail end,
					case when v_error_hint <> '' then 'hint: ' || v_error_hint end,
					case when v_error_context <> '' then 'context: ' || v_error_context end,
					case when v_error_column <> '' then 'column: ' || v_error_column end,
					case when v_error_constraint <> '' then 'constraint: ' || v_error_constraint end,
					case when v_error_datatype <> '' then 'datatype: ' || v_error_datatype end,
					case when v_error_table <> '' then 'table: ' || v_error_table end,
					case when v_error_schema <> '' then 'schema: ' || v_error_schema end
				], chr(10))
			into
				v_error_message;

			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
				p_etl_load_id := v_etl_load_id,
				p_function_name := v_function_name,
				p_log_message := v_error_message,
				p_error_code := sqlstate
			);
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
				p_etl_load_id := v_etl_load_id,
				p_state_id := -1::int2
			);
			return;
	end;

	begin
		-- 20. Стейджинг: по строке на сработавший триггер пары. Период и время расчета
		-- одинаковы у всех строк и добавляются при вставке в витрину.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20 - Заполнение tmp_stg_uzp_dwh_task_trigger'
		);

		drop table if exists tmp_stg_uzp_dwh_task_trigger;
		create temp table tmp_stg_uzp_dwh_task_trigger (
			gosb_id int4,
			inn int8,
			trigger_name varchar(255),
			trigger_type varchar(255),
			attributes_json json
		)
		with (
			appendonly=true,
			compresstype=none
		)
		distributed by (inn);

		-- 20.01. ЕПП флаг: за T у пары только выплата ЕПП.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			epp.gosb_id,
			epp.inn,
			'ЕПП флаг',
			'Вспомогательный',
			json_build_object(
				'month', v_month_txt,
				'epp_cnt', epp.epp_cnt
			)
		from (
				select
					nav.gosb_id,
					s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.try_cast(nav.inn, null::int8) as inn,
					sum(nav.fl_count) as epp_cnt
				from
					s_grnplm_ld_salesntwrk_pcap_sn_uzp.mis_data_payroll_fl_navigator_planning_base nav
				where
					nav.report_dt = v_end_dt
					and nav.version_plan_dt = v_end_dt
					and nav.gosb_id is not null
				group by
					nav.gosb_id,
					s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.try_cast(nav.inn, null::int8)
				-- Выплата другого типа у пары снимает триггер.
				having
					bool_and(coalesce(nav.sales_flag = 'Единичное платежное поручение', false))
			) epp
		where
			epp.inn is not null;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.01 - ЕПП флаг: ' || v_row_count::text
		);

		-- 20.02. Ср чек ниже среднего: чек клиента и средний чек ГОСБ + отрасли —
		-- сумма ФОТ / сумма получателей за T-5…T, месяцы без получателей не берутся.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			chk.gosb_id,
			chk.inn,
			'ср чек ниже среднего',
			'Вспомогательный',
			json_build_object(
				'month', v_month_txt,
				'avg_salary', round(chk.avg_salary, 2),
				'client_salary', round(chk.client_salary, 2)
			)
		from (
				select
					cl.gosb_id,
					cl.inn,
					cl.fot_amt / cl.fl_qty as client_salary,
					sum(cl.fot_amt) over(partition by cl.gosb_id, cl.industry_name)
						/ sum(cl.fl_qty) over(partition by cl.gosb_id, cl.industry_name) as avg_salary
				from (
						select
							hm.gosb_id,
							hm.inn,
							ind.industry_name,
							sum(hm.current_fot_amt) as fot_amt,
							sum(hm.current_fl_qty) as fl_qty
						from
							tmp_hm hm
						-- Пары без отрасли не проверяются.
						inner join
							tmp_industry ind
								on ind.inn = hm.inn
						where
							hm.month_ago between 0 and 5
							and hm.current_fl_qty <> 0
						group by
							hm.gosb_id,
							hm.inn,
							ind.industry_name
					) cl
			) chk
		where
			chk.client_salary <= 0.8 * chk.avg_salary;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.02 - ср чек ниже среднего: ' || v_row_count::text
		);

		-- 20.03. Отрицательный потенциал: НП за T больше потенциала.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			hm.gosb_id,
			hm.inn,
			'отрицательный потенциал',
			'Основной',
			json_build_object(
				'month', v_month_txt,
				'np_cnt', hm.np_cnt
			)
		from
			tmp_hm hm
		where
			hm.month_ago = 0
			and hm.np_cnt > hm.emp_potential_qty;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.03 - отрицательный потенциал: ' || v_row_count::text
		);

		-- 20.04. Кп первое зачисление: ФОТ за T > 0, в каждом из 12 месяцев T-12…T-1
		-- строка с ФОТ = 0.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			kp.gosb_id,
			kp.inn,
			'кп первое зачисление',
			'Основной',
			json_build_object(
				'month', v_month_txt,
				'fl_cnt', kp.fl_cnt
			)
		from (
				select
					hm.gosb_id,
					hm.inn,
					max(case when hm.month_ago = 0 and hm.current_fot_amt > 0 then 1 else 0 end)
						as is_cur_fot,
					max(case when hm.month_ago = 0 then hm.current_fl_qty end)
						as fl_cnt,
					sum(
						case
							when hm.month_ago between 1 and 12
								and hm.current_fot_amt = 0
								then 1
							else 0
						end
					) as zero_month_qty
				from
					tmp_hm hm
				where
					hm.month_ago between 0 and 12
				group by
					hm.gosb_id,
					hm.inn
			) kp
		where
			kp.is_cur_fot = 1
			and kp.zero_month_qty = 12;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.04 - кп первое зачисление: ' || v_row_count::text
		);

		-- 20.05. Сезонный тип: численность выросла на 10%+ из T+1 в T+2 год назад
		-- (11 → 10 месяцев назад) и два года назад (23 → 22). База 0 — роста нет.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			ss.gosb_id,
			ss.inn,
			'сезонный тип деятельности',
			'Основной',
			json_build_object(
				'month', v_month_txt,
				'fl_uplift', round((
					(ss.emp_y1_qty - ss.emp_y1_base_qty)
					+ (ss.emp_y2_qty - ss.emp_y2_base_qty)
				) / 2, 2)
			)
		from (
				select
					hm.gosb_id,
					hm.inn,
					max(case when hm.month_ago = 11 then hm.total_emp_qty end) as emp_y1_base_qty,
					max(case when hm.month_ago = 10 then hm.total_emp_qty end) as emp_y1_qty,
					max(case when hm.month_ago = 23 then hm.total_emp_qty end) as emp_y2_base_qty,
					max(case when hm.month_ago = 22 then hm.total_emp_qty end) as emp_y2_qty
				from
					tmp_hm hm
				where
					hm.month_ago in (10, 11, 22, 23)
				group by
					hm.gosb_id,
					hm.inn
			) ss
		where
			ss.emp_y1_base_qty > 0
			and ss.emp_y2_base_qty > 0
			and ss.emp_y1_qty / ss.emp_y1_base_qty - 1 >= 0.1
			and ss.emp_y2_qty / ss.emp_y2_base_qty - 1 >= 0.1;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.05 - сезонный тип деятельности: ' || v_row_count::text
		);

		-- 20.06. Высокая текучка: НФЛ есть, НП меньше 20% от НФЛ, численность снизилась к T-1.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			cur.gosb_id,
			cur.inn,
			'высокая текучка',
			'Основной',
			json_build_object(
				'month', v_month_txt,
				'nfl_cnt', cur.new_fl_cnt,
				'np_cnt', cur.np_cnt,
				'portf_last_m', round(prv.total_emp_qty, 2),
				'portf_cur_m', round(cur.total_emp_qty, 2)
			)
		from
			tmp_hm cur
		inner join
			tmp_hm prv
				on prv.inn = cur.inn
				and prv.gosb_id = cur.gosb_id
				and prv.month_ago = 1
		where
			cur.month_ago = 0
			and cur.new_fl_cnt > 0
			and cur.np_cnt < 0.2 * cur.new_fl_cnt
			and cur.total_emp_qty < prv.total_emp_qty;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.06 - высокая текучка: ' || v_row_count::text
		);

		-- 20.07. Есть договор нет зачисления: договор старше 6 месяцев, ФОТ = 0
		-- в каждом месяце T-5…T.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			nz.gosb_id,
			nz.inn,
			'есть договор нет зачисления',
			'Вспомогательный',
			json_build_object(
				'month', v_month_txt
			)
		from (
				select
					hm.gosb_id,
					hm.inn,
					max(case when hm.month_ago = 0 then 1 else 0 end) as is_cur_month,
					sum(case when hm.current_fot_amt = 0 then 1 else 0 end) as zero_month_qty
				from
					tmp_hm hm
				where
					hm.month_ago between 0 and 5
				group by
					hm.gosb_id,
					hm.inn
			) nz
		inner join
			tmp_agrmnt agr
				on agr.inn = nz.inn
		where
			nz.is_cur_month = 1
			and nz.zero_month_qty = 6
			and agr.first_start_dt <= current_date - interval '6 month';

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.07 - есть договор нет зачисления: ' || v_row_count::text
		);

		-- 20.08. Контактная политика: последняя закрытая задача МЗП раньше 1-го числа T-3
		-- или её нет. Верхней границы нет: задача, закрытая после T, тоже снимает триггер.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			hm.gosb_id,
			hm.inn,
			'контактная политика 4 месяца',
			'Основной',
			json_build_object(
				'task_close_date', to_char(tsk.last_close_dttm, 'YYYY-MM-DD')
			)
		from
			tmp_hm hm
		-- Левое соединение: пара без закрытых задач тоже получает триггер.
		left join (
				select
					tsk_src.inn,
					tsk_src.gosb_id,
					max(tsk_src.fact_close_task_dttm) as last_close_dttm
				from
					s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_sale_funnel_task tsk_src
				where
					tsk_src.is_task_closed
					and tsk_src.role_code = 'МЗП'
					and tsk_src.src_task_as_code not in ('ИСУ', 'ПРБР')
				group by
					tsk_src.inn,
					tsk_src.gosb_id
			) tsk
				on tsk.inn = hm.inn
				and tsk.gosb_id = hm.gosb_id
		where
			hm.month_ago = 0
			and (
				tsk.last_close_dttm is null
				or tsk.last_close_dttm < v_start_dt - interval '3 month'
			);

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.08 - контактная политика 4 месяца: ' || v_row_count::text
		);

		-- 20.09. Подано заявлений за прошлый месяц.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			ap.gosb_id,
			ap.inn,
			'Подано заявлений в ВСП или МП СБОЛ за прошлый месяц',
			'Вспомогательный',
			json_build_object(
				'month', v_month_txt,
				'app_cnt', ap.app_cnt_1m,
				'first_app_date', to_char(ap.first_app_dt_1m, 'YYYY-MM-DD')
			)
		from
			tmp_app_pair ap
		where
			ap.app_cnt_1m > 0;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.09 - подано заявлений за месяц: ' || v_row_count::text
		);

		-- 20.10. Заявлений с зачислениями за прошлый месяц: пишется и при 0.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			ap.gosb_id,
			ap.inn,
			'Заявлений с зачислениями из ВСП или МП СБОЛ за прошлый месяц',
			'Вспомогательный',
			json_build_object(
				'month', v_month_txt,
				'app_with_zachisl_cnt', ap.app_with_enrl_cnt_1m
			)
		from
			tmp_app_pair ap
		where
			ap.app_cnt_1m > 0;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.10 - заявлений с зачислениями за месяц: ' || v_row_count::text
		);

		-- 20.11. Подано заявлений за последние три месяца.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			ap.gosb_id,
			ap.inn,
			'Подано заявлений в ВСП или МП СБОЛ за последние три месяца',
			'Вспомогательный',
			json_build_object(
				'month', v_month_txt,
				'app_cnt_3m', ap.app_cnt_3m,
				'first_app_date', to_char(ap.first_app_dt_3m, 'YYYY-MM-DD')
			)
		from
			tmp_app_pair ap;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.11 - подано заявлений за три месяца: ' || v_row_count::text
		);

		-- 20.12. Заявлений с зачислениями за последние три месяца: пишется и при 0.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			ap.gosb_id,
			ap.inn,
			'Заявлений с зачислениями из ВСП или МП СБОЛ за последние три месяца',
			'Вспомогательный',
			json_build_object(
				'month', v_month_txt,
				'app_with_zachisl_cnt_3m', ap.app_with_enrl_cnt_3m
			)
		from
			tmp_app_pair ap;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.12 - заявлений с зачислениями за три месяца: ' || v_row_count::text
		);

		-- 20.13. Отток за месяц: отток 3+ и численность снизилась.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			fo.gosb_id,
			fo.inn,
			'По клиенту был отток в прошлом месяце, численность',
			'Основной',
			json_build_object(
				'month', v_month_txt,
				'portf_last_m', fo.prev_m_fl_val,
				'portf_cur_m', fo.fact_payee_qty,
				'churn_cnt', fo.prev_m_fl_val - fo.fact_payee_qty,
				'churn_pct', round((fo.prev_m_fl_val - fo.fact_payee_qty)::numeric / fo.prev_m_fl_val, 2)
			)
		from
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_fact_outflow fo
		where
			fo.report_dt = v_end_dt
			and fo.outflow_qty >= 3
			and fo.fact_payee_qty < fo.prev_m_fl_val;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.13 - отток за месяц: ' || v_row_count::text
		);

		-- 20.14. Отток за три месяца: условие оттока за месяц выполнено в T-2, T-1 и T.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			o3.gosb_id,
			o3.inn,
			'По клиенту был отток в последние три месяца',
			'Основной',
			json_build_object(
				'month', v_month_txt,
				'portf_3m_ago', o3.portf_3m_ago,
				'portf_cur_m', o3.portf_cur_m,
				'churn_pct_3m', round((o3.portf_3m_ago - o3.portf_cur_m)::numeric / o3.portf_3m_ago, 2)
			)
		from (
				select
					fo.gosb_id,
					fo.inn,
					count(*) as churn_month_qty,
					max(case when fo.report_dt = v_t2_dt then fo.prev_m_fl_val end) as portf_3m_ago,
					max(case when fo.report_dt = v_end_dt then fo.fact_payee_qty end) as portf_cur_m
				from
					s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_fact_outflow fo
				where
					fo.report_dt between v_t2_dt and v_end_dt
					and fo.outflow_qty >= 3
					and fo.fact_payee_qty < fo.prev_m_fl_val
				group by
					fo.gosb_id,
					fo.inn
			) o3
		where
			o3.churn_month_qty = 3;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.14 - отток за три месяца: ' || v_row_count::text
		);

		-- 20.15. Доля по клиенту больше 90%.
		insert into tmp_stg_uzp_dwh_task_trigger (
			gosb_id,
			inn,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			hm.gosb_id,
			hm.inn,
			'Доля по клиенту больше 90%',
			'Вспомогательный',
			json_build_object(
				'month', v_month_txt,
				'fl_cnt', hm.current_fl_qty,
				'fl_dolay', round(hm.zp_fl_perc, 2)
			)
		from
			tmp_hm hm
		where
			hm.month_ago = 0
			and hm.zp_fl_perc > 0.9;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20.15 - доля по клиенту больше 90%: ' || v_row_count::text
		);

		analyze tmp_stg_uzp_dwh_task_trigger;

		-- 21. Проверка ККД: ключ витрины в стейджинге уникален.
		select
			count(*)
		into
			v_dup_qty
		from (
				select
					stg.inn
				from
					tmp_stg_uzp_dwh_task_trigger stg
				group by
					stg.inn,
					stg.gosb_id,
					stg.trigger_name
				having
					count(*) > 1
			) dup;

		if v_dup_qty > 0 then
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
				p_etl_load_id := v_etl_load_id,
				p_function_name := v_function_name,
				p_log_message := '97 - Проверка ККД не пройдена: дублей ключа в стейджинге '
					|| v_dup_qty::text
			);
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
				p_etl_load_id := v_etl_load_id,
				p_state_id := 97::int2
			);
			return;
		end if;
	exception
		when others then
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
				p_etl_load_id := v_etl_load_id,
				p_function_name := v_function_name,
				p_log_message := '20 - Ошибка заполнения стейджинга: ' || sqlerrm,
				p_error_code := sqlstate
			);
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
				p_etl_load_id := v_etl_load_id,
				p_state_id := -1::int2
			);
			return;
	end;

	begin
		-- 30. Замена строк своих триггеров за месяц: delete, а не truncate — меняется
		-- один период из многих (меньше 20% таблицы).
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '30 - Обновление uzp_dwh_task_trigger за ' || v_start_dt::text
				|| ' - ' || v_end_dt::text
		);

		delete from s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger tt
		where
			tt.start_dt = v_start_dt
			and tt.end_dt = v_end_dt
			and tt.trigger_name = any(v_trigger_names);

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '30 - Удалено строк: ' || v_row_count::text
		);

		insert into s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger (
			gosb_id,
			inn,
			modified_dttm,
			start_dt,
			end_dt,
			trigger_name,
			trigger_type,
			attributes_json
		)
		select
			stg.gosb_id,
			stg.inn,
			now()::timestamp,
			v_start_dt,
			v_end_dt,
			stg.trigger_name,
			stg.trigger_type,
			stg.attributes_json
		from
			tmp_stg_uzp_dwh_task_trigger stg;

		get diagnostics v_row_count = row_count;
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '30 - Вставлено строк: ' || v_row_count::text
		);

		-- 40. Сбор статистики.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_table_analyze(
			tbl := 's_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger'
		);

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '99 - Расчет завершен'
		);
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
			p_etl_load_id := v_etl_load_id,
			p_state_id := 99::int2
		);
	exception
		when others then
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
				p_etl_load_id := v_etl_load_id,
				p_function_name := v_function_name,
				p_log_message := '30 - Ошибка обновления uzp_dwh_task_trigger: ' || sqlerrm,
				p_error_code := sqlstate
			);
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
				p_etl_load_id := v_etl_load_id,
				p_state_id := -1::int2
			);
			return;
	end;
end;
$$;

comment on function s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger_month_calc(int8, timestamp, timestamp)
	is 'Расчетные данные ежемесячных триггеров для задач по ИНН и ГОСБ (витрина uzp_dwh_task_trigger)';
