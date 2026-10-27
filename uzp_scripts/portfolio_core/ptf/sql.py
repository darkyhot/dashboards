"""Весь SQL прогона. Ни одной строки SQL за пределами этого файла.

Плейсхолдеры `{schema}`, `{schema_t}`, `{payroll}`, `{seg_hist}`, `{code_col}`
подставляет `db.render`; значения — именованными параметрами `:name`.

Ловушки, на которых такой расчёт ломается тихо
-----------------------------------------------
1. **Партиция.** Ведомости партиционированы по `report_dt`; каждое обращение к ним
   ограничено ОДНИМ месяцем (`= CAST(:m AS date)`) — проверяет самопроверка.
2. **Номер организации** в ведомостях — текст («ID123» бывает). В число — только
   под маской `MASK`, внутри `CASE`.
3. **Диалект 9.4.** Нет `GROUPING SETS`, `ON CONFLICT`, `make_interval`. Сдвиги
   месяцев — через номер месяца окна `mi` (0 … 35), а не через даты.
4. **NULL в ключе тройки.** Пустой ГОСБ → -1, иначе тройка «исчезала» бы.
5. **count(DISTINCT)** на объёме банка не используется — только `GROUP BY`.

Ведомости читаются РОВНО ОДИН РАЗ: каждый месяц сразу сворачивается до тройки
(`T_TR_MONTH`), и дальше всё считается из временной таблицы `t_tr`.

Тяжёлые запросы (метки, остатки) идут кусками по людям: `MOD(epk_id, :nch) = :ch`.
Все соединения внутри них — по человеку, поэтому кусок считается точно, а итог по
организации — суммой кусков.
"""
from __future__ import annotations

from . import segments as S

AMT_MIN = 2500
EXC_HOLDING = "МИНОБОРОНЫ"
BAD_ORG = "('7707083893', 'Не определено', '0', '-1')"
KSB_RAW = ("('СКБ-Средние','СКБ-Прочее','СКБ-Крупные','Средний бизнес','Крупный бизнес',"
           "'ККСБ - прочие', 'ККСБ - Прочие')")
MASK = "'^[0-9]{1,12}$'"

BASES = {"m": 1, "q": 3, "y": 12}            # сравнение с M−1, M−3, M−12
IN_LABELS = ["in_new_bank", "in_returned", "in_from_nonzp", "in_from_zp_out",
             "in_oth_same_org", "in_oth_same_hold", "in_oth_other"]
OUT_LABELS = ["out_to_nonzp", "out_zp_out", "out_oth_same_org", "out_oth_same_hold",
              "out_oth_other", "lb_seasonal", "lb_org_stopped", "lb_one_time", "lb_unknown"]
# Порядок столбцов одной базы в сводке (без приставки).
BASE_COLS = ["base_pf", "base_seg",
             "in_new_bank", "in_returned", "in_from_other_org", "in_from_nonzp", "in_from_zp_out",
             "in_oth_same_org", "in_oth_same_hold", "in_oth_other",
             "out_left_bank", "out_to_other_org", "out_to_nonzp", "out_zp_out",
             "out_oth_same_org", "out_oth_same_hold", "out_oth_other",
             "lb_seasonal", "lb_org_stopped", "lb_one_time", "lb_unknown",
             "fl_in", "fl_out", "fl_conc"]
STOCK_COLS = ["pf_recip", "pf_fl", "pf_amt", "fot_amt", "zp_out_recip", "nonzp_recip", "any_recip",
              "multi_recip", "multi_same_org", "multi_same_hold", "multi_other",
              "flip_to_nonzp", "flip_to_zp", "flapping_recip",
              "one_time_recip", "one_time_pf", "one_time_person",
              "ten_lt3", "ten_3_12", "ten_12_24", "ten_24p"]
SURV_COLS = ["surv3", "surv6", "surv12", "surv12_bank"]
KEY_COLS = ["report_dt", "org_id", "sys_gosb_id", "sys_tb_id", "seg", "seg_src"]
FLAP_MIN = 3                                   # смен зп ↔ не зп за 12 мес. для «мигающих»


def final_columns() -> list[str]:
    cols = KEY_COLS + STOCK_COLS + SURV_COLS
    for b in BASES:
        cols += [f"{b}_{c}" for c in BASE_COLS]
    return cols + ["m_out_temp"]


def gosb_fix(gosb: str, tb: str, seg: str) -> str:
    """Перекодировка ГОСБ заказчика (сегмент — сырой, ведомостный)."""
    return (f"CASE WHEN {tb} = 38 THEN 9038\n"
            f"           WHEN {gosb} = 1009 THEN 8557\n"
            f"           WHEN {gosb} = 0 AND {tb} = 40 THEN 9040\n"
            f"           WHEN {seg} IN {KSB_RAW} AND {gosb} = 8591 THEN 8646\n"
            f"           ELSE {gosb} END")


def org_num(col: str) -> str:
    return f"CASE WHEN CAST({col} AS text) ~ {MASK} THEN CAST(CAST({col} AS text) AS bigint) END"


# relev заказчика (дословно; номер организации — после «Не определено» → '0').
RELEV = ("""((p.enrollment_kind_descr = 'Основные' AND p.report_dt >= DATE '2024-01-01')
          OR (p.report_dt BETWEEN DATE '2023-11-01' AND DATE '2023-12-31'))
      AND ((p.market_share_flag_name <> 'ИП 1 чел.' AND p.report_dt >= DATE '2024-01-01')
          OR (p.report_dt BETWEEN DATE '2023-11-01' AND DATE '2023-12-31'))
      AND (CASE WHEN p.inn = 'Не определено' THEN '0' ELSE p.inn END) NOT IN """ + BAD_ORG)

# --------------------------------------------------------------------------- #
# Справочники
# --------------------------------------------------------------------------- #
T_TIK = """
SELECT DISTINCT k.inn
FROM {schema_t}.yva_tik_cik k
WHERE k.is_tik_cik IS TRUE AND k.inn IS NOT NULL
"""

# Организации без порога: образовательные и холдинг :exc_holding (по reference_holding_name).
T_EXC = """
SELECT inn_exc FROM (
  SELECT """ + org_num("d.inn") + """ AS inn_exc
  FROM {schema}.uzp_dim_education_organization d
  UNION
  SELECT c.inn
  FROM {schema}.uzp_data_epk_consolidation c
  WHERE c.reference_holding_name = :exc_holding AND c.inn IS NOT NULL
) t
WHERE inn_exc IS NOT NULL
"""

# ЕПК, свёрнутый до id орг (иначе соединение задвоит ведомости): текущий сегмент —
# запасной для пар, которых нет в истории; холдинг — для «перешёл в тот же холдинг».
T_ORG = """
SELECT e.inn                                        AS org_id,
       min(""" + S.seg_case_epk("e.segment_name") + """) AS seg,
       min(NULLIF(btrim(e.holding_name), ''))       AS hold
FROM {schema}.uzp_data_epk_consolidation e
LEFT JOIN t_tik k ON k.inn = e.inn
WHERE e.inn IS NOT NULL AND k.inn IS NULL
GROUP BY e.inn
"""

# Исторический сегмент пары (месяц, id орг, ГОСБ) — запрос заказчика. Дата
# приводится к концу месяца, как в ведомостях. Несколько сегментов у ключа — min().
T_SEG = """
SELECT report_dt, org_id, gosb_id, min(seg) AS seg
FROM (
  SELECT CAST(date_trunc('month', h.report_dt) + interval '1 month' - interval '1 day' AS date) AS report_dt,
         """ + org_num("h.inn") + """ AS org_id,
         COALESCE(""" + gosb_fix("h.gosb_id", "h.tb_id", "h.segment_name") + """, -1) AS gosb_id,
         """ + S.seg_case_pay("h.segment_name") + """ AS seg
  FROM {schema}.{seg_hist} h
  WHERE h.report_dt >= CAST(:w_first_day AS date) AND h.report_dt <= CAST(:w_to AS date)
) x
WHERE org_id IS NOT NULL
GROUP BY report_dt, org_id, gosb_id
"""


def t_months(months: list[str]) -> str:
    """Месяцы окна с номером mi. Значения — константы кода (даты окна), не ввод."""
    rows = ",\n  ".join(f"('{d}', {i})" for i, d in enumerate(months))
    return f"SELECT CAST(v.d AS date) AS report_dt, v.mi\nFROM (VALUES\n  {rows}\n) v(d, mi)"


# --------------------------------------------------------------------------- #
# Тройки по месяцам — ЕДИНСТВЕННОЕ чтение ведомостей, оператор на месяц.
#   st: P — в портфеле; Z — есть портфельные строки (зп-коды, is_portfolio_enrollment),
#       но тройка не в портфеле (ниже порога); N — только не портфельные виды.
#   Отбор строк — как в payroll_bank_yoy (T_RAW_MONTH), ведомости — {payroll}.
#   Тройки без положительных сумм не берутся.
#   В портфеле: вид 2, или портфельная строка и (без порога или сумма > порога).
#   pf_amt / fot_amt — суммы строк, которые засчитал бы эталон заказчика.
# --------------------------------------------------------------------------- #
T_TR_MONTH = """
SELECT CAST(:mi AS integer) AS mi, x.epk_id, x.org_id, x.gosb_id, x.tb_id,
       CASE WHEN x.in_pf THEN 'P' WHEN x.pos_pf THEN 'Z' ELSE 'N' END AS st,
       CASE WHEN x.in_pf
            THEN x.a_t2 + CASE WHEN x.exc OR x.sum_pf > :amt_min THEN x.a_pf ELSE 0 END
            ELSE 0 END                                        AS pf_amt,
       x.a_t2 + CASE WHEN x.exc OR x.sum_fot > :amt_min THEN x.a_fot ELSE 0 END AS fot_amt,
       x.n_rows
FROM (
  SELECT a.*,
         (e.inn_exc IS NOT NULL)                                          AS exc,
         (a.pos_t2 OR (a.pos_pf AND (e.inn_exc IS NOT NULL OR a.sum_pf > :amt_min))) AS in_pf
  FROM (
    SELECT p.epk_id,
           """ + org_num("p.inn") + """ AS org_id,
           COALESCE(""" + gosb_fix("p.sys_gosb_id", "p.sys_tb_id", "p.segment_name") + """, -1) AS gosb_id,
           min(p.sys_tb_id)                                                     AS tb_id,
           sum(CASE WHEN COALESCE(d.is_portfolio_enrollment, false) THEN p.amt ELSE 0 END) AS sum_pf,
           sum(CASE WHEN COALESCE(d.is_fot_enrollment, false) THEN p.amt ELSE 0 END)       AS sum_fot,
           bool_or(COALESCE(d.is_portfolio_enrollment, false) AND p.amt > 0)    AS pos_pf,
           bool_or(COALESCE(d.enrollment_type_id = 2, false) AND p.amt > 0)     AS pos_t2,
           bool_or(p.amt > 0)                                                   AS pos_any,
           sum(CASE WHEN p.amt > 0 AND COALESCE(d.enrollment_type_id = 2, false)
                    THEN p.amt ELSE 0 END)                                      AS a_t2,
           sum(CASE WHEN p.amt > 0 AND COALESCE(d.is_portfolio_enrollment, false)
                     AND NOT COALESCE(d.enrollment_type_id = 2, false)
                    THEN p.amt ELSE 0 END)                                      AS a_pf,
           sum(CASE WHEN p.amt > 0 AND COALESCE(d.is_fot_enrollment, false)
                     AND NOT COALESCE(d.enrollment_type_id = 2, false)
                    THEN p.amt ELSE 0 END)                                      AS a_fot,
           count(*)                                                             AS n_rows
    FROM {schema}.{payroll} p
    LEFT JOIN {schema}.uzp_dim_enrollment_type d ON d.enrollment_type_id = p.{code_col}
    LEFT JOIN t_tik k ON k.inn = """ + org_num("p.inn") + """
    WHERE p.report_dt = CAST(:m AS date)
      AND k.inn IS NULL
      AND p.epk_id IS NOT NULL
      AND p.inn ~ """ + MASK + """
      AND """ + RELEV + """
    GROUP BY 1, 2, 3
  ) a
  LEFT JOIN t_exc e ON e.inn_exc = a.org_id
) x
WHERE x.pos_any
"""

# --------------------------------------------------------------------------- #
# Люди и истории (по t_tr, один проход каждая)
# --------------------------------------------------------------------------- #
# Человек в месяце: сколько у него троек в портфеле.
T_PM = """
SELECT mi, epk_id, count(*) AS k_pf
FROM t_tr WHERE st = 'P'
GROUP BY mi, epk_id
"""

# Человек за окно: в скольких месяцах были любые зачисления, первый месяц в портфеле.
T_PERSON = """
SELECT epk_id, count(*) AS n_any, min(first_pf) AS first_pf
FROM (SELECT epk_id, mi, min(CASE WHEN st = 'P' THEN mi END) AS first_pf
      FROM t_tr GROUP BY epk_id, mi) x
GROUP BY epk_id
"""

# Тройка за окно: сколько месяцев с зачислениями, первый месяц в портфеле.
T_THIST = """
SELECT epk_id, org_id, gosb_id, count(*) AS n_any,
       min(CASE WHEN st = 'P' THEN mi END) AS first_pf
FROM t_tr
GROUP BY epk_id, org_id, gosb_id
"""

# Сколько троек в портфеле у человека в организации / в холдинге в месяце.
T_PO = """
SELECT mi, epk_id, org_id, count(*) AS n
FROM t_tr WHERE st = 'P'
GROUP BY mi, epk_id, org_id
"""

T_PH = """
SELECT t.mi, t.epk_id, o.hold, count(*) AS n
FROM t_tr t
JOIN t_org o ON o.org_id = t.org_id
WHERE t.st = 'P' AND o.hold IS NOT NULL
GROUP BY t.mi, t.epk_id, o.hold
"""

# Организация в месяце: сколько троек в портфеле по всем ГОСБ.
T_ORGM = """
SELECT mi, org_id, count(*) AS n
FROM t_tr WHERE st = 'P'
GROUP BY mi, org_id
"""

# Смены зп ↔ не зп: тройка в соседних месяцах сменила группу (P/Z — портфельные
# виды, N — только не портфельные).
T_FLIP = """
SELECT c.epk_id, c.org_id, c.gosb_id, c.mi, (c.st = 'N') AS to_nonzp
FROM t_tr c
JOIN t_tr p ON p.epk_id = c.epk_id AND p.org_id = c.org_id AND p.gosb_id = c.gosb_id
           AND p.mi = c.mi - 1
WHERE (c.st = 'N') <> (p.st = 'N')
"""

# «Мигающие»: тройка есть в M и сменила группу не меньше :flap_min раз за M−11 … M.
T_FLAP = """
SELECT c.epk_id, c.org_id, c.gosb_id, c.mi
FROM t_tr c
JOIN t_flip f ON f.epk_id = c.epk_id AND f.org_id = c.org_id AND f.gosb_id = c.gosb_id
             AND f.mi BETWEEN c.mi - 11 AND c.mi
GROUP BY c.epk_id, c.org_id, c.gosb_id, c.mi
HAVING count(*) >= :flap_min
"""

# --------------------------------------------------------------------------- #
# Метки движений. :k — глубина базы (1, 3, 12), :last — последний mi окна.
# --------------------------------------------------------------------------- #
_TRIPLE = "{a}.epk_id = {b}.epk_id AND {a}.org_id = {b}.org_id AND {a}.gosb_id = {b}.gosb_id"


def _tj(a: str, b: str) -> str:
    return _TRIPLE.format(a=a, b=b)


def _count_cols(labels: list[str]) -> str:
    return ",\n       ".join(f"sum(CASE WHEN lab = '{x}' THEN 1 ELSE 0 END) AS {x}" for x in labels)


# Пришёл: в портфеле в M, нет в B = M − k. Первое подошедшее условие.
LAB_IN = """
SELECT mi, org_id, gosb_id,
       """ + _count_cols(IN_LABELS) + """,
       sum(fl_in) AS fl_in
FROM (
  SELECT c.mi, c.org_id, c.gosb_id,
         CASE WHEN b.st = 'N' THEN 'in_from_nonzp'
              WHEN b.st = 'Z' THEN 'in_from_zp_out'
              WHEN pb.k_pf IS NOT NULL THEN
                   CASE WHEN so.n IS NOT NULL THEN 'in_oth_same_org'
                        WHEN sh.n IS NOT NULL THEN 'in_oth_same_hold'
                        ELSE 'in_oth_other' END
              WHEN pr.first_pf < c.mi - :k THEN 'in_returned'
              ELSE 'in_new_bank' END                                 AS lab,
         CASE WHEN pb.k_pf IS NULL THEN CAST(1.0 / pc.k_pf AS float8) ELSE 0 END AS fl_in
  FROM t_tr c
  JOIN t_pm pc ON pc.epk_id = c.epk_id AND pc.mi = c.mi
  LEFT JOIN t_tr b ON """ + _tj("b", "c") + """ AND b.mi = c.mi - :k
  LEFT JOIN t_pm pb ON pb.epk_id = c.epk_id AND pb.mi = c.mi - :k
  LEFT JOIN t_po so ON so.epk_id = c.epk_id AND so.mi = c.mi - :k AND so.org_id = c.org_id
  LEFT JOIN t_org o ON o.org_id = c.org_id
  LEFT JOIN t_ph sh ON sh.epk_id = c.epk_id AND sh.mi = c.mi - :k AND sh.hold = o.hold
  LEFT JOIN t_person pr ON pr.epk_id = c.epk_id
  WHERE c.st = 'P' AND c.mi >= :k
    AND (b.st IS NULL OR b.st <> 'P')
    AND MOD(c.epk_id, :nch) = :ch
) z
GROUP BY mi, org_id, gosb_id
"""

# Ушёл: в портфеле в B, нет в M = B + k. Уход из банка делится на известные причины:
# сезонный (тройка была в портфеле в M−13, не было в M−12, снова была в M−11 — тот же
# провал год назад; первым, потому что сезонно может пропадать и вся организация) →
# организация не платит в портфель ни в одном ГОСБ → тройка разовая → неизвестно.
LAB_OUT = """
SELECT mi, org_id, gosb_id,
       """ + _count_cols(OUT_LABELS) + """,
       sum(fl_out) AS fl_out,
       sum(out_temp) AS out_temp
FROM (
  SELECT b.mi + :k AS mi, b.org_id, b.gosb_id,
         CASE WHEN c.st = 'N' THEN 'out_to_nonzp'
              WHEN c.st = 'Z' THEN 'out_zp_out'
              WHEN pc.k_pf IS NOT NULL THEN
                   CASE WHEN so.n IS NOT NULL THEN 'out_oth_same_org'
                        WHEN sh.n IS NOT NULL THEN 'out_oth_same_hold'
                        ELSE 'out_oth_other' END
              WHEN s13.mi IS NOT NULL AND s12.mi IS NULL AND s11.mi IS NOT NULL THEN 'lb_seasonal'
              WHEN om.n IS NULL THEN 'lb_org_stopped'
              WHEN th.n_any = 1 THEN 'lb_one_time'
              ELSE 'lb_unknown' END                                  AS lab,
         CASE WHEN pc.k_pf IS NULL THEN CAST(1.0 / pb.k_pf AS float8) ELSE 0 END AS fl_out,
         CASE WHEN r.mi IS NOT NULL THEN 1 ELSE 0 END                AS out_temp
  FROM t_tr b
  JOIN t_pm pb ON pb.epk_id = b.epk_id AND pb.mi = b.mi
  LEFT JOIN t_tr c ON """ + _tj("c", "b") + """ AND c.mi = b.mi + :k
  LEFT JOIN t_pm pc ON pc.epk_id = b.epk_id AND pc.mi = b.mi + :k
  LEFT JOIN t_po so ON so.epk_id = b.epk_id AND so.mi = b.mi + :k AND so.org_id = b.org_id
  LEFT JOIN t_org o ON o.org_id = b.org_id
  LEFT JOIN t_ph sh ON sh.epk_id = b.epk_id AND sh.mi = b.mi + :k AND sh.hold = o.hold
  LEFT JOIN t_orgm om ON om.org_id = b.org_id AND om.mi = b.mi + :k
  LEFT JOIN t_thist th ON """ + _tj("th", "b") + """
  LEFT JOIN t_tr s13 ON """ + _tj("s13", "b") + """ AND s13.mi = b.mi + :k - 13 AND s13.st = 'P'
  LEFT JOIN t_tr s12 ON """ + _tj("s12", "b") + """ AND s12.mi = b.mi + :k - 12 AND s12.st = 'P'
  LEFT JOIN t_tr s11 ON """ + _tj("s11", "b") + """ AND s11.mi = b.mi + :k - 11 AND s11.st = 'P'
  LEFT JOIN t_tr r ON """ + _tj("r", "b") + """ AND r.mi = b.mi + :k + 1 AND r.st = 'P'
  WHERE b.st = 'P' AND b.mi + :k <= :last
    AND (c.st IS NULL OR c.st <> 'P')
    AND MOD(b.epk_id, :nch) = :ch
) z
GROUP BY mi, org_id, gosb_id
"""

# --------------------------------------------------------------------------- #
# Остатки месяца: портфель, ФЛ, суммы, состояния, совместительство, смены кодов,
# разовые, стаж, дожитие пришедших.
# --------------------------------------------------------------------------- #
def _surv(n: int, cond: str) -> str:
    return (f"CASE WHEN y.mi + {n} <= :last THEN CASE WHEN arrived AND {cond} THEN 1 ELSE 0 END END")


STOCK = """
SELECT mi, org_id, gosb_id, min(tb_id) AS tb_id,
       sum(CASE WHEN st = 'P' THEN 1 ELSE 0 END)                    AS pf_recip,
       sum(CASE WHEN st = 'P' THEN CAST(1.0 / k_pf AS float8) ELSE 0 END) AS pf_fl,
       sum(pf_amt)                                                  AS pf_amt,
       sum(fot_amt)                                                 AS fot_amt,
       sum(CASE WHEN st = 'Z' THEN 1 ELSE 0 END)                    AS zp_out_recip,
       sum(CASE WHEN st = 'N' THEN 1 ELSE 0 END)                    AS nonzp_recip,
       count(*)                                                     AS any_recip,
       sum(CASE WHEN multi IS NOT NULL THEN 1 ELSE 0 END)           AS multi_recip,
       sum(CASE WHEN multi = 'org' THEN 1 ELSE 0 END)               AS multi_same_org,
       sum(CASE WHEN multi = 'hold' THEN 1 ELSE 0 END)              AS multi_same_hold,
       sum(CASE WHEN multi = 'other' THEN 1 ELSE 0 END)             AS multi_other,
       sum(flip_to_nonzp)                                           AS flip_to_nonzp,
       sum(flip_to_zp)                                              AS flip_to_zp,
       sum(flapping)                                                AS flapping_recip,
       sum(one_time)                                                AS one_time_recip,
       sum(CASE WHEN st = 'P' THEN one_time ELSE 0 END)             AS one_time_pf,
       sum(one_time_person)                                         AS one_time_person,
       sum(CASE WHEN st = 'P' AND ten < 3 THEN 1 ELSE 0 END)        AS ten_lt3,
       sum(CASE WHEN st = 'P' AND ten BETWEEN 3 AND 11 THEN 1 ELSE 0 END)  AS ten_3_12,
       sum(CASE WHEN st = 'P' AND ten BETWEEN 12 AND 23 THEN 1 ELSE 0 END) AS ten_12_24,
       sum(CASE WHEN st = 'P' AND ten >= 24 THEN 1 ELSE 0 END)      AS ten_24p,
       sum(surv3) AS surv3, sum(surv6) AS surv6, sum(surv12) AS surv12,
       sum(surv12_bank) AS surv12_bank
FROM (
  SELECT y.*,
         """ + _surv(3, "s3 IS NOT NULL") + """   AS surv3,
         """ + _surv(6, "s6 IS NOT NULL") + """   AS surv6,
         """ + _surv(12, "s12 IS NOT NULL") + """ AS surv12,
         """ + _surv(12, "b12 IS NOT NULL") + """ AS surv12_bank
  FROM (
    SELECT c.mi, c.org_id, c.gosb_id, c.tb_id, c.st, c.pf_amt, c.fot_amt, pc.k_pf,
           CASE WHEN c.st <> 'P' OR pc.k_pf = 1 THEN NULL
                WHEN po.n > 1 THEN 'org'
                WHEN ph.n > po.n THEN 'hold'
                ELSE 'other' END                                       AS multi,
           CASE WHEN c.st = 'N' AND p.st IN ('P', 'Z') THEN 1 ELSE 0 END AS flip_to_nonzp,
           CASE WHEN c.st IN ('P', 'Z') AND p.st = 'N' THEN 1 ELSE 0 END AS flip_to_zp,
           CASE WHEN fl.mi IS NOT NULL THEN 1 ELSE 0 END               AS flapping,
           CASE WHEN th.n_any = 1 AND c.mi > 0 THEN 1 ELSE 0 END        AS one_time,
           CASE WHEN th.n_any = 1 AND c.mi > 0 AND pr.n_any = 1 THEN 1 ELSE 0 END AS one_time_person,
           c.mi - th.first_pf                                          AS ten,
           (c.st = 'P' AND c.mi >= 1 AND (p.st IS NULL OR p.st <> 'P')) AS arrived,
           s3.mi AS s3, s6.mi AS s6, s12.mi AS s12, b12.mi AS b12
    FROM t_tr c
    LEFT JOIN t_pm pc ON pc.epk_id = c.epk_id AND pc.mi = c.mi
    LEFT JOIN t_po po ON po.epk_id = c.epk_id AND po.mi = c.mi AND po.org_id = c.org_id
    LEFT JOIN t_org o ON o.org_id = c.org_id
    LEFT JOIN t_ph ph ON ph.epk_id = c.epk_id AND ph.mi = c.mi AND ph.hold = o.hold
    LEFT JOIN t_tr p ON """ + _tj("p", "c") + """ AND p.mi = c.mi - 1
    LEFT JOIN t_flap fl ON """ + _tj("fl", "c") + """ AND fl.mi = c.mi
    LEFT JOIN t_thist th ON """ + _tj("th", "c") + """
    LEFT JOIN t_person pr ON pr.epk_id = c.epk_id
    LEFT JOIN t_tr s3 ON """ + _tj("s3", "c") + """ AND s3.mi = c.mi + 3 AND s3.st = 'P'
    LEFT JOIN t_tr s6 ON """ + _tj("s6", "c") + """ AND s6.mi = c.mi + 6 AND s6.st = 'P'
    LEFT JOIN t_tr s12 ON """ + _tj("s12", "c") + """ AND s12.mi = c.mi + 12 AND s12.st = 'P'
    LEFT JOIN t_pm b12 ON b12.epk_id = c.epk_id AND b12.mi = c.mi + 12
    WHERE MOD(c.epk_id, :nch) = :ch
  ) y
) z
GROUP BY mi, org_id, gosb_id
"""


def regroup(part: str, sums: list[str], extra: str = "") -> str:
    """Сложить куски по людям в итог по (месяц, id орг, ГОСБ)."""
    cols = ",\n       ".join(f"sum({c}) AS {c}" for c in sums)
    return f"SELECT mi, org_id, gosb_id{extra},\n       {cols}\nFROM {part}\nGROUP BY mi, org_id, gosb_id"


STOCK_SUMS = [c for c in STOCK_COLS] + SURV_COLS

# Ключи сводки: пары с тройками в M и пары, у которых был портфель в одной из баз.
T_KEYS = """
SELECT mi, org_id, gosb_id FROM (
  SELECT mi, org_id, gosb_id FROM t_s
  UNION ALL SELECT mi + 1, org_id, gosb_id FROM t_s WHERE pf_recip > 0 AND mi + 1 <= :last
  UNION ALL SELECT mi + 3, org_id, gosb_id FROM t_s WHERE pf_recip > 0 AND mi + 3 <= :last
  UNION ALL SELECT mi + 12, org_id, gosb_id FROM t_s WHERE pf_recip > 0 AND mi + 12 <= :last
) x
GROUP BY mi, org_id, gosb_id
"""

# Сегмент каждого ключа: история, иначе текущий ЕПК.
T_KSEG = """
SELECT k.mi, k.org_id, k.gosb_id,
       COALESCE(h.seg, o.seg, '""" + S.NOT_FOUND + """') AS seg,
       CASE WHEN h.seg IS NOT NULL THEN 'история'
            WHEN o.seg IS NOT NULL THEN 'ЕПК'
            ELSE 'нет' END                     AS seg_src
FROM t_keys k
JOIN t_months mo ON mo.mi = k.mi
LEFT JOIN t_seg h ON h.report_dt = mo.report_dt AND h.org_id = k.org_id AND h.gosb_id = k.gosb_id
LEFT JOIN t_org o ON o.org_id = k.org_id
"""


def final_select() -> str:
    """Сборка сводки. Столбцы базы пусты (NULL), если база выходит за окно."""
    kj = "{a}.org_id = k.org_id AND {a}.gosb_id = k.gosb_id"
    sel = ["mo.report_dt", "k.org_id", "k.gosb_id AS sys_gosb_id",
           "COALESCE(s.tb_id, s_m.tb_id, s_q.tb_id, s_y.tb_id) AS sys_tb_id",
           "ks.seg", "ks.seg_src"]
    sel += [f"COALESCE(s.{c}, 0) AS {c}" for c in STOCK_COLS]
    for c, n in zip(SURV_COLS, (3, 6, 12, 12)):
        sel.append(f"CASE WHEN k.mi + {n} <= :last THEN COALESCE(s.{c}, 0) END AS {c}")
    joins = ["JOIN t_months mo ON mo.mi = k.mi",
             "JOIN t_kseg ks ON ks.mi = k.mi AND " + kj.format(a="ks"),
             "LEFT JOIN t_s s ON s.mi = k.mi AND " + kj.format(a="s")]
    for b, n in BASES.items():
        on = f"k.mi >= {n}"

        def v(expr: str) -> str:
            return f"CASE WHEN {on} THEN {expr} END"

        ins = {x: f"COALESCE(i_{b}.{x}, 0)" for x in IN_LABELS}
        outs = {x: f"COALESCE(o_{b}.{x}, 0)" for x in OUT_LABELS}
        fl_in, fl_out = f"COALESCE(i_{b}.fl_in, 0)", f"COALESCE(o_{b}.fl_out, 0)"
        expr = {
            "base_pf": f"COALESCE(s_{b}.pf_recip, 0)",
            "base_seg": f"ks_{b}.seg",
            "in_from_other_org": " + ".join(ins[x] for x in IN_LABELS if x.startswith("in_oth_")),
            "out_left_bank": " + ".join(outs[x] for x in OUT_LABELS if x.startswith("lb_")),
            "out_to_other_org": " + ".join(outs[x] for x in OUT_LABELS if x.startswith("out_oth_")),
            "fl_in": fl_in, "fl_out": fl_out,
            "fl_conc": (f"(COALESCE(s.pf_fl, 0) - COALESCE(s_{b}.pf_fl, 0)) - {fl_in} + {fl_out}"),
            **ins, **outs,
        }
        sel += [f"{v(expr[c])} AS {b}_{c}" for c in BASE_COLS]
        joins += [f"LEFT JOIN t_s s_{b} ON s_{b}.mi = k.mi - {n} AND " + kj.format(a=f"s_{b}"),
                  f"LEFT JOIN t_kseg ks_{b} ON ks_{b}.mi = k.mi - {n} AND " + kj.format(a=f"ks_{b}"),
                  f"LEFT JOIN t_in_{b} i_{b} ON i_{b}.mi = k.mi AND " + kj.format(a=f"i_{b}"),
                  f"LEFT JOIN t_out_{b} o_{b} ON o_{b}.mi = k.mi AND " + kj.format(a=f"o_{b}")]
    sel.append("CASE WHEN k.mi >= 1 AND k.mi + 1 <= :last THEN COALESCE(o_m.out_temp, 0) END AS m_out_temp")
    sel = [_typed(x) for x in sel]
    return ("SELECT " + ",\n       ".join(sel) + "\nFROM t_keys k\n" + "\n".join(joins))


# Счётчики — bigint (сумма сумм в Postgres даёт numeric); ФЛ — float8; суммы — numeric.
_NOT_COUNT = {"report_dt", "org_id", "sys_gosb_id", "sys_tb_id", "seg", "seg_src", "pf_fl",
              "pf_amt", "fot_amt"}


def _typed(item: str) -> str:
    expr, _, name = item.rpartition(" AS ")
    if not expr or name in _NOT_COUNT or name.endswith(("_base_seg", "_fl_in", "_fl_out", "_fl_conc")):
        return item
    return f"CAST({expr} AS bigint) AS {name}"


# --------------------------------------------------------------------------- #
# Постоянные таблицы
# --------------------------------------------------------------------------- #
TARGET = "{schema_t}.yva_ptf_month_org"
RUNS = "{schema_t}.yva_ptf_runs"
GP_STORAGE = "WITH (appendonly=true, orientation=column, compresstype=zlib, compresslevel=5)"

CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS {schema_t}.yva_ptf_runs (
  run_id bigint, run_ts timestamp, report_dt date, window_from date, window_to date,
  pf_recip bigint, pf_fl float8, fot_amt numeric, any_recip bigint, raw_rows bigint,
  seg_hist_share float8, one_time_prelim boolean, checks_ok boolean, duration_min float8
)"""

INSERT_RUNS = """
INSERT INTO {schema_t}.yva_ptf_runs
  (run_id, run_ts, report_dt, window_from, window_to, pf_recip, pf_fl, fot_amt, any_recip,
   raw_rows, seg_hist_share, one_time_prelim, checks_ok, duration_min)
VALUES (:run_id, CAST(:run_ts AS timestamp), CAST(:report_dt AS date), CAST(:window_from AS date),
        CAST(:window_to AS date), :pf_recip, :pf_fl, :fot_amt, :any_recip, :raw_rows,
        :seg_hist_share, :one_time_prelim, :checks_ok, :duration_min)
"""

PREV_RUN = """
SELECT r.report_dt, r.pf_recip, r.run_id
FROM {schema_t}.yva_ptf_runs r
WHERE r.run_id = (SELECT max(run_id) FROM {schema_t}.yva_ptf_runs WHERE checks_ok)
"""

MONTH_TOTALS = """
SELECT report_dt,
       sum(pf_recip) AS pf_recip, sum(pf_fl) AS pf_fl, sum(fot_amt) AS fot_amt,
       sum(any_recip) AS any_recip,
       sum(CASE WHEN seg_src = 'история' THEN pf_recip ELSE 0 END) AS pf_hist,
       sum(m_fl_conc) AS m_conc, sum(q_fl_conc) AS q_conc, sum(y_fl_conc) AS y_conc
FROM {schema_t}.yva_ptf_month_org_new
GROUP BY report_dt
"""

TR_TOTALS = """
SELECT m.report_dt, t.n_pf, t.n_rows, p.n_fl
FROM t_months m
LEFT JOIN (SELECT mi, sum(CASE WHEN st = 'P' THEN 1 ELSE 0 END) AS n_pf, sum(n_rows) AS n_rows
           FROM t_tr GROUP BY mi) t ON t.mi = m.mi
LEFT JOIN (SELECT mi, count(*) AS n_fl FROM t_pm GROUP BY mi) p ON p.mi = m.mi
"""


def row_checks() -> str:
    """Число строк, где нарушено тождество или «части = целому». Должно быть 0."""
    parts = []
    for b in BASES:
        ins = " + ".join(f"{b}_{x}" for x in ["in_new_bank", "in_returned", "in_from_other_org",
                                               "in_from_nonzp", "in_from_zp_out"])
        outs = " + ".join(f"{b}_{x}" for x in ["out_left_bank", "out_to_other_org",
                                                "out_to_nonzp", "out_zp_out"])
        parts.append(f"sum(CASE WHEN {b}_base_pf IS NOT NULL AND "
                     f"pf_recip - {b}_base_pf <> ({ins}) - ({outs}) THEN 1 ELSE 0 END) AS {b}_identity")
        parts.append(f"sum(CASE WHEN {b}_in_from_other_org <> {b}_in_oth_same_org + {b}_in_oth_same_hold"
                     f" + {b}_in_oth_other THEN 1 ELSE 0 END) AS {b}_in_parts")
        parts.append(f"sum(CASE WHEN {b}_out_to_other_org <> {b}_out_oth_same_org + {b}_out_oth_same_hold"
                     f" + {b}_out_oth_other THEN 1 ELSE 0 END) AS {b}_out_parts")
        parts.append(f"sum(CASE WHEN {b}_out_left_bank <> {b}_lb_org_stopped + {b}_lb_one_time"
                     f" + {b}_lb_seasonal + {b}_lb_unknown THEN 1 ELSE 0 END) AS {b}_lb_parts")
    parts.append("sum(CASE WHEN multi_recip <> multi_same_org + multi_same_hold + multi_other "
                 "THEN 1 ELSE 0 END) AS multi_parts")
    parts.append("sum(CASE WHEN pf_recip <> ten_lt3 + ten_3_12 + ten_12_24 + ten_24p "
                 "THEN 1 ELSE 0 END) AS tenure_parts")
    parts.append("sum(CASE WHEN any_recip <> pf_recip + zp_out_recip + nonzp_recip "
                 "THEN 1 ELSE 0 END) AS state_parts")
    return "SELECT " + ",\n       ".join(parts) + "\nFROM {schema_t}.yva_ptf_month_org_new"


SWAP = [
    "DROP TABLE IF EXISTS {schema_t}.yva_ptf_month_org_old",
    "ALTER TABLE IF EXISTS {schema_t}.yva_ptf_month_org RENAME TO yva_ptf_month_org_old",
    "ALTER TABLE {schema_t}.yva_ptf_month_org_new RENAME TO yva_ptf_month_org",
    "DROP TABLE IF EXISTS {schema_t}.yva_ptf_month_org_old",
]
DROP_NEW = "DROP TABLE IF EXISTS {schema_t}.yva_ptf_month_org_new"

# Порядок временных таблиц (каждая читает предыдущие) и ключ распределения.
DIST = {"t_months": "mi", "t_tik": "inn", "t_exc": "inn_exc", "t_org": "org_id", "t_seg": "org_id",
        "t_tr": "epk_id", "t_pm": "epk_id", "t_person": "epk_id", "t_thist": "epk_id",
        "t_po": "epk_id", "t_ph": "epk_id", "t_orgm": "org_id", "t_flip": "epk_id",
        "t_flap": "epk_id", "t_s_part": "org_id", "t_s": "org_id", "t_keys": "org_id",
        "t_kseg": "org_id",
        **{f"t_{d}_{b}{p}": "org_id" for d in ("in", "out") for b in BASES for p in ("", "_part")}}
