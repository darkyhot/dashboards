"""Независимый эталон сводки на Python — только для синтетики открытого контура.

Тот же смысл, что в `sql.py`, но написан иначе: отбор — построчно по определению
заказчика (строка засчитывается или нет), история — множествами месяцев (в SQL —
битовые маски), разметка — циклами по словарям. Если SQL и эталон расходятся хоть в
одной ячейке, ошибка в одном из них, и сверка это ловит.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from statistics import median

import pandas as pd

from . import months as M
from . import segments as S
from . import sql as Q

KSB_RAW = {"СКБ-Средние", "СКБ-Прочее", "СКБ-Крупные", "Средний бизнес", "Крупный бизнес",
           "ККСБ - прочие", "ККСБ - Прочие"}
BAD = {"7707083893", "Не определено", "0", "-1"}
DIGITS = re.compile(r"^[0-9]{1,12}$")
MEM = 12


def _gosb(g, tb, seg):
    if tb == 38:
        return 9038
    if g == 1009:
        return 8557
    if g == 0 and tb == 40:
        return 9040
    if seg in KSB_RAW and g == 8591:
        return 8646
    return -1 if g is None or pd.isna(g) else int(g)


def _relev(d: date, kind, msf, org: str) -> bool:
    if date(2023, 11, 1) <= d <= date(2023, 12, 31):
        ok = True
    elif d >= date(2024, 1, 1):
        ok = kind == "Основные" and msf is not None and msf != "ИП 1 чел."
    else:
        ok = False
    return ok and ("0" if org == "Не определено" else org) not in BAD


def _nn(x):
    return None if x is None or (isinstance(x, float) and pd.isna(x)) or x is pd.NA else x


def _inr(s: set, lo: int, hi: int) -> bool:
    """Есть ли в множестве месяцев хоть один из lo…hi (месяцы до окна не существуют)."""
    return any(x in s for x in range(max(lo, 0), hi + 1))


def _one_time(s: set, at: int) -> bool:
    return not _inr(s, at - MEM, at - 1) and (at + 1) not in s and (at + 2) not in s


def _ten(p: set, at: int) -> str:
    if not _inr(p, at - MEM, at - 3):
        return "lt3"
    return "12p" if (at - MEM) in p else "3_12"


def compute(t: dict, report_month: str, n_months: int = 36, amt_min: float = Q.AMT_MIN,
            pair_min: int = 10) -> dict:
    last_m = M.parse(report_month)
    first = max(M.parse("2023-11"), M.shift(last_m, -(n_months - 1)))
    months = [M.iso(m) for m in M.span(first, last_m)]
    mi_of = {m: i for i, m in enumerate(months)}
    last = len(months) - 1
    cmp0 = next((i for i, m in enumerate(months) if m >= "2024-01-31"), len(months))

    codes = {int(r.enrollment_type_id): (bool(r.is_portfolio_enrollment), bool(r.is_fot_enrollment))
             for r in t["codes"].itertuples()}
    tik = {int(r.inn) for r in t["tik"].itertuples() if r.is_tik_cik}
    epk = t["epk"]
    exc = {int(x) for x in t["edu"]["inn"] if DIGITS.match(str(x))}
    exc |= {int(r.inn) for r in epk.itertuples() if _nn(r.reference_holding_name) == "МИНОБОРОНЫ"}
    org_seg, org_hold = {}, {}
    for r in epk.itertuples():
        o = int(r.inn)
        if o in tik:
            continue
        s = S.epk_to_short(_nn(r.segment_name))
        org_seg[o] = min(org_seg.get(o, s), s)
        h = _nn(r.holding_name)
        h = h.strip() if isinstance(h, str) and h.strip() else None
        if h is not None:
            org_hold[o] = min(org_hold.get(o, h), h)
    hist = {}
    for r in t["seg_hist"].itertuples():
        d = pd.Timestamp(r.report_dt).date()
        me = M.iso(M.month_end(d.year, d.month))
        if me not in mi_of or not DIGITS.match(str(r.inn)):
            continue
        key = (mi_of[me], int(r.inn), _gosb(_nn(r.gosb_id), _nn(r.tb_id), _nn(r.segment_name)))
        s = S.pay_to_short(_nn(r.segment_name))
        if s is not None:
            hist[key] = min(hist.get(key, s), s)

    # --- отбор строк и тройки по месяцам ---
    rows = defaultdict(list)          # (mi, e, o, g) -> [(code, amt, tb, tq)]
    for r in t["payroll"].itertuples():
        m = M.iso(pd.Timestamp(r.report_dt).date())
        if m not in mi_of or _nn(r.epk_id) is None:
            continue
        org = str(r.inn)
        if not DIGITS.match(org) or int(org) in tik:
            continue
        if not _relev(pd.Timestamp(r.report_dt).date(), _nn(r.enrollment_kind_descr),
                      _nn(r.market_share_flag_name), org):
            continue
        g = _gosb(_nn(r.sys_gosb_id), _nn(r.sys_tb_id), _nn(r.segment_name))
        tq = _nn(r.transaction_qty)
        rows[(mi_of[m], int(r.epk_id), int(org), g)].append(
            (int(r.enrollment_type), float(r.amt), _nn(r.sys_tb_id), 1 if tq is None else int(tq)))

    ST, PFA, FOTA, TB, NR, NP = {}, {}, {}, {}, {}, {}
    for key, rr in rows.items():
        o = key[2]
        fl = lambda c: codes.get(c, (False, False))            # noqa: E731
        sum_pf = sum(a for c, a, _, _ in rr if fl(c)[0])
        sum_fot = sum(a for c, a, _, _ in rr if fl(c)[1])
        is_exc = o in exc
        # Строка засчитывается эталоном заказчика (is_salary_client / is_salary_fot).
        sal = [(c, a) for c, a, _, _ in rr
               if a > 0 and ((fl(c)[0] and sum_pf > amt_min) or (c == 2 and c in codes) or (fl(c)[0] and is_exc))]
        fot = [(c, a) for c, a, _, _ in rr
               if a > 0 and ((fl(c)[1] and sum_fot > amt_min) or (c == 2 and c in codes) or (fl(c)[1] and is_exc))]
        if not any(a > 0 for _, a, _, _ in rr):
            continue
        if sal:
            st = "P"
        elif any(a > 0 and fl(c)[0] for c, a, _, _ in rr):
            st = "Z"
        else:
            st = "N"
        ST[key] = st
        PFA[key] = sum(a for _, a in sal)
        FOTA[key] = sum(a for _, a in fot)
        TB[key] = min((tb for _, _, tb, _ in rr if tb is not None), default=None)
        NR[key] = len(rr)
        NP[key] = sum(tq for _, a, _, tq in rr if a > 0)

    # --- истории множествами ---
    any_m, p_m, n_m = defaultdict(set), defaultdict(set), defaultdict(set)
    per_any, per_p = defaultdict(set), defaultdict(set)
    p_by_pm = defaultdict(list)                 # (mi, e) -> [(o, g)] в портфеле
    org_pf = set()                              # (mi, o) с портфелем
    for (mi, e, o, g), st in ST.items():
        trip = (e, o, g)
        any_m[trip].add(mi)
        per_any[e].add(mi)
        if st == "P":
            p_m[trip].add(mi)
            per_p[e].add(mi)
            p_by_pm[(mi, e)].append((o, g))
            org_pf.add((mi, o))
        elif st == "N":
            n_m[trip].add(mi)

    def where_else(mi, e, o):
        lst = p_by_pm.get((mi, e), [])
        if not lst:
            return None
        if any(oo == o for oo, _ in lst):
            return "org"
        h = org_hold.get(o)
        if h is not None and any(org_hold.get(oo) == h for oo, _ in lst):
            return "hold"
        return "other"

    def flips(trip, lo, hi):
        a, n = any_m[trip], n_m[trip]
        return sum(1 for x in range(max(lo, 1), hi + 1)
                   if x in a and x - 1 in a and ((x in n) != (x - 1 in n)))

    # --- остатки ---
    stock = defaultdict(lambda: defaultdict(float))
    stock_tb = {}
    for (mi, e, o, g), st in ST.items():
        trip = (e, o, g)
        k = (mi, o, g)
        s = stock[k]
        tb = TB[(mi, e, o, g)]
        if tb is not None:
            stock_tb[k] = min(stock_tb.get(k, tb), tb)
        else:
            stock_tb.setdefault(k, None)
        s["any_recip"] += 1
        s["raw_rows"] += NR[(mi, e, o, g)]
        s["pf_amt"] += PFA[(mi, e, o, g)]
        s["fot_amt"] += FOTA[(mi, e, o, g)]
        s["zp_out_recip"] += st == "Z"
        s["nonzp_recip"] += st == "N"
        s["flapping_recip"] += mi >= 11 and flips(trip, mi - 11, mi) >= Q.FLAP_MIN
        for c in ("pf_recip", "pf_fl", "single_pay_recip", "multi_recip", "multi_same_org",
                  "multi_same_hold", "multi_other", "one_time_pf", "one_time_person",
                  "ten_lt3", "ten_3_12", "ten_12p"):
            s[c] += 0
        if st == "P":
            kp = len(p_by_pm[(mi, e)])
            s["pf_recip"] += 1
            s["pf_fl"] += 1.0 / kp
            s["single_pay_recip"] += NP[(mi, e, o, g)] == 1
            if kp > 1:
                n_org = sum(1 for oo, _ in p_by_pm[(mi, e)] if oo == o)
                h = org_hold.get(o)
                if n_org > 1:
                    s["multi_same_org"] += 1
                elif h is not None and any(org_hold.get(oo) == h and oo != o for oo, _ in p_by_pm[(mi, e)]):
                    s["multi_same_hold"] += 1
                else:
                    s["multi_other"] += 1
                s["multi_recip"] += 1
            s["one_time_pf"] += _one_time(any_m[trip], mi)
            s["one_time_person"] += _one_time(per_any[e], mi)
            s["ten_" + _ten(p_m[trip], mi)] += 1

    # --- метки и потоки ---
    lab = {b: defaultdict(lambda: defaultdict(float)) for b in Q.BASES}
    moves_out, moves_in = defaultdict(list), defaultdict(list)   # (mi, e) -> [(o, g)] для k=1
    for b, kk in Q.BASES.items():
        L = lab[b]
        for (mi, e, o, g), st in ST.items():
            trip = (e, o, g)
            if st != "P":
                continue
            # пришёл в mi
            B = mi - kk
            if B >= 0 and B not in p_m[trip]:
                w = where_else(B, e, o)
                if B in n_m[trip]:
                    x = "in_from_nonzp"
                elif B in any_m[trip]:
                    x = "in_from_zp_out"
                elif w is not None:
                    x = {"org": "in_oth_same_org", "hold": "in_oth_same_hold", "other": "in_oth_other"}[w]
                elif _inr(per_p[e], B - MEM, B - 1):
                    x = "in_returned"
                else:
                    x = "in_new_bank"
                L[(mi, o, g)][x] += 1
                if not p_by_pm.get((B, e)):
                    L[(mi, o, g)]["fl_in"] += 1.0 / len(p_by_pm[(mi, e)])
                if kk == 1 and x.startswith("in_oth_"):
                    moves_in[(mi, e)].append((o, g))
            # ушёл в m2 (mi — база)
            m2 = mi + kk
            if m2 <= last and m2 not in p_m[trip]:
                w = where_else(m2, e, o)
                p = p_m[trip]
                if m2 in n_m[trip]:
                    x = "out_to_nonzp"
                elif m2 in any_m[trip]:
                    x = "out_zp_out"
                elif w is not None:
                    x = {"org": "out_oth_same_org", "hold": "out_oth_same_hold", "other": "out_oth_other"}[w]
                elif (m2 - 13) in p and (m2 - 12) not in p and _inr(p, m2 - 11, m2 - (12 - Q.SEAS_RET)):
                    x = "lb_seasonal"
                elif (m2, o) not in org_pf:
                    x = "lb_org_stopped"
                elif _one_time(any_m[trip], mi):
                    x = "lb_one_time"
                else:
                    x = "lb_unknown"
                L[(m2, o, g)][x] += 1
                if not p_by_pm.get((m2, e)):
                    L[(m2, o, g)]["fl_out"] += 1.0 / len(p_by_pm[(mi, e)])
                L[(m2, o, g)]["out_temp"] += (m2 + 1) in p
                L[(m2, o, g)]["out_ten_" + _ten(p, mi)] += 1
                if kk == 1 and x.startswith("out_oth_"):
                    moves_out[(m2, e)].append((o, g))

    pairs = Counter()
    for (mi, e), outs in moves_out.items():
        if mi - 1 < cmp0:
            continue
        for fo, fg in outs:
            for to, tg in moves_in.get((mi, e), []):
                pairs[(months[mi], fo, fg, to, tg)] += 1
    pairs_df = pd.DataFrame([(*k, v) for k, v in pairs.items() if v >= pair_min],
                            columns=["report_dt", "from_org_id", "from_gosb_id", "to_org_id",
                                     "to_gosb_id", "n_recip"])

    # --- сегменты ---
    keys = set(stock)
    for (mi, o, g), s in stock.items():
        if s["pf_recip"] > 0:
            for kk in (1, 12):
                if mi + kk <= last:
                    keys.add((mi + kk, o, g))
    cov = {}
    for (mi, o, g), s in stock.items():
        pf, ph = cov.get(mi, (0, 0))
        cov[mi] = (pf + s["pf_recip"], ph + (s["pf_recip"] if (mi, o, g) in hist else 0))
    shares = {mi: ph / pf for mi, (pf, ph) in cov.items() if pf > 0}
    med = median(shares.values()) if shares else 0
    full = [mi for mi, sh in shares.items() if sh >= 0.9 * med]
    cur_mi = max(full) if full else (max(shares) if shares else 0)
    cur_pair = {(o, g): s for (mi, o, g), s in hist.items() if mi == cur_mi}
    cur_org = {}
    for (o, g), s in cur_pair.items():
        cur_org[o] = min(cur_org.get(o, s), s)

    def seg_of(key):
        mi, o, g = key
        if key in hist:
            return hist[key], "история"
        if o in org_seg:
            return org_seg[o], "ЕПК"
        return S.NOT_FOUND, "нет"

    def seg_cur_of(o, g):
        return cur_pair.get((o, g)) or cur_org.get(o) or org_seg.get(o) or S.NOT_FOUND

    def sv(key, c):
        return stock[key][c] if key in stock else 0

    # --- сборка ---
    out = []
    for key in sorted(keys):
        mi, o, g = key
        seg, src = seg_of(key)
        tb = next((stock_tb[(mi - d, o, g)] for d in (0, 1, 12) if stock_tb.get((mi - d, o, g)) is not None),
                  None)
        row = {"report_dt": months[mi], "org_id": o, "sys_gosb_id": g, "sys_tb_id": tb,
               "seg_rep": seg, "seg_cur": seg_cur_of(o, g), "seg_src": src}
        for c in Q.STOCK_COLS:
            v = sv(key, c)
            if c.startswith("ten_") or c.startswith("one_time"):
                v = v if mi >= MEM else None
            elif c == "flapping_recip":
                v = v if mi >= cmp0 + 12 else None
            row[c] = v
        row["prelim"] = mi + 2 > last
        for b, kk in Q.BASES.items():
            on = mi - kk >= cmp0
            mem_ok = on and mi - kk - MEM >= 0
            bk = (mi - kk, o, g)
            L = lab[b].get(key, {})
            get = lambda x: L.get(x, 0)                           # noqa: E731
            val = {
                "base_pf": sv(bk, "pf_recip"),
                "base_seg": seg_of(bk)[0] if bk in keys else None,
                "in_from_outside": get("in_new_bank") + get("in_returned"),
                "in_from_other_org": sum(get(x) for x in Q.IN_LABELS if x.startswith("in_oth_")),
                "out_to_other_org": sum(get(x) for x in Q.OUT_LABELS if x.startswith("out_oth_")),
                "out_left_bank": sum(get(x) for x in Q.OUT_LABELS if x.startswith("lb_")),
                "fl_in": get("fl_in"), "fl_out": get("fl_out"),
                "fl_conc": (sv(key, "pf_fl") - sv(bk, "pf_fl")) - get("fl_in") + get("fl_out"),
            }
            for x in Q.IN_LABELS + Q.OUT_LABELS:
                val[x] = get(x)
            for c in Q.base_cols(b):
                need_mem = c in ("in_new_bank", "in_returned") or c.startswith("lb_")
                ok = mem_ok if need_mem else on
                row[f"{b}_{c}"] = val[c] if ok else None
        Lm = lab["m"].get(key, {})
        row["m_out_temp"] = Lm.get("out_temp", 0) if mi - 1 >= cmp0 and mi + 1 <= last else None
        for tt in Q.TEN:
            row[f"m_out_ten_{tt}"] = (Lm.get(f"out_ten_{tt}", 0)
                                      if mi - 1 >= cmp0 and mi - 1 - MEM >= 0 else None)
        out.append(row)
    summary = pd.DataFrame(out, columns=Q.final_columns())

    # --- точное число людей по срезам (журнал) ---
    kseg = {(r["report_dt"], r["org_id"], r["sys_gosb_id"]): (r["seg_rep"], r["seg_cur"]) for r in out}
    pers = defaultdict(set)
    for (mi, e, o, g), st in ST.items():
        if st != "P":
            continue
        sr, sc = kseg[(months[mi], o, g)]
        tb = TB[(mi, e, o, g)]
        pers[(months[mi], "bank", "банк")].add(e)
        pers[(months[mi], "tb", None if tb is None else str(tb))].add(e)
        pers[(months[mi], "seg_rep", sr)].add(e)
        pers[(months[mi], "seg_cur", sc)].add(e)
    persons = pd.DataFrame([(*k, len(v)) for k, v in pers.items()],
                           columns=["report_dt", "slice_type", "slice_value", "persons"])
    return {"summary": summary, "pairs": pairs_df, "persons": persons,
            "months": months, "cmp0": cmp0, "seg_cur_dt": months[cur_mi]}
