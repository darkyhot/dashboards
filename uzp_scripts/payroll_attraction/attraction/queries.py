"""Весь SQL отчёта. Ни одной строки SQL за пределами этого файла.

Плейсхолдеры `{schema}` (основная схема) и `{schema_t}` (техническая: архив НФЛ,
клики СБОЛ) подставляет `db.render`; значения — именованными параметрами `:name`.

Ловушки
-------
1. **Большие витрины читаются один раз.** Ведомости `mis_data_payroll_m` —
   узкой копией `t_pay`, оператор на месяц (`report_dt = :m`); всё остальное —
   из копии. Клики и операции ВСП — одним проходом, сразу сжатые до «ФЛ × месяц».
2. **Номер организации.** В ведомостях — text, в НФЛ и справочниках — bigint.
   Приведение — только под маской цифр (`MASK`), иначе одно «Не определено»
   роняет весь запрос.
3. **Диалект 9.4.** Нет `make_interval`, `last_day()` (своя функция заказчика —
   вместо неё `date_trunc` и interval), `GROUPING SETS`.
4. **count(DISTINCT) по большим таблицам** не используется: двухступенчатая
   группировка (сначала до ключа, потом count(*)).
5. **ГОСБ.** Ведомости, НФЛ и воронка продаж кодируют ГОСБ одинаково «сыро»;
   перекодировка заказчика (`GOSB_FIX`) применяется ко всем трём, иначе ячейка
   ведомостей и сделка не сойдутся по ГОСБ.
"""
from __future__ import annotations

from . import segments as S

MASK = "'^[0-9]{1,12}$'"
AMT_MIN = 2500
BAD_INNS = "('7707083893', 'Не определено', '0', '-1')"
KSB_RAW = ("('СКБ-Средние','СКБ-Прочее','СКБ-Крупные','Средний бизнес','Крупный бизнес',"
           "'ККСБ - прочие', 'ККСБ - Прочие')")

VSP_GROUPS = "('ВИП','ВСП','ПРЕМЬЕР')"
VSP_PRODUCTS = """(
        'Оформление заявления на перевод заработной платы на счет, открытый в Сбербанке',
        'Заявка на перевод заработной платы на счет, открытый в Сбербанке',
        'Заявление на перевод заработной платы (оформление)',
        'Пилот - Активный получатель заработной платы по заявлению на перевод заработной платы на счет открытый в Сбербанке'
    )"""
VSP_EXCLUDED_OP = "'Зачисление заработной платы на счет в Сбербанке (оформление)'"
MZP_POS = "Менеджер по продаже зарплатных проектов"

# Каналы: код, приоритет при равной дате действия, подпись.
CHANNELS = [("mzp", 1, "МЗП"), ("vsp", 2, "ВСП"), ("dig", 3, "Digital"), ("other", 9, "Остальное")]


def gosb_fix(gosb: str, tb: str, seg: str | None = None) -> str:
    """Перекодировка ГОСБ заказчика. Правило по сегменту КСБ — только где сегмент есть
    (в ведомостях); у НФЛ и сделок его нет, там действуют три первых правила."""
    seg_rule = (f"\n         WHEN {seg} IN {KSB_RAW} AND {gosb} = 8591 THEN 8646" if seg else "")
    return (f"CASE WHEN {tb} = 38 THEN 9038\n         WHEN {gosb} = 1009 THEN 8557\n"
            f"         WHEN {gosb} = 0 AND {tb} = 40 THEN 9040{seg_rule}\n         ELSE {gosb} END")


def valid_to(d: str) -> str:
    """Конец действия: последний день месяца (d + 2 месяца). 15 января → 31 марта."""
    return f"CAST(date_trunc('month', {d}) + interval '3 month' - interval '1 day' AS date)"


# --------------------------------------------------------------------------- #
# Разведка
# --------------------------------------------------------------------------- #
PROBE_TABLE = """
SELECT count(*) AS n_cols
FROM information_schema.columns
WHERE table_schema = :schema AND table_name = :table
"""
PROBE_TEMP = "CREATE TEMP TABLE t_probe_tmp AS SELECT 1 AS x"
DROP_PROBE_TEMP = "DROP TABLE IF EXISTS t_probe_tmp"

# --------------------------------------------------------------------------- #
# Временные таблицы
# --------------------------------------------------------------------------- #
CREATE_TMP = "CREATE TEMP TABLE {name} AS\n{body}\nDISTRIBUTED BY ({dist})"
CREATE_TMP_PLAIN = "CREATE TEMP TABLE {name} AS\n{body}"
INSERT_TMP = "INSERT INTO {name}\n{body}"
ANALYZE_TMP = "ANALYZE {name}"
DROP_TMP = "DROP TABLE IF EXISTS {name}"

# Справочник организаций, свёрнутый до номера организации. Названия — только
# колонками `org_name` / `holding_name`: каждая выборка проходит маску ФИО.
T_ORG = """
SELECT e.inn,
       min(""" + S.seg_case("e.segment_name") + """) AS seg,
       min(e.company_name)                        AS org_name,
       min(NULLIF(btrim(e.holding_name), ''))     AS holding_name,
       min(e.industry_name)                       AS industry_name
FROM {schema}.uzp_data_epk_consolidation e
WHERE e.inn IS NOT NULL
GROUP BY e.inn
"""

# Организации, которые идут в портфель БЕЗ порога суммы (условие 3 заказчика):
# образовательные и холдинг :exc_holding.
T_EXC = """
SELECT inn_exc FROM (
  SELECT CAST(CAST(d.inn AS text) AS bigint) AS inn_exc
  FROM {schema}.uzp_dim_education_organization d
  WHERE CAST(d.inn AS text) ~ """ + MASK + """
  UNION
  SELECT c.inn
  FROM {schema}.uzp_data_epk_consolidation c
  WHERE c.reference_holding_name = :exc_holding AND c.inn IS NOT NULL
) t
"""

# ЕДИНСТВЕННОЕ обращение к ведомостям: узкая копия месяца :m с перекодировками
# заказчика (ГОСБ, «Не определено», сегмент).
T_PAY_MONTH = """
SELECT p.report_dt,
       p.epk_id,
       """ + gosb_fix("p.sys_gosb_id", "p.sys_tb_id", "p.segment_name") + """ AS sys_gosb_id,
       CASE WHEN p.inn = 'Не определено' THEN '0' ELSE p.inn END AS inn,
       p.sys_tb_id,
       p.amt,
       p.enrollment_type,
       p.enrollment_kind_descr,
       p.market_share_flag_name,
       CASE WHEN p.segment_name IN """ + KSB_RAW + """ THEN 'КСБ'
            WHEN p.segment_name IN ('ОПК','Клиенты машиностроения','СКМ','Прочие','УРКМ') THEN 'СКМ'
            WHEN p.segment_name IN ('Малый бизнес','Микробизнес','БМО','УМБ-Малые','УМБ-Микро') THEN 'ММБ'
            WHEN p.segment_name IN ('Крупнейший бизнес','CIB') THEN 'КФИ'
            WHEN p.segment_name IN ('Государственный сектор') THEN 'РГС'
            ELSE p.segment_name END AS segment_name
FROM {schema}.mis_data_payroll_m p
WHERE p.report_dt = CAST(:m AS date)
"""

# Признак relev заказчика (дословно).
_RELEV = """CASE WHEN ((r.enrollment_kind_descr = 'Основные' AND r.report_dt >= DATE '2024-01-01')
                    OR (r.report_dt BETWEEN DATE '2023-11-01' AND DATE '2023-12-31'))
                 AND ((r.market_share_flag_name <> 'ИП 1 чел.' AND r.report_dt >= DATE '2024-01-01')
                    OR (r.report_dt BETWEEN DATE '2023-11-01' AND DATE '2023-12-31'))
                 AND r.inn NOT IN """ + BAD_INNS + """
            THEN 1 ELSE 0 END"""
_INN_R = "CASE WHEN r.inn ~ " + MASK + " THEN CAST(r.inn AS bigint) END"

# Численность ячейки «ГОСБ × организация» за все месяцы копии ОДНИМ запросом:
# логика is_salary_client заказчика (sum_pf по ФЛ × ГОСБ × организации > порога,
# или вид зачисления 2, или организация без порога), ФЛ — двухступенчато.
# Номер организации, не прошедший маску, — ключ −1 (одна ячейка «непригодные»).
T_CELL = """
SELECT report_dt, sys_gosb_id, inn, count(*) AS n_fl, max(tb_id) AS tb_id
FROM (
  SELECT y.report_dt, y.sys_gosb_id, COALESCE(y.inn_id, -1) AS inn, y.epk_id, max(y.sys_tb_id) AS tb_id
  FROM (
    SELECT x.*,
           sum(CASE WHEN x.is_pf AND x.relev = 1 THEN x.amt ELSE 0 END)
             OVER (PARTITION BY x.report_dt, x.epk_id, x.sys_gosb_id, x.inn) AS sum_pf
    FROM (
      SELECT r.report_dt, r.epk_id, r.sys_gosb_id, r.inn, r.sys_tb_id, r.amt,
             """ + _INN_R + """ AS inn_id,
             """ + _RELEV + """ AS relev,
             COALESCE(d.is_portfolio_enrollment, false) AS is_pf,
             d.enrollment_type_id,
             e.inn_exc
      FROM t_pay r
      LEFT JOIN {schema}.uzp_dim_enrollment_type d ON d.enrollment_type_id = r.enrollment_type
      LEFT JOIN t_exc e ON e.inn_exc = """ + _INN_R + """
    ) x
  ) y
  WHERE y.relev = 1 AND y.amt > 0 AND y.epk_id IS NOT NULL
    AND ((y.is_pf AND y.sum_pf > :amt_min)
         OR y.enrollment_type_id = 2
         OR (y.is_pf AND y.inn_exc IS NOT NULL))
  GROUP BY y.report_dt, y.sys_gosb_id, COALESCE(y.inn_id, -1), y.epk_id
) z
GROUP BY report_dt, sys_gosb_id, inn
"""

# Классы ячеек для двух пар лет: 'cur' = :c_prev → :c_cur, 'prev' = :c_prev2 → :c_prev.
#   grow (рост, n_c > n_b > 0), new (новая, 0 → n_c), decl, closed, flat.
_CLS_PAIR = """
SELECT '{tag}' AS pair,
       COALESCE(c.sys_gosb_id, b.sys_gosb_id) AS gosb_id,
       COALESCE(c.inn, b.inn)                 AS inn,
       COALESCE(c.tb_id, b.tb_id)             AS tb_id,
       COALESCE(b.n_fl, 0)                    AS n_b,
       COALESCE(c.n_fl, 0)                    AS n_c
FROM (SELECT * FROM t_cell WHERE report_dt = CAST(:{b} AS date)) b
FULL OUTER JOIN (SELECT * FROM t_cell WHERE report_dt = CAST(:{c} AS date)) c
  ON c.sys_gosb_id = b.sys_gosb_id AND c.inn = b.inn
"""
T_CLS = """
SELECT f.*,
       CASE WHEN f.n_b = 0 THEN 'new' WHEN f.n_c = 0 THEN 'closed'
            WHEN f.n_c > f.n_b THEN 'grow' WHEN f.n_c < f.n_b THEN 'decl' ELSE 'flat' END AS cls
FROM (""" + _CLS_PAIR.format(tag="cur", b="c_prev", c="c_cur") + "UNION ALL" + \
    _CLS_PAIR.format(tag="prev", b="c_prev2", c="c_prev") + """) f
"""

# НФЛ: архив (до :nfl_split включительно) + текущая витрина — как у заказчика.
# Месяцы НФЛ — :nfl_months (янв–авг обоих лет). ГОСБ перекодируется так же,
# как в ведомостях. ФЛ без ЕПК в атрибуцию не идут — их число печатается.
_NFL_SRC = """
  SELECT report_dt, tb_id, gosb_id, inn, fl_epk_id, first_payment_dt, '{src}' AS src
  FROM {tbl}
  WHERE is_nfl AND NOT is_overflow_fl
    AND report_dt {op} CAST(:nfl_split AS date)
    AND report_dt = ANY(CAST(:nfl_months AS date[]))
"""
T_NFL = """
SELECT DISTINCT n.report_dt, n.tb_id,
       """ + gosb_fix("n.gosb_id", "n.tb_id") + """ AS gosb_id,
       n.inn, n.fl_epk_id AS epk_id, n.first_payment_dt, n.src
FROM (""" + _NFL_SRC.format(src="arch", tbl="{schema_t}.bkv_bkv_uzp_data_nfl_channel", op="<=") + \
    "  UNION ALL" + _NFL_SRC.format(src="cur", tbl="{schema}.uzp_data_nfl_channel", op=">") + """) n
WHERE n.fl_epk_id IS NOT NULL
"""
NFL_NO_EPK = """
SELECT src, count(*) AS n FROM (""" + _NFL_SRC.format(src="arch", tbl="{schema_t}.bkv_bkv_uzp_data_nfl_channel", op="<=") + \
    "  UNION ALL" + _NFL_SRC.format(src="cur", tbl="{schema}.uzp_data_nfl_channel", op=">") + """) n
WHERE n.fl_epk_id IS NULL
GROUP BY src
"""

T_NKEY = "SELECT DISTINCT epk_id FROM t_nfl"

# Консультации ВСП (фильтры заказчика). Сразу сжаты до «ФЛ × месяц → самая ранняя
# дата»: раннее действие месяца не хуже позднего (конец окна зависит только от
# месяца), поэтому этого достаточно — это и есть DISTINCT ON заказчика.
T_VSP = """
SELECT t.epk_id,
       min(CAST(t.src_report_dt AS date)) AS act_dt,
       """ + valid_to("min(CAST(t.src_report_dt AS date))") + """ AS valid_to
FROM {schema}.asm_data_operation t
JOIN t_nkey k ON k.epk_id = t.epk_id
WHERE t.sales_channel_group IN """ + VSP_GROUPS + """
  AND t.fraud_type = 0
  AND t.calc_product IN """ + VSP_PRODUCTS + """
  AND t.src_report_dt BETWEEN CAST(:act_from AS date) AND CAST(:act_to AS date)
  AND t.src_operation_name != """ + VSP_EXCLUDED_OP + """
GROUP BY t.epk_id, date_trunc('month', t.src_report_dt)
"""

# Консультации СБОЛ (Digital): то же сжатие.
T_DIG = """
SELECT c.epk_id,
       min(CAST(c.data_timestamp AS date)) AS act_dt,
       """ + valid_to("min(CAST(c.data_timestamp AS date))") + """ AS valid_to
FROM {schema_t}.ml_ksa_clickstream_events_oaa c
JOIN t_nkey k ON k.epk_id = c.epk_id
WHERE c.data_timestamp >= CAST(:act_from AS date)
  AND c.data_timestamp < CAST(:act_to AS date) + 1
GROUP BY c.epk_id, date_trunc('month', c.data_timestamp)
"""

# Сделки МЗП: deal_code есть, роль МЗП; одна строка на сделку — из последнего
# снимка, где она есть (там актуальный факт).
T_DEAL = """
SELECT z.deal_code, z.deal_dt, """ + valid_to("z.deal_dt") + """ AS valid_to,
       z.inn, """ + gosb_fix("z.gosb_id", "z.tb_id") + """ AS gosb_id, z.tb_id,
       z.emp_id, z.plan_qty, z.fact_qty
FROM (
  SELECT f.deal_code, CAST(f.deal_create_dttm AS date) AS deal_dt, f.inn, f.gosb_id, f.tb_id,
         f.isu_struct_saphr_id AS emp_id,
         f.plan_staff_deal_qty AS plan_qty, f.fact_staff_deal_qty AS fact_qty,
         row_number() OVER (PARTITION BY f.deal_code ORDER BY f.report_dt DESC) AS rn
  FROM {schema}.uzp_dwh_sale_funnel_task f
  WHERE f.deal_code IS NOT NULL
    AND f.role_code = 'МЗП'
    AND f.deal_create_dttm >= CAST(:act_from AS date)
    AND f.deal_create_dttm < CAST(:act_to AS date) + 1
) z
WHERE z.rn = 1
"""

# Все действия, действующие на дату первой ЗП: act_dt <= first_payment_dt <= valid_to.
# МЗП — по организации И ГОСБ, ВСП и Digital — по ФЛ.
_NK = "n.report_dt, n.epk_id, n.inn, n.gosb_id, n.first_payment_dt"
T_ACT = """
SELECT """ + _NK + """, 'mzp' AS ch, 1 AS prio, d.deal_dt AS act_dt, d.deal_code AS act_key
FROM t_nfl n
JOIN t_deal d ON d.inn = n.inn AND d.gosb_id = n.gosb_id
 AND d.deal_dt <= n.first_payment_dt AND n.first_payment_dt <= d.valid_to
UNION ALL
SELECT """ + _NK + """, 'vsp', 2, v.act_dt, CAST(NULL AS varchar)
FROM t_nfl n
JOIN t_vsp v ON v.epk_id = n.epk_id
 AND v.act_dt <= n.first_payment_dt AND n.first_payment_dt <= v.valid_to
UNION ALL
SELECT """ + _NK + """, 'dig', 3, g.act_dt, CAST(NULL AS varchar)
FROM t_nfl n
JOIN t_dig g ON g.epk_id = n.epk_id
 AND g.act_dt <= n.first_payment_dt AND n.first_payment_dt <= g.valid_to
"""

# НФЛ с каналом: побеждает САМОЕ РАННЕЕ действующее действие (при равной дате —
# МЗП, ВСП, Digital). Без действия — Остальное. Флаги «был ли канал» — для
# пересечений; лаг — месяцев от действия до первой ЗП. Разрезы — по справочнику
# организаций; класс ячейки — пара лет, к которой относится год НФЛ.
_JK = ("w.report_dt = n.report_dt AND w.epk_id = n.epk_id AND w.inn = n.inn "
       "AND w.gosb_id = n.gosb_id AND w.first_payment_dt = n.first_payment_dt")
T_NFLC = """
SELECT n.report_dt, n.tb_id, n.gosb_id, n.inn, n.epk_id, n.first_payment_dt, n.src,
       CASE WHEN n.report_dt = ANY(CAST(:m_cur AS date[])) THEN 'cur' ELSE 'prev' END AS yr,
       COALESCE(w.ch, 'other') AS ch,
       w.act_dt, w.act_key,
       CASE WHEN w.act_dt IS NULL THEN NULL
            ELSE (EXTRACT(YEAR FROM n.first_payment_dt) * 12 + EXTRACT(MONTH FROM n.first_payment_dt))
               - (EXTRACT(YEAR FROM w.act_dt) * 12 + EXTRACT(MONTH FROM w.act_dt)) END AS lag_m,
       COALESCE(f.has_mzp, false) AS has_mzp,
       COALESCE(f.has_vsp, false) AS has_vsp,
       COALESCE(f.has_dig, false) AS has_dig,
       COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg,
       o.holding_name, o.industry_name,
       c.cls
FROM t_nfl n
LEFT JOIN (
  SELECT a.*, row_number() OVER (PARTITION BY a.report_dt, a.epk_id, a.inn, a.gosb_id, a.first_payment_dt
                                 ORDER BY a.act_dt, a.prio, a.act_key) AS rn
  FROM t_act a
) w ON """ + _JK + """ AND w.rn = 1
LEFT JOIN (
  SELECT report_dt, epk_id, inn, gosb_id, first_payment_dt,
         bool_or(ch = 'mzp') AS has_mzp, bool_or(ch = 'vsp') AS has_vsp, bool_or(ch = 'dig') AS has_dig
  FROM t_act GROUP BY 1, 2, 3, 4, 5
) f ON """ + _JK.replace("w.", "f.") + """
LEFT JOIN t_org o ON o.inn = n.inn
LEFT JOIN t_cls c ON c.pair = CASE WHEN n.report_dt = ANY(CAST(:m_cur AS date[])) THEN 'cur' ELSE 'prev' END
               AND c.gosb_id = n.gosb_id AND c.inn = n.inn
"""

WORKSET_DIST = {"t_org": "inn", "t_exc": "inn_exc", "t_pay": "epk_id", "t_cell": "inn",
                "t_cls": "inn", "t_nfl": "epk_id", "t_nkey": "epk_id", "t_vsp": "epk_id",
                "t_dig": "epk_id", "t_deal": "inn", "t_act": "epk_id", "t_nflc": "epk_id"}
SHOW_ORDER = ["t_org", "t_exc", "t_pay", "t_cell", "t_cls", "t_nfl", "t_nkey", "t_vsp", "t_dig",
              "t_deal", "t_act", "t_nflc"]

# --------------------------------------------------------------------------- #
# Выгрузки (лёгкие — по временным таблицам)
# --------------------------------------------------------------------------- #
# Ячейки: класс × ступень изменения × пара лет.
CELL_SUM = """
SELECT pair, cls,
       CASE WHEN n_c - n_b = 0 THEN '0'
            WHEN abs(n_c - n_b) = 1 THEN '1'
            WHEN abs(n_c - n_b) <= 5 THEN '2-5'
            WHEN abs(n_c - n_b) <= 20 THEN '6-20'
            WHEN abs(n_c - n_b) <= 100 THEN '21-100'
            ELSE '100+' END AS step,
       count(*) AS n_cells, sum(n_b) AS n_b, sum(n_c) AS n_c
FROM t_cls
GROUP BY 1, 2, 3
"""

# Итог портфеля по месяцам копии — сверка: Σ Δ по ячейкам = Δ портфеля.
CELL_TOT = """
SELECT report_dt, count(*) AS n_cells, sum(n_fl) AS n_fl FROM t_cell GROUP BY report_dt
"""

# Ячейки по разрезам: сегмент, холдинг, отрасль, ТБ. Холдинг — колонкой под маску.
_CELL_AGG = """count(*) FILTER (WHERE cls IN ('grow', 'new')) AS n_grow,
       count(*) FILTER (WHERE cls IN ('decl', 'closed')) AS n_decl,
       count(*) FILTER (WHERE cls = 'flat') AS n_flat,
       sum(n_b) AS n_b, sum(n_c) AS n_c,
       sum(CASE WHEN cls IN ('grow', 'new') THEN n_c - n_b ELSE 0 END) AS d_grow,
       sum(CASE WHEN cls IN ('decl', 'closed') THEN n_c - n_b ELSE 0 END) AS d_decl"""
CELL_DIM = """
WITH s AS (
  SELECT c.*, COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg, o.holding_name, o.industry_name
  FROM t_cls c LEFT JOIN t_org o ON o.inn = c.inn
)
SELECT 'seg' AS dim, seg AS key, CAST(NULL AS text) AS holding_name, pair, """ + _CELL_AGG + """
FROM s GROUP BY seg, pair
UNION ALL
SELECT 'holding', NULL, holding_name, pair, """ + _CELL_AGG + """
FROM s GROUP BY holding_name, pair
UNION ALL
SELECT 'industry', industry_name, NULL, pair, """ + _CELL_AGG + """
FROM s GROUP BY industry_name, pair
UNION ALL
SELECT 'tb', CAST(tb_id AS text), NULL, pair, """ + _CELL_AGG + """
FROM s GROUP BY tb_id, pair
"""

# Верх выросших ячеек этого года — с НФЛ этого года по каналам в той же ячейке.
CELL_TOP = """
WITH n AS (
  SELECT gosb_id, inn,
         count(*) AS nfl,
         count(*) FILTER (WHERE ch = 'mzp') AS nfl_mzp,
         count(*) FILTER (WHERE ch = 'vsp') AS nfl_vsp,
         count(*) FILTER (WHERE ch = 'dig') AS nfl_dig,
         count(*) FILTER (WHERE ch = 'other') AS nfl_other
  FROM t_nflc WHERE yr = 'cur' GROUP BY gosb_id, inn
)
SELECT c.gosb_id, c.inn, c.tb_id, c.n_b, c.n_c, c.n_c - c.n_b AS delta, c.cls,
       COALESCE(o.seg, '""" + S.NO_DIM + """') AS seg, o.org_name, o.holding_name, o.industry_name,
       COALESCE(n.nfl, 0) AS nfl, COALESCE(n.nfl_mzp, 0) AS nfl_mzp, COALESCE(n.nfl_vsp, 0) AS nfl_vsp,
       COALESCE(n.nfl_dig, 0) AS nfl_dig, COALESCE(n.nfl_other, 0) AS nfl_other
FROM t_cls c
LEFT JOIN t_org o ON o.inn = c.inn
LEFT JOIN n ON n.gosb_id = c.gosb_id AND n.inn = c.inn
WHERE c.pair = 'cur' AND c.cls IN ('grow', 'new') AND c.inn <> -1
ORDER BY c.n_c - c.n_b DESC, c.inn
LIMIT :top_rows
"""

# НФЛ: месяц × канал, плюс пересечения и лаг.
NFL_MONTH = """
SELECT report_dt, yr, ch, src, count(*) AS n
FROM t_nflc GROUP BY 1, 2, 3, 4
"""
NFL_OVERLAP = """
SELECT yr, ch, has_mzp, has_vsp, has_dig, count(*) AS n
FROM t_nflc GROUP BY 1, 2, 3, 4, 5
"""
NFL_LAG = """
SELECT yr, ch, lag_m, count(*) AS n
FROM t_nflc WHERE ch <> 'other' GROUP BY 1, 2, 3
"""
# НФЛ по классу ячейки (рост / снижение / ноль) и каналу.
NFL_CELL = """
SELECT yr, COALESCE(cls, 'none') AS cls, ch, count(*) AS n
FROM t_nflc GROUP BY 1, 2, 3
"""
# НФЛ по разрезам × канал × год. Холдинг — колонкой под маску.
NFL_DIM = """
SELECT 'seg' AS dim, seg AS key, CAST(NULL AS text) AS holding_name, yr, ch, count(*) AS n
FROM t_nflc GROUP BY seg, yr, ch
UNION ALL
SELECT 'holding', NULL, holding_name, yr, ch, count(*) FROM t_nflc GROUP BY holding_name, yr, ch
UNION ALL
SELECT 'industry', industry_name, NULL, yr, ch, count(*) FROM t_nflc GROUP BY industry_name, yr, ch
UNION ALL
SELECT 'tb', CAST(tb_id AS text), NULL, yr, ch, count(*) FROM t_nflc GROUP BY tb_id, yr, ch
"""

# Сделки МЗП по месяцам создания: число, план, факт, сотрудники (двухступенчато),
# НФЛ, которые сделка «выиграла» в атрибуции.
DEAL_MONTH = """
WITH w AS (SELECT act_key, count(*) AS n_nfl FROM t_nflc WHERE ch = 'mzp' GROUP BY act_key),
d AS (
  SELECT CAST(date_trunc('month', t.deal_dt) + interval '1 month' - interval '1 day' AS date) AS deal_m,
         t.tb_id, t.emp_id, t.plan_qty, t.fact_qty, COALESCE(w.n_nfl, 0) AS n_nfl
  FROM t_deal t LEFT JOIN w ON w.act_key = t.deal_code
)
SELECT deal_m, tb_id, sum(n_deals) AS n_deals, sum(plan_qty) AS plan_qty, sum(fact_qty) AS fact_qty,
       sum(n_nfl) AS n_nfl, count(*) AS n_emp
FROM (
  SELECT deal_m, tb_id, emp_id, count(*) AS n_deals, sum(plan_qty) AS plan_qty,
         sum(fact_qty) AS fact_qty, sum(n_nfl) AS n_nfl
  FROM d GROUP BY deal_m, tb_id, emp_id
) e
GROUP BY deal_m, tb_id
"""

# Привязка сделок по ГОСБ: есть ли ячейка ведомостей с той же организацией и ГОСБ,
# и с той же организацией в другом ГОСБ (сделка «не в своём» ГОСБ).
DEAL_MATCH = """
WITH k AS (SELECT DISTINCT sys_gosb_id AS gosb_id, inn FROM t_cell),
ki AS (SELECT DISTINCT inn FROM t_cell)
SELECT CASE WHEN k.inn IS NOT NULL THEN 'same_gosb'
            WHEN ki.inn IS NOT NULL THEN 'other_gosb'
            ELSE 'no_org' END AS match, count(*) AS n_deals
FROM t_deal d
LEFT JOIN k ON k.gosb_id = d.gosb_id AND k.inn = d.inn
LEFT JOIN ki ON ki.inn = d.inn
GROUP BY 1
"""

# Штат МЗП на последние дни месяцев :staff_dates (запрос заказчика), по ТБ.
STAFF = """
SELECT report_dt, tb_id, count(DISTINCT saphr_id) AS n_staff
FROM {schema}.uzp_dwh_sap_staff_emp
WHERE report_dt = ANY(CAST(:staff_dates AS date[])) AND pos_name = :staff_pos
GROUP BY report_dt, tb_id
"""

# Штат всего — ровно запрос заказчика: сотрудник, числящийся в двух ТБ, — один.
STAFF_TOT = """
SELECT report_dt, count(DISTINCT saphr_id) AS n_staff
FROM {schema}.uzp_dwh_sap_staff_emp
WHERE report_dt = ANY(CAST(:staff_dates AS date[])) AND pos_name = :staff_pos
GROUP BY report_dt
"""

TB_DIM = """
SELECT d.tb_id, min(d.tb_short_name) AS tb_short_name
FROM {schema}.uzp_dim_gosb d
WHERE d.tb_id IS NOT NULL
GROUP BY d.tb_id
"""

SHOW_DEFS = {"t_org": T_ORG, "t_exc": T_EXC,
             "t_pay": T_PAY_MONTH.replace("p.report_dt = CAST(:m AS date)",
                                          "p.report_dt = ANY(CAST(:cell_months AS date[]))"),
             "t_cell": T_CELL, "t_cls": T_CLS, "t_nfl": T_NFL, "t_nkey": T_NKEY, "t_vsp": T_VSP,
             "t_dig": T_DIG, "t_deal": T_DEAL, "t_act": T_ACT, "t_nflc": T_NFLC}


def all_sql() -> dict[str, str]:
    """Все запросы файла (шаблоны с подчёркиванием — части других запросов — не в счёт)."""
    return {k: v for k, v in globals().items()
            if k.isupper() and not k.startswith("_") and isinstance(v, str) and ("SELECT" in v or "CREATE" in v)}
