-- Еженедельные триггеры для задач УЗП по заявлениям ВСП / МП СБОЛ.
-- Интент 001 (UZPPZPS1-422), спеки — marts/uzp_dwh_task_trigger/openspec/specs/
-- (trigger-mart, weekly-triggers). Формулы триггеров — только оттуда.

create or replace function s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger_week_calc(
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
 *      Расчет еженедельных триггеров витрины uzp_dwh_task_trigger за неделю Пн–Вс,
 *      в которую попадает p_end_dttm. Пересчитывает только свои 2 триггера за эту неделю.
 *
 * Параметры:
 *      p_etl_load_id - уникальный идентификатор загрузки
 *      p_start_dttm - дата начала периода (не используется)
 *      p_end_dttm - любая дата расчетной недели
 *
 * Изменения:
 *      2026-11-16 <ФИО>. Функция создана. (<номер задачи>; интент 001)
 */

declare
	v_function_name text := 'uzp_dwh_task_trigger_week_calc';
	v_etl_load_id int8 := p_etl_load_id;
	v_row_count int8 := 0;

	v_start_dt date;
	v_end_dt date;
	v_min_app_dt date;

	v_missing_src text;
	v_missing_tbl text;
	v_missing_col text;
	v_fact_dt text;
	v_dup_qty int8;

	-- Триггеры этой функции: только их строки заменяются при перерасчёте.
	v_trigger_names text[] := array[
		'Заявление из ВСП без договора и зачислений',
		'Заявление из ВСП с договором,(но по договору в сумме нет зачислений. По договору смотреть за последний полный месяц + до сегодняшней даты)'
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

		-- 01. Период: неделя Пн–Вс, в которую попадает p_end_dttm.
		v_start_dt := date_trunc('week', p_end_dttm)::date;
		v_end_dt := v_start_dt + 6;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '01 - Период: ' || v_start_dt::text || ' - ' || v_end_dt::text
				|| ', дата расчета: ' || current_date::text
		);

		-- 02. Проверка ККД: у каждого источника есть данные за неделю.
		-- tbl_name и dt_col нужны, чтобы показать в логе последнюю дату источника.
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
					'vsp_data_salary_detail_report, ожидались заявления за ' || v_start_dt::text
						|| ' - ' || v_end_dt::text as src_name,
					's_grnplm_ld_salesntwrk_pcap_sn_uzp.vsp_data_salary_detail_report' as tbl_name,
					'report_dt' as dt_col,
					exists (
						select
							1
						from
							s_grnplm_ld_salesntwrk_pcap_sn_uzp.vsp_data_salary_detail_report vsp
						where
							vsp.report_dt between v_start_dt and v_end_dt
					) as is_exists
				union all
				select
					2,
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
					3,
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
					4,
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
					5,
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
		-- 10.01. Заявления ВСП / МП СБОЛ за неделю. ГОСБ — по ТБ и номеру ГОСБ из urf_code:
		-- по номеру без ТБ справочник неоднозначен. Заявление без ГОСБ не берётся.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.01 - Заполнение tmp_app'
		);

		drop table if exists tmp_app;
		create temp table tmp_app (
			gosb_id int4,
			inn int8,
			epk_id int8,
			app_dt date
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
			app_dt
		)
		select
			gosb.new_gosb_id,
			coalesce(vsp.fact_inn, vsp.gu_inn),
			vsp.epk_id,
			vsp.report_dt
		from
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.vsp_data_salary_detail_report vsp
		inner join
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_gosb gosb
				on gosb.tb_id = s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.try_cast(
					split_part(vsp.urf_code, '_', 1), null::int4)
				and gosb.old_gosb_id = s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.try_cast(
					split_part(vsp.urf_code, '_', 2), null::int4)
		where
			vsp.report_dt between v_start_dt and v_end_dt
			and vsp.is_asm = 1
			and coalesce(vsp.fact_inn, vsp.gu_inn) is not null
			and gosb.new_gosb_id is not null;

		get diagnostics v_row_count = row_count;
		analyze tmp_app;

		select min(app.app_dt) into v_min_app_dt from tmp_app app;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.01 - Вставлено строк в tmp_app: ' || v_row_count::text
		);

		-- 10.02. ИНН с действующим договором.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.02 - Заполнение tmp_agrmnt'
		);

		drop table if exists tmp_agrmnt;
		create temp table tmp_agrmnt (
			inn int8
		)
		with (
			appendonly=true,
			compresstype=none
		)
		distributed by (inn);

		insert into tmp_agrmnt (
			inn
		)
		select distinct
			agr.inn
		from
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.jupiter_data_agrmnt_num agr
		where
			agr.inn is not null
			and (
				agr.end_dt is null
				or agr.end_dt >= current_date
			);

		get diagnostics v_row_count = row_count;
		analyze tmp_agrmnt;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.02 - Вставлено строк в tmp_agrmnt: ' || v_row_count::text
		);

		-- 10.03. Последнее ЗП-зачисление человека с первой даты заявления до даты расчета.
		-- Заявление с зачислением, если last_enrl_dt >= даты заявления.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10.03 - Заполнение tmp_enrl'
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
			p_log_message := '10.03 - Вставлено строк в tmp_enrl: ' || v_row_count::text
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
		-- 20. Стейджинг. Признак договора — по ИНН, признак зачисления — по заявлению.
		-- Триггер — если есть заявления без зачисления; тип триггера задаёт договор.
		-- Договор есть и все заявления с зачислением — не отклонение, строки нет.
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
			case
				when ap.is_agrmnt = 1
					then 'Заявление из ВСП с договором,(но по договору в сумме нет зачислений. По договору смотреть за последний полный месяц + до сегодняшней даты)'
				else 'Заявление из ВСП без договора и зачислений'
			end,
			'Основной',
			json_build_object(
				'first_app_date', to_char(ap.first_app_dt, 'YYYY-MM-DD'),
				'app_cnt', ap.app_cnt,
				'app_with_zachisl_cnt', ap.app_with_enrl_cnt,
				'app_no_zachisl_cnt', ap.app_cnt - ap.app_with_enrl_cnt
			)
		from (
				select
					app.gosb_id,
					app.inn,
					max(case when agr.inn is not null then 1 else 0 end)
						as is_agrmnt,
					min(app.app_dt) as first_app_dt,
					count(*) as app_cnt,
					sum(
						case
							when enrl.last_enrl_dt >= app.app_dt then 1
							else 0
						end
					) as app_with_enrl_cnt
				from
					tmp_app app
				-- Левые соединения: нет договора и нет зачисления — это признак 0.
				left join
					tmp_agrmnt agr
						on agr.inn = app.inn
				left join
					tmp_enrl enrl
						on enrl.epk_id = app.epk_id
				group by
					app.gosb_id,
					app.inn
			) ap
		where
			ap.app_cnt > ap.app_with_enrl_cnt;

		get diagnostics v_row_count = row_count;
		analyze tmp_stg_uzp_dwh_task_trigger;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20 - Вставлено строк в tmp_stg_uzp_dwh_task_trigger: ' || v_row_count::text
		);

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
		-- 30. Замена строк своих триггеров за неделю: delete, а не truncate — меняется
		-- один период из многих (меньше 20% таблицы). Фильтр по trigger_name обязателен:
		-- месяц Пн–Вс совпадает с неделей по start_dt и end_dt.
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
			tbl := 'uzp_dwh_task_trigger'
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

comment on function s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger_week_calc(int8, timestamp, timestamp)
	is 'Расчетные данные еженедельных триггеров для задач по заявлениям ВСП по ИНН и ГОСБ (витрина uzp_dwh_task_trigger)';
