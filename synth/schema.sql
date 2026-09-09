-- DDL открытого контура: повторяет пром ТОЧЬ-В-ТОЧЬ (та же схема и имена),
-- чтобы SQL дэшей был идентичен в обоих контурах.
-- __SCHEMA__ / __SCHEMA_T__ заменяются на имена схем при применении
-- (reference.py / setup-ноутбук). Схем две, как на проме: основная витринная и
-- __SCHEMA_T__ — там лежит пайплайн (yva_pl_task_deal_code).

CREATE SCHEMA IF NOT EXISTS __SCHEMA__;
CREATE SCHEMA IF NOT EXISTS __SCHEMA_T__;
SET search_path TO __SCHEMA__;

DROP TABLE IF EXISTS uzp_dim_gosb CASCADE;
DROP TABLE IF EXISTS uzp_dim_metric CASCADE;
DROP TABLE IF EXISTS uzp_dwh_metrics CASCADE;
DROP TABLE IF EXISTS uzp_dwh_company_holding_metric CASCADE;
DROP TABLE IF EXISTS uzp_dwh_sale_funnel_task CASCADE;
DROP TABLE IF EXISTS uzp_dim_company CASCADE;
DROP TABLE IF EXISTS uzp_dim_mzp_reference_base CASCADE;
DROP TABLE IF EXISTS uzp_dwh_day_outflow CASCADE;
DROP TABLE IF EXISTS uzp_dwh_fact_outflow CASCADE;
DROP TABLE IF EXISTS uzp_data_outflow_return_detail CASCADE;
DROP TABLE IF EXISTS uzp_data_emp_epk_assignment CASCADE;
DROP TABLE IF EXISTS uzp_data_epk_consolidation CASCADE;
DROP TABLE IF EXISTS uzp_dwh_sap_staff_emp CASCADE;
DROP TABLE IF EXISTS uzp_data_mzp_motivation_detail_corr CASCADE;
DROP TABLE IF EXISTS __SCHEMA_T__.yva_pl_task_deal_code CASCADE;

-- ============ Справочники (грузятся из CSV как есть) ============

CREATE TABLE uzp_dim_gosb (
  tb_id                   integer,
  tb_short_name           text,
  tb_full_name            text,
  old_gosb_name           text,
  old_gosb_id             integer,
  new_gosb_name           text,
  new_gosb_id             integer,
  isu_branch_id           bigint,
  isu_branch_name         text,
  web_gosb_id             integer,
  pirs_gosb_id            bigint,
  pirs_gosb_name          text,
  utc_timezone            smallint,
  timezone_violation_msk  smallint,
  region_id               smallint,
  region_name             varchar,
  inserted_dttm           timestamp,
  author_login            text
);

CREATE TABLE uzp_dim_metric (
  metric_id         bigint,
  metric_name       varchar,
  metric_short_name varchar,
  owner_saphr_id    bigint,
  dev_saphr_id      bigint,
  metric_calc_lvl   varchar,
  is_active         boolean,
  measure_unit      varchar,
  is_rank           boolean,
  rank_sort         varchar,
  is_infopanel      boolean,
  is_navigator      boolean,
  is_sbolpro        boolean,
  cmnt              varchar,
  load_marker       varchar,
  modified_dt       date,
  inserted_dttm     timestamp,
  author_login      text
);

-- ============ Факты план/факт (Единое хранилище метрик) ============

CREATE TABLE uzp_dwh_metrics (
  metric_id          integer,
  start_dt           date,
  end_dt             date,
  level_name         text,        -- sb / tb / gosb / tab_num
  level_value        text,
  level_id           bigint,
  period_type        text,        -- m / q / qtd / y / ytd
  plan_amt           numeric,
  fact_amt           numeric,
  execution_percent  numeric,     -- доля, 1.0 = 100%
  prediction_amt     numeric,
  prediction_percent numeric,
  modified_dttm      timestamp,
  extended_dim_1     bigint,      -- сегмент (короткий код); 1 = все. Маппинг зашит в отчёте
  extended_dim_2     bigint,
  extended_dim_3     bigint,
  extended_dim_4     bigint,
  extended_dim_5     bigint,
  extended_dim_6     bigint,
  extended_dim_7     bigint,
  extended_dim_8     bigint,
  extended_dim_9     bigint,
  extended_dim_10    bigint
);

-- ============ Витрина по организациям (потенциал / отток / ФОТ) ============

CREATE TABLE uzp_dwh_company_holding_metric (
  report_dt          date,
  level_name         varchar,     -- gosb
  level_id           integer,     -- id ГОСБ
  org_type           varchar,     -- inn
  org_id             bigint,      -- ИНН
  ul_outflow_qty     bigint,
  fl_outflow_qty     bigint,
  fot_outflow_amt    numeric,
  current_fot_amt    numeric,
  fot_y_1_diff_amt   numeric,
  current_fl_qty     bigint,
  fl_y_1_diff_qty    bigint,
  emp_potential_qty  numeric,
  fot_potential_amt  numeric,
  total_emp_qty      numeric,
  zp_fl_perc         numeric,
  new_fl_cnt         bigint,
  np_cnt             bigint,
  modified_dttm      timestamp
);

-- ============ Воронка продаж / активности по задачам ============

CREATE TABLE uzp_dwh_sale_funnel_task (
  report_dt              date,
  tb_id                  integer,
  tb_name                varchar,
  gosb_id                integer,
  gosb_name              varchar,
  inn                    bigint,
  company_name           varchar,
  segment_name           varchar,
  task_type              varchar,
  task_subtype           varchar,
  task_category          varchar,
  task_code              varchar,
  task_create_dt         date,
  fact_close_task_dttm   timestamp,   -- факт. закрытие задачи (для «закрыто в день создания»)
  is_task_closed         boolean,
  is_task_closed_success boolean,
  is_task_in_progress    boolean,
  task_text_status       varchar,
  isu_struct_saphr_id    bigint,      -- табельный автора (для различения сотрудников)
  role_code              varchar,     -- МЗП / СЗП / МКК
  last_active_type       varchar,     -- Звонок / Встреча
  last_active_status     varchar,
  last_active_dttm       timestamp,
  unrealized_deal_potential integer,
  deal_code              varchar,     -- код сделки (есть, если сделка заведена)
  deal_create_dttm       timestamp,   -- дата создания СДЕЛКИ (не задачи!)
  plan_staff_deal_qty    integer,
  fact_staff_deal_qty    integer,
  task_text              varchar,     -- текст задачи
  task_comment           varchar,     -- комментарий по отработке (свободный текст)
  task_questionnaire     varchar      -- чек-лист/анкета по задаче
);

-- ============ Справочник компаний: сегмент по ИНН ============
-- Маппинг сегментов (extended_dim_1) на короткие названия зашит в коде отчёта.

CREATE TABLE uzp_dim_company (
  epk_id                   bigint,
  company_name             text,
  inn                      bigint PRIMARY KEY,   -- ИНН — ключ поиска сегмента
  kpp                      text,
  segment_name             text,                 -- большое имя сегмента
  holding_name             text,
  mzp_last_action_dt       date,
  km_last_action_dt        date,
  crm_client_id            text,
  agrmnt_flag              smallint,
  rko_flag                 smallint,
  dbo_flag                 smallint,
  credit_flag              smallint,
  deposit_flag             smallint,
  corporate_card_flag      smallint,
  internet_acquiring_flag  smallint,
  merchant_acquiring_flag  smallint,
  significance_level_id    smallint,
  info                     text,
  modified_dttm            timestamp
);

-- ============ Эталонная база закрепления ИУП (СПОД) ============
-- Перечень организаций, с которыми можно работать: грейн (ГОСБ, ИНН).
-- gosb_id здесь — НОВЫЙ идентификатор ГОСБ (uzp_dim_gosb.new_gosb_id).
-- Организации вне этой базы в рекомендации дэша не попадают вообще.

CREATE TABLE uzp_dim_mzp_reference_base (
  gosb_id        integer,     -- new_gosb_id
  inn            bigint,      -- ИНН компании
  main_pos_id    bigint,      -- основная штатная единица сотрудника
  reserve_pos_id bigint,      -- резервная штатная единица
  actual_dt      date,        -- дата актуальности эталонной базы (срезов несколько)
  is_q_ref_base  boolean,     -- признак эталонной базы квартала
  inserted_dttm  timestamp,
  author_login   text
);


-- ============ Месячный факт оттока (отдельная витрина) ============
-- Дэш tb_health её не использует: модель оттока он строит по
-- uzp_dwh_company_holding_metric. Синтетика её всё равно наполняет, потому что
-- forecast_lab перебирает и варианты модели ПО ЭТОЙ витрине, а непроверенных
-- веток алгоритма быть не должно.
-- Ключевая особенность прома, которую воспроизводим: здесь отток заполнен ГУЩЕ,
-- чем fl_outflow_qty в company_holding_metric.

CREATE TABLE uzp_dwh_fact_outflow (
  report_dt            date,
  tb_id                integer,
  gosb_id              integer,
  inn                  bigint,
  segment_name         varchar,
  is_force             boolean,
  mzp_fio              varchar,
  saphr_id             bigint,
  calc_fl_qty          integer,   -- расчётная численность за период
  prev_m_overflow_qty  integer,
  plan_payee_qty       integer,   -- плановое количество получателей
  fact_payee_qty       integer,   -- фактическое количество получателей
  outflow_qty          integer,   -- перестали быть ЗП-клиентами
  outflow_perc         numeric,
  other_inn_emp_perc   numeric,
  m_avg_salary_amt     numeric,
  prev_m_avg_salary_amt numeric,
  next_m_avg_salary_amt numeric,   -- смотрит ВПЕРЁД: forecast_lab её не читает
  prev_m_fl_val        integer,
  next_m_fl_val        integer,    -- смотрит ВПЕРЁД: forecast_lab её не читает
  is_task              boolean,
  -- Территориальная привязка по ОКТМО. Разрезы «по территориям» строятся отсюда.
  -- На проме oktmo_subject_code НЕГОДЕН: длина 0-2, среди значений «"0», «М»,
  -- «П», «tr» — это мусор загрузки, а не код субъекта. Синтетика воспроизводит
  -- его таким же намеренно: ветка «код субъекта негоден, берём substr(oktmo,1,2)»
  -- обязана проверяться снаружи, а не открыться на проме.
  is_oktmo             boolean,
  oktmo_subject_code   varchar,
  oktmo_subject_district_code      varchar,
  oktmo_subject_district_city_code varchar,
  oktmo                varchar,
  client_communication_infopovod   varchar,
  inserted_dttm        timestamp,
  author_login         text
);

-- ============ Возвраты оттока ============
-- Кто из ушедших вернулся. Грейн тот же, что у uzp_dwh_fact_outflow, связь —
-- (report_dt, gosb_id, inn). Дэш считает «отток, который НЕ вернулся»:
--     outflow_qty − COALESCE(return_qty, 0)
-- Поэтому строки здесь обязаны ссылаться на реально существующие строки оттока;
-- сгенерированные независимо, они дали бы пустой join и молча нулевой блок.

CREATE TABLE uzp_data_outflow_return_detail (
  report_dt                 date,
  tb_id                     integer,
  gosb_id                   integer,
  inn                       bigint,
  segment_name              varchar,
  is_outflow_task           boolean,
  outflow_qty               integer,   -- сколько ушло (копия строки оттока)
  is_outflow_return_success boolean,
  return_qty                integer,   -- сколько из них вернулось, <= outflow_qty
  inserted_dttm             timestamp
);

-- ============ Доп. атрибуты по ключевым клиентам ============
-- Единственный источник в витринах, где названы БАНКИ-КОНКУРЕНТЫ и кэптивный банк.
-- Ключевая особенность прома, которую воспроизводим: витрина покрывает ТОЛЬКО
-- ключевых клиентов (на проме ~16.7 тыс. ИНН против ~88 тыс. в витрине оттока).
-- Код обязан считать и показывать это покрытие: без него блок конкурентов
-- выглядит как «конкурентов почти нет», хотя на самом деле их просто не спросили.
-- Колонок на проме под сотню; здесь заведены те, которые реально читаются.

CREATE TABLE uzp_data_key_client_info_add_attr (
  report_dt              date,
  inn                    bigint,
  tb_id                  integer,
  gosb_id                integer,
  segment_name           varchar,   -- БОЛЬШОЕ имя сегмента («Рег. госсектор»)
  industry_name          varchar,
  company_name           varchar,
  holding_name           varchar,
  holding_strategy_name  varchar,   -- отток | привлечение | удержание
  bank_competitor        varchar,   -- основные банки-конкуренты
  captive_bank_name      varchar,
  is_key_client          boolean,
  inn_current_fl_qty     bigint,
  inn_emp_potential_qty  numeric,
  modified_dttm          timestamp
);

-- ============ Справочники сотрудников и ЕПК ============
-- Дэшем пока НЕ используются: заведены под будущие разрезы (закрепление
-- сотрудника за клиентом, консолидированная карточка ЕПК, штатное расписание).
-- Наполняются минимально-правдоподобно, чтобы схема открытого контура совпадала
-- с промом и запросы к ним не падали на «нет такой таблицы».

CREATE TABLE uzp_data_emp_epk_assignment (
  epk_id            bigint,
  saphr_id          bigint,
  post_id           bigint,
  pos_id            bigint,
  role_id           integer,
  start_dttm        timestamp,
  end_dttm          timestamp,
  balance_unit_code varchar,
  tb_id             integer,
  gosb_id           integer,
  gosb_code         varchar,
  sap_gosb_code     varchar,
  vsp_code          varchar,
  modified_dttm     timestamp
);

CREATE TABLE uzp_data_epk_consolidation (
  epk_id           bigint,
  epk_create_dttm  timestamp,
  client_type_id   integer,
  client_type_name varchar,
  industry_id      integer,
  industry_name    varchar,
  inn              bigint,
  kpp              varchar,
  ogrn             varchar,
  okato            varchar,
  oktmo            varchar,
  old_epk_id       bigint,
  segment_id       integer,
  segment_name     varchar,
  tb_id            integer,
  gosb_id          integer,
  full_name        varchar,
  short_name       varchar,
  is_active        boolean,
  inserted_dttm    timestamp
);

CREATE TABLE uzp_dwh_sap_staff_emp (
  report_dt               date,
  saphr_id                bigint,
  fio                     varchar,
  post_id                 bigint,
  post_name               varchar,
  pos_id                  bigint,
  pos_name                varchar,
  tb_code                 varchar,
  tb_id                   integer,
  gosb_code               varchar,
  sap_gosb_code           varchar,
  gosb_id                 integer,
  city                    varchar,
  post_total_experience_ym numeric,
  is_actual               boolean,
  inserted_dttm           timestamp
);

-- ============ Пайплайн: помесячная раскладка плана привлечения ============
-- Схема ОТДЕЛЬНАЯ (__SCHEMA_T__), как на проме.
-- В uzp_dwh_sale_funnel_task.plan_staff_deal_qty план размазан на все 3 месяца
-- жизни сделки; помесячная разбивка есть только здесь. Ключ связи —
-- coalesce(funnel.deal_code, funnel.task_code) = pl_task_deal_code.

CREATE TABLE __SCHEMA_T__.yva_pl_task_deal_code (
  pl_task_deal_code text,        -- код оффера или сделки в пайплайне
  pl_month_num      integer,     -- номер месяца (1..12), когда зайдут ФЛ и ФОТ
  pl_plan_fot_amt   bigint,      -- сколько ФОТа зайдёт за этот месяц
  pl_plan_np_amt    bigint,      -- сколько ФЛ зайдёт за этот месяц
  PRIMARY KEY (pl_task_deal_code, pl_month_num)
);

-- ============ Факт привлечения по сделкам, помесячно ============
-- Витрина премирования МЗП. Для дэша важны четыре вещи:
--   report_dt  — МЕСЯЦ ИЗ ПАЙПЛАЙНА, на который сделка обещала привлечение;
--   sales_amt  — сколько НП по ней реально пришло (факт продаж, количество);
--   (gosb_id, inn, saphr_id) — грейн сравнения плана с фактом.
-- Сравнивать надо АГРЕГАТ: сотрудник мог завести две сделки по одной организации и
-- обе запланировать на один месяц — факт месяца относится к их сумме, а не к каждой.
-- metric_id = 1000636 («Новые получатели b2b»), учитываются строки product_cmnt='учтено'.

CREATE TABLE uzp_data_mzp_motivation_detail_corr (
  report_dt                date,        -- отчётный месяц (месяц из пайплайна)
  tb_id                    smallint,
  gosb_id                  integer,
  inn                      bigint,
  company_name             text,
  segment_name             text,
  agrmnt_num               integer,
  saphr_id                 bigint,      -- табельный сотрудника (= isu_struct_saphr_id)
  position_name            text,
  metric_id                bigint,
  product_group_name       text,
  product_id               bigint,
  product_name             text,
  sales_amt                numeric,     -- ФАКТ продаж, количество
  up_weight                numeric,
  sales_prediction_percent numeric,
  up_sales_amt             numeric,
  consultation_start_dt    date,
  consultation_success_dt  date,
  product_cmnt             text,        -- 'учтено' | 'не соответствует критериям учета' | …
  is_motiv                 boolean,
  is_fraud                 boolean,
  metric_name              varchar,
  sale_plan_amt            numeric,
  sale_prediction_amt      numeric,
  up_sale_prediction_amt   numeric,
  kpp                      varchar,
  deal_code                varchar,
  offer_code               varchar,
  task_code                varchar,
  ul_epk_id                bigint,
  fl_epk_id                bigint,
  sale_approve_dt          timestamp,
  calc_dttm                timestamp,
  inserted_dttm            timestamp,
  author_login             text
);

-- Индексы под запросы дэшей
CREATE INDEX ix_metrics_lookup ON uzp_dwh_metrics (metric_id, level_name, period_type, end_dt);
CREATE INDEX ix_chm_gosb ON uzp_dwh_company_holding_metric (level_id, report_dt);
CREATE INDEX ix_funnel_inn ON uzp_dwh_sale_funnel_task (inn);
CREATE INDEX ix_gosb_tb ON uzp_dim_gosb (tb_id);
CREATE INDEX ix_ref_base ON uzp_dim_mzp_reference_base (gosb_id, inn);
CREATE INDEX ix_chm_hist ON uzp_dwh_company_holding_metric (org_id, level_id, report_dt);
CREATE INDEX ix_motiv ON uzp_data_mzp_motivation_detail_corr (tb_id, metric_id, report_dt);
CREATE INDEX ix_fact_outflow ON uzp_dwh_fact_outflow (report_dt, gosb_id, inn);
CREATE INDEX ix_outflow_return ON uzp_data_outflow_return_detail (report_dt, gosb_id, inn);
