-- DDL открытого контура: повторяет пром ТОЧЬ-В-ТОЧЬ (та же схема и имена),
-- чтобы SQL дэшей был идентичен в обоих контурах.
-- __SCHEMA__ заменяется на имя схемы при применении (reference.py / setup-ноутбук).

CREATE SCHEMA IF NOT EXISTS __SCHEMA__;
SET search_path TO __SCHEMA__;

DROP TABLE IF EXISTS uzp_dim_gosb CASCADE;
DROP TABLE IF EXISTS uzp_dim_metric CASCADE;
DROP TABLE IF EXISTS uzp_dwh_metrics CASCADE;
DROP TABLE IF EXISTS uzp_dwh_company_holding_metric CASCADE;
DROP TABLE IF EXISTS uzp_dwh_sale_funnel_task CASCADE;
DROP TABLE IF EXISTS uzp_dim_company CASCADE;
DROP TABLE IF EXISTS uzp_dim_mzp_reference_base CASCADE;

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
  is_task_closed         boolean,
  is_task_closed_success boolean,
  is_task_in_progress    boolean,
  task_text_status       varchar,
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

-- Индексы под запросы дэшей
CREATE INDEX ix_metrics_lookup ON uzp_dwh_metrics (metric_id, level_name, period_type, end_dt);
CREATE INDEX ix_chm_gosb ON uzp_dwh_company_holding_metric (level_id, report_dt);
CREATE INDEX ix_funnel_inn ON uzp_dwh_sale_funnel_task (inn);
CREATE INDEX ix_gosb_tb ON uzp_dim_gosb (tb_id);
CREATE INDEX ix_ref_base ON uzp_dim_mzp_reference_base (gosb_id, inn);
