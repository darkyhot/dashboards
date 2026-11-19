"""Весь SQL прогона. Ни одной строки SQL за пределами этого файла.

Плейсхолдеры `{schema}`, `{schema_t}`, `{payroll}`, `{payroll_d}`, `{seg_hist}`, `{code_col}`
подставляет `db.render`; значения — именованными параметрами `:name`.

Ловушки, на которых такой расчёт ломается тихо
-----------------------------------------------
1. **Партиция.** Ведомости партиционированы по `report_dt`; каждое обращение к ним
   ограничено ОДНИМ месяцем (`= CAST(:m AS date)`) — проверяет самопроверка. Дневная
   витрина `{payroll_d}` — диапазоном дат не длиннее двух соседних партиций
   (`:s_from` … `:c_to`), тоже проверяется.
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

from itertools import combinations

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
            "fl_in", "fl_out", "fl_conc", "in_person_new", "out_person_gone", "in_conc", "out_conc"]
    return [c for c in cols if not (b == "y" and c == "lb_seasonal")]


M_ONLY = ["m_out_temp", "m_out_temp_left", "m_in_temp_outside",
          "m_out_ten_lt3", "m_out_ten_3_12", "m_out_ten_12p"]


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
_TRIPLES = """
SELECT x.mi, x.epk_id, x.org_id, x.gosb_id, x.tb_id,
       CASE WHEN x.in_pf THEN 'P' WHEN x.pos_pf THEN 'Z' ELSE 'N' END AS st,
       CASE WHEN x.in_pf
            THEN x.a_t2 + CASE WHEN x.exc OR x.sum_pf > :amt_min THEN x.a_pf ELSE 0 END
            ELSE 0 END                                        AS pf_amt,
       x.a_t2 + CASE WHEN x.exc OR x.sum_fot > :amt_min THEN x.a_fot ELSE 0 END AS fot_amt,
       x.n_rows, x.n_pay@org_output@
FROM (
  SELECT a.*,
         (e.inn_exc IS NOT NULL)                                          AS exc,
         (a.pos_t2 OR (a.pos_pf AND (e.inn_exc IS NOT NULL OR a.sum_pf > :amt_min))) AS in_pf
  FROM (
    SELECT @period@ AS mi, p.epk_id,
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
           sum(CASE WHEN p.amt > 0 THEN COALESCE(p.transaction_qty, 1) ELSE 0 END) AS n_pay@org_input@
    FROM {schema}.@source@ p
    @joins@
    LEFT JOIN {schema}.uzp_dim_enrollment_type d ON d.enrollment_type_id = p.{code_col}
    LEFT JOIN t_tik k ON k.inn = """ + org_num("p.inn") + """
    WHERE @dates@
      AND k.inn IS NULL
      AND p.epk_id IS NOT NULL
      AND p.inn ~ """ + MASK + """
      AND """ + RELEV + """
    GROUP BY 1, 2, 3, 4
  ) a
  LEFT JOIN t_exc e ON e.inn_exc = a.org_id
) x
WHERE x.pos_any
"""

def _triples(source, period, dates, joins="", org_digits=False):
    return (_TRIPLES.replace("@source@", source).replace("@period@", period)
            .replace("@dates@", dates).replace("@joins@", joins)
            .replace("@org_output@", ", x.org_digits" if org_digits else "")
            .replace("@org_input@", ", CAST(max(length(p.inn)) AS smallint) AS org_digits" if org_digits else ""))


T_TR_MONTH = _triples("{payroll}", "CAST(:mi AS integer)", "p.report_dt = CAST(:m AS date)")
LATEST_TR_MONTH = _triples("{payroll}", "CAST(:mi AS integer)", "p.report_dt = CAST(:m AS date)", org_digits=True)


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
       CASE WHEN pb.k_pf IS NULL THEN CAST(1.0 / pc.k_pf AS float8) ELSE 0 END AS fl,
       CASE WHEN pb.k_pf IS NULL THEN 1 ELSE 0 END                       AS in_new,
       CASE WHEN pb.k_pf IS NOT NULL AND pc.k_pf > pb.k_pf
            THEN CAST(pc.k_pf - pb.k_pf AS float8) / count(*) OVER (PARTITION BY c.mi, c.epk_id)
            ELSE 0 END                                                    AS in_conc,
       CASE WHEN :k = 1 AND """ + bit("th.b_p", "c.mi - 2") + """ = 1
            THEN 1 ELSE 0 END AS in_temp
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
       CASE WHEN pc.k_pf IS NULL THEN 1 ELSE 0 END                       AS out_gone,
       CASE WHEN pc.k_pf IS NOT NULL AND pb.k_pf > pc.k_pf
            THEN CAST(pb.k_pf - pc.k_pf AS float8) / count(*) OVER (PARTITION BY b.mi, b.epk_id)
            ELSE 0 END                                                    AS out_conc,
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
            f"       sum(fl) AS fl_in, sum(in_new) AS in_person_new, sum(in_conc) AS in_conc,\n"
            f"       sum(CASE WHEN lab IN ('in_new_bank', 'in_returned') THEN in_temp ELSE 0 END) "
            f"AS in_temp_outside\nFROM {part}\nGROUP BY mi, org_id, gosb_id")


def agg_out(part: str) -> str:
    return (f"SELECT mi, org_id, gosb_id,\n       {_count_cols('lab', OUT_LABELS)},\n"
            f"       {_count_cols('ten', TEN, 'out_ten_')},\n"
            f"       sum(fl) AS fl_out, sum(out_gone) AS out_person_gone, sum(out_conc) AS out_conc,\n"
            f"       sum(out_temp) AS out_temp,\n"
            f"       sum(CASE WHEN lab LIKE 'lb_%' THEN out_temp ELSE 0 END) "
            f"AS out_temp_left\nFROM {part}\nGROUP BY mi, org_id, gosb_id")


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


IN_SUMS = IN_LABELS + ["fl_in", "in_person_new", "in_conc"]
OUT_SUMS = OUT_LABELS + [f"out_ten_{t}" for t in TEN] + ["fl_out", "out_person_gone", "out_conc", "out_temp"]

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
#   seg — сегмент отчётного месяца: история пары за этот месяц, иначе история той же
#         организации в другом ГОСБ (сегмент один на организацию — правило аналитика),
#         иначе ЕПК;
#   seg_cur — текущий: история за последний полный месяц по паре, иначе по
#   организации в тот же месяц, иначе ЕПК.
T_KSEG = """
SELECT k.mi, k.org_id, k.gosb_id,
       COALESCE(h.seg, ho.seg, o.seg, '""" + S.NOT_FOUND + """')   AS seg,
       COALESCE(cp.seg, co.seg, o.seg, '""" + S.NOT_FOUND + """')  AS seg_cur,
       CASE WHEN h.seg IS NOT NULL THEN 'история'
            WHEN ho.seg IS NOT NULL THEN 'история орг.'
            WHEN o.seg IS NOT NULL THEN 'ЕПК'
            ELSE 'нет' END                     AS seg_src
FROM t_keys k
LEFT JOIN t_seg h ON h.mi = k.mi AND h.org_id = k.org_id AND h.gosb_id = k.gosb_id
LEFT JOIN (SELECT mi, org_id, min(seg) AS seg FROM t_seg GROUP BY mi, org_id) ho
       ON ho.mi = k.mi AND ho.org_id = k.org_id
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
            "in_person_new": f"COALESCE(i_{b}.in_person_new, 0)",
            "out_person_gone": f"COALESCE(o_{b}.out_person_gone, 0)",
            "in_conc": f"COALESCE(i_{b}.in_conc, 0)",
            "out_conc": f"COALESCE(o_{b}.out_conc, 0)",
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
    sel.append("CASE WHEN k.mi - 1 >= :cmp0 AND k.mi + 1 <= :last "
               "THEN COALESCE(o_m.out_temp_left, 0) END AS m_out_temp_left")
    sel.append("CASE WHEN k.mi - 2 >= :cmp0 "
               "THEN COALESCE(i_m.in_temp_outside, 0) END AS m_in_temp_outside")
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
    if not expr or name in _NOT_COUNT or name.endswith(("_base_seg", "_fl_in", "_fl_out", "_fl_conc", "_in_conc", "_out_conc")):
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
# Меры поколения — они же входы сторожа. Помесячные движения разложены до причин ухода
# (календарь виден по «вернулся в M+1» и «одна выплата», остановка организации — по
# lb_org_stopped) и до людей / совместительства (fl_*). Год к году — полное тождество.
VINT_MEAS = {
    "pf_recip": "pf_recip", "pf_fl": "pf_fl", "any_recip": "any_recip", "raw_rows": "raw_rows",
    "fot_amt": "fot_amt", "single_pay_recip": "single_pay_recip", "multi_recip": "multi_recip",
    "one_time_pf": "one_time_pf",
    "m_base_pf": "m_base_pf", "m_in_from_outside": "m_in_from_outside",
    "m_in_returned": "m_in_returned",
    "m_in_temp_outside": "m_in_temp_outside",
    "m_in_from_other_org": "m_in_from_other_org",
    "m_in_codes": "m_in_from_nonzp + m_in_from_zp_out",
    "m_out_left_bank": "m_out_left_bank", "m_lb_unknown": "m_lb_unknown",
    "m_lb_seasonal": "m_lb_seasonal", "m_lb_org_stopped": "m_lb_org_stopped",
    "m_lb_one_time": "m_lb_one_time",
    "m_out_to_other_org": "m_out_to_other_org", "m_out_codes": "m_out_to_nonzp + m_out_zp_out",
    "m_out_temp": "m_out_temp",
    "m_out_temp_left": "m_out_temp_left",
    "m_fl_in": "m_fl_in", "m_fl_out": "m_fl_out", "m_fl_conc": "m_fl_conc",
    "y_base_pf": "y_base_pf", "y_in_from_outside": "y_in_from_outside",
    "y_in_from_other_org": "y_in_from_other_org", "y_in_codes": "y_in_from_nonzp + y_in_from_zp_out",
    "y_out_left_bank": "y_out_left_bank", "y_out_to_other_org": "y_out_to_other_org",
    "y_out_codes": "y_out_to_nonzp + y_out_zp_out", "y_fl_conc": "y_fl_conc",
}
VINT_FLOAT = {"pf_fl", "m_fl_in", "m_fl_out", "m_fl_conc", "y_fl_conc"}


def vint_type(m: str) -> str:
    return "float8" if m in VINT_FLOAT else "numeric" if m == "fot_amt" else "bigint"


CREATE_VINTAGE = """
CREATE TABLE IF NOT EXISTS {schema_t}.yva_ptf_vintage (
  run_id bigint, def_ver integer, lvl varchar(8), report_dt date, seg_rep varchar(64),
  sys_tb_id integer, sys_gosb_id integer, org_id bigint,
  """ + ", ".join(f"{m} {vint_type(m)}" for m in VINT_MEAS) + """
)"""

# Таблица поколений копится: новые меры добавляются столбцами, у старых прогонов — пусто.
VINT_COLS = """
SELECT column_name FROM information_schema.columns
WHERE table_schema = :schema AND table_name = 'yva_ptf_vintage'
"""


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


# --------------------------------------------------------------------------- #
# Источники срезов шага 2: холдинг и отрасль — текущий срез ЕПК (в сводке не хранятся).
# Только агрегаты; номера организаций и названия холдингов не выводятся.
# Greenplum 9.4: без FILTER и count(DISTINCT) — CASE и подзапросы.
# --------------------------------------------------------------------------- #
_SLICE_BASE = """
WITH last AS (SELECT max(report_dt) AS dt FROM {schema_t}.yva_ptf_month_org),
o AS (
  SELECT m.org_id, sum(m.pf_recip) AS pf
  FROM {schema_t}.yva_ptf_month_org m
  JOIN last l ON m.report_dt = l.dt
  GROUP BY m.org_id
  HAVING sum(m.pf_recip) > 0
),
e AS (
  SELECT c.inn AS org_id,
         min(NULLIF(btrim(c.holding_name), ''))  AS hold,
         min(NULLIF(btrim(c.industry_name), '')) AS ind
  FROM {schema}.uzp_data_epk_consolidation c
  WHERE c.inn IS NOT NULL
  GROUP BY c.inn
),
j AS (
  SELECT o.org_id, o.pf, CASE WHEN e.org_id IS NULL THEN 0 ELSE 1 END AS in_epk, e.hold, e.ind
  FROM o LEFT JOIN e ON e.org_id = o.org_id
)
"""

SLICE_COVERAGE = _SLICE_BASE + """
SELECT (SELECT dt FROM last)                                  AS report_dt,
       count(*)                                               AS n_org,
       sum(pf)                                                AS pf,
       sum(CASE WHEN in_epk = 1 THEN pf ELSE 0 END)           AS pf_in_epk,
       sum(CASE WHEN ind IS NOT NULL THEN pf ELSE 0 END)      AS pf_with_ind,
       sum(CASE WHEN hold IS NOT NULL THEN pf ELSE 0 END)     AS pf_with_hold,
       sum(CASE WHEN hold IS NOT NULL THEN 1 ELSE 0 END)      AS n_org_with_hold,
       (SELECT count(*) FROM (SELECT ind FROM j WHERE ind IS NOT NULL GROUP BY ind) a)   AS n_ind,
       (SELECT count(*) FROM (SELECT hold FROM j WHERE hold IS NOT NULL GROUP BY hold) b) AS n_hold
FROM j
"""

SLICE_INDUSTRY = _SLICE_BASE + """
SELECT COALESCE(ind, '(нет в ЕПК или не указана)') AS industry, count(*) AS n_org, sum(pf) AS pf
FROM j
GROUP BY COALESCE(ind, '(нет в ЕПК или не указана)')
ORDER BY sum(pf) DESC
"""

# Размер холдингов без названий: сколько холдингов и получателей в классах размера.
SLICE_HOLDING_SIZE = _SLICE_BASE + """
SELECT CASE WHEN h.pf >= 100000 THEN '1) от 100 тыс.'
            WHEN h.pf >= 10000 THEN '2) 10–100 тыс.'
            WHEN h.pf >= 1000 THEN '3) 1–10 тыс.'
            ELSE '4) до 1 тыс.' END                          AS size_class,
       count(*)                                           AS n_hold,
       sum(h.n_org)                                       AS n_org,
       sum(CASE WHEN h.n_org >= 2 THEN 1 ELSE 0 END)      AS n_hold_multi_org,
       sum(h.pf)                                          AS pf
FROM (SELECT hold, count(*) AS n_org, sum(pf) AS pf FROM j WHERE hold IS NOT NULL GROUP BY hold) h
GROUP BY 1
ORDER BY 1
"""


# --------------------------------------------------------------------------- #
# Календарь выплат по дневной витрине (шаг 2: подпись «календарь»).
# Тот же отбор строк и порог, что в T_TR_MONTH, но тройка считается дважды:
#   календарный месяц M   — даты :c_from … :c_to (1-е … последнее число M);
#   сдвинутый месяц M     — даты :s_from … :s_to (16-е M−1 … 15-е M).
# Выплата, переехавшая через 1-е число (выходной, праздник, ранняя выплата), остаётся
# в том же сдвинутом месяце. Читаются даты :s_from … :c_to — две соседние партиции.
# Наружу — только счётчики троек по организации × ГОСБ (агрегат yva_).
# --------------------------------------------------------------------------- #
CREATE_CALENDAR = """
CREATE TABLE IF NOT EXISTS {schema_t}.yva_ptf_calendar (
  report_dt date, org_id bigint, sys_gosb_id integer, sys_tb_id integer,
  n_cal bigint, n_sh bigint, n_both bigint,
  amt_min numeric, def_ver integer, created_at timestamp
)"""

CALENDAR_DELETE = """
DELETE FROM {schema_t}.yva_ptf_calendar
WHERE report_dt = CAST(:m AS date) AND amt_min = :amt_min AND def_ver = :def_ver
"""

CALENDAR_INSERT = """
INSERT INTO {schema_t}.yva_ptf_calendar
  (report_dt, org_id, sys_gosb_id, sys_tb_id, n_cal, n_sh, n_both, amt_min, def_ver, created_at)
SELECT CAST(:m AS date), x.org_id, x.gosb_id, x.tb_id,
       sum(CASE WHEN x.in_c THEN 1 ELSE 0 END),
       sum(CASE WHEN x.in_s THEN 1 ELSE 0 END),
       sum(CASE WHEN x.in_c AND x.in_s THEN 1 ELSE 0 END),
       :amt_min, :def_ver, CAST(:created_at AS timestamp)
FROM (
  SELECT a.org_id, a.gosb_id, a.tb_id,
         (a.t2_c OR (a.pf_c AND (e.inn_exc IS NOT NULL OR a.sum_c > :amt_min))) AS in_c,
         (a.t2_s OR (a.pf_s AND (e.inn_exc IS NOT NULL OR a.sum_s > :amt_min))) AS in_s
  FROM (
    SELECT p.epk_id,
           """ + org_num("p.inn") + """ AS org_id,
           COALESCE(""" + gosb_fix("p.sys_gosb_id", "p.sys_tb_id", "p.segment_name") + """, -1) AS gosb_id,
           min(p.sys_tb_id)                                                     AS tb_id,
           sum(CASE WHEN p.report_dt >= CAST(:c_from AS date)
                     AND COALESCE(d.is_portfolio_enrollment, false) THEN p.amt ELSE 0 END) AS sum_c,
           sum(CASE WHEN p.report_dt <= CAST(:s_to AS date)
                     AND COALESCE(d.is_portfolio_enrollment, false) THEN p.amt ELSE 0 END) AS sum_s,
           bool_or(p.report_dt >= CAST(:c_from AS date)
                   AND COALESCE(d.is_portfolio_enrollment, false) AND p.amt > 0)         AS pf_c,
           bool_or(p.report_dt <= CAST(:s_to AS date)
                   AND COALESCE(d.is_portfolio_enrollment, false) AND p.amt > 0)         AS pf_s,
           bool_or(p.report_dt >= CAST(:c_from AS date)
                   AND COALESCE(d.enrollment_type_id = 2, false) AND p.amt > 0)          AS t2_c,
           bool_or(p.report_dt <= CAST(:s_to AS date)
                   AND COALESCE(d.enrollment_type_id = 2, false) AND p.amt > 0)          AS t2_s
    FROM {schema}.{payroll_d} p
    LEFT JOIN {schema}.uzp_dim_enrollment_type d ON d.enrollment_type_id = p.{code_col}
    LEFT JOIN t_tik k ON k.inn = """ + org_num("p.inn") + """
    WHERE p.report_dt >= CAST(:s_from AS date) AND p.report_dt <= CAST(:c_to AS date)
      AND k.inn IS NULL
      AND p.epk_id IS NOT NULL
      AND p.inn ~ """ + MASK + """
      AND """ + RELEV + """
    GROUP BY 1, 2, 3
  ) a
  LEFT JOIN t_exc e ON e.inn_exc = a.org_id
) x
WHERE x.in_c OR x.in_s
GROUP BY x.org_id, x.gosb_id, x.tb_id
"""

CALENDAR_DONE = """
SELECT report_dt, count(*) AS n_rows, sum(n_cal) AS n_cal, max(created_at) AS created_at
FROM {schema_t}.yva_ptf_calendar
WHERE amt_min = :amt_min AND def_ver = :def_ver
GROUP BY report_dt
"""

# Сегмент — отчётный сегмент сводки за тот же месяц; если пары «орг × ГОСБ» в этом месяце
# в сводке нет (выплата только во «сдвинутой» части) — её сегмент в ближайших месяцах.
CALENDAR_SUMMARY = """
SELECT c.report_dt, COALESCE(s.seg, s2.seg, '(нет в сводке)') AS seg, c.sys_tb_id,
       sum(c.n_cal) AS n_cal, sum(c.n_sh) AS n_sh, sum(c.n_both) AS n_both
FROM {schema_t}.yva_ptf_calendar c
LEFT JOIN (
  SELECT report_dt, org_id, sys_gosb_id, min(seg_rep) AS seg
  FROM {schema_t}.yva_ptf_month_org
  WHERE report_dt >= CAST(:d_from AS date) AND report_dt <= CAST(:d_to AS date)
  GROUP BY report_dt, org_id, sys_gosb_id
) s ON s.report_dt = c.report_dt AND s.org_id = c.org_id AND s.sys_gosb_id = c.sys_gosb_id
LEFT JOIN (
  SELECT org_id, sys_gosb_id, min(seg_rep) AS seg
  FROM {schema_t}.yva_ptf_month_org
  WHERE report_dt >= CAST(:d_from AS date) - interval '2 month'
    AND report_dt <= CAST(:d_to AS date) + interval '2 month'
  GROUP BY org_id, sys_gosb_id
) s2 ON s2.org_id = c.org_id AND s2.sys_gosb_id = c.sys_gosb_id
WHERE c.amt_min = :amt_min AND c.def_ver = :def_ver
  AND c.report_dt >= CAST(:d_from AS date) AND c.report_dt <= CAST(:d_to AS date)
GROUP BY 1, 2, 3
"""

# Месячная сводка в тех же срезах — сверка: календарный месяц из дневной витрины обязан
# совпасть с месячной (тот же отбор и порог).
CALENDAR_MONTHLY = """
SELECT report_dt, COALESCE(seg_rep, '(нет в сводке)') AS seg, sys_tb_id, sum(pf_recip) AS pf
FROM {schema_t}.yva_ptf_month_org
WHERE report_dt >= CAST(:d_from AS date) AND report_dt <= CAST(:d_to AS date)
GROUP BY 1, 2, 3
"""

# Отличие банка от прошлого года по месяцам сводки — для автоматического выбора месяцев.
CALENDAR_BANK = """
SELECT report_dt, sum(pf_recip) AS pf
FROM {schema_t}.yva_ptf_month_org
GROUP BY report_dt
ORDER BY report_dt
"""

# Выплаты по дням у границы месяца: последние дни M−1 и первые дни M (строки зп-видов > 0,
# без ТИК/ЦИК — их выплаты к выборам дали бы ложный всплеск). Окно месяца M хранится
# в yva_ptf_calendar_days (дата × число строк) и повторно не читается.
CREATE_CALENDAR_DAYS = """
CREATE TABLE IF NOT EXISTS {schema_t}.yva_ptf_calendar_days (
  report_dt date, pay_dt date, n_rows bigint, def_ver integer, created_at timestamp
)"""

CALENDAR_DAYS_CACHED = """
SELECT pay_dt, n_rows FROM {schema_t}.yva_ptf_calendar_days
WHERE report_dt = CAST(:m AS date) AND def_ver = :def_ver
"""

CALENDAR_DAYS_DELETE = """
DELETE FROM {schema_t}.yva_ptf_calendar_days
WHERE report_dt = CAST(:m AS date) AND def_ver = :def_ver
"""

CALENDAR_DAYS_INSERT = """
INSERT INTO {schema_t}.yva_ptf_calendar_days (report_dt, pay_dt, n_rows, def_ver, created_at)
SELECT CAST(:m AS date), p.report_dt, count(*), :def_ver, CAST(:created_at AS timestamp)
FROM {schema}.{payroll_d} p
JOIN {schema}.uzp_dim_enrollment_type d ON d.enrollment_type_id = p.{code_col}
LEFT JOIN (SELECT inn FROM {schema_t}.yva_tik_cik WHERE is_tik_cik IS TRUE GROUP BY inn) k
       ON k.inn = """ + org_num("p.inn") + """
WHERE p.report_dt >= CAST(:s_from AS date) AND p.report_dt <= CAST(:c_to AS date)
  AND k.inn IS NULL
  AND d.is_portfolio_enrollment AND p.amt > 0
  AND p.epk_id IS NOT NULL
  AND p.inn ~ """ + MASK + """
  AND """ + RELEV + """
GROUP BY p.report_dt
"""


# --------------------------------------------------------------------------- #
# Лента шага 2: ряды «срез × месяц» по всей сводке (все организации).
# Шаг 1 — временная t_lenta: строки сводки свёрнуты до атома × отрасль × холдинг ×
# класс размера × крупная организация (мелкие сливаются). Шаг 2 — ряды срезов.
# Холдинг и отрасль — текущий срез ЕПК. Наружу номера организаций и названия холдингов
# не уходят: код — первые 8 знаков md5 (по нему же SQL проверки находит строку).
# Пустая мера у активной строки (есть портфель или база) — счётчик nn_*: «неизвестно».
# --------------------------------------------------------------------------- #
LENTA_MEAS = ["pf_recip", "pf_fl", "m_base_pf", "m_in_from_outside", "m_in_temp_outside",
              "m_in_from_other_org", "m_in_from_nonzp", "m_in_from_zp_out",
              "m_out_left_bank", "m_out_temp_left", "m_out_to_other_org", "m_out_to_nonzp",
              "m_out_zp_out", "m_lb_seasonal", "m_lb_org_stopped", "m_lb_one_time", "m_lb_unknown",
              "m_fl_in", "m_fl_out", "m_in_person_new", "m_out_person_gone", "m_in_conc", "m_out_conc",
              "ten_lt3", "ten_3_12", "ten_12p",
              "m_out_ten_lt3", "m_out_ten_3_12", "m_out_ten_12p", "single_pay_recip", "multi_recip",
              "y_base_pf", "y_fl_in", "y_fl_out", "y_in_person_new", "y_out_person_gone",
              "y_in_conc", "y_out_conc"]
LENTA_SIZE = ("CASE WHEN om.pf_max >= 10000 THEN '1) от 10 тыс.' WHEN om.pf_max >= 1000 THEN '2) 1–10 тыс.' "
              "WHEN om.pf_max >= 100 THEN '3) 100–1 000' ELSE '4) до 100' END")
_ACTIVE = "(COALESCE(m.pf_recip, 0) > 0 OR COALESCE(m.m_base_pf, 0) > 0)"

# Источник сегмента строки: история пары или организации — раньше ЕПК.
_SRC_RANK = "CASE WHEN seg_src LIKE 'история%' THEN 0 ELSE 1 END"

MONTH_ORG_COLS = """
SELECT column_name FROM information_schema.columns
WHERE table_schema = :schema AND table_name = 'yva_ptf_month_org'
"""


def t_lenta(have: set | None = None) -> str:
    """Свёртка сводки для ленты. Столбца нет в сводке (собрана до его добавления) —
    мера «неизвестна» у всех активных строк, а не 0."""
    have = set(LENTA_MEAS) if have is None else set(have)
    sums, nns = [], []
    for c in LENTA_MEAS:
        if c in have:
            sums.append(f"sum(m.{c}) AS {c}")
            nns.append(f"sum(CASE WHEN m.{c} IS NULL AND {_ACTIVE} THEN 1 ELSE 0 END) AS nn_{c}")
        else:
            sums.append(f"sum(CAST(NULL AS float8)) AS {c}")
            nns.append(f"sum(CASE WHEN {_ACTIVE} THEN 1 ELSE 0 END) AS nn_{c}")
    return """
WITH e AS (
  SELECT c.inn AS org_id,
         min(NULLIF(btrim(c.holding_name), ''))  AS hold,
         min(NULLIF(btrim(c.industry_name), '')) AS ind,
         min(""" + S.seg_case_epk("c.segment_name") + """) AS seg_epk
  FROM {schema}.uzp_data_epk_consolidation c
  WHERE c.inn IS NOT NULL
  GROUP BY c.inn
),
om AS (
  SELECT org_id, max(pf) AS pf_max
  FROM (SELECT report_dt, org_id, sum(pf_recip) AS pf
        FROM {schema_t}.yva_ptf_month_org GROUP BY report_dt, org_id) a
  GROUP BY org_id
),
hs AS (
  SELECT e.hold
  FROM e JOIN om ON om.org_id = e.org_id
  WHERE e.hold IS NOT NULL
  GROUP BY e.hold
  HAVING count(*) >= 2 AND sum(om.pf_max) >= :hold_min
),
os AS (
  -- Сегмент один на организацию по всем ГОСБ: на дату выплаты — сегмент её ГОСБ с наибольшим
  -- числом получателей (расхождение ГОСБ внутри организации считает LENTA_SEG_CONFLICTS).
  SELECT report_dt, org_id, seg_rep
  FROM (SELECT report_dt, org_id, seg_rep,
               row_number() OVER (PARTITION BY report_dt, org_id
                                  ORDER BY min(""" + _SRC_RANK + """), sum(pf_recip) DESC, seg_rep) AS rn
        FROM {schema_t}.yva_ptf_month_org
        GROUP BY report_dt, org_id, seg_rep) a
  WHERE rn = 1
)
SELECT m.report_dt, COALESCE(os.seg_rep, '(нет)') AS seg_rep,
       COALESCE(e.seg_epk, '(нет в ЕПК)') AS seg_epk,
       m.sys_tb_id, m.sys_gosb_id,
       COALESCE(e.ind, '(нет в ЕПК или не указана)')                          AS ind,
       CASE WHEN hs.hold IS NOT NULL THEN 'Холдинг ' || substr(md5(hs.hold), 1, 8) END AS hold_code,
       """ + LENTA_SIZE + """                                                 AS size_class,
       CASE WHEN om.pf_max >= :org_min
            THEN 'Орг. ' || substr(md5(CAST(m.org_id AS text)), 1, 8) END     AS org_code,
       """ + ",\n       ".join(sums) + """,
       """ + ",\n       ".join(nns) + """
FROM {schema_t}.yva_ptf_month_org m
LEFT JOIN e  ON e.org_id = m.org_id
LEFT JOIN om ON om.org_id = m.org_id
LEFT JOIN hs ON hs.hold = e.hold
LEFT JOIN os ON os.report_dt = m.report_dt AND os.org_id = m.org_id
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
"""


T_LENTA = t_lenta()

# Сегмент — один на организацию по всем ГОСБ, в двух видах: текущий по ЕПК («Сегмент»,
# основные цифры) и на дату выплаты (история сегментов). Их расхождение — пересегментация.
LENTA_SLICES = [("Банк", "CAST('банк' AS text)"),
                ("Сегмент", "seg_epk"),
                ("Сегмент на дату выплаты", "seg_rep"),
                ("ТБ", "'ТБ ' || CAST(sys_tb_id AS text)"),
                ("ГОСБ", "'ГОСБ ' || CAST(sys_gosb_id AS text)"),
                ("Отрасль", "ind"),
                ("Размер организации", "size_class"),
                ("Холдинг", "hold_code"),
                ("Организация", "org_code"),
                ("Сегмент × ТБ", "seg_epk || ' / ТБ ' || CAST(sys_tb_id AS text)"),
                ("Сегмент × отрасль", "seg_epk || ' / ' || ind"),
                ("Сегмент на дату выплаты × ТБ", "seg_rep || ' / ТБ ' || CAST(sys_tb_id AS text)"),
                ("Сегмент на дату выплаты × отрасль", "seg_rep || ' / ' || ind")]
SEGMENT_VIEWS = ["Сегмент", "Сегмент на дату выплаты"]

# Организации, у которых в одном месяце разные ГОСБ имеют разный сегмент на дату выплаты
# (по правилу сегмент один на организацию — это ошибка данных истории сегментов).
# Сегмент организации по ГОСБ: n_org/pf — история разных ГОСБ расходится (ошибка данных);
# n_mix/pf_mix — у части ГОСБ истории нет, сегмент взят из ЕПК и отличается от истории.
LENTA_SEG_CONFLICTS = """
SELECT a.report_dt,
       sum(CASE WHEN a.h_min <> a.h_max THEN 1 ELSE 0 END)   AS n_org,
       sum(CASE WHEN a.h_min <> a.h_max THEN a.pf ELSE 0 END) AS pf,
       sum(CASE WHEN a.h_min = a.h_max AND a.e_dif = 1 THEN 1 ELSE 0 END)   AS n_mix,
       sum(CASE WHEN a.h_min = a.h_max AND a.e_dif = 1 THEN a.pf ELSE 0 END) AS pf_mix
FROM (SELECT report_dt, org_id, sum(pf_recip) AS pf,
             min(CASE WHEN seg_src LIKE 'история%' THEN seg_rep END) AS h_min,
             max(CASE WHEN seg_src LIKE 'история%' THEN seg_rep END) AS h_max,
             CASE WHEN min(seg_rep) <> max(seg_rep) THEN 1 ELSE 0 END AS e_dif
      FROM {schema_t}.yva_ptf_month_org
      GROUP BY report_dt, org_id) a
GROUP BY a.report_dt
"""


def lenta_part(kind: str, key: str) -> str:
    cols = ",\n       ".join([f"sum({c}) AS {c}" for c in LENTA_MEAS]
                             + [f"sum(nn_{c}) AS nn_{c}" for c in LENTA_MEAS])
    return (f"SELECT CAST('{kind}' AS text) AS slice_type, CAST({key} AS text) AS slice_key, report_dt,\n"
            f"       {cols}\nFROM t_lenta\nWHERE ({key}) IS NOT NULL\nGROUP BY 1, 2, 3")


LENTA_SERIES = "\nUNION ALL\n".join(lenta_part(k, v) for k, v in LENTA_SLICES)


# «Последний месяц». Использует ту же единицу учёта и тот же отбор, что сводка.
LATEST_MONTHS = """
SELECT report_dt, pf_recip, fot_amt, run_id, def_ver
FROM {schema_t}.yva_ptf_journal
WHERE run_id = (SELECT max(run_id) FROM {schema_t}.yva_ptf_journal WHERE checks_ok IS TRUE)
  AND slice_type = 'bank' AND report_dt <= CAST(:closed AS date)
ORDER BY report_dt
"""

LATEST_TOTALS = """
SELECT t.mi, sum(CASE WHEN t.st = 'P' THEN 1 ELSE 0 END) AS n,
       sum(t.fot_amt) AS fot, sum(t.pf_amt) AS amount
FROM ptf_latest_tr t GROUP BY t.mi
"""

LATEST_MONTHLY_TOTALS = """
SELECT report_dt, sum(pf_recip) AS n, sum(fot_amt) AS fot, sum(pf_amt) AS amount
FROM {schema_t}.yva_ptf_month_org
WHERE report_dt IN (CAST(:m0 AS date), CAST(:m1 AS date), CAST(:m2 AS date),
                    CAST(:m3 AS date), CAST(:m4 AS date), CAST(:m5 AS date))
GROUP BY report_dt
"""

LATEST_TB_COVERAGE = """
SELECT mi, tb_id, count(*) AS n
FROM ptf_latest_tr
WHERE mi IN (0, 1, 3, 4)
GROUP BY mi, tb_id
"""

# Суммы четырёх месяцев уже есть в строках «вся пара». К тройкам возвращаемся
# только при банковском расхождении в одном из двух месяцев истории выплат.
def latest_changed_pairs(slots):
    measures = {0: ("n_actual", "actual"), 1: ("n_prior", "prior"),
                3: ("n_year_actual", "year_actual"), 4: ("n_year_prior", "year_prior")}
    dates = ", ".join(f"({i}, CAST(:m{i} AS date))" for i in slots)
    dates_list = ", ".join(f"CAST(:m{i} AS date)" for i in slots)
    history = [i for i in slots if i not in measures]
    fields = {kind: " ".join(f"WHEN {i} THEN g.{cols[pos]}" for i, cols in measures.items())
              for pos, kind in enumerate(("n", "rub"))}
    live = f"""
    SELECT d.mi, g.org_id, g.gosb_id,
           CASE d.mi {fields["n"]} END AS live_n,
           CASE d.mi {fields["rub"]} END AS live_rub,
           0 AS saved_n, 0 AS saved_rub
    FROM ptf_latest_groups g JOIN pairs p ON p.org_id = g.org_id AND p.gosb_id = g.gosb_id
    CROSS JOIN dates d
    WHERE g.income = -1 AND g.history = -1 AND g.jobs = -1 AND d.mi IN (0, 1, 3, 4)
    """
    if history:
        live += f"""
    UNION ALL
    SELECT t.mi, t.org_id, t.gosb_id, count(*), sum(t.pf_amt), 0, 0
    FROM ptf_latest_tr t JOIN pairs p ON p.org_id = t.org_id AND p.gosb_id = t.gosb_id
    WHERE t.st = 'P' AND t.mi IN ({", ".join(map(str, history))})
    GROUP BY 1, 2, 3
    """
    return f"""
WITH pairs AS (
    SELECT org_id, gosb_id FROM ptf_latest_shortlist GROUP BY 1, 2
), dates AS (
    SELECT mi, report_dt FROM (VALUES {dates}) d(mi, report_dt)
), figures AS (
    {live}
    UNION ALL
    SELECT d.mi, t.org_id, t.sys_gosb_id, 0, 0, sum(t.pf_recip), sum(t.pf_amt)
    FROM {{schema_t}}.yva_ptf_month_org t
    JOIN pairs p ON p.org_id = t.org_id AND p.gosb_id = t.sys_gosb_id
    JOIN dates d ON d.report_dt = t.report_dt
    WHERE t.report_dt IN ({dates_list})
    GROUP BY 1, 2, 3
), changed AS (
    SELECT org_id, gosb_id FROM figures
    GROUP BY mi, org_id, gosb_id
    HAVING sum(live_n) <> sum(saved_n) OR abs(sum(live_rub) - sum(saved_rub)) > :sync_rub
)
SELECT DISTINCT substr(md5(CAST(org_id AS text) || ':' || CAST(gosb_id AS text) || ':' || :salt), 1, 12)
    AS pair_code
FROM changed
"""


LATEST_JOBS = """
SELECT mi, epk_id, count(*) AS n, sum(amount) AS amount FROM (
    SELECT mi, epk_id, org_id, sum(pf_amt) AS amount FROM ptf_latest_tr
    WHERE mi IN (1, 4) AND st = 'P' GROUP BY mi, epk_id, org_id
) jobs GROUP BY mi, epk_id
"""

LATEST_LOCATIONS = """
SELECT t.mi, t.epk_id, bool_or(old.epk_id IS NULL) AS new_place, sum(t.pf_amt) AS amount
FROM ptf_latest_tr t
LEFT JOIN ptf_latest_tr old ON old.mi = t.mi + 1 AND old.st = 'P'
    AND old.epk_id = t.epk_id AND old.org_id = t.org_id AND old.gosb_id = t.gosb_id
WHERE t.mi IN (0, 3) AND t.st = 'P'
GROUP BY t.mi, t.epk_id
"""

# Один человек попадает в одну полосу по сумме портфельных зачислений
# от всех работодателей. Таблицы уже посчитаны для классификации движений.
LATEST_PORTFOLIO_BANDS = """
SELECT mi, CASE WHEN amount < 30000 THEN 1 WHEN amount < 60000 THEN 2
                WHEN amount < 100000 THEN 3 WHEN amount < 200000 THEN 4 ELSE 5 END AS band,
       count(*) AS people, sum(amount) AS amount
FROM (
    SELECT mi, amount FROM ptf_latest_jobs
    UNION ALL
    SELECT mi, amount FROM ptf_latest_locations
) people
GROUP BY 1, 2
"""

LATEST_PORTFOLIO_GOSB = """
SELECT gosb_id, sum(actual) AS actual, sum(prior) AS prior
FROM ptf_latest_groups
WHERE income = -1 AND history = -1 AND jobs = -1 AND (actual > 0 OR prior > 0)
GROUP BY gosb_id
ORDER BY 2 DESC, 1
"""

LATEST_ORG_LOCATIONS = """
SELECT mi, epk_id, org_id FROM ptf_latest_tr
WHERE mi IN (0, 3) AND st = 'P' GROUP BY mi, epk_id, org_id
"""

LATEST_EVENTS = """
SELECT pair.*,
       CASE
         WHEN pair.in_prev = 0 THEN 0
         WHEN pair.prev_rub < 30000 THEN 1
         WHEN pair.prev_rub < 60000 THEN 2
         WHEN pair.prev_rub < 100000 THEN 3
         WHEN pair.prev_rub < 200000 THEN 4 ELSE 5
       END AS income,
       CASE WHEN pair.in_prev = 0 THEN 0
            WHEN earlier.epk_id IS NOT NULL THEN 2 ELSE 1 END AS history,
       CASE WHEN pair.in_prev = 0 THEN 0 ELSE least(jobs.n, 4) END AS jobs,
       CASE WHEN pair.in_prev = 1 AND pair.in_curr = 1 THEN 'continuing'
            WHEN pair.in_prev = 0 THEN 'added'
            WHEN pair.curr_state = 'Z' THEN 'threshold'
            WHEN pair.curr_state = 'N' THEN 'code'
            WHEN org.epk_id IS NOT NULL THEN 'same_org'
            WHEN loc.new_place THEN 'new_place'
            WHEN loc.epk_id IS NOT NULL THEN 'other_job'
            ELSE 'missing' END AS movement
FROM (
    SELECT coalesce(a.epk_id, b.epk_id) AS epk_id,
           coalesce(a.org_id, b.org_id) AS org_id, coalesce(a.gosb_id, b.gosb_id) AS gosb_id,
           coalesce(a.tb_id, b.tb_id) AS tb_id, coalesce(a.org_digits, b.org_digits) AS org_digits,
           CASE WHEN a.st = 'P' THEN 1 ELSE 0 END AS in_curr,
           CASE WHEN b.st = 'P' THEN 1 ELSE 0 END AS in_prev,
           a.st AS curr_state, coalesce(a.pf_amt, 0) AS curr_rub, coalesce(b.pf_amt, 0) AS prev_rub,
           :period AS period
    FROM (SELECT * FROM ptf_latest_tr WHERE mi = :curr) a
    FULL JOIN (SELECT * FROM ptf_latest_tr WHERE mi = :prev AND st = 'P') b
        ON a.epk_id = b.epk_id AND a.org_id = b.org_id AND a.gosb_id = b.gosb_id
    WHERE a.st = 'P' OR b.epk_id IS NOT NULL
) pair
LEFT JOIN ptf_latest_tr earlier ON earlier.mi = :earlier AND earlier.st = 'P'
    AND earlier.epk_id = pair.epk_id AND earlier.org_id = pair.org_id AND earlier.gosb_id = pair.gosb_id
LEFT JOIN ptf_latest_jobs jobs ON jobs.mi = :prev AND jobs.epk_id = pair.epk_id
LEFT JOIN ptf_latest_locations loc ON loc.mi = :curr AND loc.epk_id = pair.epk_id
LEFT JOIN ptf_latest_org_locations org ON org.mi = :curr AND org.epk_id = pair.epk_id
    AND org.org_id = pair.org_id
"""

# Добавление признака расширяет поиск всеми сочетаниями до двух признаков
# одним и тем же способом для общего отбора и проверки дневной витрины.
LATEST_FEATURES = ("income", "history", "jobs")


def _latest_group_keys(alias):
    groups = [()] + [part for size in (1, 2) for part in combinations(LATEST_FEATURES, size)]
    values = ", ".join("(" + ", ".join(str(int(f in part)) for f in LATEST_FEATURES) + ")"
                       for part in groups)
    columns = ", ".join(f"CASE WHEN g.{f} = 1 THEN {alias}.{f} ELSE -1 END AS {f}"
                        for f in LATEST_FEATURES)
    return columns, f"CROSS JOIN (VALUES {values}) g({', '.join(LATEST_FEATURES)})"


LATEST_MOVEMENTS = ("threshold", "code", "same_org", "new_place", "other_job", "missing")


def latest_groups():
    keys, cross = _latest_group_keys("e")
    reasons = ",\n".join(
        f"sum(CASE WHEN movement = '{label}' AND period = 0 THEN {value} ELSE 0 END) AS {label}{suffix}"
        for label in LATEST_MOVEMENTS for suffix, value in (("", "1"), ("_rub", "prev_rub"))
    )
    return f"""
SELECT org_id, gosb_id, income, history, jobs, min(tb_id) AS tb_id,
       coalesce(max(CASE WHEN period = 0 AND in_curr = 1 THEN org_digits END), max(org_digits)) AS org_digits,
       sum(CASE WHEN period = 0 THEN curr_rub ELSE 0 END) AS actual,
       sum(CASE WHEN period = 0 THEN prev_rub ELSE 0 END) AS prior,
       sum(CASE WHEN period = 1 THEN curr_rub ELSE 0 END) AS year_actual,
       sum(CASE WHEN period = 1 THEN prev_rub ELSE 0 END) AS year_prior,
       sum(CASE WHEN period = 0 THEN in_curr ELSE 0 END) AS n_actual,
       sum(CASE WHEN period = 0 THEN in_prev ELSE 0 END) AS n_prior,
       sum(CASE WHEN period = 1 THEN in_curr ELSE 0 END) AS n_year_actual,
       sum(CASE WHEN period = 1 THEN in_prev ELSE 0 END) AS n_year_prior,
       sum(CASE WHEN period = 0 AND movement = 'continuing' THEN curr_rub ELSE 0 END) AS stay_actual,
       sum(CASE WHEN period = 0 AND movement = 'continuing' THEN prev_rub ELSE 0 END) AS stay_prior,
       sum(CASE WHEN period = 1 AND movement = 'continuing' THEN curr_rub ELSE 0 END) AS stay_year_actual,
       sum(CASE WHEN period = 1 AND movement = 'continuing' THEN prev_rub ELSE 0 END) AS stay_year_prior,
       sum(CASE WHEN period = 0 AND movement = 'continuing' THEN 1 ELSE 0 END) AS stay_n,
       sum(CASE WHEN period = 1 AND movement = 'continuing' THEN 1 ELSE 0 END) AS stay_year_n,
       {reasons}
FROM (
    SELECT e.org_id, e.gosb_id, e.tb_id, e.org_digits, e.period, e.curr_rub, e.prev_rub, e.in_curr, e.in_prev,
           e.movement, {keys}
    FROM ptf_latest_events e {cross}
) expanded
GROUP BY org_id, gosb_id, income, history, jobs
"""


LATEST_COMPARED = """
SELECT g.*,
    prior + year_actual - year_prior AS expected,
    n_prior + n_year_actual - n_year_prior AS expected_n,
    stay_prior + (stay_year_actual - stay_year_prior) * CAST(stay_n AS numeric)
        / nullif(stay_year_n, 0) AS expected_stay
FROM ptf_latest_groups g
"""

def _latest_supported(column, prior):
    return f"{column} BETWEEN {prior} * :min_base_ratio AND {prior} / :min_base_ratio AND {column} > 0"


LATEST_SCORED = f"""
SELECT c.*,
    CASE WHEN n_prior >= :min_people AND n_year_prior >= :min_people
          AND {_latest_supported("expected", "prior")}
         THEN CAST(actual - expected AS numeric) / greatest(expected, prior) END AS rub_change,
    CASE WHEN n_prior >= :min_people AND n_year_prior >= :min_people
          AND {_latest_supported("expected_n", "n_prior")}
         THEN CAST(n_actual - expected_n AS numeric) / greatest(expected_n, n_prior) END AS n_change,
    CASE WHEN stay_n >= :min_people AND stay_year_n >= :min_people
          AND {_latest_supported("expected_stay", "stay_prior")}
         THEN (stay_actual - expected_stay) / greatest(expected_stay, stay_prior) END AS stay_change
FROM ({LATEST_COMPARED}) c
"""

LATEST_SUMMARY = """
SELECT count(*) AS pairs, sum(n_actual) AS n_actual, sum(actual) AS actual,
       sum(CASE WHEN rub_change IS NOT NULL THEN 1 ELSE 0 END) AS comparable,
       sum(CASE WHEN rub_change IS NOT NULL THEN actual ELSE 0 END) AS comparable_actual,
       count(CASE WHEN n_prior >= :min_people AND n_year_prior >= :min_people
                  AND rub_change IS NULL THEN 1 END) AS weak_rub_base,
       sum(CASE WHEN rub_change IS NOT NULL THEN expected ELSE 0 END) AS comparable_expected,
       sum(CASE WHEN rub_change < 0 THEN expected - actual ELSE 0 END) AS gross_down,
       sum(CASE WHEN rub_change > 0 THEN actual - expected ELSE 0 END) AS gross_up,
       sum(CASE WHEN n_prior = 0 AND n_actual > 0 THEN 1 ELSE 0 END) AS added,
       sum(CASE WHEN n_prior > 0 AND n_actual = 0 THEN 1 ELSE 0 END) AS stopped,
       sum(CASE WHEN rub_change IS NOT NULL AND abs(rub_change) < :min_change
                 AND (prior - actual) / nullif(prior, 0) >= :min_change THEN 1 ELSE 0 END) AS seasonal
FROM ptf_latest_scored WHERE income = -1 AND history = -1 AND jobs = -1
  AND (n_actual > 0 OR n_prior > 0)
"""

_LATEST_SIGNAL_FILTER = """
    abs(rub_change) >= :min_change OR abs(n_change) >= :min_change
    OR abs(stay_change) >= :min_change
"""


def latest_shortlist():
    # Относительное отклонение определяет кандидата. В бюджет дневной проверки
    # идут крупнейшие рублёвые сигналы отдельно по причинам знака и зерну.
    return f"""
WITH candidates AS (
    SELECT s.*, CASE WHEN income = -1 AND history = -1 AND jobs = -1
                     THEN 'pair' ELSE 'group' END AS level,
           (CASE WHEN income >= 0 THEN 1 ELSE 0 END + CASE WHEN history >= 0 THEN 1 ELSE 0 END
            + CASE WHEN jobs >= 0 THEN 1 ELSE 0 END) AS depth
    FROM ptf_latest_scored s WHERE {_LATEST_SIGNAL_FILTER}
    OR (income = -1 AND history = -1 AND jobs = -1
        AND ((n_prior = 0 AND n_actual >= :min_people) OR (n_actual = 0 AND n_prior >= :min_people)))
), valued AS (
    SELECT s.*, d.direction,
           greatest(
               CASE WHEN abs(d.direction) = 1 AND rub_change * d.direction >= :min_change THEN (actual - expected) * d.direction END,
               CASE WHEN abs(d.direction) = 1 AND n_change * d.direction >= :min_change
                    THEN (n_actual - expected_n) * d.direction * prior / nullif(n_prior, 0) END,
               CASE WHEN abs(d.direction) = 1 AND stay_change * d.direction >= :min_change
                    THEN (stay_actual - expected_stay) * d.direction END,
               CASE WHEN level = 'pair' AND d.direction = -2 AND n_actual = 0 AND n_prior >= :min_people
                    THEN prior END,
               CASE WHEN level = 'pair' AND d.direction = 2 AND n_prior = 0 AND n_actual >= :min_people
                    THEN actual END
           ) AS priority
    FROM candidates s CROSS JOIN (VALUES (-1), (1), (-2), (2)) d(direction)
    WHERE abs(d.direction) = 1 OR NOT coalesce(({_LATEST_SIGNAL_FILTER}), false)
), per_pair AS (
    SELECT valued.*,
           row_number() OVER (PARTITION BY level, direction, org_id, gosb_id
                              ORDER BY priority DESC, depth, income, history, jobs) AS within_pair
    FROM valued WHERE priority > 0
), ranked AS (
    SELECT per_pair.*,
           row_number() OVER (PARTITION BY level, direction ORDER BY priority DESC, org_id, gosb_id) AS pos
    FROM per_pair WHERE within_pair = 1
), chosen AS (
    SELECT org_id, gosb_id FROM ranked WHERE pos <= :max_pairs GROUP BY 1, 2
)
SELECT s.org_id, s.gosb_id, s.income, s.history, s.jobs
FROM per_pair s JOIN chosen c ON c.org_id = s.org_id AND c.gosb_id = s.gosb_id
WHERE s.within_pair <= :max_groups
GROUP BY 1, 2, 3, 4, 5
"""


LATEST_SIGNAL_COUNTS = f"""
SELECT count(*) AS tested_groups,
       count(CASE WHEN {_LATEST_SIGNAL_FILTER} THEN 1 END) AS signals
FROM ptf_latest_scored
WHERE rub_change IS NOT NULL OR n_change IS NOT NULL OR stay_change IS NOT NULL
"""


def latest_cards():
    fields = ", ".join(f"s.{label}{suffix}" for label in LATEST_MOVEMENTS for suffix in ("", "_rub"))
    return f"""
SELECT substr(md5(CAST(s.org_id AS text) || ':' || CAST(s.gosb_id AS text) || ':' || :salt), 1, 12)
           AS pair_code,
       substr(md5(CAST(s.org_id AS text) || ':org:' || :salt), 1, 10) AS org_code,
       lpad(CAST(s.org_id AS text), greatest(length(CAST(s.org_id AS text)), s.org_digits), '0') AS org_original,
       s.gosb_id, s.tb_id, s.income, s.history, s.jobs, s.actual, s.prior, s.year_actual, s.year_prior,
       s.n_actual, s.n_prior, s.n_year_actual, s.n_year_prior, s.stay_actual, s.stay_prior,
       s.stay_year_actual, s.stay_year_prior, s.stay_n, s.stay_year_n,
       s.expected, s.expected_n, s.expected_stay, s.rub_change, s.n_change, s.stay_change,
       {fields}
FROM ptf_latest_shortlist a JOIN ptf_latest_scored s
    ON a.org_id = s.org_id AND a.gosb_id = s.gosb_id
    AND a.income = s.income AND a.history = s.history AND a.jobs = s.jobs
"""


LATEST_CHOSEN_PAIRS = """
SELECT org_id, gosb_id FROM ptf_latest_shortlist GROUP BY 1, 2
"""


# Только однозначный переход: та же организация, одна ушедшая и одна новая
# точка выплат у человека, совпадает сумма. Прочие совпадения — только признаки.
LATEST_MATCHED_MOVES = """
WITH paired AS (
    SELECT period, epk_id, org_id,
           max(CASE WHEN movement = 'same_org' THEN gosb_id END) AS source,
           max(CASE WHEN movement = 'added' THEN gosb_id END) AS target,
           sum(prev_rub) AS amount
    FROM ptf_latest_events e
    WHERE movement IN ('same_org', 'added')
      AND EXISTS (SELECT 1 FROM ptf_latest_pairs p WHERE p.org_id = e.org_id)
    GROUP BY period, epk_id, org_id
    HAVING count(CASE WHEN movement = 'same_org' THEN 1 END) = 1
       AND count(CASE WHEN movement = 'added' THEN 1 END) = 1
       AND sum(curr_rub) = sum(prev_rub) AND sum(prev_rub) > 0
)
SELECT period, epk_id, org_id, CASE WHEN edge.kind = 0 THEN source ELSE target END AS gosb_id,
       source AS source_gosb, target AS target_gosb, amount
FROM paired CROSS JOIN (VALUES (0), (1)) edge(kind)
"""

LATEST_CURRENT_ROUTES = """
WITH counts AS (
    SELECT org_id, source_gosb, target_gosb, count(*) AS people, sum(amount) AS amount
    FROM ptf_latest_moves WHERE period = 0 AND gosb_id = source_gosb
    GROUP BY 1, 2, 3
)
SELECT min(substr(md5(CAST(p.org_id AS text) || ':' || CAST(p.gosb_id AS text) || ':' || :salt), 1, 12))
           AS pair_code,
       c.source_gosb, c.target_gosb, c.people, c.amount
FROM counts c JOIN ptf_latest_pairs p
    ON p.org_id = c.org_id AND p.gosb_id IN (c.source_gosb, c.target_gosb)
GROUP BY c.org_id, c.source_gosb, c.target_gosb, c.people, c.amount
ORDER BY c.amount DESC, 1, 2, 3
"""


def latest_daily_triples():
    gosb = gosb_fix("p.sys_gosb_id", "p.sys_tb_id", "p.segment_name")
    joins = f"""
    JOIN ptf_latest_pairs target ON target.org_id = {org_num('p.inn')}
        AND target.gosb_id = coalesce({gosb}, -1)
    CROSS JOIN (VALUES (0), (1)) w(kind)
    """
    dates = """
    p.report_dt >= CAST(:s_from AS date) AND p.report_dt <= CAST(:c_to AS date)
    AND ((w.kind = 0 AND p.report_dt >= CAST(:c_from AS date))
         OR (w.kind = 1 AND p.report_dt <= CAST(:s_to AS date)))
    """
    return _triples("{payroll_d}", "CAST(:mi AS integer) * 2 + w.kind", dates, joins)


def latest_daily_groups():
    keys, cross = _latest_group_keys("d")
    return f"""
WITH tagged AS (
    SELECT t.mi, t.org_id, t.gosb_id, t.pf_amt,
           coalesce(e.income, 0) AS income, coalesce(e.history, 0) AS history,
           coalesce(e.jobs, 0) AS jobs, e.movement, m.epk_id IS NOT NULL AS matched_move
    FROM ptf_latest_days t LEFT JOIN ptf_latest_events e
        ON e.epk_id = t.epk_id AND e.org_id = t.org_id AND e.gosb_id = t.gosb_id
        AND e.period = CASE WHEN t.mi < 4 THEN 0 ELSE 1 END
    LEFT JOIN ptf_latest_moves m ON e.epk_id = m.epk_id AND e.org_id = m.org_id
        AND e.gosb_id = m.gosb_id AND e.period = m.period
    WHERE t.st = 'P'
), totals AS (
    SELECT d.mi, d.org_id, d.gosb_id, {keys}, count(*) AS n,
           sum(d.pf_amt) AS amount,
           sum(CASE WHEN d.movement = 'continuing' THEN d.pf_amt ELSE 0 END) AS stay,
           sum(CASE WHEN d.matched_move THEN d.pf_amt ELSE 0 END) AS moved
    FROM tagged d {cross} GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT substr(md5(CAST(s.org_id AS text) || ':' || CAST(s.gosb_id AS text) || ':' || :salt), 1, 12)
           AS pair_code,
       s.income, s.history, s.jobs, w.mi, coalesce(t.n, 0) AS n,
       coalesce(t.amount, 0) AS amount, coalesce(t.stay, 0) AS stay,
       coalesce(t.moved, 0) AS moved
FROM ptf_latest_shortlist s CROSS JOIN (VALUES (0), (1), (2), (3), (4), (5), (6), (7)) w(mi)
LEFT JOIN totals t ON t.org_id = s.org_id AND t.gosb_id = s.gosb_id AND t.mi = w.mi
    AND t.income = s.income AND t.history = s.history AND t.jobs = s.jobs
"""


# Выплаты организации во всех ГОСБ для отобранных пар, где пропали выплаты.
# Используются уже рассчитанные агрегаты из тех же ведомостей.
LATEST_STOPPED_ORGANIZATIONS = """
WITH places AS (
    SELECT org_id, gosb_id, n_prior, n_actual, prior, actual
    FROM ptf_latest_groups
    WHERE income = -1 AND history = -1 AND jobs = -1 AND (n_prior > 0 OR n_actual > 0)
), stopped AS (
    SELECT p.org_id, p.gosb_id
    FROM places p JOIN ptf_latest_shortlist s ON s.org_id = p.org_id AND s.gosb_id = p.gosb_id
    WHERE s.income = -1 AND s.history = -1 AND s.jobs = -1
      AND p.n_prior > 0 AND p.n_actual = 0
)
SELECT substr(md5(CAST(s.org_id AS text) || ':' || CAST(s.gosb_id AS text) || ':' || :salt), 1, 12)
           AS pair_code,
       s.gosb_id AS stopped_gosb, p.gosb_id AS gosb,
       p.n_prior AS previous_n, p.n_actual AS current_n,
       p.prior AS previous_amount, p.actual AS current_amount
FROM stopped s JOIN places p ON p.org_id = s.org_id
ORDER BY 1, 3
"""


# Физлицо с портфельными выплатами в предыдущем месяце и без них в текущем.
# Переходы между организациями и ГОСБ не выводят человека из портфеля банка.
LATEST_BANK_LEAVERS = """
SELECT j.epk_id
FROM ptf_latest_jobs j
LEFT JOIN ptf_latest_locations loc ON loc.mi = 0 AND loc.epk_id = j.epk_id
WHERE j.mi = 1 AND loc.epk_id IS NULL
"""

LATEST_BANK_LEAVER_COUNT = "SELECT count(*) AS n FROM ptf_latest_bank_leavers"

LATEST_BANK_EARLY_DAY = _triples(
    "{payroll_d}", "CAST(:mi AS integer)",
    "p.report_dt >= CAST(:s_from AS date) AND p.report_dt <= CAST(:c_to AS date)",
    "JOIN ptf_latest_bank_leavers leavers ON leavers.epk_id = p.epk_id",
)

# Только совпавшие платежи первого числа в двух предыдущих месяцах и такой
# же платёж в последний будний день перед выходным первым числом. Прежняя
# месячная ведомость должна содержать оба платежа в той же паре.
LATEST_BANK_EARLY_COUNT = """
SELECT count(*) AS n FROM (
    SELECT a.epk_id
    FROM ptf_latest_bank_days a
    JOIN ptf_latest_bank_days b ON b.mi = 1 AND b.st = 'P' AND b.epk_id = a.epk_id
        AND b.org_id = a.org_id AND b.gosb_id = a.gosb_id AND b.pf_amt = a.pf_amt
    JOIN ptf_latest_bank_days c ON c.mi = 2 AND c.st = 'P' AND c.epk_id = a.epk_id
        AND c.org_id = a.org_id AND c.gosb_id = a.gosb_id AND c.pf_amt = a.pf_amt
    JOIN ptf_latest_tr p ON p.mi = 1 AND p.st = 'P' AND p.epk_id = a.epk_id
        AND p.org_id = a.org_id AND p.gosb_id = a.gosb_id AND p.pf_amt >= 2 * a.pf_amt
    JOIN ptf_latest_tr q ON q.mi = 2 AND q.st = 'P' AND q.epk_id = a.epk_id
        AND q.org_id = a.org_id AND q.gosb_id = a.gosb_id AND q.pf_amt >= a.pf_amt
    WHERE a.mi = 0 AND a.st = 'P' AND a.pf_amt > 0
    GROUP BY a.epk_id
) matched
"""
