drop table if exists x1 cascade;
create temp table x1 as
select 
	report_dt, 
	desc_name as desk_nm, 
	last_contact_report_dt,
	mkk_ca_saphr_id,
	tb_id, 
	key_client.gosb_id,
	key_client.inn,
	company_name,
	lower(inn_strategy_name) as inn_in_gosb_strategy,
	gosb_inn_role as org_fixed_role,
	is_outflow_risk as outflow_risk, 
	outflow_risk_fot_amt, 
	outflow_risk_fl_qty as outflow_risk_fl_amt, 
	is_expansion, 
	expansion_fot_amt, 
	expansion_fl_qty as expansion_fl_amt
from
	 s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_key_client_info_add_attr key_client
left join
	(select 
		gosb_id, role_code, inn, max(fact_close_task_dttm) as last_contact_report_dt
	from
		 s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_sale_funnel_task
	group by 1,2,3) sale_funnel
		on sale_funnel.gosb_id = key_client.gosb_id
		and sale_funnel.inn = key_client.inn
		and role_code = case when gosb_inn_role = 'КМ+МЗП' then 'МЗП' else gosb_inn_role end
where
	is_inn_in_gosb is true
	and report_dt = s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.last_day(current_date);


	




drop table if exists tmp_scoring cascade;
create temp table tmp_scoring as
select 
	x1.report_dt, 
	desk_nm, 
	last_contact_report_dt,
	mkk_ca_saphr_id, 
	tb_id, 
	gosb_id, 
	inn, 
	company_name,
	inn_in_gosb_strategy, 
	org_fixed_role,
	--- риск оттока
	case when outflow_risk is true then 1 else 0 end as outflow_risk_flag,
	coalesce(outflow_risk_fot_amt,0) as outflow_risk_fot,
	coalesce(outflow_risk_fl_amt,0) as outflow_risk_fl,
	--- возможность расширения
	case when is_expansion is true then 1 else 0 end as extension_flag,
	coalesce(expansion_fot_amt,0) as extension_fot,
	coalesce(expansion_fl_amt,0) as extension_fl,
	ul_outflow as outflow_flag,
	fl_outflow_qty as outflow_fl, 
	fot_outflow_amt as outflow_fot,
	current_fot_amt, 
	current_fl_qty, 
	emp_potential_qty as fl_potential_qty, 
	fot_potential_amt as fot_potential_qty
from
	 x1	
left join
	(select 
		report_dt, level_id, org_id, 
		ul_outflow_qty as ul_outflow, fl_outflow_qty, fot_outflow_amt, current_fot_amt, fot_y_1_diff_amt, current_fl_qty, 
		fl_y_1_diff_qty, emp_potential_qty, fot_potential_amt, total_emp_qty, zp_fl_perc
	from
		 s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_company_holding_metric
	where
		org_type = 'inn' and level_name = 'gosb') t
			on t.level_id = gosb_id and t.org_id = inn
			and t.report_dt = s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.last_day(x1.report_dt - interval '1 month');



drop table if exists tmp_sap_mkk_ca_fio cascade;
create temp table tmp_sap_mkk_ca_fio as
select * from (
select 
	saphr_id, fio,
	row_number() over(partition by saphr_id order by report_dt desc) as rn
from
	 s_grnplm_ld_salesntwrk_pcap_sn_uzp.sap_dwh_staff_detail
where
	fio is not null) t
where
	rn = 1;

drop table if exists kk_scoring cascade;
create temp table kk_scoring as
with t as (	
select 
	gosb_id, 
	inn,
	company_name,
	desk_nm, 
	mkk_ca_saphr_id,
	inn_in_gosb_strategy,
	last_contact_report_dt,
	case 
		when last_contact_report_dt is null then 1
		when org_fixed_role in ('УПР', 'ЗУПР КИБ') and current_date::date - interval '6 month' > last_contact_report_dt then 1
		when org_fixed_role in ('УПР', 'ЗУПР КИБ') and current_date::date - interval '6 month' <= last_contact_report_dt then 0
		when current_date::date - interval '3 month' > last_contact_report_dt then 1
		else 0
	end as contact_flag, 
	case 
		when lower(inn_in_gosb_strategy) = 'привлечение' then 1.2
		when lower(inn_in_gosb_strategy) = 'удержание' then 1.15
		when lower(inn_in_gosb_strategy) = 'отток' then 1.18
	end as strategy_coef,
	org_fixed_role, 
	outflow_risk_flag, 
	outflow_risk_fot, 
	outflow_risk_fl, 
	sum(outflow_risk_fot) over(partition by gosb_id,org_fixed_role) as outflow_risk_fot_sum,
	sum(outflow_risk_fl) over(partition by gosb_id,org_fixed_role) as outflow_risk_fl_sum,
	extension_flag, 
	extension_fot, 
	extension_fl, 
	sum(extension_fot) over(partition by gosb_id,org_fixed_role) as extension_fot_sum,
	sum(extension_fl) over(partition by gosb_id,org_fixed_role) as extension_fl_sum,
	outflow_flag, 
	outflow_fot, 
	outflow_fl, 
	sum(outflow_fot) over(partition by gosb_id,org_fixed_role) as outflow_fot_sum,
	sum(outflow_fl) over(partition by gosb_id,org_fixed_role) as outflow_fl_sum,
	current_fot_amt, 
	current_fl_qty, 
	sum(current_fot_amt) over(partition by gosb_id,org_fixed_role) as current_fot_amt_sum,
	sum(current_fl_qty) over(partition by gosb_id,org_fixed_role) as current_fl_qty_sum,
	fot_potential_qty, 
	fl_potential_qty,
	sum(fot_potential_qty) over(partition by gosb_id,org_fixed_role) as fot_potential_qty_sum,
	sum(fl_potential_qty) over(partition by gosb_id,org_fixed_role) as fl_potential_qty_sum
from
	 tmp_scoring),
t2 as (
select
	gosb_id, 
	inn,
	company_name,
	desk_nm, 
	mkk_ca_saphr_id,
	inn_in_gosb_strategy,
	last_contact_report_dt,
	contact_flag, 
	strategy_coef,
	org_fixed_role, 
	----------
	outflow_risk_flag, 
	outflow_risk_fot,
	outflow_risk_fl, 
	outflow_risk_fot_sum, 
	outflow_risk_fl_sum, 
	0 as outflow_risk_coef,
	extension_flag, 
	extension_fot, 
	extension_fl, 
	extension_fot_sum, 
	extension_fl_sum,
	0 as extension_coef,
	outflow_flag, 
	outflow_fot,
	outflow_fl, 
	outflow_fot_sum, 
	outflow_fl_sum,
	(case when lower(inn_in_gosb_strategy) = 'отток' and coalesce(outflow_fl,0) >= 50 then 0 else 1 end)*outflow_flag*((2.5-1)/4)*
		((case when outflow_fot_sum = 0 then 0 else outflow_fot/outflow_fot_sum end)+(case when outflow_fl_sum = 0 then 0 else outflow_fl/outflow_fl_sum end)) as outflow_coef,
	current_fot_amt, 
	current_fl_qty, 
	current_fot_amt_sum, 
	current_fl_qty_sum, 
	contact_flag*((1.4-1)/4)*
		((case when current_fot_amt_sum = 0 then 0 else current_fot_amt/current_fot_amt_sum end)+(case when current_fl_qty_sum = 0 then 0 else current_fl_qty/current_fl_qty_sum end)) as portfel_coef,
	fot_potential_qty, 
	fl_potential_qty::int8,
	fot_potential_qty_sum,
	fl_potential_qty_sum,
	contact_flag*((1.5-1)/4)*
		((case when fot_potential_qty_sum = 0 then 0 else fot_potential_qty/fot_potential_qty_sum end)+(case when fl_potential_qty_sum = 0 then 0 else fl_potential_qty/fl_potential_qty_sum end)) as potential_coef
from t)
select
	gosb_id, inn, company_name, desk_nm, mkk_ca_saphr_id, fio, lower(inn_in_gosb_strategy) as inn_in_gosb_strategy, last_contact_report_dt, contact_flag, strategy_coef, org_fixed_role, outflow_risk_flag, 
	outflow_risk_fot, outflow_risk_fl, outflow_risk_fot_sum, outflow_risk_fl_sum, outflow_risk_coef, extension_flag, extension_fot, extension_fl, 
	extension_fot_sum, extension_fl_sum, extension_coef, outflow_flag, outflow_fot, outflow_fl, outflow_fot_sum, outflow_fl_sum, outflow_coef,
	current_fot_amt, coalesce(current_fl_qty, 0) as current_fl_qty, current_fot_amt_sum, 
	current_fl_qty_sum, portfel_coef, fot_potential_qty, coalesce(fl_potential_qty, 0) as fl_potential_qty, fot_potential_qty_sum, fl_potential_qty_sum, potential_coef,
	strategy_coef*(outflow_risk_coef+extension_coef+outflow_coef+portfel_coef+potential_coef) as final_coef,
	rank() over(partition by gosb_id, org_fixed_role order by strategy_coef*(outflow_risk_coef+extension_coef+outflow_coef+portfel_coef+potential_coef) desc nulls last) as rn_final_coef,
	case when mkk_ca_saphr_id is null then '' else  'При возникновении вопросов по задаче просьба обращаться к ' || fio || ' (Управление зарплатных проектов, ЦА).' end as mkk_ca_text,
	concat('<a href="https://navigator.ca.sbrf.ru/gdash/1000004325?vName_Visible=0&pLevelName=gosb_',
		gosb_id,--8586
		'&vLevel_test=%D0%98%D1%80%D0%BA%D1%83%D1%82%D1%81%D0%BA%D0%BE%D0%B5%20%D0%BE%D1%82%D0%B4%D0%B5%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5%20%E2%84%96',
		gosb_id,--8586
		'&vLevel_Visible=1&vINN_Visible=1&vName_test=%D0%9E%D0%91%D0%A9%D0%95%D0%A1%D0%A2%D0%92%D0%9E%20%D0%A1%20%D0%9E%D0%93%D0%A0%D0%90%D0%9D%D0%98%D0%A7%D0%95%D0%9D%D0%9D%D0%9E%D0%99%20%D0%9E%D0%A2%D0%92%D0%95%D0%A2%D0%A1%D0%A2%D0%92%D0%95%D0%9D%D0%9D%D0%9E%D0%A1%D0%A2%D0%AC%D0%AE%20%D0%93%D0%90%D0%97%D0%9F%D0%A0%D0%9E%D0%9C%20%D0%94%D0%9E%D0%91%D0%AB%D0%A7%D0%90%20%D0%98%D0%A0%D0%9A%D0%A3%D0%A2%D0%A1%D0%9A&vWidget=1&vINN_test=',
		inn,'">ссылка omega</a><br><a href="https://navigator.sigma.sbrf.ru/gdash/1000004325?vName_Visible=0&pLevelName=gosb_',
		gosb_id,--8586
		'&vLevel_test=%D0%98%D1%80%D0%BA%D1%83%D1%82%D1%81%D0%BA%D0%BE%D0%B5%20%D0%BE%D1%82%D0%B4%D0%B5%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5%20%E2%84%96',
		gosb_id,--8586
		'&vLevel_Visible=1&vINN_Visible=1&vName_test=%D0%9E%D0%91%D0%A9%D0%95%D0%A1%D0%A2%D0%92%D0%9E%20%D0%A1%20%D0%9E%D0%93%D0%A0%D0%90%D0%9D%D0%98%D0%A7%D0%95%D0%9D%D0%9D%D0%9E%D0%99%20%D0%9E%D0%A2%D0%92%D0%95%D0%A2%D0%A1%D0%A2%D0%92%D0%95%D0%9D%D0%9D%D0%9E%D0%A1%D0%A2%D0%AC%D0%AE%20%D0%93%D0%90%D0%97%D0%9F%D0%A0%D0%9E%D0%9C%20%D0%94%D0%9E%D0%91%D0%AB%D0%A7%D0%90%20%D0%98%D0%A0%D0%9A%D0%A3%D0%A2%D0%A1%D0%9A&vWidget=1&vINN_test=',
		inn,'">ссылка sigma</a>') as navigator_link
from t2
left join
	tmp_sap_mkk_ca_fio
		on tmp_sap_mkk_ca_fio.saphr_id = t2.mkk_ca_saphr_id;
where
	contact_flag = 1;


	 
drop table if exists s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.yva_kk_new_scoring_v2 cascade;
create table s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.yva_kk_new_scoring_v2 as 
select 
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	org_fixed_role as task_role,
	'основная' as task_type,
	case 
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'привлечение' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || 'Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' или запросить у AI-помощника ЛИСА.
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || ').
Цель встречи:
1. договориться о расширении сотрудничества по зарплатному проекту;
2. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
Роли:
1. Управляющий ГОСБ - ответственный исполнитель (проведение встречи, финальный результат);
2. Закреплённый КМ - соисполнитель (подготовка материалов, участие во встрече, отработка операционных вопросов);
3. Начальник управления прямых продаж ГОСБ - соисполнитель (подготовка материалов и уточнение лимита преференций).
При закрытии необходимо указать:
1. дату проведённой встречи/коммуникации;
2. должность представителя клиента, с которым проведена встреча/коммуникация;
3. ключевые тезисы разговора, выявленные риски;
4. достигнутые договорённости с приложением подтверждающего документа (скан протокола встречи, письмо клиента). Рекомендуем использовать GigaПротокол AI-помощника ЛИСА.
Критерии успешного закрытия: задача считается выполненной, если проведена личная встреча (или официальная коммуникация с ЛПР), в комментарии к задаче зафиксированы конкретные договорённости, влияющие на сохранение портфеля. Отчёты вида «задача исполнена» без аналитики не принимаются.
Особые случаи: если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке, начните заполнение комментария к задаче с хэштега #эскалация. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
' || mkk_ca_text
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'удержание' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: удержание. ' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ. Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' или запросить у AI-помощника ЛИСА.
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || ').
Цель встречи:
1. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
2. подтвердить намерения клиента по сохранению объёмов зачислений заработной платы и кол-ва получателей на действующем уровне;
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
Роли:
1. Управляющий ГОСБ - ответственный исполнитель (проведение встречи, финальный результат);
2. Закреплённый КМ - соисполнитель (подготовка материалов, участие во встрече, отработка операционных вопросов);
3. Начальник управления прямых продаж ГОСБ - соисполнитель (подготовка материалов и уточнение лимита преференций).
При закрытии необходимо указать:
1. дату проведённой встречи/коммуникации;
2. должность представителя клиента, с которым проведена встреча/коммуникация;
3. ключевые тезисы разговора, выявленные риски;
4. достигнутые договорённости с приложением подтверждающего документа (скан протокола встречи, письмо клиента). Рекомендуем использовать GigaПротокол AI-помощника ЛИСА.
Критерии успешного закрытия: задача считается выполненной, если проведена личная встреча (или официальная коммуникация с ЛПР), в комментарии к задаче зафиксированы конкретные договорённости, влияющие на сохранение портфеля. Отчёты вида «задача исполнена» без аналитики не принимаются.
Особые случаи: если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке, начните заполнение комментария к задаче с хэштега #эскалация. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
' || mkk_ca_text
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'отток' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении зафиксированы риски, установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ. Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' или запросить у AI-помощника ЛИСА.
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || ').
Цель встречи:
1. поддержать конструктивные отношения для сохранения оставшихся объёмов сотрудничества;
2. выявить наличие новых рисков дальнейшего сокращения;
3. создать условия для возможного восстановления объёмов в будущем (при необходимости предложить меры поддержки в рамках утверждённого лимита преференций).
Роли:
1. Управляющий ГОСБ - ответственный исполнитель (проведение встречи, финальный результат);
2. Закреплённый КМ - соисполнитель (подготовка материалов, участие во встрече, отработка операционных вопросов);
3. Начальник управления прямых продаж ГОСБ - соисполнитель (подготовка материалов и уточнение лимита преференций).
При закрытии необходимо указать:
1. дату проведённой встречи/коммуникации;
2. должность представителя клиента, с которым проведена встреча/коммуникация;
3. подтверждённый клиентом объём сохраняемых перечислений (в ФЛ или сумме);
4. выявленные риски дальнейшего сокращения (если есть);
5. достигнутые договорённости с приложением подтверждающего документа (скан протокола встречи, письмо клиента). Рекомендуем использовать GigaПротокол AI-помощника ЛИСА.
Критерии успешного закрытия: задача считается выполненной, если проведена личная встреча (или официальная коммуникация с ЛПР), в комментарии к задаче зафиксированы конкретные договорённости, влияющие на сохранение портфеля. Отчёты вида «задача исполнена» без аналитики не принимаются.
Особые случаи: если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке, начните заполнение комментария к задаче с хэштега #эскалация. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
' || mkk_ca_text
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'УПР'
union all
select
gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'КМ ВКО' as task_role,
	'инфо' as task_type,
case
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'привлечение' then 'Информируем Вас, что на Управляющего ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Необходимо провести переговоры для расширения сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'удержание' then 'Информируем Вас, что на Управляющего ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо провести переговоры для развития сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'отток' then 'Информируем Вас, что на Управляющего ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо связаться с клиентом, выяснить причины и согласовать действия по восстановлению зачислений, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'	 
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'УПР'	 
union all
select
gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'КМ МТ' as task_role,
	'инфо' as task_type,
case
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'привлечение' then 'Информируем Вас, что на Управляющего ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Необходимо провести переговоры для расширения сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'удержание' then 'Информируем Вас, что на Управляющего ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо провести переговоры для развития сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'отток' then 'Информируем Вас, что на Управляющего ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо связаться с клиентом, выяснить причины и согласовать действия по восстановлению зачислений, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'	 
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'УПР'	
union all
select
gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'НУ' as task_role,
	'инфо' as task_type,
case
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'привлечение' then 'Уважаемый руководитель!
Информируем Вас, что на Управляющего ГОСБ выставлена задача "Стратегия по ключевым клиентам" со сроком отработки до ' || (current_date::date + interval '32 days')::date || ').
Необходимо обеспечить подготовку Управляющего ГОСБ и закреплённого МЗП ко встрече с клиентом, предоставить уточненные данные по доступному лимиту преференций.' 
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'удержание' then 'Уважаемый руководитель!
Информируем Вас, что на Управляющего ГОСБ выставлена задача "Стратегия по ключевым клиентам" со сроком отработки до ' || (current_date::date + interval '32 days')::date || ').
Необходимо обеспечить подготовку Управляющего ГОСБ и закреплённого МЗП ко встрече с клиентом, предоставить уточненные данные по доступному лимиту преференций.'
		when org_fixed_role = 'УПР' and inn_in_gosb_strategy = 'отток' then 'Уважаемый руководитель!
Информируем Вас, что на Управляющего ГОСБ и закрепленного Клиентского менеджера выставлены задачи "Стратегия по ключевым клиентам" со сроком отработки до ' || (current_date::date + interval '32 days')::date || ').
Необходимо обеспечить подготовку Управляющего ГОСБ и закреплённого МЗП ко встрече с клиентом, предоставить уточненные данные по доступному лимиту преференций.'
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'УПР'	
union all
select
gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	org_fixed_role as task_role,
	'основная' as task_type,	 
case
when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'привлечение' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || 'Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' или запросить у AI-помощника ЛИСА.
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать GigaПротокол AI-помощника ЛИСА.
Цель встречи:
1. договориться о расширении сотрудничества по зарплатному проекту;
2. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
Роли:
1. Управляющий ГОСБ - ответственный исполнитель (проведение встречи, финальный результат);
2. Закреплённый КМ - соисполнитель (подготовка материалов, участие во встрече, отработка операционных вопросов);
3. Начальник отдела зарплатных проектов ГОСБ - соисполнитель (подготовка материалов и уточнение лимита преференций).
При закрытии задачи необходимо указать достигнутые договоренности. Рекомендуем использовать GigaПротокол AI-помощника ЛИСА.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.

' || mkk_ca_text
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'удержание' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ. Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' или запросить у AI-помощника ЛИСА.
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать GigaПротокол AI-помощника ЛИСА.
Цель встречи:
1. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
2. подтвердить намерения клиента по сохранению объёмов зачислений заработной платы и кол-ва получателей на действующем уровне;
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
Роли:
1. Управляющий ГОСБ - ответственный исполнитель (проведение встречи, финальный результат);
2. Закреплённый КМ - соисполнитель (подготовка материалов, участие во встрече, отработка операционных вопросов);
3. Начальник отдела зарплатных проектов ГОСБ - соисполнитель (подготовка материалов и уточнение лимита преференций).
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.

' || mkk_ca_text
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'отток' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении зафиксированы риски, установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ. Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' или запросить у AI-помощника ЛИСА.
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать GigaПротокол AI-помощника ЛИСА.
Цель встречи:
1. поддержать конструктивные отношения для сохранения оставшихся объёмов сотрудничества;
2. выявить наличие новых рисков дальнейшего сокращения;
3. создать условия для возможного восстановления объёмов в будущем (при необходимости предложить меры поддержки в рамках утверждённого лимита преференций).
Роли:
1. Управляющий ГОСБ - ответственный исполнитель (проведение встречи, финальный результат);
2. Закреплённый КМ - соисполнитель (подготовка материалов, участие во встрече, отработка операционных вопросов);
3. Начальник отдела зарплатных проектов ГОСБ - соисполнитель (подготовка материалов и уточнение лимита преференций).
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.

' || mkk_ca_text
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'ЗУПР КИБ'	
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'КМ ВКО' as task_role,
	'инфо' as task_type,	 	
case
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'привлечение' then 'Информируем Вас, что на ЗУпр КИБ ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Необходимо провести переговоры для расширения сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'удержание' then 'Информируем Вас, что на ЗУпр КИБ ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо провести переговоры для развития сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'отток' then 'Информируем Вас, что на ЗУпр КИБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо связаться с клиентом, выяснить причины и согласовать действия по восстановлению зачислений, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'	
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'ЗУПР КИБ'		
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'КМ МТ' as task_role,
	'инфо' as task_type,	 	
case
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'привлечение' then 'Информируем Вас, что на ЗУпр КИБ ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Необходимо провести переговоры для расширения сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'удержание' then 'Информируем Вас, что на ЗУпр КИБ ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо провести переговоры для развития сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'отток' then 'Информируем Вас, что на ЗУпр КИБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо связаться с клиентом, выяснить причины и согласовать действия по восстановлению зачислений, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'	
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'ЗУПР КИБ'		
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'НО' as task_role,
	'инфо' as task_type,	 	
case
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'привлечение' then 'Уважаемый руководитель!
Информируем Вас, что на ЗУпр КИБ ГОСБ выставлена задача "Стратегия по ключевым клиентам" со сроком отработки до ' || (current_date::date + interval '32 days')::date || ').
Необходимо обеспечить подготовку ЗУпр КИБ ГОСБ и закреплённого МЗП ко встрече с клиентом, предоставить уточненные данные по доступному лимиту преференций.'
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'удержание' then 'Уважаемый руководитель!
Информируем Вас, что на ЗУпр КИБ ГОСБ выставлена задача "Стратегия по ключевым клиентам" со сроком отработки до ' || (current_date::date + interval '32 days')::date || ').
Необходимо обеспечить подготовку ЗУпр КИБ ГОСБ и закреплённого МЗП ко встрече с клиентом, предоставить уточненные данные по доступному лимиту преференций.'
		when org_fixed_role = 'ЗУПР КИБ' and inn_in_gosb_strategy = 'отток' then 'Уважаемый руководитель!
Информируем Вас, что на ЗУпр КИБ ГОСБ выставлена задача "Стратегия по ключевым клиентам" со сроком отработки до ' || (current_date::date + interval '32 days')::date || ').
Необходимо обеспечить подготовку ЗУпр КИБ ГОСБ и закреплённого МЗП ко встрече с клиентом, предоставить уточненные данные по доступному лимиту преференций.'
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'ЗУПР КИБ'
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	org_fixed_role as task_role,
	'основная' as task_type,	 	
case
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'привлечение' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || ' Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' .
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать АС Поток (функция Автопротокол).
Цель встречи:
1. договориться о расширении сотрудничества по зарплатному проекту;
2. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
Роли:
1. Управляющий ГОСБ - ответственный исполнитель (проведение встречи, финальный результат);
2. Закреплённый КМ - соисполнитель (подготовка материалов, участие во встрече, отработка операционных вопросов);
3. Начальник отдела зарплатных проектов ГОСБ - соисполнитель (подготовка материалов и уточнение лимита преференций).
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.

' || mkk_ca_text
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'удержание' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ. Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' .
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать АС Поток (функция Автопротокол).
Цель встречи:
1. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
2. подтвердить намерения клиента по сохранению объёмов зачислений заработной платы и кол-ва получателей на действующем уровне;
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
Роли:
1. Управляющий ГОСБ - ответственный исполнитель (проведение встречи, финальный результат);
2. Закреплённый КМ - соисполнитель (подготовка материалов, участие во встрече, отработка операционных вопросов);
3. Начальник отдела зарплатных проектов ГОСБ - соисполнитель (подготовка материалов и уточнение лимита преференций).
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.

' || mkk_ca_text
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'отток' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении зафиксированы риски, установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ. Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' .
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать АС Поток (функция Автопротокол).
Цель встречи:
1. поддержать конструктивные отношения для сохранения оставшихся объёмов сотрудничества;
2. выявить наличие новых рисков дальнейшего сокращения;
3. создать условия для возможного восстановления объёмов в будущем (при необходимости предложить меры поддержки в рамках утверждённого лимита преференций).
Роли:
1. Управляющий ГОСБ - ответственный исполнитель (проведение встречи, финальный результат);
2. Закреплённый КМ - соисполнитель (подготовка материалов, участие во встрече, отработка операционных вопросов);
3. Начальник отдела зарплатных проектов ГОСБ - соисполнитель (подготовка материалов и уточнение лимита преференций).
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.

' || mkk_ca_text
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'ЗУПР РБ'
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'КМ ВКО' as task_role,
	'инфо' as task_type,	 	
case	
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'привлечение' then 'Информируем Вас, что на ЗУпр РБ ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Необходимо провести переговоры для расширения сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'удержание' then 'Информируем Вас, что на ЗУпр РБ ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо провести переговоры для развития сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'отток' then 'Информируем Вас, что на ЗУпр РБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо связаться с клиентом, выяснить причины и согласовать действия по восстановлению зачислений, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'	
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'ЗУПР РБ'	
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'КМ МТ' as task_role,
	'инфо' as task_type,	 	
case	
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'привлечение' then 'Информируем Вас, что на ЗУпр РБ ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Необходимо провести переговоры для расширения сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'удержание' then 'Информируем Вас, что на ЗУпр РБ ГОСБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо провести переговоры для развития сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'отток' then 'Информируем Вас, что на ЗУпр РБ №' || gosb_id || ' выставлена задача по клиенту.
По клиенту в головном отделении установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо связаться с клиентом, выяснить причины и согласовать действия по восстановлению зачислений, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.'	
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'ЗУПР РБ'		
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'НО' as task_role,
	'инфо' as task_type,	 	
case		
when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'привлечение' then 'Уважаемый руководитель!
Информируем Вас, что на ЗУпр РБ ГОСБ выставлена задача "Стратегия по ключевым клиентам" со сроком отработки до ' || (current_date::date + interval '32 days')::date || ').
Необходимо обеспечить подготовку ЗУпр РБ ГОСБ и закреплённого МЗП ко встрече с клиентом, предоставить уточненные данные по доступному лимиту преференций.'
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'удержание' then 'Уважаемый руководитель!
Информируем Вас, что на ЗУпр РБ ГОСБ выставлена задача "Стратегия по ключевым клиентам" со сроком отработки до ' || (current_date::date + interval '32 days')::date || ').
Необходимо обеспечить подготовку ЗУпр РБ ГОСБ и закреплённого МЗП ко встрече с клиентом, предоставить уточненные данные по доступному лимиту преференций.'
		when org_fixed_role = 'ЗУПР РБ' and inn_in_gosb_strategy = 'отток' then 'Уважаемый руководитель!
Информируем Вас, что на ЗУпр РБ ГОСБ выставлена задача "Стратегия по ключевым клиентам" со сроком отработки ' || (current_date::date + interval '32 days')::date || ').
Необходимо обеспечить подготовку ЗУпр РБ ГОСБ и закреплённого МЗП ко встрече с клиентом, предоставить уточненные данные по доступному лимиту преференций.'	
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'ЗУПР РБ'		
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'НУ' as task_role,
	'основная' as task_type,		
case		
when org_fixed_role = 'НУ ГОСБ' and inn_in_gosb_strategy = 'привлечение' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty > 0 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || 'Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' .
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать АС Поток (функция Автопротокол).
Цель встречи:
1. договориться о расширении сотрудничества по зарплатному проекту;
2. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.'
		when org_fixed_role = 'НУ ГОСБ' and inn_in_gosb_strategy = 'удержание' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty > 0 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ. Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' .
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать АС Поток (функция Автопротокол).
Цель встречи:
1. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
2. подтвердить намерения клиента по сохранению объёмов зачислений заработной платы и кол-ва получателей на действующем уровне;
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
		when org_fixed_role = 'НУ ГОСБ' and inn_in_gosb_strategy = 'отток' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении зафиксированы риски, установлена стратегия: отток.' || case when fl_potential_qty > 0 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ. Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' .
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать АС Поток (функция Автопротокол).
Цель встречи:
1. поддержать конструктивные отношения для сохранения оставшихся объёмов сотрудничества;
2. выявить наличие новых рисков дальнейшего сокращения;
3. создать условия для возможного восстановления объёмов в будущем (при необходимости предложить меры поддержки в рамках утверждённого лимита преференций).
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'	
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'НУ ГОСБ'	
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'НО' as task_role,
	'основная' as task_type,		
case		
		when org_fixed_role = 'НО ЗП' and inn_in_gosb_strategy = 'привлечение' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty > 0 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || 'Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' .
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать АС Поток (функция Автопротокол).
Цель встречи:
1. договориться о расширении сотрудничества по зарплатному проекту;
2. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
		when org_fixed_role = 'НО ЗП' and inn_in_gosb_strategy = 'удержание' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty > 0 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ. Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' .
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать АС Поток (функция Автопротокол).
Цель встречи:
1. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
2. подтвердить намерения клиента по сохранению объёмов зачислений заработной платы и кол-ва получателей на действующем уровне;
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
		when org_fixed_role = 'НО ЗП' and inn_in_gosb_strategy = 'отток' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении зафиксированы риски, установлена стратегия: отток.' || case when fl_potential_qty > 0 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ. Более подробную информацию можно изучить в Карточке клиента в АС Навигатор ' || navigator_link || ' .
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || '). Для фиксации договоренностей рекомендуем использовать АС Поток (функция Автопротокол).
Цель встречи:
1. поддержать конструктивные отношения для сохранения оставшихся объёмов сотрудничества;
2. выявить наличие новых рисков дальнейшего сокращения;
3. создать условия для возможного восстановления объёмов в будущем (при необходимости предложить меры поддержки в рамках утверждённого лимита преференций).
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'НО ЗП'		
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'МКК ТБ' as task_role,
	'основная' as task_type,		
case		
		when org_fixed_role = 'МКК ТБ' and inn_in_gosb_strategy = 'привлечение' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty > 0 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || ').
Цель встречи:
1. договориться о расширении сотрудничества по зарплатному проекту;
2. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
		when org_fixed_role = 'МКК ТБ' and inn_in_gosb_strategy = 'удержание' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty > 0 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || ').
Цель встречи:
1. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
2. подтвердить намерения клиента по сохранению объёмов зачислений заработной платы и кол-ва получателей на действующем уровне;
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
		when org_fixed_role = 'МКК ТБ' and inn_in_gosb_strategy = 'отток' then 'Компания ' || company_name || ' (' || inn || '). ГОСБ №' || gosb_id || '

По клиенту в головном отделении зафиксированы риски, установлена стратегия: отток.' || case when fl_potential_qty > 0 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Задача: Провести встречу с руководителем компании (срок — до ' || (current_date::date + interval '32 days')::date || ').
Цель встречи:
1. поддержать конструктивные отношения для сохранения оставшихся объёмов сотрудничества;
2. выявить наличие новых рисков дальнейшего сокращения;
3. создать условия для возможного восстановления объёмов в будущем (при необходимости предложить меры поддержки в рамках утверждённого лимита преференций).
При закрытии задачи необходимо указать достигнутые договоренности.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'МКК ТБ'
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'МЗП' as task_role,
	'основная' as task_type,		
case
		when org_fixed_role = 'МЗП' and inn_in_gosb_strategy = 'привлечение' then 'По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty > 0 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Вам необходимо провести встречу с руководителем компании для расширения сотрудничества по зарплатному проекту.
В рамках встречи необходимо:
1. договориться о расширении сотрудничества по зарплатному проекту;
2. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
Дополнительную информацию о клиенте вы можете получить в разделе Помощник GigaChat.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
		when org_fixed_role = 'МЗП' and inn_in_gosb_strategy = 'удержание' then 'По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty > 0 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Вам необходимо провести встречу с руководителем компании для поддержания конструктивных отношений с целью сохранения объемов сотрудничества.
В рамках встречи необходимо:
1. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
2. подтвердить намерения клиента по сохранению объёмов зачислений заработной платы и кол-ва получателей на действующем уровне;
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
Дополнительную информацию о клиенте вы можете получить в разделе Помощник GigaChat.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
		when org_fixed_role = 'МЗП' and inn_in_gosb_strategy = 'отток' then 'По клиенту в головном отделении зафиксированы риски, установлена стратегия: отток.' || case when fl_potential_qty > 0 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Вам необходимо провести встречу с руководителем компании для поддержания конструктивных отношений с целью сохранения оставшихся объемов сотрудничества и возможности восстановления объемов в будущем.
В рамках встречи необходимо:
1. уточнить намерения клиента по оставшемуся объему перечислений;
2. выявить, есть ли новый риск дальнейшего сокращения;
3. создать условия для возможного восстановления объёмов в будущем (при необходимости предложить меры поддержки в рамках утверждённого лимита преференций).
Дополнительную информацию о клиенте вы можете получить в разделе Помощник GigaChat.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'МЗП'
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'МЗП' as task_role,
	'основная' as task_type,
case
		when org_fixed_role = 'КМ+МЗП' and inn_in_gosb_strategy = 'привлечение' then 'По клиенту в головном отделении установлена стратегия: привлечение. ' || case when fl_potential_qty > 0 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Вам необходимо провести встречу с руководителем компании для расширения сотрудничества по зарплатному проекту.
В рамках встречи необходимо:
1. договориться о расширении сотрудничества по зарплатному проекту;
2. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
Дополнительную информацию о клиенте вы можете получить в разделе Помощник GigaChat.
Аналогичная задача выставлена на закрепленного КМ.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
		when org_fixed_role = 'КМ+МЗП' and inn_in_gosb_strategy = 'удержание' then 'По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty > 0 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Вам необходимо провести встречу с руководителем компании для поддержания конструктивных отношений с целью сохранения объемов сотрудничества.
В рамках встречи необходимо:
1. выявить текущий уровень лояльности и возможные риски (неудовлетворённость сервисом, активность конкурентов);
2. подтвердить намерения клиента по сохранению объёмов зачислений заработной платы и кол-ва получателей на действующем уровне;
3. укрепить партнёрские отношения, при необходимости предложить меры поддержки в рамках утверждённого лимита преференций.
Дополнительную информацию о клиенте вы можете получить в разделе Помощник GigaChat.
Аналогичная задача выставлена на закрепленного КМ.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'		when org_fixed_role = 'КМ+МЗП' and inn_in_gosb_strategy = 'отток' then 'По клиенту в головном отделении зафиксированы риски, установлена стратегия: отток.' || case when fl_potential_qty > 0 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Вам необходимо провести встречу с руководителем компании для поддержания конструктивных отношений с целью сохранения оставшихся объемов сотрудничества и возможности восстановления объемов в будущем.
В рамках встречи необходимо:
1. уточнить намерения клиента по оставшемуся объему перечислений;
2. выявить, есть ли новый риск дальнейшего сокращения;
3. создать условия для возможного восстановления объёмов в будущем (при необходимости предложить меры поддержки в рамках утверждённого лимита преференций).
Дополнительную информацию о клиенте вы можете получить в разделе Помощник GigaChat.
Аналогичная задача выставлена на закрепленного КМ.

Вы можете эскалировать вопрос, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации. Проблема будет направлена на уровень ВКО клиента.
'
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'КМ+МЗП'	
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'КМ ВКО' as task_role,
	'основная' as task_type,
case
		when org_fixed_role = 'КМ+МЗП' and inn_in_gosb_strategy = 'привлечение' then 'По клиенту в головном отделении установлена стратегия привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Необходимо провести переговоры для расширения сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.
При закрытии ЛИДа отказом начните заполнение с хэштега #эскалация, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации.'
		when org_fixed_role = 'КМ+МЗП' and inn_in_gosb_strategy = 'удержание' then 'По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо провести переговоры для развития сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.
При закрытии ЛИДа отказом начните заполнение с хэштега #эскалация, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации.'
		when org_fixed_role = 'КМ+МЗП' and inn_in_gosb_strategy = 'отток' then 'По клиенту в головном отделении установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо связаться с клиентом, выяснить причины и согласовать действия по восстановлению зачислений, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.
При закрытии ЛИДа отказом начните заполнение с хэштега #эскалация, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации.'
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'КМ+МЗП'		
union all
select
	gosb_id, inn, mkk_ca_saphr_id, inn_in_gosb_strategy, org_fixed_role, current_fl_qty, fl_potential_qty, company_name,outflow_fl,current_fot_amt, rn_final_coef,
	'КМ МТ' as task_role,
	'основная' as task_type,
case
		when org_fixed_role = 'КМ+МЗП' and inn_in_gosb_strategy = 'привлечение' then 'По клиенту в головном отделении установлена стратегия привлечение. ' || case when fl_potential_qty >= 30 then 'Текущий потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ. ' else '' end || '
Необходимо провести переговоры для расширения сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.
При закрытии ЛИДа отказом начните заполнение с хэштега #эскалация, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации.'
		when org_fixed_role = 'КМ+МЗП' and inn_in_gosb_strategy = 'удержание' then 'По клиенту в головном отделении установлена стратегия: удержание.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо провести переговоры для развития сотрудничества по зарплатному проекту, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.
При закрытии ЛИДа отказом начните заполнение с хэштега #эскалация, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации.'
		when org_fixed_role = 'КМ+МЗП' and inn_in_gosb_strategy = 'отток' then 'По клиенту в головном отделении установлена стратегия: отток.' || case when fl_potential_qty >= 30 then 'Справочно: потенциал по данным РОССТАТ составляет ' || fl_potential_qty || ' ФЛ, ' else ' ' end || 'Портфель ФЛ в предыдущем месяце ' || current_fl_qty || ' ФЛ.
Необходимо связаться с клиентом, выяснить причины и согласовать действия по восстановлению зачислений, в т.ч. предложите преференции (при наличии свободного лимита).
Достигнутые договоренности отразите в комментариях к ЛИДу.
При закрытии ЛИДа отказом начните заполнение с хэштега #эскалация, если руководитель компании отказался от встречи или Вы узнали о предстоящем оттоке. Подробно опишите ситуацию: ожидаемая потеря Портфеля ФЛ, должность представителя клиента, дата коммуникации.'
end as task_text
from 
	kk_scoring
where
	org_fixed_role = 'КМ+МЗП';
	








drop table if exists x1 cascade;
create temp table x1 as
select 
	desc_name, holding_name,holding_strategy_name, tb_id, uzp_data_key_client_info_add_attr.gosb_id, uzp_data_key_client_info_add_attr.inn as abc, company_name, segment_name, 
	scoring.inn_in_gosb_strategy, org_fixed_role, uzp_data_key_client_info_add_attr.mkk_ca_saphr_id, current_fl_qty, fl_potential_qty,
	coalesce(outflow_fl, 0) as outflow_fl,coalesce(current_fot_amt, 0) as current_fot_amt, rn_final_coef, task_role, task_type, 
	task_text
from
	 s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_key_client_info_add_attr
inner join
	(select 
	gosb_id, inn, org_fixed_role, mkk_ca_saphr_id, current_fl_qty, fl_potential_qty, outflow_fl,current_fot_amt, rn_final_coef, task_role, task_type, task_text, inn_in_gosb_strategy
from
	 s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.yva_kk_new_scoring_v2)scoring
	 	on scoring.gosb_id = uzp_data_key_client_info_add_attr.gosb_id
	 	and scoring.inn = uzp_data_key_client_info_add_attr.inn
where
	report_dt = '2026-07-31'
	and case 
			when task_role in ('КМ ВКО', 'КМ МТ') and scoring.inn_in_gosb_strategy = 'привлечение' and fl_potential_qty < 30 then 0
		else 1 end = 1;
	
	

	

--drop table if exists s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.x1 cascade;	
--create table s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.x1 as
--select 
--	* 
--from
--	 x1
--where
--	task_role <> 'МЗП'
--union all
--select 
--	desc_name, holding_name, holding_strategy_name, tb_id, gosb_id, abc, company_name, segment_name, 
--	inn_in_gosb_strategy, org_fixed_role, mkk_ca_saphr_id, current_fl_qty, 
--	fl_potential_qty, outflow_fl, current_fot_amt, rn_final_coef, task_role, x1.task_type, concat(x1.task_text,case when t.task_text is not null then '
--' || t.task_text else '' end, case when t2.task_text is not null then '
--
--' || t2.task_text else '' end) as task_text, deal_create_dttm
--from
--	 x1
--inner join
--	(select 
--		report_month_dt, 
--		new_gosb_id, 
--		client,
--		'По организации за период c 01.07.2026 по 20.07.2026 в сети ВСП оформлены заявления ФЛ о переводе ЗП на карту Сбера.' || case when el_zayavl <> 0 then '
--Передано электронных заявлений ' || el_zayavl || ' шт., из них с зачислениями - ' || fl_zach_el || ' шт.' else '' end || case when bum_zayavl <> 0 then '
--Всего оформлено бумажных заявлений ФЛ: ' || bum_zayavl || ' шт., из них с зачислениями - ' || fl_zach_bum || ' шт.' else '' end || '
--Используйте информацию при переговорах о переводе на карты Сбера всех сотрудников.
--Памятка и детальные скрипты доступны по [ссылке](https://hr.sberbank.ru/platform/pages/payroll-projects/alias-5168/).' as task_text
--	from
--		 s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.vmv_zayavleniya_na_perevod_zp_iiv
--	left join
--		(select 
--			distinct new_gosb_name, new_gosb_id
--		from
--			 s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dim_gosb) t
--			on vmv_zayavleniya_na_perevod_zp_iiv.new_gosb_name = t.new_gosb_name
--	where
--		report_month_dt = '2026-07-31'
--		and (fl_zach_el <> el_zayavl or bum_zayavl <> fl_zach_bum)) t
--			on  t.new_gosb_id = x1.gosb_id
--			and abc = client
--inner join
--	(select 
--		distinct * 
--	from
--		 s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.temp_new_product) t2
--		on t2.new_gosb_id = x1.gosb_id
--		and inn = abc
--where
--	task_role = 'МЗП'	

	
--- как добавить про Новый договор в текст задачи:	
	
--select *
--from s_grnplm_ld_salesntwrk_pcap_sn_t_uzp.tmp_potencial_ssa


--- как добавить про Новый договор в текст задачи:
select 
	lvl_id as gosb_id, uzp_data_emp_potential.inn, emp_potential_qty - coalesce(outflow_qty,0) as new_agreement_qty
from
	 s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_data_emp_potential
left join
	(select 
		gosb_id, inn, sum(outflow_qty) as outflow_qty
	from
		 s_grnplm_ld_salesntwrk_pcap_sn_uzp.uzp_dwh_fact_outflow
	where
		report_dt between date_trunc('month', current_date - interval '3 month') and current_date
	group by 1,2) t
		on t.gosb_id = lvl_id and t.inn = uzp_data_emp_potential.inn
where
	lvl_name = 'gosb'
	and emp_potential_qty - coalesce(outflow_qty,0) > 0
	
