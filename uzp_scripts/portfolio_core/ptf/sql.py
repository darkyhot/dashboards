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
   месяцев — через номер месяца окна `mi` (0 … ≤ 59), а не через даты.
4. **NULL в ключе тройки.** Пустой ГОСБ → -1, иначе тройка «исчезала» бы.
5. **count(DISTINCT)** на объёме банка не используется — только `GROUP BY`.

Ведомости читаются РОВНО ОДИН РАЗ: каждый месяц сразу сворачивается до тройки
(`T_TR_MONTH`), и дальше всё считается из временной таблицы `t_tr`.

История тройки и человека за окно — битовые маски bigint (бит mi = месяц окна):
`b_any` — любые зачисления, `b_p` — в портфеле, `b_n` — только не зп. Маски заменяют
самосоединения `t_tr` (возврат, дожитие, сезон, разовые, «мигание»): на объёме банка
каждое такое соединение — сотни миллионов строк. Окно ≤ 60 месяцев, поэтому сдвиги
не выходят за 63 бита.

Тяжёлые запросы (метки, остатки) могут идти кусками по людям: `MOD(epk_id, :nch) = :ch`.
"""
from __future__ import annotations

from . import segments as S

AMT_MIN = 2500
EXC_HOLDING = "МИНОБОРОНЫ"
BAD_ORG = "('7707083893', 'Не определено', '0', '-1')"
KSB_RAW = ("('СКБ-Средние','СКБ-Прочее','СКБ-Крупные','Средний бизнес','Крупный бизнес',"
           "'ККСБ - прочие', 'ККСБ - Прочие')")
MASK = "'^[0-9]{1,12}$'"
FIRST_MONTH = "2023-11-30"       # с этого месяца определён отбор relev
CMP_MONTH = "2024-01-31"         # с этого месяца relev действует в нынешнем виде
MEM = 12                         # глубина памяти: «вернулся», разовые, стаж, сезон
MAX_MONTHS = 60                  # битовые маски bigint
FLAP_MIN = 3                     # смен зп ↔ не зп за 12 мес. для «мигающих»
SEAS_RET = 3                     # сезон: возврат год назад в течение стольких месяцев
DEF_VER = 2                      # версия определений (журнал и поколения)

BASES = {"m": 1, "y": 12}        # сравнение с M−1 и M−12
IN_LABELS = ["in_new_bank", "in_returned", "in_from_nonzp", "in_from_zp_out",
             "in_oth_same_org", "in_oth_same_hold", "in_oth_other"]
OUT_LABELS = ["out_to_nonzp", "out_zp_out", "out_oth_same_org", "out_oth_same_hold",
              "out_oth_other", "lb_seasonal", "lb_org_stopped", "lb_one_time", "lb_unknown"]
TEN = ["lt3", "3_12", "12p"]
STOCK_COLS = ["pf_recip", "pf_fl", "pf_amt", "fot_amt", "zp_out_recip", "nonzp_recip", "any_recip",
              "raw_rows", "single_pay_recip",
              "multi_recip", "multi_same_org", "multi_same_hold", "multi_other",
              "flapping_recip", "one_time_pf", "one_time_person",
              "ten_lt3", "ten_3_12", "ten_12p"]
KEY_COLS = ["report_dt", "org_id", "sys_gosb_id", "sys_tb_id", "seg_rep", "seg_cur", "seg_src"]


def base_cols(b: str) -> list[str]:
    """Столбцы одной базы (без приставки). Для y нет lb_seasonal: тройка была в портфеле
    ровно год назад, значит провала в этом месяце год назад не было — всегда 0."""
    cols = ["base_pf", "base_seg", "in_from_outside", "in_new_bank", "in_returned",
            "in_from_other_org", "in_from_nonzp", "in_from_zp_out",
            "in_oth_same_org", "in_oth_same_hold", "in_oth_other",
            "out_left_bank", "out_to_other_org", "out_to_nonzp", "out_zp_out",
            "out_oth_same_org", "out_oth_same_hold", "out_oth_other",
            "lb_seasonal", "lb_org_stopped", "lb_one_time", "lb_unknown",
            "fl_in", "fl_out", "fl_conc"]
    return [c for c in cols if not (b == "y" and c == "lb_seasonal")]


M_ONLY = ["m_out_temp", "m_out_ten_lt3", "m_out_ten_3_12", "m_out_ten_12p"]


def final_columns() -> list[str]:
    cols = KEY_COLS + STOCK_COLS + ["prelim"]
    for b in BASES:
        cols += [f"{b}_{c}" for c in base_cols(b)]
    return cols + M_ONLY


def gosb_fix(gosb: str, tb: str, seg: str) -> str:
    """Перекодировка ГОСБ заказчика (сегмент — сырой, ведомостный)."""
    return (f"CASE WHEN {tb} = 38 THEN 9038\n"
            f"           WHEN {gosb} = 1009 THEN 8557\n"
            f"           WHEN {gosb} = 0 AND {tb} = 40 THEN 9040\n"
            f"           WHEN {seg} IN {KSB_RAW} AND {gosb} = 8591 THEN 8646\n"
            f"           ELSE {gosb} END")


def org_num(col: str) -> str:
    return f"CASE WHEN CAST({col} AS text) ~ {MASK} THEN CAST(CAST({col} AS text) AS bigint) END"


def bit(c: str, i: str) -> str:
    """Бит месяца i маски c (0 при i < 0)."""
    return f"(CASE WHEN ({i}) >= 0 THEN (({c}) >> ({i})) & 1 ELSE 0 END)"


def rng(c: str, lo: str, hi: str) -> str:
    """Биты маски c в месяцах lo…hi (0, если отрезок пуст или целиком до окна).
    Результат ≠ 0 ⇔ хоть один месяц отрезка отмечен."""
    lo0 = f"GREATEST(({lo}), 0)"
    return (f"(CASE WHEN ({hi}) < {lo0} THEN 0 ELSE ({c}) & "
            f"((CAST(1 AS bigint) << (({hi}) + 1)) - (CAST(1 AS bigint) << {lo0})) END)")


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
SELECT mo.mi, x.org_id, x.gosb_id, min(x.seg) AS seg
FROM (
  SELECT CAST(date_trunc('month', h.report_dt) + interval '1 month' - interval '1 day' AS date) AS report_dt,
         """ + org_num("h.inn") + """ AS org_id,
         COALESCE(""" + gosb_fix("h.gosb_id", "h.tb_id", "h.segment_name") + """, -1) AS gosb_id,
         """ + S.seg_case_pay("h.segment_name") + """ AS seg
  FROM {schema}.{seg_hist} h
  WHERE h.report_dt >= CAST(:w_first_day AS date) AND h.report_dt <= CAST(:w_to AS date)
) x
JOIN t_months mo ON mo.report_dt = x.report_dt
WHERE x.org_id IS NOT NULL AND x.seg IS NOT NULL
GROUP BY mo.mi, x.org_id, x.gosb_id
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
#   n_pay — число положительных выплат (transaction_qty, пусто → 1): «одна выплата в
#   месяц» — главный носитель календарного сдвига выплат через границу месяца.
# --------------------------------------------------------------------------- #
T_TR_MONTH = """
SELECT CAST(:mi AS integer) AS mi, x.epk_id, x.org_id, x.gosb_id, x.tb_id,
       CASE WHEN x.in_pf THEN 'P' WHEN x.pos_pf THEN 'Z' ELSE 'N' END AS st,
       CASE WHEN x.in_pf
            THEN x.a_t2 + CASE WHEN x.exc OR x.sum_pf > :amt_min THEN x.a_pf ELSE 0 END
            ELSE 0 END                                        AS pf_amt,
       x.a_t2 + CASE WHEN x.exc OR x.sum_fot > :amt_min THEN x.a_fot ELSE 0 END AS fot_amt,
       x.n_rows, x.n_pay
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
           count(*)                                                             AS n_rows,
           sum(CASE WHEN p.amt > 0 THEN COALESCE(p.transaction_qty, 1) ELSE 0 END) AS n_pay
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

# Маски истории тройки: любые зачисления / в портфеле / только не зп.
T_THIST = """
SELECT epk_id, org_id, gosb_id,
       bit_or(CAST(1 AS bigint) << mi)                                              AS b_any,
       bit_or(CASE WHEN st = 'P' THEN CAST(1 AS bigint) << mi ELSE CAST(0 AS bigint) END) AS b_p,
       bit_or(CASE WHEN st = 'N' THEN CAST(1 AS bigint) << mi ELSE CAST(0 AS bigint) END) AS b_n
FROM t_tr
GROUP BY epk_id, org_id, gosb_id
"""

# Маски истории человека по всему банку.
T_PBITS = """
SELECT epk_id, bit_or(b_any) AS b_any, bit_or(b_p) AS b_p
FROM t_thist
GROUP BY epk_id
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

# --------------------------------------------------------------------------- #
# Метки движений — построчно по людям (нужно потокам «орг → орг»), потом свёртка.
# :k — глубина базы (1, 12), :last — последний mi окна.
# --------------------------------------------------------------------------- #
_TRIPLE = "{a}.epk_id = {b}.epk_id AND {a}.org_id = {b}.org_id AND {a}.gosb_id = {b}.gosb_id"


def _tj(a: str, b: str) -> str:
    return _TRIPLE.format(a=a, b=b)


def _ten(mask: str, at: str) -> str:
    """Полоса стажа тройки в портфеле в месяце at (память 12 мес.)."""
    return (f"CASE WHEN {rng(mask, f'{at} - 12', f'{at} - 3')} = 0 THEN 'lt3'\n"
            f"              WHEN {bit(mask, f'{at} - 12')} = 1 THEN '12p'\n"
            f"              ELSE '3_12' END")


def _one_time(mask: str, at: str) -> str:
    """Причинный «разовый» в месяце at: нет зачислений в at−12…at−1 и в at+1, at+2."""
    return (f"({rng(mask, f'{at} - 12', f'{at} - 1')} = 0 AND {bit(mask, f'{at} + 1')} = 0 "
            f"AND {bit(mask, f'{at} + 2')} = 0)")


# Пришёл: в портфеле в M, нет в B = M − k. Первое подошедшее условие.
LAB_IN = """
SELECT c.mi, c.epk_id, c.org_id, c.gosb_id,
       CASE WHEN """ + bit("th.b_n", "c.mi - :k") + """ = 1 THEN 'in_from_nonzp'
            WHEN """ + bit("th.b_any", "c.mi - :k") + """ = 1 THEN 'in_from_zp_out'
            WHEN pb.k_pf IS NOT NULL THEN
                 CASE WHEN so.n IS NOT NULL THEN 'in_oth_same_org'
                      WHEN sh.n IS NOT NULL THEN 'in_oth_same_hold'
                      ELSE 'in_oth_other' END
            WHEN """ + rng("pr.b_p", "c.mi - :k - 12", "c.mi - :k - 1") + """ <> 0 THEN 'in_returned'
            ELSE 'in_new_bank' END                                         AS lab,
       CASE WHEN pb.k_pf IS NULL THEN CAST(1.0 / pc.k_pf AS float8) ELSE 0 END AS fl
FROM t_tr c
JOIN t_pm pc ON pc.epk_id = c.epk_id AND pc.mi = c.mi
JOIN t_thist th ON """ + _tj("th", "c") + """
JOIN t_pbits pr ON pr.epk_id = c.epk_id
LEFT JOIN t_pm pb ON pb.epk_id = c.epk_id AND pb.mi = c.mi - :k
LEFT JOIN t_po so ON so.epk_id = c.epk_id AND so.mi = c.mi - :k AND so.org_id = c.org_id
LEFT JOIN t_org o ON o.org_id = c.org_id
LEFT JOIN t_ph sh ON sh.epk_id = c.epk_id AND sh.mi = c.mi - :k AND sh.hold = o.hold
WHERE c.st = 'P' AND c.mi >= :k
  AND """ + bit("th.b_p", "c.mi - :k") + """ = 0
  AND MOD(c.epk_id, :nch) = :ch
"""

# Ушёл: в портфеле в B, нет в M = B + k. Уход из банка делится на известные причины:
# сезонный (год назад в этом месяце тот же провал: в портфеле в M−13, нет в M−12, снова
# в портфеле в течение SEAS_RET мес.; первым — сезонно может пропадать и вся
# организация) → организация не платит в портфель ни в одном ГОСБ → тройка разовая в B
# → неизвестно. Попутно: вернулся ли в M+1 (пропуск) и полоса стажа в B.
LAB_OUT = """
SELECT b.mi + :k AS mi, b.epk_id, b.org_id, b.gosb_id,
       CASE WHEN """ + bit("th.b_n", "b.mi + :k") + """ = 1 THEN 'out_to_nonzp'
            WHEN """ + bit("th.b_any", "b.mi + :k") + """ = 1 THEN 'out_zp_out'
            WHEN pc.k_pf IS NOT NULL THEN
                 CASE WHEN so.n IS NOT NULL THEN 'out_oth_same_org'
                      WHEN sh.n IS NOT NULL THEN 'out_oth_same_hold'
                      ELSE 'out_oth_other' END
            WHEN """ + bit("th.b_p", "b.mi + :k - 13") + """ = 1
             AND """ + bit("th.b_p", "b.mi + :k - 12") + """ = 0
             AND """ + rng("th.b_p", "b.mi + :k - 11", f"b.mi + :k - {12 - SEAS_RET}") + """ <> 0
                 THEN 'lb_seasonal'
            WHEN om.n IS NULL THEN 'lb_org_stopped'
            WHEN """ + _one_time("th.b_any", "b.mi") + """ THEN 'lb_one_time'
            ELSE 'lb_unknown' END                                          AS lab,
       CASE WHEN pc.k_pf IS NULL THEN CAST(1.0 / pb.k_pf AS float8) ELSE 0 END AS fl,
       """ + bit("th.b_p", "b.mi + :k + 1") + """                          AS out_temp,
       """ + _ten("th.b_p", "b.mi") + """                                   AS ten
FROM t_tr b
JOIN t_pm pb ON pb.epk_id = b.epk_id AND pb.mi = b.mi
JOIN t_thist th ON """ + _tj("th", "b") + """
LEFT JOIN t_pm pc ON pc.epk_id = b.epk_id AND pc.mi = b.mi + :k
LEFT JOIN t_po so ON so.epk_id = b.epk_id AND so.mi = b.mi + :k AND so.org_id = b.org_id
LEFT JOIN t_org o ON o.org_id = b.org_id
LEFT JOIN t_ph sh ON sh.epk_id = b.epk_id AND sh.mi = b.mi + :k AND sh.hold = o.hold
LEFT JOIN t_orgm om ON om.org_id = b.org_id AND om.mi = b.mi + :k
WHERE b.st = 'P' AND b.mi + :k <= :last
  AND """ + bit("th.b_p", "b.mi + :k") + """ = 0
  AND MOD(b.epk_id, :nch) = :ch
"""


def _count_cols(col: str, labels: list[str], prefix: str = "") -> str:
    return ",\n       ".join(f"sum(CASE WHEN {col} = '{x}' THEN 1 ELSE 0 END) AS {prefix}{x}"
                              for x in labels)


def agg_in(part: str) -> str:
    return (f"SELECT mi, org_id, gosb_id,\n       {_count_cols('lab', IN_LABELS)},\n"
            f"       sum(fl) AS fl_in\nFROM {part}\nGROUP BY mi, org_id, gosb_id")


def agg_out(part: str) -> str:
    return (f"SELECT mi, org_id, gosb_id,\n       {_count_cols('lab', OUT_LABELS)},\n"
            f"       {_count_cols('ten', TEN, 'out_ten_')},\n"
            f"       sum(fl) AS fl_out, sum(out_temp) AS out_temp\nFROM {part}\nGROUP BY mi, org_id, gosb_id")


# --------------------------------------------------------------------------- #
# Остатки месяца: портфель, ФЛ, суммы, состояния, совместительство, «мигание»,
# разовые, стаж. Смены зп ↔ не зп — по маскам: смена в месяце t = тройка есть в t и
# t−1 и признак «только не зп» в них разный; «мигающая» — ≥ FLAP_MIN смен за 12 мес.
# --------------------------------------------------------------------------- #
_FLIPS = "((th.b_n # (th.b_n << 1)) & th.b_any & (th.b_any << 1))"
# Число смен за M−11…M — сумма 12 битов (арифметика дешевле приведения к строке).
_FLAP = ("(CASE WHEN c.mi >= 11 THEN " + " + ".join(
    f"(({_FLIPS} >> (c.mi - {j})) & 1)" for j in range(12)) + " ELSE 0 END)")

STOCK = """
SELECT c.mi, c.org_id, c.gosb_id, min(c.tb_id) AS tb_id,
       sum(CASE WHEN c.st = 'P' THEN 1 ELSE 0 END)                          AS pf_recip,
       sum(CASE WHEN c.st = 'P' THEN CAST(1.0 / pc.k_pf AS float8) ELSE 0 END) AS pf_fl,
       sum(c.pf_amt)                                                        AS pf_amt,
       sum(c.fot_amt)                                                       AS fot_amt,
       sum(CASE WHEN c.st = 'Z' THEN 1 ELSE 0 END)                          AS zp_out_recip,
       sum(CASE WHEN c.st = 'N' THEN 1 ELSE 0 END)                          AS nonzp_recip,
       count(*)                                                             AS any_recip,
       sum(c.n_rows)                                                        AS raw_rows,
       sum(CASE WHEN c.st = 'P' AND c.n_pay = 1 THEN 1 ELSE 0 END)          AS single_pay_recip,
       sum(CASE WHEN c.st = 'P' AND pc.k_pf > 1 THEN 1 ELSE 0 END)          AS multi_recip,
       sum(CASE WHEN c.st = 'P' AND pc.k_pf > 1 AND po.n > 1 THEN 1 ELSE 0 END) AS multi_same_org,
       sum(CASE WHEN c.st = 'P' AND pc.k_pf > 1 AND po.n = 1 AND ph.n > po.n
                THEN 1 ELSE 0 END)                                          AS multi_same_hold,
       sum(CASE WHEN c.st = 'P' AND pc.k_pf > 1 AND po.n = 1 AND (ph.n IS NULL OR ph.n = po.n)
                THEN 1 ELSE 0 END)                                          AS multi_other,
       sum(CASE WHEN """ + _FLAP + """ >= :flap_min THEN 1 ELSE 0 END)       AS flapping_recip,
       sum(CASE WHEN c.st = 'P' AND """ + _one_time("th.b_any", "c.mi") + """
                THEN 1 ELSE 0 END)                                          AS one_time_pf,
       sum(CASE WHEN c.st = 'P' AND """ + _one_time("pr.b_any", "c.mi") + """
                THEN 1 ELSE 0 END)                                          AS one_time_person,
       """ + ",\n       ".join(
    f"sum(CASE WHEN c.st = 'P' AND ({_ten('th.b_p', 'c.mi')}) = '{t}' THEN 1 ELSE 0 END) AS ten_{t}"
    for t in TEN) + """
FROM t_tr c
JOIN t_thist th ON """ + _tj("th", "c") + """
JOIN t_pbits pr ON pr.epk_id = c.epk_id
LEFT JOIN t_pm pc ON pc.epk_id = c.epk_id AND pc.mi = c.mi
LEFT JOIN t_po po ON po.epk_id = c.epk_id AND po.mi = c.mi AND po.org_id = c.org_id
LEFT JOIN t_org o ON o.org_id = c.org_id
LEFT JOIN t_ph ph ON ph.epk_id = c.epk_id AND ph.mi = c.mi AND ph.hold = o.hold
WHERE MOD(c.epk_id, :nch) = :ch
GROUP BY c.mi, c.org_id, c.gosb_id
"""


def regroup(part: str, sums: list[str], extra: str = "") -> str:
    """Сложить куски по людям в итог по (месяц, id орг, ГОСБ)."""
    cols = ",\n       ".join(f"sum({c}) AS {c}" for c in sums)
    return f"SELECT mi, org_id, gosb_id{extra},\n       {cols}\nFROM {part}\nGROUP BY mi, org_id, gosb_id"


IN_SUMS = IN_LABELS + ["fl_in"]
OUT_SUMS = OUT_LABELS + [f"out_ten_{t}" for t in TEN] + ["fl_out", "out_temp"]

# Ключи сводки: пары с тройками в M и пары, у которых был портфель в одной из баз.
T_KEYS = """
SELECT mi, org_id, gosb_id FROM (
  SELECT mi, org_id, gosb_id FROM t_s
  UNION ALL SELECT mi + 1, org_id, gosb_id FROM t_s WHERE pf_recip > 0 AND mi + 1 <= :last
  UNION ALL SELECT mi + 12, org_id, gosb_id FROM t_s WHERE pf_recip > 0 AND mi + 12 <= :last
) x
GROUP BY mi, org_id, gosb_id
"""

# Полнота истории сегментов по месяцам: доля портфеля, у пар которого есть сегмент.
SEG_COVERAGE = """
SELECT s.mi, sum(s.pf_recip) AS pf,
       sum(CASE WHEN h.seg IS NOT NULL THEN s.pf_recip ELSE 0 END) AS pf_hist
FROM t_s s
LEFT JOIN t_seg h ON h.mi = s.mi AND h.org_id = s.org_id AND h.gosb_id = s.gosb_id
GROUP BY s.mi
"""

# Текущая сегментация: последний ПОЛНЫЙ месяц истории (:cur_mi выбирает прогон).
T_SEG_CUR = """
SELECT org_id, gosb_id, seg
FROM t_seg
WHERE mi = :cur_mi
"""

# Два сегмента каждого ключа (одним словарём заказчика):
#   seg — сегмент отчётного месяца: история за этот месяц, иначе ЕПК;
#   seg_cur — текущий: история за последний полный месяц по паре, иначе по
#   организации в тот же месяц, иначе ЕПК.
T_KSEG = """
SELECT k.mi, k.org_id, k.gosb_id,
       COALESCE(h.seg, o.seg, '""" + S.NOT_FOUND + """')           AS seg,
       COALESCE(cp.seg, co.seg, o.seg, '""" + S.NOT_FOUND + """')  AS seg_cur,
       CASE WHEN h.seg IS NOT NULL THEN 'история'
            WHEN o.seg IS NOT NULL THEN 'ЕПК'
            ELSE 'нет' END                     AS seg_src
FROM t_keys k
LEFT JOIN t_seg h ON h.mi = k.mi AND h.org_id = k.org_id AND h.gosb_id = k.gosb_id
LEFT JOIN t_seg_cur cp ON cp.org_id = k.org_id AND cp.gosb_id = k.gosb_id
LEFT JOIN (SELECT org_id, min(seg) AS seg FROM t_seg_cur GROUP BY org_id) co ON co.org_id = k.org_id
LEFT JOIN t_org o ON o.org_id = k.org_id
"""


def final_select() -> str:
    """Сборка сводки. Правила пустых значений (NULL):
    * база b пуста, если её месяц раньше 2024-01 (:cmp0) — до того действовал другой отбор;
    * «новый / вернулся», причины ухода, стаж, разовые пусты, если до месяца меньше
      12 месяцев истории (память);
    * «мигание» — если нет 12 месяцев сравнимых состояний;
    * пропуск (m_out_temp) — если следующего месяца ещё нет."""
    kj = "{a}.org_id = k.org_id AND {a}.gosb_id = k.gosb_id"
    sel = ["mo.report_dt", "k.org_id", "k.gosb_id AS sys_gosb_id",
           "COALESCE(s.tb_id, s_m.tb_id, s_y.tb_id) AS sys_tb_id",
           "ks.seg AS seg_rep", "ks.seg_cur", "ks.seg_src"]
    mem = f"k.mi >= {MEM}"
    for c in STOCK_COLS:
        e = f"COALESCE(s.{c}, 0)"
        if c.startswith("ten_") or c.startswith("one_time"):
            e = f"CASE WHEN {mem} THEN {e} END"
        elif c == "flapping_recip":
            e = f"CASE WHEN k.mi >= :cmp0 + 12 THEN {e} END"
        sel.append(f"{e} AS {c}")
    sel.append("(k.mi + 2 > :last) AS prelim")
    joins = ["JOIN t_months mo ON mo.mi = k.mi",
             "JOIN t_kseg ks ON ks.mi = k.mi AND " + kj.format(a="ks"),
             "LEFT JOIN t_s s ON s.mi = k.mi AND " + kj.format(a="s")]
    for b, n in BASES.items():
        on = f"k.mi - {n} >= :cmp0"
        mem_b = f"{on} AND k.mi - {n} - {MEM} >= 0"
        ins = {x: f"COALESCE(i_{b}.{x}, 0)" for x in IN_LABELS}
        outs = {x: f"COALESCE(o_{b}.{x}, 0)" for x in OUT_LABELS}
        fl_in, fl_out = f"COALESCE(i_{b}.fl_in, 0)", f"COALESCE(o_{b}.fl_out, 0)"
        expr = {
            "base_pf": f"COALESCE(s_{b}.pf_recip, 0)",
            "base_seg": f"ks_{b}.seg",
            "in_from_outside": f"{ins['in_new_bank']} + {ins['in_returned']}",
            "in_from_other_org": " + ".join(ins[x] for x in IN_LABELS if x.startswith("in_oth_")),
            "out_left_bank": " + ".join(outs[x] for x in OUT_LABELS if x.startswith("lb_")),
            "out_to_other_org": " + ".join(outs[x] for x in OUT_LABELS if x.startswith("out_oth_")),
            "fl_in": fl_in, "fl_out": fl_out,
            "fl_conc": f"(COALESCE(s.pf_fl, 0) - COALESCE(s_{b}.pf_fl, 0)) - {fl_in} + {fl_out}",
            **ins, **outs,
        }
        for c in base_cols(b):
            cond = mem_b if (c in ("in_new_bank", "in_returned") or c.startswith("lb_")) else on
            sel.append(f"CASE WHEN {cond} THEN {expr[c]} END AS {b}_{c}")
        joins += [f"LEFT JOIN t_s s_{b} ON s_{b}.mi = k.mi - {n} AND " + kj.format(a=f"s_{b}"),
                  f"LEFT JOIN t_kseg ks_{b} ON ks_{b}.mi = k.mi - {n} AND " + kj.format(a=f"ks_{b}"),
                  f"LEFT JOIN t_in_{b} i_{b} ON i_{b}.mi = k.mi AND " + kj.format(a=f"i_{b}"),
                  f"LEFT JOIN t_out_{b} o_{b} ON o_{b}.mi = k.mi AND " + kj.format(a=f"o_{b}")]
    sel.append("CASE WHEN k.mi - 1 >= :cmp0 AND k.mi + 1 <= :last "
               "THEN COALESCE(o_m.out_temp, 0) END AS m_out_temp")
    for t in TEN:
        sel.append(f"CASE WHEN k.mi - 1 >= :cmp0 AND k.mi - 1 - {MEM} >= 0 "
                   f"THEN COALESCE(o_m.out_ten_{t}, 0) END AS m_out_ten_{t}")
    sel = [_typed(x) for x in sel]
    return ("SELECT " + ",\n       ".join(sel) + "\nFROM t_keys k\n" + "\n".join(joins))


# Счётчики — bigint (сумма сумм в Postgres даёт numeric); ФЛ — float8; суммы — numeric.
_NOT_COUNT = {"report_dt", "org_id", "sys_gosb_id", "sys_tb_id", "seg_rep", "seg_cur", "seg_src",
              "pf_fl", "pf_amt", "fot_amt", "prelim"}


def _typed(item: str) -> str:
    expr, _, name = item.rpartition(" AS ")
    if not expr or name in _NOT_COUNT or name.endswith(("_base_seg", "_fl_in", "_fl_out", "_fl_conc")):
        return item
    return f"CAST({expr} AS bigint) AS {name}"


# --------------------------------------------------------------------------- #
# Потоки «организация → организация» к прошлому месяцу: человек ушёл из тройки A
# в другую организацию банка и пришёл в тройку B того же месяца. Реорганизация —
# одно событие, а не «A −5 000 и B +5 000». Мелкие пары (< :pair_min) не хранятся.
# --------------------------------------------------------------------------- #
FLOW_PAIRS = """
SELECT mo.report_dt, d.org_id AS from_org_id, d.gosb_id AS from_gosb_id,
       a.org_id AS to_org_id, a.gosb_id AS to_gosb_id, count(*) AS n_recip
FROM t_out_m_part d
JOIN t_in_m_part a ON a.epk_id = d.epk_id AND a.mi = d.mi
JOIN t_months mo ON mo.mi = d.mi
WHERE d.lab IN ('out_oth_same_org', 'out_oth_same_hold', 'out_oth_other')
  AND a.lab IN ('in_oth_same_org', 'in_oth_same_hold', 'in_oth_other')
  AND d.mi - 1 >= :cmp0
GROUP BY mo.report_dt, d.org_id, d.gosb_id, a.org_id, a.gosb_id
HAVING count(*) >= :pair_min
"""

# --------------------------------------------------------------------------- #
# Постоянные таблицы
# --------------------------------------------------------------------------- #
TARGET = "{schema_t}.yva_ptf_month_org"
PAIRS = "{schema_t}.yva_ptf_flow_pairs"
GP_STORAGE = "WITH (appendonly=true, orientation=column, compresstype=zlib, compresslevel=5)"

# Журнал прогонов: итоги по фиксированным срезам (банк, ТБ, сегмент отчётного месяца,
# текущий сегмент) и ТОЧНОЕ число разных людей — дробные ФЛ точны только по банку.
CREATE_JOURNAL = """
CREATE TABLE IF NOT EXISTS {schema_t}.yva_ptf_journal (
  run_id bigint, run_ts timestamp, def_ver integer, report_dt date,
  slice_type varchar(16), slice_value varchar(128),
  pf_recip bigint, persons bigint, any_recip bigint, raw_rows bigint, fot_amt numeric,
  window_from date, window_to date, seg_cur_dt date, checks_ok boolean, duration_min float8
)"""

# Люди в портфеле со срезами их троек (для точного числа людей по срезам).
T_PSLICE = """
SELECT t.mi, t.epk_id, CAST(t.tb_id AS varchar(128)) AS tb, ks.seg AS seg_rep, ks.seg_cur
FROM t_tr t
JOIN t_kseg ks ON ks.mi = t.mi AND ks.org_id = t.org_id AND ks.gosb_id = t.gosb_id
WHERE t.st = 'P'
"""


def _persons(col: str, typ: str) -> str:
    return (f"SELECT mi, '{typ}' AS slice_type, {col} AS slice_value, count(*) AS persons\n"
            f"FROM (SELECT mi, {col}, epk_id FROM t_pslice GROUP BY mi, {col}, epk_id) x\n"
            f"GROUP BY mi, {col}")


T_JPERS = "\nUNION ALL\n".join(
    ["SELECT mi, 'bank' AS slice_type, 'банк' AS slice_value, count(*) AS persons FROM t_pm GROUP BY mi",
     _persons("tb", "tb"), _persons("seg_rep", "seg_rep"), _persons("seg_cur", "seg_cur")])


def _jrow(col: str | None, typ: str) -> str:
    val = f"CAST({col} AS varchar(128))" if col else "CAST('банк' AS varchar(128))"
    grp = f"report_dt, {col}" if col else "report_dt"
    return (f"SELECT report_dt, '{typ}' AS slice_type, {val} AS slice_value,\n"
            f"       sum(pf_recip) AS pf_recip, sum(any_recip) AS any_recip, sum(raw_rows) AS raw_rows,\n"
            f"       sum(fot_amt) AS fot_amt\n"
            f"FROM {{schema_t}}.yva_ptf_month_org_new GROUP BY {grp}")


T_JROWS = "\nUNION ALL\n".join([_jrow(None, "bank"), _jrow("sys_tb_id", "tb"),
                                _jrow("seg_rep", "seg_rep"), _jrow("seg_cur", "seg_cur")])

INSERT_JOURNAL = """
INSERT INTO {schema_t}.yva_ptf_journal
SELECT :run_id, CAST(:run_ts AS timestamp), :def_ver, r.report_dt, r.slice_type, r.slice_value,
       r.pf_recip, p.persons, r.any_recip, r.raw_rows, r.fot_amt,
       CAST(:window_from AS date), CAST(:window_to AS date), CAST(:seg_cur_dt AS date),
       :checks_ok, :duration_min
FROM t_jrows r
JOIN t_months mo ON mo.report_dt = r.report_dt
LEFT JOIN t_jpers p ON p.mi = mo.mi AND p.slice_type = r.slice_type
                   AND p.slice_value IS NOT DISTINCT FROM r.slice_value
"""

PREV_RUN = """
SELECT j.report_dt, j.pf_recip, j.run_id
FROM {schema_t}.yva_ptf_journal j
WHERE j.slice_type = 'bank'
  AND j.run_id = (SELECT max(run_id) FROM {schema_t}.yva_ptf_journal WHERE checks_ok)
"""

# Поколения: входы сторожа на дату прогона — атомы «месяц × сегмент отчётного месяца
# × ТБ × ГОСБ» и крупные организации (их ячейки) по ключевым мерам. Копится: честная
# проверка сторожа «на дату» и пересчёт прошлого по срезам. Мелкие организации среза =
# атом − крупные, отдельно не хранятся.
VINT_MEAS = {
    "pf_recip": "pf_recip", "pf_fl": "pf_fl", "any_recip": "any_recip", "raw_rows": "raw_rows",
    "fot_amt": "fot_amt", "single_pay_recip": "single_pay_recip", "multi_recip": "multi_recip",
    "one_time_pf": "one_time_pf",
    "m_base_pf": "m_base_pf", "m_in_from_outside": "m_in_from_outside",
    "m_in_from_other_org": "m_in_from_other_org",
    "m_in_codes": "m_in_from_nonzp + m_in_from_zp_out",
    "m_out_left_bank": "m_out_left_bank", "m_lb_unknown": "m_lb_unknown",
    "m_out_to_other_org": "m_out_to_other_org", "m_out_codes": "m_out_to_nonzp + m_out_zp_out",
    "y_base_pf": "y_base_pf", "y_in_from_outside": "y_in_from_outside",
    "y_in_from_other_org": "y_in_from_other_org", "y_out_left_bank": "y_out_left_bank",
    "y_out_to_other_org": "y_out_to_other_org",
}

CREATE_VINTAGE = """
CREATE TABLE IF NOT EXISTS {schema_t}.yva_ptf_vintage (
  run_id bigint, def_ver integer, lvl varchar(8), report_dt date, seg_rep varchar(64),
  sys_tb_id integer, sys_gosb_id integer, org_id bigint,
  """ + ", ".join(f"{m} {'float8' if m == 'pf_fl' else 'numeric' if m == 'fot_amt' else 'bigint'}"
                  for m in VINT_MEAS) + """
)"""


def insert_vintage() -> str:
    meas = ", ".join(f"sum({e}) AS {m}" for m, e in VINT_MEAS.items())
    cols = ", ".join(VINT_MEAS)
    return f"""
INSERT INTO {{schema_t}}.yva_ptf_vintage
  (run_id, def_ver, lvl, report_dt, seg_rep, sys_tb_id, sys_gosb_id, org_id, {cols})
SELECT :run_id, :def_ver, 'atom', report_dt, seg_rep, sys_tb_id, sys_gosb_id, CAST(NULL AS bigint),
       {meas}
FROM {{schema_t}}.yva_ptf_month_org
GROUP BY report_dt, seg_rep, sys_tb_id, sys_gosb_id
UNION ALL
SELECT :run_id, :def_ver, 'org', f.report_dt, f.seg_rep, f.sys_tb_id, f.sys_gosb_id, f.org_id,
       {meas.replace('sum(', 'sum(f.').replace(' + ', ' + f.')}
FROM {{schema_t}}.yva_ptf_month_org f
JOIN (SELECT org_id FROM (SELECT org_id, report_dt, sum(pf_recip) AS pf
                          FROM {{schema_t}}.yva_ptf_month_org GROUP BY org_id, report_dt) o
      GROUP BY org_id HAVING max(pf) >= :vint_org_min) big ON big.org_id = f.org_id
GROUP BY f.report_dt, f.seg_rep, f.sys_tb_id, f.sys_gosb_id, f.org_id
"""


MONTH_TOTALS = """
SELECT report_dt,
       sum(pf_recip) AS pf_recip, sum(pf_fl) AS pf_fl, sum(fot_amt) AS fot_amt,
       sum(any_recip) AS any_recip, sum(raw_rows) AS raw_rows,
       sum(CASE WHEN seg_src = 'история' THEN pf_recip ELSE 0 END) AS pf_hist,
       sum(m_fl_conc) AS m_conc, sum(y_fl_conc) AS y_conc
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
        ins = " + ".join(f"{b}_{x}" for x in ["in_from_outside", "in_from_other_org",
                                               "in_from_nonzp", "in_from_zp_out"])
        outs = " + ".join(f"{b}_{x}" for x in ["out_left_bank", "out_to_other_org",
                                                "out_to_nonzp", "out_zp_out"])
        lbs = " + ".join(f"{b}_{x}" for x in base_cols(b) if x.startswith("lb_"))
        parts.append(f"sum(CASE WHEN {b}_base_pf IS NOT NULL AND "
                     f"pf_recip - {b}_base_pf <> ({ins}) - ({outs}) THEN 1 ELSE 0 END) AS {b}_identity")
        parts.append(f"sum(CASE WHEN {b}_in_new_bank IS NOT NULL AND "
                     f"{b}_in_from_outside <> {b}_in_new_bank + {b}_in_returned THEN 1 ELSE 0 END)"
                     f" AS {b}_outside_parts")
        parts.append(f"sum(CASE WHEN {b}_in_from_other_org <> {b}_in_oth_same_org + {b}_in_oth_same_hold"
                     f" + {b}_in_oth_other THEN 1 ELSE 0 END) AS {b}_in_parts")
        parts.append(f"sum(CASE WHEN {b}_out_to_other_org <> {b}_out_oth_same_org + {b}_out_oth_same_hold"
                     f" + {b}_out_oth_other THEN 1 ELSE 0 END) AS {b}_out_parts")
        parts.append(f"sum(CASE WHEN {b}_lb_unknown IS NOT NULL AND {b}_out_left_bank <> {lbs} "
                     f"THEN 1 ELSE 0 END) AS {b}_lb_parts")
    parts.append("sum(CASE WHEN m_out_ten_lt3 IS NOT NULL AND m_out_ten_lt3 + m_out_ten_3_12 + m_out_ten_12p"
                 " <> m_out_left_bank + m_out_to_other_org + m_out_to_nonzp + m_out_zp_out "
                 "THEN 1 ELSE 0 END) AS out_ten_parts")
    parts.append("sum(CASE WHEN multi_recip <> multi_same_org + multi_same_hold + multi_other "
                 "THEN 1 ELSE 0 END) AS multi_parts")
    parts.append("sum(CASE WHEN ten_lt3 IS NOT NULL AND pf_recip <> ten_lt3 + ten_3_12 + ten_12p "
                 "THEN 1 ELSE 0 END) AS tenure_parts")
    parts.append("sum(CASE WHEN any_recip <> pf_recip + zp_out_recip + nonzp_recip "
                 "THEN 1 ELSE 0 END) AS state_parts")
    return "SELECT " + ",\n       ".join(parts) + "\nFROM {schema_t}.yva_ptf_month_org_new"


# Подмена — ОДНОЙ транзакцией: сбой посередине не оставит схему без сводки.
SWAP = [
    "DROP TABLE IF EXISTS {schema_t}.yva_ptf_month_org_old",
    "ALTER TABLE IF EXISTS {schema_t}.yva_ptf_month_org RENAME TO yva_ptf_month_org_old",
    "ALTER TABLE {schema_t}.yva_ptf_month_org_new RENAME TO yva_ptf_month_org",
    "DROP TABLE IF EXISTS {schema_t}.yva_ptf_month_org_old",
    "DROP TABLE IF EXISTS {schema_t}.yva_ptf_flow_pairs_old",
    "ALTER TABLE IF EXISTS {schema_t}.yva_ptf_flow_pairs RENAME TO yva_ptf_flow_pairs_old",
    "ALTER TABLE {schema_t}.yva_ptf_flow_pairs_new RENAME TO yva_ptf_flow_pairs",
    "DROP TABLE IF EXISTS {schema_t}.yva_ptf_flow_pairs_old",
]
DROP_NEW = ["DROP TABLE IF EXISTS {schema_t}.yva_ptf_month_org_new",
            "DROP TABLE IF EXISTS {schema_t}.yva_ptf_flow_pairs_new"]

# Распределение временных таблиц.
DIST = {"t_months": "mi", "t_tik": "inn", "t_exc": "inn_exc", "t_org": "org_id", "t_seg": "org_id",
        "t_tr": "epk_id", "t_pm": "epk_id", "t_thist": "epk_id", "t_pbits": "epk_id",
        "t_po": "epk_id", "t_ph": "epk_id", "t_orgm": "org_id",
        "t_s_part": "org_id", "t_s": "org_id", "t_keys": "org_id", "t_seg_cur": "org_id",
        "t_kseg": "org_id", "t_pslice": "epk_id", "t_jpers": "mi", "t_jrows": "report_dt",
        **{f"t_{d}_{b}_part": "epk_id" for d in ("in", "out") for b in BASES},
        **{f"t_{d}_{b}": "org_id" for d in ("in", "out") for b in BASES}}
