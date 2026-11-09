"""Синтетика открытого контура: витрины отчёта в своих схемах
`synth_attraction` и `synth_attraction_t`.

Запуск из папки отчёта:  python -m attraction.synth  [--url ...]

Таблицы повторяют прод-схему (имена и типы колонок — по профилям и запросам
заказчика), поэтому SQL отчёта переносится на пром без правок.

Ожидания считаются НЕЗАВИСИМО — отдельной реализацией правил на pandas по тем же
сгенерированным строкам, — и пишутся в `output/synth_expect.json`;
`selfcheck.check_against_synth` требует, чтобы отчёт совпал с ними.

Заложенные события:
* ячеек роста в 2026 меньше, чем в 2025; есть «10 ушли — 10 пришли» (ноль);
* шум ведомостей, который в портфель не идёт: «Дополнительные», «ИП 1 чел.»,
  ниже порога, только непортфельные виды, банк-работодатель; без порога —
  образовательные организации и холдинг МИНОБОРОНЫ;
* перекодировки ГОСБ: ТБ 38 → 9038, ГОСБ 1009 → 8557, ГОСБ 0 в ТБ 40 → 9040;
* штат МЗП +50%, а НФЛ МЗП растут меньше; ВСП падает, Digital растёт;
* у НФЛ несколько действий — побеждает раннее; действие вне окна, клик после
  первой ЗП, сделка в другом ГОСБ, сделка не МЗП — это «Остальное»;
* граница архива НФЛ 31.03.2026 и строки «не со своей стороны» границы;
* ИП и холдинг с ФИО в названии.
"""
from __future__ import annotations

import argparse
import io
import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from . import config
from . import months as M

SEED = 20261008
AUGS = [date(2024, 8, 31), date(2025, 8, 31), date(2026, 8, 31)]
YEARS = [2025, 2026]
NFL_SPLIT = date(2026, 3, 31)
TB = [(38, "Московский"), (40, "Северо-Западный"), (42, "Сибирский"), (44, "Поволжский"),
      (52, "Среднерусский")]
BIG = {"Средние": ("КСБ", "Средний бизнес"), "Крупные": ("КСБ", "Крупный бизнес"),
       "Малые": ("ММБ", "Малый бизнес"), "Микро": ("ММБ", "Микробизнес"),
       "Рег. госсектор": ("РГС", "Государственный сектор"),
       "Клиенты машиностроения": ("СКМ", "Клиенты машиностроения"),
       "Крупнейшие": ("КСБ", "Крупнейший бизнес")}
INDUSTRY = ["Торговля", "Образование", "Строительство", "Транспорт", "Здравоохранение", None]
HOLDING_FIO = "ГК Сидоров Пётр Ильич"
MZP_POS = "Менеджер по продаже зарплатных проектов"
VSP_PRODUCT = "Заявка на перевод заработной платы на счет, открытый в Сбербанке"
VSP_EXCLUDED_OP = "Зачисление заработной платы на счет в Сбербанке (оформление)"
# План каналов НФЛ по годам: МЗП растёт слабо при +50% штата, ВСП падает, Digital растёт.
PLAN = {2025: {"n_month": 520, "mzp": .17, "vsp": .30, "dig": .13},
        2026: {"n_month": 450, "mzp": .19, "vsp": .19, "dig": .24}}
STAFF = {date(2025, 8, 31): 60, date(2026, 8, 31): 90}


def month_end(d: date) -> date:
    return M.parse(d)


def valid_to(d: date) -> date:
    return M.shift(d, 2)


class Gen:
    def __init__(self, seed: int = SEED, n_orgs: int = 520) -> None:
        self.r = np.random.default_rng(seed)
        self.n_orgs = n_orgs
        self.next_epk = 5_000_000_000
        self.orgs: list[dict] = []
        self.pay: list[tuple] = []
        self.nfl: list[dict] = []
        self.asm: list[dict] = []
        self.clicks: list[dict] = []
        self.deals: list[dict] = []
        self.deal_idx: dict[tuple, list[dict]] = {}

    def epk(self) -> int:
        self.next_epk += int(self.r.integers(1, 7))
        return self.next_epk

    # -- организации ------------------------------------------------------- #
    def build_orgs(self) -> None:
        r = self.r
        bigs = list(BIG)
        for k in range(self.n_orgs):
            tb = TB[k % len(TB)][0]
            big = bigs[int(r.integers(len(bigs)))]
            gosbs = [tb * 100 + int(r.integers(1, 4))]
            if r.random() < 0.3:
                gosbs.append(tb * 100 + 5)
            if tb == 40 and k % 17 == 0:
                gosbs = [1009]                     # → 8557
            if tb == 40 and k % 23 == 0:
                gosbs = [0]                        # → 9040
            name = (f"ИП Кузнецов{k} Олег Николаевич" if big == "Микро" and k % 3 == 0
                    else f"ООО «Привлечение {k:04d}»")
            holding = (HOLDING_FIO if k % 41 == 7 else
                       f"Холдинг {k % 25:02d}" if k % 2 == 0 else None)
            self.orgs.append({
                "id": k, "inn": 7800000000 + k * 13, "tb": tb, "big": big, "seg": BIG[big][0],
                "pay_seg": BIG[big][1], "gosbs": gosbs, "name": name, "holding": holding,
                "industry": INDUSTRY[k % len(INDUSTRY)],
                "edu": k % 29 == 3, "mo": k % 97 == 5,
            })

    # -- ведомости ---------------------------------------------------------- #
    def _rows_person(self, o, gosb, d, epk, kind):
        """Строки ведомостей одного ФЛ: kind — как он должен (не) попасть в портфель."""
        inn = str(o["inn"])
        base = dict(report_dt=d, epk_id=epk, sys_gosb_id=gosb, inn=inn, sys_tb_id=o["tb"],
                    segment_name=o["pay_seg"], enrollment_kind_descr="Основные",
                    market_share_flag_name="Обычный")
        s = float(self.r.lognormal(10.6, 0.3))
        rows = []
        if kind == "ok":
            rows += [dict(base, enrollment_type=1, amt=0.6 * s), dict(base, enrollment_type=16, amt=0.4 * s)]
            if self.r.random() < 0.1:              # лишняя «Дополнительные» — не мешает
                rows.append(dict(base, enrollment_type=1, amt=9000, enrollment_kind_descr="Дополнительные"))
        elif kind == "low_exc":                    # ниже порога, но организация без порога
            rows.append(dict(base, enrollment_type=1, amt=1500))
        elif kind == "type2":                      # вид 2 — без порога
            rows.append(dict(base, enrollment_type=2, amt=900))
        elif kind == "low":
            rows.append(dict(base, enrollment_type=1, amt=1200))
        elif kind == "extra_only":
            rows.append(dict(base, enrollment_type=1, amt=50000, enrollment_kind_descr="Дополнительные"))
        elif kind == "ip1":
            rows.append(dict(base, enrollment_type=1, amt=50000, market_share_flag_name="ИП 1 чел."))
        elif kind == "nonpf":
            rows.append(dict(base, enrollment_type=7, amt=30000))
        elif kind == "refund":
            rows.append(dict(base, enrollment_type=1, amt=-4000))
        self.pay += rows

    def build_payroll(self) -> None:
        r = self.r
        self.cells = {}                             # (org_id, gosb) → [n24, n25, n26]
        cls_p = {0: {"grow": .46, "decl": .28, "flat": .20, "closed": .06},
                 1: {"grow": .32, "decl": .40, "flat": .22, "closed": .06}}
        for o in self.orgs:
            for g in o["gosbs"]:
                n = max(1, int(r.lognormal(2.6, 0.9)))
                start = 0 if r.random() > 0.08 else (1 if r.random() < 0.5 else 2)   # новые ячейки
                people = [self.epk() for _ in range(n)] if start == 0 else []
                sizes = []
                for yi, d in enumerate(AUGS):
                    if yi == start and start > 0:
                        people = [self.epk() for _ in range(n)]
                    elif yi > 0 and people:
                        p = cls_p[yi - 1]
                        c = r.choice(list(p), p=list(p.values()))
                        m = len(people)
                        if c == "closed":
                            people = []
                        else:
                            if c == "grow":
                                target = m + int(r.integers(1, max(2, m // 3 + 2)))
                            elif c == "decl":
                                target = max(1, m - int(r.integers(1, max(2, m // 3 + 1)))) if m > 1 else m
                            else:
                                target = m
                            churn = int(r.integers(0, max(1, m // 4 + 1)))     # «ушли — пришли»
                            keep = list(r.choice(people, size=max(0, min(m, target) - churn), replace=False)) \
                                if min(m, target) - churn > 0 else []
                            people = [int(x) for x in keep] + [self.epk() for _ in range(target - len(keep))]
                    sizes.append(len(people))
                    for epk in people:
                        kind = "ok"
                        if (o["edu"] or o["mo"]) and r.random() < 0.3:
                            kind = "low_exc"
                        elif r.random() < 0.03:
                            kind = "type2"
                        self._rows_person(o, g, d, epk, kind)
                    # Шум, который в портфель не идёт.
                    if people and r.random() < 0.5:
                        for kind in r.choice(["low", "extra_only", "ip1", "nonpf", "refund"],
                                             size=int(r.integers(1, 3))):
                            if kind == "low" and (o["edu"] or o["mo"]):
                                continue
                            self._rows_person(o, g, d, self.epk(), str(kind))
                self.cells[(o["id"], g)] = sizes
        # Банк-работодатель и «Не определено» — не в портфеле.
        for d in AUGS:
            for inn in ("7707083893", "Не определено"):
                for _ in range(5):
                    self.pay.append(dict(report_dt=d, epk_id=self.epk(), sys_gosb_id=4201, inn=inn,
                                         sys_tb_id=42, segment_name="Средний бизнес",
                                         enrollment_kind_descr="Основные", market_share_flag_name="Обычный",
                                         enrollment_type=1, amt=60000))

    # -- НФЛ и действия ----------------------------------------------------- #
    def _deal(self, o, gosb, d: date, role="МЗП"):
        code = f"D{len(self.deals):06d}"
        dl = {"deal_code": code, "deal_dt": d, "inn": o["inn"], "gosb": gosb, "tb": o["tb"], "role": role,
              "emp": 100000 + int(self.r.integers(0, 150)), "plan": int(self.r.integers(5, 60)),
              "fact": 0, "seg": o["big"], "name": o["name"]}
        self.deals.append(dl)
        if role == "МЗП":
            self.deal_idx.setdefault((o["inn"], gosb), []).append(dl)
        return dl

    def _deal_for(self, o, gosb, pay: date):
        """Сделка МЗП ячейки, действующая на дату ЗП; нет — создаётся новая."""
        for dl in self.deal_idx.get((o["inn"], gosb), []):
            if dl["deal_dt"] <= pay <= valid_to(dl["deal_dt"]):
                return dl
        return self._deal(o, gosb, pay - timedelta(days=int(self.r.integers(0, 50))))

    def _vsp(self, epk, d: date, ok=True, why=""):
        row = {"report_dt": month_end(d), "epk_id": epk, "src_report_dt": d, "sales_channel_group": "ВСП",
               "fraud_type": 0, "calc_product": VSP_PRODUCT, "src_operation_name": "Заявка"}
        if not ok:
            if why == "fraud":
                row["fraud_type"] = 1
            elif why == "product":
                row["calc_product"] = "Кредитная карта"
            elif why == "op":
                row["src_operation_name"] = VSP_EXCLUDED_OP
            elif why == "group":
                row["sales_channel_group"] = "КЦ"
        self.asm.append(row)

    def _click(self, epk, d: date):
        self.clicks.append({"epk_id": epk, "data_timestamp": datetime(d.year, d.month, d.day,
                                                                      int(self.r.integers(8, 22)), 15)})

    def build_nfl(self) -> None:
        r = self.r
        w = np.array([sum(self.cells[(o["id"], g)]) + 1 for o in self.orgs for g in o["gosbs"]], float)
        keys = [(o, g) for o in self.orgs for g in o["gosbs"]]
        for y in YEARS:
            pl = PLAN[y]
            for mo in range(1, 9):
                me = month_end(date(y, mo, 1))
                for _ in range(int(pl["n_month"] * r.uniform(0.9, 1.1))):
                    o, g = keys[int(r.choice(len(keys), p=w / w.sum()))]
                    pay = date(y, mo, int(r.integers(1, me.day + 1)))
                    epk = self.epk()
                    row = {"report_dt": me, "tb_id": o["tb"], "gosb_id": g, "inn": o["inn"],
                           "fl_epk_id": epk, "first_payment_dt": pay, "is_nfl": True, "is_overflow_fl": False}
                    self.nfl.append(row)
                    u = r.random()
                    if u < pl["mzp"]:
                        self._deal_for(o, g, pay)
                        if r.random() < 0.15:                   # клик ПОЗЖЕ сделки — МЗП победит
                            self._click(epk, pay)
                    elif u < pl["mzp"] + pl["vsp"]:
                        d = pay - timedelta(days=int(r.integers(5, 55)))
                        self._vsp(epk, d)
                        if r.random() < 0.15:
                            self._vsp(epk, d + timedelta(days=1))      # второй визит
                        if r.random() < 0.2:
                            self._click(epk, d + timedelta(days=3))     # позже ВСП — ВСП победит
                        if r.random() < 0.2:
                            self._vsp(epk, d - timedelta(days=2), ok=False,
                                      why=str(r.choice(["fraud", "product", "op", "group"])))
                    elif u < pl["mzp"] + pl["vsp"] + pl["dig"]:
                        d = pay - timedelta(days=int(r.integers(1, 50)))
                        for k in range(int(r.integers(1, 4))):
                            self._click(epk, d + timedelta(days=k))
                    else:
                        v = r.random()
                        if v < 0.15:                             # консультация вне окна
                            self._vsp(epk, pay - timedelta(days=int(r.integers(100, 160))))
                        elif v < 0.25:                           # клик после ЗП
                            self._click(epk, pay + timedelta(days=int(r.integers(1, 20))))
                        elif v < 0.33 and len(o["gosbs"]) > 1:   # сделка в другом ГОСБ
                            other = [x for x in o["gosbs"] if x != g][0]
                            if not any(dl["deal_dt"] <= pay <= valid_to(dl["deal_dt"])
                                       for dl in self.deal_idx.get((o["inn"], g), [])):
                                self._deal(o, other, pay - timedelta(days=10))
                        elif v < 0.38:                           # сделка не МЗП
                            self._deal(o, g, pay - timedelta(days=10), role="КМ")
        # Шум НФЛ: переток, не НФЛ, без ЕПК, «не со своей стороны» границы архива.
        extra = []
        for row in self.r.choice(self.nfl, size=200, replace=False):
            extra.append(dict(row, is_overflow_fl=True, fl_epk_id=self.epk()))
        for row in self.r.choice(self.nfl, size=100, replace=False):
            extra.append(dict(row, is_nfl=False, fl_epk_id=self.epk()))
        for row in self.r.choice(self.nfl, size=40, replace=False):
            extra.append(dict(row, fl_epk_id=None))
        self.nfl += extra
        # Сделки без НФЛ — у новых менеджеров 2026 больше сделок с низкой отдачей.
        for y, n in ((2025, 300), (2026, 560)):
            for _ in range(n):
                o = self.orgs[int(r.integers(len(self.orgs)))]
                # ГОСБ без ячеек ведомостей: сделка не по месту — диагностика привязки.
                self._deal(o, o["tb"] * 100 + 9, date(y, int(r.integers(1, 9)), int(r.integers(1, 28))))
        for dl in self.deals:
            dl["fact"] = int(dl["plan"] * r.uniform(0.1, 0.9 if dl["deal_dt"].year == 2025 else 0.6))

    # -- независимые ожидания ---------------------------------------------- #
    @staticmethod
    def gosb_fix(g, tb, seg=None):
        if tb == 38:
            return 9038
        if g == 1009:
            return 8557
        if g == 0 and tb == 40:
            return 9040
        return g

    def expect(self, pay: pd.DataFrame, nfl_split_rows: tuple) -> dict:
        exc = {o["inn"] for o in self.orgs if o["edu"] or o["mo"]}
        p = pay.copy()
        p["g"] = [self.gosb_fix(g, t) for g, t in zip(p["sys_gosb_id"], p["sys_tb_id"])]
        p["inn_k"] = [int(x) if str(x).isdigit() else -1 for x in p["inn"].replace({"Не определено": "0"})]
        p["relev"] = ((p["enrollment_kind_descr"] == "Основные") & (p["market_share_flag_name"] != "ИП 1 чел.")
                      & ~p["inn"].isin(["7707083893", "Не определено", "0", "-1"]))
        p["pf"] = p["enrollment_type"].isin([1, 2, 16, 26])
        p["pf_amt"] = np.where(p["pf"] & p["relev"], p["amt"], 0)
        p["sum_pf"] = p.groupby(["report_dt", "epk_id", "g", "inn"])["pf_amt"].transform("sum")
        ok = p["relev"] & (p["amt"] > 0) & ((p["pf"] & (p["sum_pf"] > 2500)) | (p["enrollment_type"] == 2)
                                            | (p["pf"] & p["inn_k"].isin(exc)))
        cell = (p[ok].groupby(["report_dt", "g", "inn_k"])["epk_id"].nunique())
        out = {"portfolio": {str(d): int(cell[cell.index.get_level_values(0) == d].sum()) for d in AUGS}}
        cls = {}
        for tag, b, c in (("cur", AUGS[1], AUGS[2]), ("prev", AUGS[0], AUGS[1])):
            cb = cell[cell.index.get_level_values(0) == b].droplevel(0)
            cc = cell[cell.index.get_level_values(0) == c].droplevel(0)
            j = pd.concat([cb.rename("b"), cc.rename("c")], axis=1).fillna(0)
            k = np.select([j["b"] == 0, j["c"] == 0, j["c"] > j["b"], j["c"] < j["b"]],
                          ["new", "closed", "grow", "decl"], "flat")
            cls[tag] = pd.Series(k).value_counts().to_dict()
        out["cells"] = {t: {k: int(v) for k, v in d.items()} for t, d in cls.items()}

        # НФЛ и атрибуция — по правилам заказчика, отдельно от SQL.
        n = pd.DataFrame(self.nfl)
        months = [month_end(date(y, m, 1)) for y in YEARS for m in range(1, 9)]
        arch, cur = nfl_split_rows
        n = n[n["is_nfl"] & ~n["is_overflow_fl"] & n["report_dt"].isin(months)]
        n = n[[((d <= NFL_SPLIT) and a) or ((d > NFL_SPLIT) and not a)
               for d, a in zip(n["report_dt"], n["_arch"])]]
        out["nfl_no_epk"] = int(n["fl_epk_id"].isna().sum())
        n = n[n["fl_epk_id"].notna()].copy()
        n["g"] = [self.gosb_fix(g, t) for g, t in zip(n["gosb_id"], n["tb_id"])]
        act_from, act_to = date(2024, 11, 1), date(2026, 8, 31)
        vsp: dict[int, list] = {}
        for a in self.asm:
            if (a["sales_channel_group"] in ("ВИП", "ВСП", "ПРЕМЬЕР") and a["fraud_type"] == 0
                    and a["calc_product"] == VSP_PRODUCT and a["src_operation_name"] != VSP_EXCLUDED_OP
                    and act_from <= a["src_report_dt"] <= act_to):
                vsp.setdefault(a["epk_id"], []).append(a["src_report_dt"])
        dig: dict[int, list] = {}
        for c in self.clicks:
            d = c["data_timestamp"].date()
            if act_from <= d <= act_to:
                dig.setdefault(c["epk_id"], []).append(d)
        deals: dict[tuple, list] = {}
        for dl in self.deals:
            if dl["role"] == "МЗП" and act_from <= dl["deal_dt"] <= act_to:
                deals.setdefault((dl["inn"], self.gosb_fix(dl["gosb"], dl["tb"])), []).append(dl["deal_dt"])
        res = {}
        for row in n.itertuples():
            pay = row.first_payment_dt
            cand = []
            for d in deals.get((row.inn, row.g), []):
                if d <= pay <= valid_to(d):
                    cand.append((d, 1, "mzp"))
            for d in vsp.get(row.fl_epk_id, []):
                if d <= pay <= valid_to(d):
                    cand.append((d, 2, "vsp"))
            for d in dig.get(row.fl_epk_id, []):
                if d <= pay <= valid_to(d):
                    cand.append((d, 3, "dig"))
            ch = min(cand)[2] if cand else "other"
            y = row.report_dt.year
            res.setdefault(str(y), {}).setdefault(ch, 0)
            res[str(y)][ch] += 1
        out["nfl"] = res
        out["staff"] = {str(k): v for k, v in STAFF.items()}
        out["deals_mzp"] = {str(y): sum(1 for dl in self.deals if dl["role"] == "МЗП"
                                        and dl["deal_dt"].year == y and dl["deal_dt"].month <= 8)
                            for y in YEARS}
        out["fio_names"] = [o["name"] for o in self.orgs if o["name"].startswith("ИП ")] + [HOLDING_FIO]
        return out


# --------------------------------------------------------------------------- #
# Схемы витрин (имена и типы — как на проме)
# --------------------------------------------------------------------------- #
NFL_COLS = ("report_dt date, tb_id smallint, gosb_id integer, ul_epk_id bigint, inn bigint, "
            "payroll_agrmnt_num bigint, fl_epk_id bigint, is_nfl boolean, is_overflow_fl boolean, "
            "nfl_enrollment_amt numeric, first_payment_dt date, is_motiv boolean, is_lower_cut_off boolean, "
            "enrollment_next_m_inn_gosb_amt smallint, portfolio_growth_coeff numeric, b2c_distribution_dt date, "
            "b2c_distribution_saphr_id bigint, b2c_distribution_type varchar, b2b_distribution_dt date, "
            "b2b_distribution_saphr_id bigint, b2b_deal_code varchar, b2b_deal_created_dttm timestamp, "
            "b2b_offer_code varchar, b2b_offer_created_dttm timestamp, ca_task_code varchar, "
            "ca_task_created_dttm timestamp, tb_task_code varchar, tb_task_create_dttm timestamp, "
            "manager_task_code varchar, manager_task_created_dttm timestamp")
DDL = """
DROP SCHEMA IF EXISTS {s} CASCADE;
DROP SCHEMA IF EXISTS {t} CASCADE;
CREATE SCHEMA {s};
CREATE SCHEMA {t};
CREATE TABLE {s}.mis_data_payroll_m (
  acc_num text, acc_open_dt date, actual_client_tid bigint, amt numeric, company_name text,
  agrmnt_dt date, agrmnt_num text, enrollment_transcription text, enrollment_type smallint,
  epk_id bigint, sys_gosb_id integer, inn text, ipt_name text, sys_osb_id integer, card_type text,
  modified_dttm timestamp, report_dt date, sys_tb_id smallint, transaction_qty smallint,
  sys_vsp_id integer, report_id bigint, is_security_force boolean, segment_name text,
  enrollment_kind_descr text, market_share_flag_name text, src_system_name text);
CREATE TABLE {s}.uzp_dim_enrollment_type (
  enrollment_type_id smallint, enrollment_type_name varchar, is_fot_enrollment boolean,
  is_portfolio_enrollment boolean);
CREATE TABLE {s}.uzp_dim_education_organization (inn varchar, company_name varchar);
CREATE TABLE {s}.uzp_dim_gosb (
  tb_id integer, tb_short_name text, tb_full_name text, old_gosb_name text,
  old_gosb_id integer, new_gosb_name text, new_gosb_id integer, isu_branch_id bigint,
  isu_branch_name text, web_gosb_id integer, pirs_gosb_id bigint, pirs_gosb_name text,
  utc_timezone smallint, timezone_violation_msk smallint, region_id smallint,
  region_name varchar, inserted_dttm timestamp, author_login text);
CREATE TABLE {s}.uzp_data_epk_consolidation (
  epk_id bigint, epk_create_dttm timestamp, client_type_id smallint, client_type_name varchar,
  industry_id smallint, industry_name varchar, inn bigint, kpp bigint, ogrn bigint,
  okato bigint, oktmo bigint, old_epk_id varchar, segment_id smallint, segment_name varchar,
  priority_id smallint, priority_name varchar, company_name varchar, holding_epk_id bigint,
  holding_name varchar, reference_holding_name varchar, head_holding_epk_id bigint,
  head_holding_name varchar, reference_head_holding_name varchar, is_parent boolean,
  is_key_client boolean, importance_lvl_id smallint, tb_id smallint, gosb_id integer,
  oktmo_gosb_id integer, okato_gosb_id integer, epk_gosb_id integer, payroll_gosb_id integer,
  km_gosb_id integer, last_mzp_activity_gosb_id integer, last_deal_gosb_id integer,
  kpp_gosb_id integer, gosb_method_id smallint, status_id smallint, status_name varchar,
  is_educational boolean, is_military boolean, report_id bigint, modified_dttm timestamp);
CREATE TABLE {s}.uzp_data_nfl_channel ({nfl});
CREATE TABLE {t}.bkv_bkv_uzp_data_nfl_channel ({nfl});
CREATE TABLE {s}.asm_data_operation (
  report_dt date, epk_id bigint, src_report_dt date, sales_channel_group varchar, fraud_type smallint,
  calc_product varchar, src_operation_name varchar);
CREATE TABLE {t}.ml_ksa_clickstream_events_oaa (epk_id bigint, data_timestamp text);  -- на проме текст
CREATE TABLE {s}.uzp_dwh_sale_funnel_task (
  report_dt date, tb_id integer, tb_name varchar, gosb_id integer, gosb_name varchar, saphr_gosb_id integer,
  manager_saphr_id bigint, manager_fio varchar, isu_struct_saphr_id bigint, task_struct_saphr_id bigint,
  emp_fio varchar, emp_post_id bigint, emp_post varchar, role_code varchar, src_task_as_code varchar,
  src_task_business varchar, inn bigint, company_name varchar, segment_name varchar,
  escalation_parent_task_code varchar, task_category varchar, task_code varchar, task_create_dt date,
  plan_close_task_dttm timestamp, fact_close_task_dttm timestamp, task_type varchar, task_subtype varchar,
  last_active_type varchar, last_active_dttm timestamp, last_active_status varchar, campaign_code varchar,
  is_task_closed boolean, is_task_closed_success boolean, is_task_in_progress boolean,
  task_text_status varchar, unrealized_deal_potential integer, deal_code varchar, deal_create_dttm timestamp,
  plan_staff_deal_qty integer, fact_staff_deal_qty integer, task_text varchar, task_comment varchar,
  task_questionnaire varchar, is_escalation_need boolean, src_update_dttm timestamp, update_dttm timestamp);
CREATE TABLE {s}.uzp_dwh_sap_staff_emp (
  report_dt date, saphr_id bigint, fio varchar, post_id bigint, post_name varchar, pos_id bigint,
  pos_name varchar, tb_code varchar, tb_id smallint, gosb_code varchar, sap_gosb_code varchar,
  gosb_id integer, city varchar, fact_qty smallint, post_cnt_fill numeric, vertical_name varchar,
  role_code varchar, is_presence boolean);
"""


def _copy(conn, df: pd.DataFrame, table: str) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    with conn.connection.cursor() as cur:
        cur.copy_expert(f"COPY {table} ({', '.join(df.columns)}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf)


def build(url: str | None = None, schema: str = config.SYNTH_SCHEMA,
          schema_t: str = config.SYNTH_SCHEMA_T) -> dict:
    print(f"→ синтетика: схемы {schema}, {schema_t}", flush=True)
    g = Gen()
    g.build_orgs()
    g.build_payroll()
    g.build_nfl()
    pay = pd.DataFrame(g.pay)
    pay["amt"] = pay["amt"].round(2)
    pay["company_name"] = None

    # НФЛ: по своей стороне границы архива + немного строк «не со своей стороны».
    nfl = pd.DataFrame(g.nfl)
    nfl["_arch"] = nfl["report_dt"] <= NFL_SPLIT
    wrong = nfl.sample(60, random_state=1).copy()
    wrong["_arch"] = ~wrong["_arch"]
    wrong["fl_epk_id"] = [g.epk() for _ in range(len(wrong))]
    nfl = pd.concat([nfl, wrong], ignore_index=True)
    g.nfl = nfl.to_dict("records")
    exp = g.expect(pay, (True, False))
    nfl_cols = ["report_dt", "tb_id", "gosb_id", "inn", "fl_epk_id", "is_nfl", "is_overflow_fl",
                "first_payment_dt"]
    nfl["fl_epk_id"] = nfl["fl_epk_id"].astype("Int64")

    epk = pd.DataFrame([{"epk_id": 9_000_000 + o["id"], "inn": o["inn"], "segment_name": o["big"],
                         "company_name": o["name"], "holding_name": o["holding"],
                         "reference_holding_name": "МИНОБОРОНЫ" if o["mo"] else None,
                         "industry_name": o["industry"], "status_name": "Активна", "tb_id": o["tb"]}
                        for o in g.orgs])
    edu = pd.DataFrame([{"inn": str(o["inn"]), "company_name": o["name"]} for o in g.orgs if o["edu"]])
    et = pd.DataFrame([{"enrollment_type_id": 1, "enrollment_type_name": "Заработная плата",
                        "is_fot_enrollment": True, "is_portfolio_enrollment": True},
                       {"enrollment_type_id": 2, "enrollment_type_name": "Денежное довольствие",
                        "is_fot_enrollment": True, "is_portfolio_enrollment": True},
                       {"enrollment_type_id": 16, "enrollment_type_name": "Аванс по заработной плате",
                        "is_fot_enrollment": True, "is_portfolio_enrollment": True},
                       {"enrollment_type_id": 26, "enrollment_type_name": "Отпускные",
                        "is_fot_enrollment": True, "is_portfolio_enrollment": True},
                       {"enrollment_type_id": 7, "enrollment_type_name": "Пособие",
                        "is_fot_enrollment": False, "is_portfolio_enrollment": False}])
    gosb = pd.DataFrame([{"tb_id": tb, "tb_short_name": name, "new_gosb_id": tb * 100 + k}
                         for tb, name in TB for k in range(1, 6)])

    # Воронка: сделка — в нескольких снимках, факт растёт; последний снимок — итог.
    fun = []
    for dl in g.deals:
        snaps = [M.shift(dl["deal_dt"], k) for k in range(0, 4) if M.shift(dl["deal_dt"], k) <= date(2026, 8, 31)]
        for i, sd in enumerate(snaps):
            fun.append({"report_dt": sd, "tb_id": dl["tb"], "gosb_id": dl["gosb"], "isu_struct_saphr_id": dl["emp"],
                        "role_code": dl["role"], "src_task_as_code": "МЗП", "inn": dl["inn"],
                        "company_name": dl["name"], "segment_name": dl["seg"], "task_category": "Задача",
                        "task_code": f"T{dl['deal_code']}", "task_create_dt": dl["deal_dt"],
                        "task_type": "Привлечение ЗП", "deal_code": dl["deal_code"],
                        "deal_create_dttm": datetime(dl["deal_dt"].year, dl["deal_dt"].month, dl["deal_dt"].day, 11),
                        "plan_staff_deal_qty": dl["plan"],
                        "fact_staff_deal_qty": int(dl["fact"] * (i + 1) / len(snaps))})
    # Задачи без сделки — не сделки.
    for k in range(300):
        fun.append({"report_dt": date(2026, 8, 31), "tb_id": 42, "gosb_id": 4201, "role_code": "МЗП",
                    "inn": g.orgs[k % len(g.orgs)]["inn"], "task_code": f"N{k:05d}", "task_type": "Отток",
                    "plan_staff_deal_qty": 0, "fact_staff_deal_qty": 0})
    fun = pd.DataFrame(fun)
    for c in ("isu_struct_saphr_id", "plan_staff_deal_qty", "fact_staff_deal_qty"):
        fun[c] = fun[c].astype("Int64")

    staff = []
    for d, n in STAFF.items():
        for k in range(n):
            row = {"report_dt": d, "saphr_id": 200000 + k, "pos_name": MZP_POS, "post_name": MZP_POS,
                   "tb_id": TB[k % len(TB)][0], "vertical_name": "zp", "fact_qty": 1}
            staff.append(row)
            if k % 10 == 0:
                staff.append(dict(row, post_cnt_fill=0.5))        # вторая строка того же человека
        for k in range(12):
            staff.append({"report_dt": d, "saphr_id": 300000 + k, "pos_name": "Клиентский менеджер",
                          "tb_id": 42, "vertical_name": "zp", "fact_qty": 1})
    staff = pd.DataFrame(staff)

    eng = create_engine(config.db_url(url), future=True)
    with eng.connect() as conn:
        for stmt in DDL.format(s=schema, t=schema_t, nfl=NFL_COLS).split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
        _copy(conn, pay[["report_dt", "epk_id", "sys_gosb_id", "inn", "sys_tb_id", "segment_name",
                         "enrollment_kind_descr", "market_share_flag_name", "enrollment_type", "amt",
                         "company_name"]], f"{schema}.mis_data_payroll_m")
        _copy(conn, et, f"{schema}.uzp_dim_enrollment_type")
        _copy(conn, edu, f"{schema}.uzp_dim_education_organization")
        _copy(conn, epk, f"{schema}.uzp_data_epk_consolidation")
        _copy(conn, gosb, f"{schema}.uzp_dim_gosb")
        _copy(conn, nfl.loc[~nfl["_arch"], nfl_cols], f"{schema}.uzp_data_nfl_channel")
        _copy(conn, nfl.loc[nfl["_arch"], nfl_cols], f"{schema_t}.bkv_bkv_uzp_data_nfl_channel")
        _copy(conn, pd.DataFrame(g.asm), f"{schema}.asm_data_operation")
        _copy(conn, pd.DataFrame(g.clicks), f"{schema_t}.ml_ksa_clickstream_events_oaa")
        _copy(conn, fun, f"{schema}.uzp_dwh_sale_funnel_task")
        _copy(conn, staff, f"{schema}.uzp_dwh_sap_staff_emp")
        conn.commit()
    print(f"  ведомости {len(pay):,}, НФЛ {len(nfl):,}, ВСП {len(g.asm):,}, клики {len(g.clicks):,}, "
          f"сделки {len(g.deals):,}", flush=True)
    config.ensure_dirs()
    path = config.OUTPUT_DIR / "synth_expect.json"
    path.write_text(json.dumps(exp, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"✓ готово; ожидания → {path}", flush=True)
    return exp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None)
    a = ap.parse_args()
    build(a.url)


if __name__ == "__main__":
    main()
