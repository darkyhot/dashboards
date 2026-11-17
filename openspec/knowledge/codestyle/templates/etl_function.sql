-- Шаблон управляющей ETL-функции расчётной витрины УЗП.
-- Скопируй, подставь имя витрины и шаги расчёта.
-- Имена объектов и полей — knowledge/codestyle/naming.md, оформление SQL — knowledge/codestyle/sql-style.md,
-- требования к целевой и временным таблицам — knowledge/codestyle/target-tables.md.
-- Формулы метрик бери из спеки витрины marts/<витрина>/openspec/specs/, не придумывай их здесь.

create or replace function s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.<витрина>_<глагол>(
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
 *      <что считает функция и для какой витрины>
 *
 * Параметры:
 *      p_etl_load_id - уникальный идентификатор загрузки
 *      p_start_dttm - дата начала периода (<не используется, если не нужен>)
 *      p_end_dttm - дата окончания периода (<не используется, если не нужен>)
 *
 * Изменения:
 *      <ГГГГ-ММ-ДД> <ФИО>. Функция создана. (<SLSNTWRKDE-****, SNDLAKE-****>)
 */

declare
	v_function_name text := '<витрина>_<глагол>';
	v_etl_load_id int8 := p_etl_load_id;
	v_row_count int8 := 0;

	v_report_dt date;

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
		-- 01. Отчётная дата расчёта.
		select
			s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.last_day(current_date)::date
		into
			v_report_dt;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '01 - Отчетная дата: v_report_dt = ' || coalesce(v_report_dt::text, 'NULL')
		);

		-- 02. Проверка ККД: в источнике есть данные за отчётную дату.
		select
			count(*)
		into
			v_row_count
		from
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.<витрина источник> src
		where
			src.report_dt = v_report_dt;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '02 - Проверка ККД: строк источника за ' || v_report_dt::text
				|| ' = ' || v_row_count::text
		);

		if v_row_count = 0 then
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
				p_etl_load_id := v_etl_load_id,
				p_function_name := v_function_name,
				p_log_message := '98 - Проверка ККД не пройдена: в витрине <источник> нет данных за '
					|| v_report_dt::text
			);
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
				p_etl_load_id := v_etl_load_id,
				p_state_id := 98::int2
			);
			return;
		end if;
	exception
		when others then
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
				p_etl_load_id := v_etl_load_id,
				p_function_name := v_function_name,
				p_log_message := '02 - Ошибка проверки данных источника',
				p_error_code := sqlstate
			);
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
				p_etl_load_id := v_etl_load_id,
				p_state_id := -1::int2
			);
			return;
	end;

	begin
		-- 10. Промежуточная таблица расчёта.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '10 - Заполнение временной таблицы tmp_calc'
		);

		drop table if exists tmp_calc;
		create temp table tmp_calc (
			<поле_1> <тип>,
			<поле_2> <тип>
		)
		with (
			appendonly=true,
			compresstype=none
		)
		distributed by (<ключ распределения>);

		insert into tmp_calc (
			<поле_1>,
			<поле_2>
		)
		select
			<поле_1>,
			<поле_2>
		from
			s_grnplm_ld_salesntwrk_pcap_sn_uzp.<витрина источник> src
		where
			src.report_dt = v_report_dt;

		get diagnostics v_row_count = row_count;
		analyze tmp_calc;

		if v_row_count = 0 then
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
				p_etl_load_id := v_etl_load_id,
				p_function_name := v_function_name,
				p_log_message := '98 - Нет данных для загрузки'
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
			p_log_message := '10 - Вставлено строк в tmp_calc: ' || v_row_count::text
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
		-- 20. Стейджинг целевой витрины.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20 - Заполнение стейджинга tmp_stg_<витрина>'
		);

		drop table if exists tmp_stg_<витрина>;
		create temp table tmp_stg_<витрина> (
			<поле целевой витрины> <тип>
		)
		with (
			appendonly=true,
			compresstype=none
		)
		distributed randomly;

		insert into tmp_stg_<витрина> (
			<поле целевой витрины>
		)
		-- Дедупликация по ключу витрины: повторный запуск не даёт дублей.
		select
			calc.<поле_1>
		from (
				select
					calc_src.<поле_1>,
					row_number() over(
						partition by
							calc_src.<ключ>
						order by
							calc_src.<версия> desc
					) as rn
				from
					tmp_calc calc_src
			) calc
		where
			calc.rn = 1;

		get diagnostics v_row_count = row_count;
		analyze tmp_stg_<витрина>;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '20 - Вставлено строк в tmp_stg_<витрина>: ' || v_row_count::text
		);
	exception
		when others then
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
				p_etl_load_id := v_etl_load_id,
				p_function_name := v_function_name,
				p_log_message := '20 - Ошибка заполнения стейджинга',
				p_error_code := sqlstate
			);
			perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_state_update(
				p_etl_load_id := v_etl_load_id,
				p_state_id := -1::int2
			);
			return;
	end;

	begin
		-- 30. Обновление целевой таблицы.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '30 - Обновление целевой таблицы <витрина>'
		);

		-- Замена 20% и более записей: стейджинг содержит таблицу целиком, целевую транкейтим.
		-- sys_table_*: имя без схемы, схему функция дописывает сама.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_table_truncate(
			tbl := '<витрина>'
		);

		-- Замена менее 20% записей: вместо транкеята удаляй только подлежащие замене строки:
		-- 	delete from s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.<витрина> target
		-- 	where
		-- 		target.report_dt = v_report_dt;

		insert into s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.<витрина> (
			<поле целевой витрины>
		)
		select
			stg.<поле целевой витрины>
		from
			tmp_stg_<витрина> stg;

		get diagnostics v_row_count = row_count;

		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_data_etl_log_write(
			p_etl_load_id := v_etl_load_id,
			p_function_name := v_function_name,
			p_log_message := '30 - Вставлено строк в <витрина>: ' || v_row_count::text
		);

		-- 40. Сбор статистики.
		perform s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.sys_table_analyze(
			tbl := '<витрина>'
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
				p_log_message := '30 - Ошибка обновления целевой таблицы',
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

comment on function s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.<витрина>_<глагол>(int8, timestamp, timestamp)
	is 'Расчетные данные <содержание витрины> — <процесс>';
