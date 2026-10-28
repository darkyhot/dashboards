"""Синтетика открытого контура: витрины и справочники в схеме `synth_ptf`.

Запуск из папки portfolio_core:  python -m ptf.synth  [--url ...] [--persons 2000]

Таблицы повторяют прод-схему (имена и типы колонок), поэтому SQL переносится на пром
без правок. Месяцы — авг-23 … окт-26: три месяца ДО окна проверяют, что окно
соблюдается.

Заложенные события (по одному на каждую ветку разметки):
* уход из банка, переход в другую организацию (та же организация / тот же холдинг /
  чужая), уход на не зп коды, выпадение ниже порога, возврат после разрыва;
* совместители (вторая работа: тот же холдинг, та же организация в другом ГОСБ, чужая);
* сезонники — пропадают каждый август и возвращаются в сентябре;
* пропуск одного месяца; разовые (один месяц зачислений); «мигающие» зп ↔ не зп;
* организация перестаёт платить целиком (июнь 2025), часть людей переходит в другую;
* смена исторического сегмента организации (ММБ → КСБ с марта 2025);
* организация без истории сегмента (запасной ЕПК) и без ЕПК вообще;
* отбор заказчика: «Дополнительные», «ИП 1 чел.», организация Сбера, «Не определено»,
  непригодный номер, пустой ФЛ (не входят; в ноябре–декабре 2023 первые два —
  входят), сторно, вид 2 ниже порога (входит), образовательная и «МИНОБОРОНЫ» без
  порога, ТИК/ЦИК (исключена целиком), перекодировки ГОСБ.
"""
from __future__ import annotations

import argparse
import io
import json
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from . import config
from . import months as M

START = date(2023, 8, 31)
N_GEN = 39                      # авг-23 … окт-26
REPORT_MONTH = "2026-10"
SEED = 20261027
SBER = "7707083893"

# код → (название, портфельный, ФОТ)
CODES = {1: ("Заработная плата", True, True), 16: ("Аванс", True, True),
         2: ("Денежное довольствие", True, True), 26: ("Отпускные", True, True),
         28: ("Премия", True, True), 7: ("Пособие", False, False),
         40: ("Командировочные", False, False), 3: ("Пенсия НПФ", False, False)}

# (ТБ, ГОСБ сырой). Перекодировки: ТБ 38 → 9038, ГОСБ 1009 → 8557, ГОСБ 0 в ТБ 40 → 9040,
# ГОСБ 8591 при КСБ-сегменте ведомостей → 8646.
GOSBS = [(52, 8601), (52, 8602), (44, 8611), (38, 8620), (40, 0), (42, 1009), (42, 8591), (44, 8612)]
SEGS = [  # (ЕПК большое имя, ведомостное имя)
    ("Микро", "Микробизнес"), ("Малые", "Малый бизнес"), ("Средние", "Средний бизнес"),
    ("Крупные", "Крупный бизнес"), ("Рег. госсектор", "Государственный сектор"),
    ("Клиенты машиностроения", "Клиенты машиностроения"), ("Фин.институты", "CIB"),
]


def gi_of(y: int, m: int) -> int:
    return (y - START.year) * 12 + (m - START.month)


GI_STOP = gi_of(2025, 6)
GI_RECLASS = gi_of(2025, 3)
GI_REORG = gi_of(2025, 9)
AUGS = [i for i in range(N_GEN) if M.shift(START, i).month == 8]
# Сезон длиной 3 мес. (авг–окт) — ловится; 4 мес. (авг–ноя) — уже не сезон.
SEAS3 = [i for i in range(N_GEN) if M.shift(START, i).month in (8, 9, 10)]
SEAS4 = [i for i in range(N_GEN) if M.shift(START, i).month in (8, 9, 10, 11)]


class Gen:
    def __init__(self, persons: int, seed: int = SEED) -> None:
        self.rng = np.random.default_rng(seed)
        self.n0 = persons
        self.orgs: list[dict] = []
        self.spells: list[dict] = []    # epk, org, gosb_k, start, end, mode, skip
        self.next_epk = 5_000_000_000
        self.noise: list[dict] = []
        self.expect: dict = {}

    # ---------------------------------------------------------------- орг --
    def build_orgs(self, n: int = 60) -> None:
        r = self.rng
        holds = [f"ГК Холдинг {i}" for i in range(8)]
        for k in range(n):
            big, pay = SEGS[k % len(SEGS)]
            g1 = GOSBS[k % len(GOSBS)]
            gs = [g1] if k % 3 else [g1, GOSBS[(k + 3) % len(GOSBS)]]
            self.orgs.append({"k": k, "inn": str(7_700_000_100 + k), "name": f"ООО Орг {k}",
                              "big": big, "pay": pay, "gosbs": gs,
                              "hold": holds[k % 8] if k < 32 else None, "ref_hold": None,
                              "role": "plain"})
        # Особые организации — в конце списка, их люди задаются отдельно.
        def special(role: str, big="Средние", pay="Средний бизнес", gosbs=((52, 8601),), **kw):
            k = len(self.orgs)
            o = {"k": k, "inn": str(7_700_000_100 + k), "name": f"Особая {role}", "big": big,
                 "pay": pay, "gosbs": list(gosbs), "hold": None, "ref_hold": None, "role": role}
            o.update(kw)
            self.orgs.append(o)
            return k
        self.o_stop = special("stop", gosbs=[(52, 8601), (44, 8611)])
        self.o_reclass = special("reclass", big="Средние", pay="Малый бизнес")
        self.o_nohist = special("nohist", big="Рег. госсектор", pay="Государственный сектор")
        self.o_noepk = special("noepk")
        self.o_edu = special("edu", big="Рег. госсектор", pay="Государственный сектор")
        self.o_mo = special("mo", big="Рег. госсектор", pay="Государственный сектор",
                            ref_hold="МИНОБОРОНЫ", hold="МИНОБОРОНЫ")
        self.o_tik = special("tik")
        self.o_tik_false = special("tik_false")
        self.o_flap = special("flap", big="Малые", pay="Малый бизнес")
        self.o_seas = special("seas", big="Малые", pay="Малый бизнес")
        self.o_seas3 = special("seas3", big="Рег. госсектор", pay="Государственный сектор")
        self.o_seas4 = special("seas4", big="Рег. госсектор", pay="Государственный сектор")
        self.o_reorg_a = special("reorg_a", gosbs=[(44, 8612)])
        self.o_reorg_b = special("reorg_b", gosbs=[(44, 8612)])
        self.plain = list(range(n))

    # ------------------------------------------------------------- люди --
    def _epk(self) -> int:
        self.next_epk += 1
        return self.next_epk

    def _spell(self, epk, org, gk, start, end, mode="zp", skip=()):
        self.spells.append({"epk": epk, "org": org, "gk": gk, "start": start, "end": end,
                            "mode": mode, "skip": set(skip)})

    def _pick_move(self, org: int, gk: int):
        """Куда переходит человек: та же организация в другом ГОСБ / тот же холдинг / чужая."""
        r = self.rng
        o = self.orgs[org]
        u = r.random()
        if u < 0.25 and len(o["gosbs"]) > 1:
            return org, 1 - gk
        if u < 0.55 and o["hold"]:
            same = [x for x in self.plain if self.orgs[x]["hold"] == o["hold"] and x != org]
            if same:
                t = int(r.choice(same))
                return t, 0
        t = int(r.choice([x for x in self.plain if x != org]))
        return t, int(r.integers(len(self.orgs[t]["gosbs"])))

    def build_people(self) -> None:
        r = self.rng
        n_join = 0
        for i in range(self.n0 + int(self.n0 * 0.012 * N_GEN)):
            epk = self._epk()
            start = 0 if i < self.n0 else int(r.integers(1, N_GEN))
            n_join += i >= self.n0
            org = int(r.choice(self.plain))
            gk = int(r.integers(len(self.orgs[org]["gosbs"])))
            self._career(epk, org, gk, start)
            if r.random() < 0.08:                                   # вторая работа
                o2, g2 = self._pick_move(org, gk)
                s2 = start + int(r.integers(0, 12))
                if s2 < N_GEN:
                    self._spell(epk, o2, g2, s2, min(N_GEN - 1, s2 + int(r.integers(4, 30))))
        # Особые группы
        stop_people = []
        for _ in range(60):
            epk = self._epk()
            gk = int(r.integers(2))
            stop_people.append(epk)
            self._spell(epk, self.o_stop, gk, 0, GI_STOP - 1)
            if r.random() < 0.2:
                t, g = self._pick_move(self.o_stop, gk)
                if t == self.o_stop:
                    t, g = int(r.choice(self.plain)), 0
                self._spell(epk, t, g, GI_STOP, N_GEN - 1)
        for role_org, cnt in ((self.o_reclass, 40), (self.o_nohist, 25), (self.o_noepk, 15),
                              (self.o_edu, 30), (self.o_mo, 30), (self.o_tik, 25),
                              (self.o_tik_false, 20)):
            for _ in range(cnt):
                epk = self._epk()
                mode = "small" if role_org in (self.o_edu, self.o_mo) else "zp"
                self._spell(epk, role_org, 0, int(r.integers(0, 6)),
                            N_GEN - 1 - int(r.integers(0, 8)), mode=mode)
        for _ in range(40):                                         # сезонники
            self._spell(self._epk(), self.o_seas, 0, 0, N_GEN - 1, mode="zp", skip=AUGS)
        for org_s, skip in ((self.o_seas3, SEAS3), (self.o_seas4, SEAS4)):
            for _ in range(30):                                     # многомесячный сезон
                self._spell(self._epk(), org_s, 0, 0, N_GEN - 1, mode="zp", skip=skip)
        for _ in range(60):                                         # реорганизация A → B
            epk = self._epk()
            self._spell(epk, self.o_reorg_a, 0, 0, GI_REORG - 1)
            self._spell(epk, self.o_reorg_b, 0, GI_REORG, N_GEN - 1)
        for _ in range(30):                                         # «Дополнительные» на стыке отбора
            epk = self._epk()
            org = int(r.choice(self.plain))
            for gi in range(gi_of(2023, 11), gi_of(2024, 3) + 1):
                self.noise.append(self._row(gi, epk, org, 0, 1, 40_000,
                                            enrollment_kind_descr="Дополнительные"))
        for _ in range(30):                                         # «мигающие»
            self._spell(self._epk(), self.o_flap, 0, int(r.integers(0, 10)), N_GEN - 1, mode="flap")
        for _ in range(60):                                         # разовые
            org = int(r.choice(self.plain))
            t = int(r.integers(0, N_GEN))
            self._spell(self._epk(), org, 0, t, t)
        for _ in range(80):                                         # пропуск одного месяца
            org = int(r.choice(self.plain))
            t = int(r.integers(4, N_GEN - 2))
            self._spell(self._epk(), org, 0, 0, N_GEN - 1, skip=[t])
        self.expect["n_joiners"] = n_join
        self.expect["stop_people"] = len(stop_people)

    def _career(self, epk, org, gk, start) -> None:
        """Основная работа и что дальше: уход, переход, не зп, ниже порога, возврат."""
        r = self.rng
        t = start
        while t < N_GEN:
            dur = int(r.geometric(0.012))
            end = min(N_GEN - 1, t + dur)
            self._spell(epk, org, gk, t, end)
            if end >= N_GEN - 1:
                return
            u = r.random()
            nxt = end + 1
            if u < 0.40:                                   # ушёл из банка
                return
            if u < 0.62:                                   # перешёл в другую организацию
                org, gk = self._pick_move(org, gk)
                t = nxt
                continue
            if u < 0.75:                                   # не зп коды, потом ушёл
                self._spell(epk, org, gk, nxt, min(N_GEN - 1, nxt + int(r.integers(2, 6))), mode="nonzp")
                return
            if u < 0.85:                                   # ниже порога
                self._spell(epk, org, gk, nxt, min(N_GEN - 1, nxt + int(r.integers(1, 5))), mode="below")
                return
            t = nxt + int(r.integers(2, 15))               # вернётся после разрыва
            if r.random() < 0.5:
                org, gk = self._pick_move(org, gk)

    # ------------------------------------------------------------ строки --
    def _row(self, gi, epk, org_k, gk, code, amt, **kw) -> dict:
        o = self.orgs[org_k]
        tb, g = o["gosbs"][gk]
        row = {"report_dt": M.iso(M.shift(START, gi)), "epk_id": epk, "inn": o["inn"],
               "sys_gosb_id": g, "sys_tb_id": tb, "enrollment_type": code, "amt": round(float(amt), 2),
               "segment_name": o["pay"], "enrollment_kind_descr": "Основные",
               "market_share_flag_name": "Прочие", "company_name": o["name"],
               "enrollment_transcription": CODES.get(code, ("?",))[0], "transaction_qty": 1}
        row.update(kw)
        return row

    def payroll(self) -> pd.DataFrame:
        r = self.rng
        rows = []
        for s in self.spells:
            flap_state = True
            for gi in range(s["start"], s["end"] + 1):
                if gi in s["skip"]:
                    continue
                sal = float(r.uniform(25_000, 140_000))
                mode = s["mode"]
                if mode == "flap":
                    if r.random() < 0.5:
                        flap_state = not flap_state
                    mode = "zp" if flap_state else "nonzp"
                if mode == "zp":
                    if r.random() < 0.5:
                        rows.append(self._row(gi, s["epk"], s["org"], s["gk"], 16, sal * 0.4))
                        rows.append(self._row(gi, s["epk"], s["org"], s["gk"], 1, sal * 0.6))
                    else:                                   # одна строка, иногда 2 транзакции
                        rows.append(self._row(gi, s["epk"], s["org"], s["gk"], 1, sal,
                                              transaction_qty=2 if r.random() < 0.1 else 1))
                    if r.random() < 0.05:
                        rows.append(self._row(gi, s["epk"], s["org"], s["gk"], 1, -500))  # сторно
                    if r.random() < 0.05:
                        rows.append(self._row(gi, s["epk"], s["org"], s["gk"], 7, 3000))
                elif mode == "nonzp":
                    rows.append(self._row(gi, s["epk"], s["org"], s["gk"], int(r.choice([7, 40])), 6000))
                    if r.random() < 0.3:
                        rows.append(self._row(gi, s["epk"], s["org"], s["gk"], 28, 1500))  # премия ниже порога
                elif mode == "below":
                    rows.append(self._row(gi, s["epk"], s["org"], s["gk"], 1, float(r.uniform(500, 2400))))
                elif mode == "small":
                    rows.append(self._row(gi, s["epk"], s["org"], s["gk"], 1, 1500))
        # Шум отбора: отдельные люди в обычных организациях.
        for _ in range(150):
            epk = self._epk()
            org = int(r.choice(self.plain))
            gi = int(r.integers(0, N_GEN))
            kind = int(r.integers(7))
            if kind == 0:
                rows.append(self._row(gi, epk, org, 0, 1, 50_000, enrollment_kind_descr="Дополнительные"))
            elif kind == 1:
                rows.append(self._row(gi, epk, org, 0, 1, 50_000, market_share_flag_name="ИП 1 чел."))
            elif kind == 2:
                rows.append(self._row(gi, epk, org, 0, 1, 50_000, inn=SBER))
            elif kind == 3:
                rows.append(self._row(gi, epk, org, 0, 1, 50_000, inn="Не определено"))
            elif kind == 4:
                rows.append(self._row(gi, epk, org, 0, 1, 50_000, inn="ID123"))
            elif kind == 5:
                rows.append(self._row(gi, None, org, 0, 1, 50_000))
            else:
                rows.append(self._row(gi, epk, org, 0, 2, 1000))           # вид 2 ниже порога
        rows += self.noise
        df = pd.DataFrame(rows)
        df["epk_id"] = df["epk_id"].astype("Int64")
        return df

    def epk(self) -> pd.DataFrame:
        rows = []
        for o in self.orgs:
            if o["role"] == "noepk":
                continue
            tb, g = o["gosbs"][0]
            base = {"inn": int(o["inn"]), "company_name": o["name"], "segment_name": o["big"],
                    "holding_name": o["hold"], "reference_holding_name": o["ref_hold"],
                    "status_name": "Активна", "tb_id": tb, "gosb_id": g}
            rows.append(base)
            if o["k"] % 7 == 0:                         # вторая запись ЕПК того же номера
                rows.append({**base, "status_name": "Ликвидирована", "holding_name": ""})
        return pd.DataFrame(rows)

    def seg_hist(self) -> pd.DataFrame:
        """История сегмента: месяц × организация × ГОСБ (сырые ГОСБ и имена, как в
        ведомостях). Дата — ПЕРВОЕ число месяца: расчёт обязан привести её к концу."""
        rows = []
        for o in self.orgs:
            if o["role"] in ("nohist", "noepk"):
                continue
            for gi in range(N_GEN):
                d = M.shift(START, gi)
                pay = o["pay"]
                if o["role"] == "reclass" and gi >= GI_RECLASS:
                    pay = "Средний бизнес"
                # Последний месяц истории загружен наполовину (по чётным организациям):
                # текущая сегментация обязана взять предпоследний, полный месяц.
                if gi == N_GEN - 1 and o["k"] % 2 == 0:
                    continue
                for tb, g in o["gosbs"]:
                    rows.append({"report_dt": date(d.year, d.month, 1).isoformat(), "inn": o["inn"],
                                 "tb_id": tb, "gosb_id": g, "segment_name": pay})
        return pd.DataFrame(rows)


DDL = """
DROP SCHEMA IF EXISTS {s} CASCADE;
CREATE SCHEMA {s};
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
CREATE TABLE {s}.uzp_data_epk_consolidation (
  epk_id bigint, inn bigint, company_name varchar, segment_name varchar, holding_name varchar,
  reference_holding_name varchar, status_name varchar, tb_id smallint, gosb_id integer,
  industry_name varchar, modified_dttm timestamp);
CREATE TABLE {s}.uzp_data_payroll_segment_month_2021 (
  report_dt date, inn text, tb_id smallint, gosb_id integer, segment_name text);
CREATE TABLE {s}.yva_tik_cik (inn int8, company_name text, is_tik_cik bool);
"""


def _copy(conn, df: pd.DataFrame, table: str) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    with conn.connection.cursor() as cur:
        cur.copy_expert(f"COPY {table} ({', '.join(df.columns)}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf)


def generate(persons: int = 2000) -> dict:
    """Все таблицы синтетики в памяти (без БД) — их же читает эталон."""
    g = Gen(persons)
    g.build_orgs()
    g.build_people()
    t = {
        "payroll": g.payroll(),
        "epk": g.epk(),
        "seg_hist": g.seg_hist(),
        "codes": pd.DataFrame([{"enrollment_type_id": c, "enrollment_type_name": v[0],
                                "is_portfolio_enrollment": v[1], "is_fot_enrollment": v[2]} for c, v in CODES.items()]),
        "edu": pd.DataFrame([{"inn": g.orgs[g.o_edu]["inn"], "company_name": "МБОУ Школа"}]),
        "tik": pd.DataFrame([{"inn": int(g.orgs[g.o_tik]["inn"]), "company_name": "ТИК", "is_tik_cik": True},
                             {"inn": int(g.orgs[g.o_tik_false]["inn"]), "company_name": "ТИК2",
                              "is_tik_cik": False}]),
    }
    roles = {o["role"]: int(o["inn"]) for o in g.orgs if o["role"] != "plain"}
    t["expect"] = {**g.expect, "roles": roles, "report_month": REPORT_MONTH,
                   "gi_stop": GI_STOP, "gi_reclass": GI_RECLASS, "gi_reorg": GI_REORG,
                   "start": M.iso(START)}
    return t


def build(url: str | None = None, persons: int = 2000, schema: str = config.SYNTH_SCHEMA) -> dict:
    print(f"→ синтетика: {persons:,} ФЛ, {N_GEN} мес., схема {schema}", flush=True)
    t = generate(persons)
    eng = create_engine(config.db_url(url), future=True)
    with eng.connect() as conn:
        for stmt in DDL.format(s=schema).split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
        _copy(conn, t["payroll"], f"{schema}.mis_data_payroll_m")
        _copy(conn, t["codes"], f"{schema}.uzp_dim_enrollment_type")
        _copy(conn, t["edu"], f"{schema}.uzp_dim_education_organization")
        _copy(conn, t["epk"], f"{schema}.uzp_data_epk_consolidation")
        _copy(conn, t["seg_hist"], f"{schema}.uzp_data_payroll_segment_month_2021")
        _copy(conn, t["tik"], f"{schema}.yva_tik_cik")
        conn.commit()
    config.ensure_dirs()
    path = config.OUTPUT_DIR / "synth_expect.json"
    path.write_text(json.dumps(t["expect"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ведомости: {len(t['payroll']):,} строк; ожидания → {path}", flush=True)
    return t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None)
    ap.add_argument("--persons", type=int, default=2000)
    ap.add_argument("--schema", default=config.SYNTH_SCHEMA)
    a = ap.parse_args()
    build(a.url, a.persons, a.schema)


if __name__ == "__main__":
    main()
