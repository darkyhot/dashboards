-- Витрина триггеров для задач УЗП.
-- Интент 001 (UZPPZPS1-422), спека — marts/uzp_dwh_task_trigger/openspec/specs/trigger-mart.
-- Одна строка — один сработавший триггер по паре ИНН + ГОСБ за период start_dt…end_dt.

create table s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger (
	gosb_id int4 not null,
	inn int8 not null,
	modified_dttm timestamp not null,
	start_dt date not null,
	end_dt date not null,
	trigger_name varchar(255) not null,
	trigger_type varchar(255) not null,
	attributes_json json
)
with (
	appendonly=true,
	compresstype=zstd,
	compresslevel=1
)
-- По ИНН идут соединения с источниками; строк меньше 10 млн, поэтому не randomly.
distributed by (inn)
-- Партиция по start_dt: неделя целиком попадает в партицию своего понедельника.
partition by range(start_dt) (
	start (date '2026-01-01')
	end (date '2031-01-01')
	every (interval '1 month')
);

comment on table s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger
	is 'Расчетные данные триггеров для задач по ИНН и ГОСБ';

comment on column s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger.gosb_id
	is 'Идентификатор ГОСБ';
comment on column s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger.inn
	is 'ИНН организации';
comment on column s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger.modified_dttm
	is 'Дата и время расчета';
comment on column s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger.start_dt
	is 'Дата начала периода (первый день месяца или понедельник)';
comment on column s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger.end_dt
	is 'Дата окончания периода (последний день месяца или воскресенье)';
comment on column s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger.trigger_name
	is 'Заголовок триггера';
comment on column s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger.trigger_type
	is 'Тип триггера (Основной / Вспомогательный)';
comment on column s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.uzp_dwh_task_trigger.attributes_json
	is 'Параметры триггера (JSON)';
