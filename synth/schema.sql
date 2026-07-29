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

-- ============ Ежедневный отток текущего (незакрытого) месяца ============
-- Витрина живёт ТОЛЬКО за текущий месяц: один report_dt (конец месяца) и один
-- act_dt (максимальная дата ЗП-зачислений в дневной ведомости). Поэтому именно
-- она задаёт «сегодня» для дэша: ref_cur = max(report_dt), ref_closed = ref_cur - 1 мес.
-- Грейн: (report_dt, act_dt, gosb_id, org_inn, payment_order_num) — по строке на
-- каждую выплатную дату месяца (обычно аванс + основная).
-- outflow_unpaid_m_qty — ФЛ, не получившие выплату с начала месяца по act_dt,
-- т.е. те, кто на эту дату уже должен был зачислиться, но не зачислился.

CREATE TABLE uzp_dwh_day_outflow (
  row_code                         varchar,     -- report_dt_act_dt_gosb_inn_order
  report_dt                        date,        -- отчётный месяц (конец месяца)
  act_dt                           date,        -- по какую дату есть факт зачислений
  tb_id                            integer,
  gosb_id                          integer,
  org_inn                          bigint,
  segment_name                     varchar,     -- КОРОТКОЕ имя сегмента (ММБ/КСБ/…)
  company_name                     varchar,
  holding_name                     varchar,
  is_security_force                boolean,
  saphr_id                         bigint,
  salary_payment_dt                date,        -- дата ЗП-выплаты этой строки
  payment_order_num                smallint,    -- порядковый номер выплаты в месяце
  expect_fl_qty                    integer,
  overflow_qty                     integer,
  plan_fl_qty                      integer,
  fl_day_qty                       integer,
  fl_2_d_qty                       integer,
  fact_fl_qty                      integer,     -- получили выплату с начала месяца по act_dt
  outflow_unpaid_report_qty        integer,
  outflow_unpaid_2_d_qty           integer,
  outflow_unpaid_m_qty             integer,     -- ключевая метрика оттока месяца
  outflow_day_perc                 numeric,
  outflow_2_d_perc                 numeric,
  outflow_unpaid_m_perc            numeric,
  overflow_other_inn_perc          numeric,
  m_avg_salary_amt                 numeric,     -- средняя ЗП в текущем месяце
  prev_m_avg_salary_amt            numeric,
  next_m_avg_salary_amt            numeric,
  fl_crnt_m_qty                    integer,
  fl_prev_m_qty                    integer,     -- ФЛ в предыдущем (закрытом) месяце
  fl_next_m_qty                    integer,
  is_d_outflow_task                boolean,
  client_communication_infopovod   varchar,
  is_oktmo                         boolean,
  oktmo_subject_code               varchar,
  oktmo_subject_district_code      varchar,
  oktmo_subject_district_city_code varchar,
  oktmo_code                       varchar,
  inserted_dttm                    timestamp,
  author_login                     text
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

-- Индексы под запросы дэшей
CREATE INDEX ix_metrics_lookup ON uzp_dwh_metrics (metric_id, level_name, period_type, end_dt);
CREATE INDEX ix_chm_gosb ON uzp_dwh_company_holding_metric (level_id, report_dt);
CREATE INDEX ix_funnel_inn ON uzp_dwh_sale_funnel_task (inn);
CREATE INDEX ix_gosb_tb ON uzp_dim_gosb (tb_id);
CREATE INDEX ix_ref_base ON uzp_dim_mzp_reference_base (gosb_id, inn);
CREATE INDEX ix_chm_hist ON uzp_dwh_company_holding_metric (org_id, level_id, report_dt);
CREATE INDEX ix_day_outflow ON uzp_dwh_day_outflow (report_dt, act_dt, gosb_id);
