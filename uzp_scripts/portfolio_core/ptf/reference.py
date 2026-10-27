"""Независимый эталон сводки на Python — только для синтетики открытого контура.

Тот же смысл, что в `sql.py`, но написан иначе: отбор — построчно по определению
заказчика (строка засчитывается или нет), разметка — циклами по словарям. Если SQL и
эталон расходятся хоть в одной ячейке, ошибка в одном из них, и сверка это ловит.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

import pandas as pd

from . import months as M
from . import segments as S
from . import sql as Q

KSB_RAW = {"СКБ-Средние", "СКБ-Прочее", "СКБ-Крупные", "Средний бизнес", "Крупный бизнес",
           "ККСБ - прочие", "ККСБ - Прочие"}
BAD = {"7707083893", "Не определено", "0", "-1"}
DIGITS = re.compile(r"^[0-9]{1,12}$")


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


def compute(t: dict, report_month: str, n_months: int = 36, amt_min: float = Q.AMT_MIN) -> pd.DataFrame:
    last_m = M.parse(report_month)
    months = [M.iso(m) for m in M.span(M.shift(last_m, -(n_months - 1)), last_m)]
    mi_of = {m: i for i, m in enumerate(months)}
    last = len(months) - 1

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
    rows = defaultdict(list)          # (mi, e, o, g) -> [(code, amt, tb)]
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
        rows[(mi_of[m], int(r.epk_id), int(org), g)].append(
            (int(r.enrollment_type), float(r.amt), _nn(r.sys_tb_id)))

    ST, PFA, FOTA, TB = {}, {}, {}, {}
    for key, rr in rows.items():
        o = key[2]
        fl = lambda c: codes.get(c, (False, False))            # noqa: E731
        sum_pf = sum(a for c, a, _ in rr if fl(c)[0])
        sum_fot = sum(a for c, a, _ in rr if fl(c)[1])
        is_exc = o in exc
        # Строка засчитывается эталоном заказчика (is_salary_client / is_salary_fot).
        sal = [(c, a) for c, a, _ in rr
               if a > 0 and ((fl(c)[0] and sum_pf > amt_min) or (c == 2 and c in codes) or (fl(c)[0] and is_exc))]
        fot = [(c, a) for c, a, _ in rr
               if a > 0 and ((fl(c)[1] and sum_fot > amt_min) or (c == 2 and c in codes) or (fl(c)[1] and is_exc))]
        if not any(a > 0 for _, a, _ in rr):
            continue
        if sal:
            st = "P"
        elif any(a > 0 and fl(c)[0] for c, a, _ in rr):
            st = "Z"
        else:
            st = "N"
        ST[key] = st
        PFA[key] = sum(a for _, a in sal)
        FOTA[key] = sum(a for _, a in fot)
        TB[key] = min(tb for _, _, tb in rr)

    def tr(key):
        return key[1:]

    p_by_pm = defaultdict(list)                 # (mi, e) -> [(o, g)] в портфеле
    org_pf = set()                              # (mi, o) с портфелем
    tri_months = defaultdict(set)               # тройка -> месяцы с зачислениями
    per_months = defaultdict(set)
    tri_first_pf, per_first_pf = {}, {}
    for (mi, e, o, g), st in ST.items():
        tri_months[(e, o, g)].add(mi)
        per_months[e].add(mi)
        if st == "P":
            p_by_pm[(mi, e)].append((o, g))
            org_pf.add((mi, o))
            tri_first_pf[(e, o, g)] = min(tri_first_pf.get((e, o, g), mi), mi)
            per_first_pf[e] = min(per_first_pf.get(e, mi), mi)

    def is_p(mi, trip):
        return ST.get((mi,) + trip) == "P"

    def where_else(mi, e, o):
        """Где у человека другой портфель в месяце mi: 'org' / 'hold' / 'other' / None."""
        lst = p_by_pm.get((mi, e), [])
        if not lst:
            return None
        if any(oo == o for oo, _ in lst):
            return "org"
        h = org_hold.get(o)
        if h is not None and any(org_hold.get(oo) == h for oo, _ in lst):
            return "hold"
        return "other"

    def flip_at(mi, trip):
        a, b = ST.get((mi,) + trip), ST.get((mi - 1,) + trip)
        return a is not None and b is not None and (a == "N") != (b == "N")

    # --- остатки ---
    stock = defaultdict(lambda: defaultdict(float))
    stock_tb = {}
    for (mi, e, o, g), st in ST.items():
        trip = (e, o, g)
        k = (mi, o, g)
        s = stock[k]
        stock_tb[k] = min(stock_tb.get(k, TB[(mi, e, o, g)]), TB[(mi, e, o, g)])
        kp = len(p_by_pm.get((mi, e), []))
        s["any_recip"] += 1
        s["pf_amt"] += PFA[(mi, e, o, g)]
        s["fot_amt"] += FOTA[(mi, e, o, g)]
        s["zp_out_recip"] += st == "Z"
        s["nonzp_recip"] += st == "N"
        prev = ST.get((mi - 1, e, o, g))
        s["flip_to_nonzp"] += st == "N" and prev in ("P", "Z")
        s["flip_to_zp"] += st in ("P", "Z") and prev == "N"
        s["flapping_recip"] += sum(flip_at(x, trip) for x in range(mi - 11, mi + 1)) >= Q.FLAP_MIN
        one = len(tri_months[trip]) == 1 and mi > 0
        s["one_time_recip"] += one
        s["one_time_pf"] += one and st == "P"
        s["one_time_person"] += one and len(per_months[e]) == 1
        if st == "P":
            s["pf_recip"] += 1
            s["pf_fl"] += 1.0 / kp
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
            ten = mi - tri_first_pf[trip]
            s["ten_lt3" if ten < 3 else "ten_3_12" if ten < 12 else "ten_12_24" if ten < 24 else "ten_24p"] += 1
        arrived = st == "P" and mi >= 1 and prev != "P"
        for col, n, cond in (("surv3", 3, lambda: is_p(mi + 3, trip)), ("surv6", 6, lambda: is_p(mi + 6, trip)),
                             ("surv12", 12, lambda: is_p(mi + 12, trip)),
                             ("surv12_bank", 12, lambda: bool(p_by_pm.get((mi + 12, e))))):
            if mi + n <= last:
                s[col] += arrived and cond()

    # --- метки ---
    lab = {b: defaultdict(lambda: defaultdict(float)) for b in Q.BASES}
    for b, kk in Q.BASES.items():
        L = lab[b]
        for (mi, e, o, g), st in ST.items():
            trip = (e, o, g)
            # пришёл
            if st == "P" and mi >= kk and not is_p(mi - kk, trip):
                sb = ST.get((mi - kk,) + trip)
                w = where_else(mi - kk, e, o)
                if sb == "N":
                    x = "in_from_nonzp"
                elif sb == "Z":
                    x = "in_from_zp_out"
                elif w is not None:
                    x = {"org": "in_oth_same_org", "hold": "in_oth_same_hold", "other": "in_oth_other"}[w]
                elif e in per_first_pf and per_first_pf[e] < mi - kk:
                    x = "in_returned"
                else:
                    x = "in_new_bank"
                L[(mi, o, g)][x] += 1
                if not p_by_pm.get((mi - kk, e)):
                    L[(mi, o, g)]["fl_in"] += 1.0 / len(p_by_pm[(mi, e)])
            # ушёл (mi — база)
            m2 = mi + kk
            if st == "P" and m2 <= last and not is_p(m2, trip):
                sc = ST.get((m2,) + trip)
                w = where_else(m2, e, o)
                if sc == "N":
                    x = "out_to_nonzp"
                elif sc == "Z":
                    x = "out_zp_out"
                elif w is not None:
                    x = {"org": "out_oth_same_org", "hold": "out_oth_same_hold", "other": "out_oth_other"}[w]
                elif is_p(m2 - 13, trip) and not is_p(m2 - 12, trip) and is_p(m2 - 11, trip):
                    x = "lb_seasonal"
                elif (m2, o) not in org_pf:
                    x = "lb_org_stopped"
                elif len(tri_months[trip]) == 1:
                    x = "lb_one_time"
                else:
                    x = "lb_unknown"
                L[(m2, o, g)][x] += 1
                if not p_by_pm.get((m2, e)):
                    L[(m2, o, g)]["fl_out"] += 1.0 / len(p_by_pm[(mi, e)])
                L[(m2, o, g)]["out_temp"] += is_p(m2 + 1, trip)

    # --- сборка ---
    keys = set(stock)
    for (mi, o, g), s in stock.items():
        if s["pf_recip"] > 0:
            for kk in (1, 3, 12):
                if mi + kk <= last:
                    keys.add((mi + kk, o, g))

    def seg_of(key):
        mi, o, g = key
        if key in hist:
            return hist[key], "история"
        if o in org_seg:
            return org_seg[o], "ЕПК"
        return S.NOT_FOUND, "нет"

    def sv(key, c):
        return stock[key][c] if key in stock else 0

    out = []
    for key in sorted(keys):
        mi, o, g = key
        seg, src = seg_of(key)
        tb = next((stock_tb[(mi - d, o, g)] for d in (0, 1, 3, 12) if (mi - d, o, g) in stock_tb), None)
        row = {"report_dt": months[mi], "org_id": o, "sys_gosb_id": g, "sys_tb_id": tb,
               "seg": seg, "seg_src": src}
        for c in Q.STOCK_COLS:
            row[c] = sv(key, c)
        for c, n in zip(Q.SURV_COLS, (3, 6, 12, 12)):
            row[c] = sv(key, c) if mi + n <= last else None
        for b, kk in Q.BASES.items():
            if mi < kk:
                for c in Q.BASE_COLS:
                    row[f"{b}_{c}"] = None
                continue
            bk = (mi - kk, o, g)
            L = lab[b].get(key, {})
            get = lambda x: L.get(x, 0)                           # noqa: E731
            row[f"{b}_base_pf"] = sv(bk, "pf_recip")
            row[f"{b}_base_seg"] = seg_of(bk)[0] if bk in keys else None
            for x in Q.IN_LABELS + Q.OUT_LABELS:
                row[f"{b}_{x}"] = get(x)
            row[f"{b}_in_from_other_org"] = sum(get(x) for x in Q.IN_LABELS if x.startswith("in_oth_"))
            row[f"{b}_out_to_other_org"] = sum(get(x) for x in Q.OUT_LABELS if x.startswith("out_oth_"))
            row[f"{b}_out_left_bank"] = sum(get(x) for x in Q.OUT_LABELS if x.startswith("lb_"))
            row[f"{b}_fl_in"] = get("fl_in")
            row[f"{b}_fl_out"] = get("fl_out")
            row[f"{b}_fl_conc"] = (sv(key, "pf_fl") - sv(bk, "pf_fl")) - get("fl_in") + get("fl_out")
        row["m_out_temp"] = lab["m"].get(key, {}).get("out_temp", 0) if 1 <= mi and mi + 1 <= last else None
        out.append(row)
    return pd.DataFrame(out, columns=Q.final_columns())
