-- Шаблон DDL целевой витрины УЗП.
-- Структура повторяет спеку витрины: marts/<витрина>/openspec/specs/.
-- Имя объекта и полей — knowledge/codestyle/naming.md, требования к целевым таблицам — knowledge/codestyle/target-tables.md.

create table s_grnplm_ld_salesntwrk_pcap_sn_uzp.<витрина> (
	report_dt date not null,
	<поле> <тип> not null,
	<поле> <тип>,
	<поле метрики> numeric(38, 6),
	<признак> bool,
	inserted_dttm timestamp(0) not null default now(),
	author_login text
)
with (
	appendonly=true,
	compresstype=zstd,
	compresslevel=1
)
-- Месячные витрины партиционят по отчётной дате. Небольшой справочник можно оставить
-- без партиционирования, тогда секцию partition by не пишут. Default-партиции избегают:
-- границы партиций задают явно с запасом вперёд.
partition by range(report_dt) (
	start (date '<первая отчётная дата>')
	end (date '<последняя отчётная дата>')
	every (interval '1 month')
)
-- Ключ дистрибуции не может быть типа date или timestamp.
-- distributed randomly допустим только при объёме от 10 млн строк.
distributed by (<ключ распределения>);

comment on table s_grnplm_ld_salesntwrk_pcap_sn_uzp.<витрина>
	is 'Расчетные данные <содержание витрины> — <бизнес-процесс>';

comment on column s_grnplm_ld_salesntwrk_pcap_sn_uzp.<витрина>.report_dt
	is 'Отчётная дата (последнее число месяца)';
comment on column s_grnplm_ld_salesntwrk_pcap_sn_uzp.<витрина>.<поле>
	is '<Описание поля: с заглавной буквы, не короче 5 символов, пояснения в скобках>';
comment on column s_grnplm_ld_salesntwrk_pcap_sn_uzp.<витрина>.inserted_dttm
	is 'Дата и время добавления записи';
comment on column s_grnplm_ld_salesntwrk_pcap_sn_uzp.<витрина>.author_login
	is 'Логин автора записи';
