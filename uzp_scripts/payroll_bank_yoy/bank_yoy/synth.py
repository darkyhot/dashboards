"""Синтетика открытого контура: витрины и справочники отчёта в своей схеме `synth_bank_yoy`.

Запуск из папки отчёта:  python -m bank_yoy.synth  [--url ...] [--persons 30000]

Таблицы повторяют прод-схему ТОЧЬ-В-ТОЧЬ (имена и типы колонок), поэтому SQL
отчёта переносится на пром без правок. Своя схема — чтобы не задеть синтетику
соседних отчётов: генератор пересоздаёт только её.

Случайные числа проверяют лишь то, что код не падает. Поэтому в ряд заложены
СОБЫТИЯ — по одному на каждую ветку разбора, — а ожидаемые ответы пишутся в
`output/synth_expect.json`, и `selfcheck.check_against_synth` требует, чтобы
отчёт нашёл каждое:

* приток новичков в июне и июле 2026, три четверти которых уходят из банка в
  августе («растворились пришедшие»);
* сезонные ямы января и августа у ОДНИХ И ТЕХ ЖЕ людей в оба года, с возвратом в
  следующем месяце (повтор год к году);
* уход совместителей РГС со второй работы в августе 2026 (схлопывание по id орг);
* сведение аванса из второго ГОСБ в основной у трети КСБ с апреля 2026 (схлопывание по
  ГОСБ — уровень, а не август);
* переходы ФЛ между сегментами;
* реорганизация: все люди id орг переезжают в организацию-приёмник;
* перенос выплаты: организации не платят в августе 2026 и платят вдвойне в
  сентябре — провал, который не потеря;
* организации с реальным сокращением (люди уходят из Сбера), «5 ушли — 5 пришли»
  и «перетока» (люди уходят в другие организации) — для списка организаций;
* непригодные номера организаций в ведомостях;
* ИП с ФИО в названии (название не должно попасть в отчёт) и холдинг с ФИО;
* выделенный холдинг «МИНОБОРОНЫ»: часть его людей уходит из Сбера в августе 2026;
* пропажа аванса: организации в августе 2026 платят только зарплату;
* шум отбора зачислений — постоянный во все месяцы, на год к году не влияет:
  «Дополнительные», «ИП 1 чел.», организация Сбера (получателями НЕ становятся) и
  вид 2 ниже порога (становится); образовательные организации — без порога;
  ТБ 38 перекодируется в ГОСБ 9038.
Эталон числа получателей-троек по месяцам считается здесь же, pandas-версией
логики заказчика (`expect["recipients"]`), и сверяется с отчётом точно.
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

START = date(2024, 8, 31)       # idx 0
HOLDING_FIO = "ГК Петров Пётр Петрович"
N_MONTHS = 26                   # авг-24 … сен-26: сентябрь нужен для проверки возврата
SEED = 20261005


def idx_of(y: int, m: int) -> int:
    return (y - START.year) * 12 + (m - START.month)


T_AUG25, T_AUG26 = idx_of(2025, 8), idx_of(2026, 8)
T_JUN26, T_JUL26 = idx_of(2026, 6), idx_of(2026, 7)
T_REORG = idx_of(2025, 12)
T_FIVE = idx_of(2026, 2)
T_PERETOK = idx_of(2026, 3)
T_SPLIT_END = idx_of(2026, 4)
AUGS = [i for i in range(N_MONTHS) if M.shift(START, i).month == 8]
JANS = [i for i in range(N_MONTHS) if M.shift(START, i).month == 1]
DECS = [i for i in range(N_MONTHS) if M.shift(START, i).month == 12]

SEG_BIG = {          # большое имя → (доля организаций, множитель размера)
    "Микро": (0.14, 0.4), "Малые": (0.18, 0.7),
    "Средние": (0.14, 1.4), "Крупные": (0.07, 2.5), "Крупнейшие": (0.02, 5.0),
    "Рег. госсектор": (0.27, 1.2),
    "Клиенты машиностроения": (0.05, 2.0),
    "Фин.институты": (0.04, 1.2),
    "SBI": (0.03, 0.8),
    None: (0.06, 0.6),                    # id орг нет в справочнике ЕПК
}
SHORT = {"Микро": "ММБ", "Малые": "ММБ", "Средние": "КСБ", "Крупные": "КСБ",
         "Крупнейшие": "КСБ", "Рег. госсектор": "РГС", "Клиенты машиностроения": "СКМ",
         "Фин.институты": "КФИ", "SBI": "БМО", None: "Не в справочнике"}
TB = [(38, "Северный"), (40, "Южный"), (42, "Западный"), (44, "Восточный"), (52, "Центральный")]
CODE_NAMES = {1: "Заработная плата", 2: "Денежное довольствие", 16: "Аванс по заработной плате",
              26: "Отпускные", 28: "Премия", 7: "Пособие", 3: "Пенсия НПФ"}
PORTFOLIO = {1, 2, 16, 26, 28}           # справочник видов: is_portfolio_enrollment
SBER_INN = "7707083893"
NOISE_PERSONS = 300                      # на каждый вид шума
PAY_SEG = {"Микро": "Микробизнес", "Малые": "Малый бизнес", "Средние": "Средний бизнес",
           "Крупные": "Крупный бизнес", "Крупнейшие": "Крупнейший бизнес",
           "Рег. госсектор": "Государственный сектор", "Клиенты машиностроения": "Клиенты машиностроения",
           "Фин.институты": "CIB", "SBI": "БМО", None: None}

# Базовая текучесть (в месяц) и куда уходит человек.
HAZARD = 0.010
EXIT_MIX = {"left": 0.45, "move_same": 0.25, "move_other": 0.10, "below": 0.10, "other": 0.10}
JOIN_RATE = 0.0082
SECOND_JOB_SHARE = 0.07
SECOND_JOB_HAZARD = 0.004
SECOND_JOB_CUT_AUG26 = 0.45      # доля вторых работ в РГС, кончающихся в авг-26
SEASONAL_RGS, SEASONAL_OTHER = 0.10, 0.02
SEASONAL_P_AUG, SEASONAL_P_JAN = 0.85, 0.5
INFLUX = {T_JUN26: 700, T_JUL26: 500}
INFLUX_LEAVE_AUG26 = 0.75
SPLIT_GOSB_SHARE = 0.12
JUNK_INN_SHARE = 0.004
FOCUS_HOLDING = "МИНОБОРОНЫ"
FOCUS_LEAVE_AUG26 = 0.15
SURNAMES = ["Иванов", "Петров", "Сидоров", "Кузнецов", "Смирнов", "Попов", "Волков", "Зайцев"]
FIRST = ["Иван", "Пётр", "Сергей", "Андрей", "Олег", "Николай"]
PATR = ["Иванович", "Петрович", "Сергеевич", "Андреевич", "Олегович", "Николаевич"]


class Gen:
    def __init__(self, persons: int, seed: int = SEED) -> None:
        self.rng = np.random.default_rng(seed)
        self.n_persons0 = persons
        self.next_epk = 7_000_000_000
        self.next_inn = 7_700_000_000
        self.orgs: list[dict] = []
        self.jobs: list[dict] = []          # epk, org, start, end, amt, second
        self.tails: list[dict] = []         # строки после ухода: ниже порога / другие коды
        self.seasonal: set[int] = set()
        self.liquidated: set[int] = set()
        self.second_by_epk: dict[int, list[dict]] = {}
        self.expect: dict = {}

    # -- справочники ------------------------------------------------------- #
    def _new_org(self, big, tb=None, size=None, split=None) -> dict:
        r = self.rng
        tb = tb if tb is not None else TB[r.integers(len(TB))][0]
        gosb = tb * 100 + int(r.integers(1, 5))
        mult = SEG_BIG[big][1]
        size = size if size is not None else max(3, int(r.lognormal(2.9, 0.9) * mult))
        split = bool(r.random() < SPLIT_GOSB_SHARE) if split is None else split
        gosb2 = tb * 100 + (gosb % 100) % 4 + 1 if split else None
        self.next_inn += int(r.integers(1, 50))
        k = len(self.orgs)
        # Каждая четвёртая микро — ИП: в названии ФИО, показывать нельзя.
        name = (f"ИП {SURNAMES[k % 8]}{k} {FIRST[k % 6]} {PATR[k % 6]}"
                if big == "Микро" and k % 4 == 0 else f"ООО «Синтетика {k:04d}»")
        o = {"id": k, "inn": self.next_inn, "big": big, "seg": SHORT[big],
             "tb": tb, "gosb": gosb, "gosb2": gosb2, "size": size,
             "name": name, "liquidated": False,
             "amt": {"Рег. госсектор": 32000}.get(big, 52000)}
        self.orgs.append(o)
        return o

    def build_orgs(self) -> None:
        bigs = list(SEG_BIG)
        p = np.array([SEG_BIG[b][0] for b in bigs])
        total = 0
        while total < self.n_persons0:
            o = self._new_org(bigs[self.rng.choice(len(bigs), p=p / p.sum())])
            total += o["size"]
        self.org_seg = np.array([o["seg"] for o in self.orgs])
        self.org_w = np.array([o["size"] for o in self.orgs], dtype=float)

    def _pick_org(self, seg=None, not_seg=None, exclude=None) -> int:
        n = len(self.org_w)
        mask = np.ones(n, dtype=bool)
        segs = self.org_seg[:n]
        if seg is not None:
            mask &= segs == seg
        if not_seg is not None:
            mask &= segs != not_seg
        if exclude is not None and exclude < n:
            mask[exclude] = False
        for oid in self.liquidated:
            if oid < n:
                mask[oid] = False
        w = np.where(mask, self.org_w[:n], 0.0)
        return int(self.rng.choice(n, p=w / w.sum()))

    # -- люди -------------------------------------------------------------- #
    def _new_epk(self) -> int:
        self.next_epk += int(self.rng.integers(1, 9))
        return self.next_epk

    def _job(self, epk, org, start, second=False) -> dict:
        o = self.orgs[org]
        j = {"epk": epk, "org": org, "start": start, "end": N_MONTHS,
             "amt": float(o["amt"] * self.rng.lognormal(0, 0.35)), "second": second,
             "influx": False, "planted": None}
        self.jobs.append(j)
        if second:
            self.second_by_epk.setdefault(epk, []).append(j)
        return j

    def build_people(self) -> None:
        r = self.rng
        for o in list(self.orgs):
            for _ in range(o["size"]):
                epk = self._new_epk()
                self._job(epk, o["id"], 0)
                p_seas = SEASONAL_RGS if o["seg"] == "РГС" else SEASONAL_OTHER
                if r.random() < p_seas:
                    self.seasonal.add(epk)
                if r.random() < SECOND_JOB_SHARE:
                    seg2 = "РГС" if r.random() < 0.5 else None
                    org2 = self._pick_org(seg=seg2, exclude=o["id"])
                    self._job(epk, org2, 0, second=True)

    def _active(self, t: int, org: int | None = None, second=False) -> list[dict]:
        return [j for j in self.jobs
                if j["start"] <= t < j["end"] and j["second"] == second
                and (org is None or j["org"] == org)]

    def _exit(self, j: dict, t: int, kind: str) -> None:
        """Человек уходит с основной работы в месяце t (с t его здесь нет)."""
        j["end"] = t
        o = self.orgs[j["org"]]
        if kind == "move_same":
            self._job(j["epk"], self._pick_org(seg=o["seg"], exclude=o["id"]), t)
        elif kind == "move_other":
            self._job(j["epk"], self._pick_org(not_seg=o["seg"]), t)
        elif kind == "below":
            self.tails.append({"epk": j["epk"], "org": j["org"], "start": t,
                               "end": min(N_MONTHS, t + 8), "code": 1, "amt": 1500.0})
        elif kind == "other":
            self.tails.append({"epk": j["epk"], "org": j["org"], "start": t,
                               "end": min(N_MONTHS, t + 4), "code": 7, "amt": 9000.0})
        if kind in ("left", "below", "other"):
            # Ушёл из банка — уходят и вторые работы.
            for s in self.second_by_epk.get(j["epk"], []):
                if s["start"] <= t < s["end"]:
                    s["end"] = t

    # -- заложенные события ------------------------------------------------ #
    def _orgs_for(self, n, segs, min_size, taken) -> list[int]:
        cand = [o["id"] for o in self.orgs
                if o["seg"] in segs and o["size"] >= min_size and o["id"] not in taken
                and not o["liquidated"]]
        pick = list(self.rng.choice(cand, size=min(n, len(cand)), replace=False))
        taken.update(pick)
        return [int(x) for x in pick]

    def plan_events(self) -> None:
        taken: set[int] = set()
        self.ev_real = {T_JUL26: self._orgs_for(6, {"КСБ", "РГС", "ММБ"}, 40, taken),
                        T_AUG26: self._orgs_for(6, {"КСБ", "РГС", "ММБ"}, 40, taken)}
        # Одна сокращающаяся организация — ИП, у другой холдинг с ФИО: в списке обязаны
        # стоять id орг и «название скрыто», а не ФИО.
        self.orgs[self.ev_real[T_AUG26][0]]["name"] = "ИП Зайцев Олег Николаевич"
        self.fio_holding_orgs = {self.ev_real[T_AUG26][1]}
        self.ev_five = self._orgs_for(10, {"КСБ", "РГС", "ММБ", "СКМ"}, 30, taken)
        self.ev_peretok = self._orgs_for(8, {"КСБ", "РГС", "ММБ"}, 30, taken)
        self.ev_reorg = self._orgs_for(4, {"КСБ", "РГС"}, 20, taken)
        # Перенос выплаты: в августе 2026 организация не платит никому, в сентябре —
        # двойная сумма. Люди не уходят — отчёт обязан это различить.
        self.ev_shift = set(self._orgs_for(15, {"КСБ", "ММБ", "РГС", "СКМ"}, 30, taken))
        # Выделенный холдинг — организации РГС.
        self.ev_focus = set(self._orgs_for(20, {"РГС"}, 20, taken))
        # Пропажа аванса: только организации без второго ГОСБ (иначе пропадёт и тройка
        # второго ГОСБ, а это другое событие — сведение ГОСБ).
        cand = [o["id"] for o in self.orgs if o["seg"] in ("КСБ", "ММБ") and o["size"] >= 20
                and o["gosb2"] is None and o["id"] not in taken]
        self.ev_adv = set(int(x) for x in self.rng.choice(cand, size=min(20, len(cand)), replace=False))
        taken.update(self.ev_adv)
        self.protected = taken
        # Образовательные (без порога) — каждая десятая РГС вне событий.
        self.edu = {o["id"] for o in self.orgs if o["seg"] == "РГС" and o["id"] % 10 == 0
                    and o["id"] not in taken}

    def simulate(self) -> None:
        r = self.rng
        kinds, probs = list(EXIT_MIX), np.array(list(EXIT_MIX.values()))
        reorg_pairs, influx_epk = [], {}
        five_counts = {}
        for t in range(1, N_MONTHS):
            active = self._active(t)
            n_active = len(active)
            # Базовая текучесть. Организации с заложенными событиями её получают
            # тоже: события ложатся поверх обычной жизни, как на проме.
            for j in active:
                if r.random() < HAZARD:
                    self._exit(j, t, kinds[r.choice(len(kinds), p=probs)])
            for j in self._active(t, second=True):
                if r.random() < SECOND_JOB_HAZARD:
                    j["end"] = t
            # Новые люди в банке.
            for _ in range(r.poisson(n_active * JOIN_RATE)):
                self._job(self._new_epk(), self._pick_org(), t)
            # Реорганизация: все люди переезжают в новый id орг того же сегмента.
            if t == T_REORG:
                for oid in self.ev_reorg:
                    src = self.orgs[oid]
                    dst = self._new_org(src["big"], tb=src["tb"], size=0,
                                        split=src["gosb2"] is not None)
                    dst["gosb"], dst["gosb2"] = src["gosb"], src["gosb2"]
                    self.org_seg = np.append(self.org_seg, dst["seg"])
                    self.org_w = np.append(self.org_w, float(src["size"]))
                    for j in self._active(t, org=oid):
                        j["end"] = t
                        nj = self._job(j["epk"], dst["id"], t)
                        nj["amt"] = j["amt"]
                    src["liquidated"] = True
                    self.liquidated.add(oid)
                    reorg_pairs.append((src["inn"], dst["inn"]))
            # «5 ушли — 5 пришли»: уход из банка и ровно столько же новых.
            if t == T_FIVE:
                for oid in self.ev_five:
                    act = self._active(t, org=oid)
                    k = max(3, int(0.25 * len(act)))
                    for j in r.choice(act, size=k, replace=False):
                        self._exit(j, t, "left")
                    for _ in range(k):
                        self._job(self._new_epk(), oid, t)
                    five_counts[self.orgs[oid]["inn"]] = k
            # Переток: треть людей уходит в РАЗНЫЕ другие организации.
            if t == T_PERETOK:
                for oid in self.ev_peretok:
                    act = self._active(t, org=oid)
                    for j in r.choice(act, size=max(5, int(0.35 * len(act))), replace=False):
                        self._exit(j, t, "move_same" if r.random() < 0.6 else "move_other")
            # Реальное сокращение: 40% людей уходят из Сбера.
            if t in self.ev_real:
                for oid in self.ev_real[t]:
                    act = self._active(t, org=oid)
                    for j in r.choice(act, size=max(10, int(0.40 * len(act))), replace=False):
                        self._exit(j, t, "left")
            # Приток новичков в июне и июле 2026 (КСБ и ММБ).
            if t in INFLUX:
                ids = []
                for _ in range(INFLUX[t]):
                    seg = "КСБ" if r.random() < 0.5 else "ММБ"
                    j = self._job(self._new_epk(), self._pick_org(seg=seg), t)
                    j["influx"] = True
                    ids.append(j["epk"])
                influx_epk[t] = ids
            # Август 2026: три четверти новичков уходят из банка.
            if t == T_AUG26:
                n_gone = 0
                for j in self.jobs:
                    if j["influx"] and j["start"] <= t < j["end"] and r.random() < INFLUX_LEAVE_AUG26:
                        self._exit(j, t, "left")
                        n_gone += 1
                self.expect["influx_gone_aug26"] = n_gone
                # Совместители РГС уходят со второй работы (основная остаётся).
                cut = 0
                for j in self._active(t, second=True):
                    if self.orgs[j["org"]]["seg"] == "РГС" and r.random() < SECOND_JOB_CUT_AUG26:
                        j["end"] = t
                        cut += 1
                self.expect["second_job_cut_aug26"] = cut
                # Выделенный холдинг: часть людей уходит из Сбера.
                fl = 0
                for oid in self.ev_focus:
                    for j in self._active(t, org=oid):
                        if r.random() < FOCUS_LEAVE_AUG26:
                            self._exit(j, t, "left")
                            fl += 1
                self.expect["focus_left_aug26"] = fl

        self.expect.update({
            "influx": {M.iso(M.shift(START, t)): len(v) for t, v in influx_epk.items()},
            "reorg_pairs": [[int(a), int(b)] for a, b in reorg_pairs],
            "five_five_inns": [int(k) for k in five_counts],
            "peretok_inns": [int(self.orgs[o]["inn"]) for o in self.ev_peretok],
            "real_cut_inns": {M.iso(M.shift(START, t)): [int(self.orgs[o]["inn"]) for o in v]
                              for t, v in self.ev_real.items()},
            "seasonal_persons": len(self.seasonal),
            "split_gosb_end": M.iso(M.shift(START, T_SPLIT_END)),
            "pay_shift_inns": [int(self.orgs[o]["inn"]) for o in sorted(self.ev_shift)],
            "focus_holding": FOCUS_HOLDING,
            "focus_inns": [int(self.orgs[o]["inn"]) for o in sorted(self.ev_focus)],
            "adv_drop_pairs": sum(1 for j in self.jobs if j["org"] in self.ev_adv and not j["second"]
                                  and j["start"] <= T_JUL26 < T_AUG26 < j["end"]),
            "fio_names": [o["name"] for o in self.orgs if o["name"].startswith("ИП ")]
                         + [HOLDING_FIO],
        })

    # -- строки витрин ----------------------------------------------------- #
    def payroll(self) -> pd.DataFrame:
        r = self.rng
        org_inn = {o["id"]: str(o["inn"]) for o in self.orgs}
        rows = []
        season_off = {}
        for epk in self.seasonal:
            for t in AUGS:
                if r.random() < SEASONAL_P_AUG:
                    season_off[(epk, t)] = True
            for t in JANS:
                if r.random() < SEASONAL_P_JAN:
                    season_off[(epk, t)] = True
        for j in self.jobs:
            o = self.orgs[j["org"]]
            for t in range(j["start"], j["end"]):
                if (j["epk"], t) in season_off:
                    continue
                if j["org"] in self.ev_shift and t == T_AUG26:
                    continue                              # август не выплачен
                no_adv = j["org"] in self.ev_adv and t == T_AUG26     # аванса нет
                amt = j["amt"] * float(r.lognormal(0, 0.05))
                if j["org"] in self.ev_shift and t == T_AUG26 + 1 and j["start"] < T_AUG26:
                    amt *= 2                              # в сентябре — за два месяца
                merged = o["seg"] == "КСБ" and o["id"] % 3 == 0 and t >= T_SPLIT_END
                g2 = o["gosb2"] if (o["gosb2"] and not merged) else o["gosb"]
                rows.append((t, j["epk"], j["org"], o["gosb"], 1, 0.6 * amt))
                if not no_adv:
                    rows.append((t, j["epk"], j["org"], g2, 16, 0.4 * amt))
                if t in AUGS and r.random() < 0.4:
                    rows.append((t, j["epk"], j["org"], o["gosb"], 26, 0.5 * amt))
                if t in DECS and r.random() < 0.3:
                    rows.append((t, j["epk"], j["org"], o["gosb"], 28, 0.7 * amt))
        for tl in self.tails:
            o = self.orgs[tl["org"]]
            for t in range(tl["start"], tl["end"]):
                rows.append((t, tl["epk"], tl["org"], o["gosb"], tl["code"], tl["amt"]))
        df = pd.DataFrame(rows, columns=["t", "epk_id", "org", "sys_gosb_id",
                                         "enrollment_type", "amt"])
        df["report_dt"] = [M.shift(START, int(t)) for t in df["t"]]
        df["inn"] = df["org"].map(org_inn)
        junk = r.random(len(df)) < JUNK_INN_SHARE
        df.loc[junk, "inn"] = "ID" + df.loc[junk, "inn"].str[-6:]
        df["sys_tb_id"] = (df["sys_gosb_id"] // 100).astype("int16")
        df["gosb_id"] = df["sys_gosb_id"] + 900000          # «старый» номер — не сходится
        df["tb_id"] = None
        df["enrollment_transcription"] = df["enrollment_type"].map(CODE_NAMES)
        df["company_name"] = df["org"].map({o["id"]: o["name"] for o in self.orgs})
        df["agrmnt_num"] = "Д-" + df["org"].astype(str)
        df["transaction_qty"] = 1
        df["amt"] = df["amt"].round(2)
        df["segment_name"] = df["org"].map({o["id"]: PAY_SEG[o["big"]] for o in self.orgs})
        df["enrollment_kind_descr"] = "Основные"
        df["market_share_flag_name"] = "Обычный"
        df = pd.concat([df.drop(columns=["t", "org", "gosb_id", "tb_id"]), self._noise()],
                       ignore_index=True)
        self.expect["recipients"] = self._recipients(df)
        return df

    def _noise(self) -> pd.DataFrame:
        """Шум отбора: одни и те же ФЛ во все месяцы (год к году не меняется)."""
        pool = [o for o in self.orgs if o["id"] not in self.protected and o["id"] not in self.edu]
        kinds = [("dop", 50000.0), ("ip1", 50000.0), ("sber", 50000.0), ("t2", 900.0)]
        rows = []
        for kind, amt in kinds:
            for i in range(NOISE_PERSONS):
                o = pool[(i * 7 + len(rows)) % len(pool)]
                epk = 8_000_000_000 + len(rows)
                for t in range(N_MONTHS):
                    rows.append({"report_dt": M.shift(START, t), "epk_id": epk,
                                 "inn": SBER_INN if kind == "sber" else str(o["inn"]),
                                 "sys_gosb_id": o["gosb"], "sys_tb_id": o["gosb"] // 100,
                                 "enrollment_type": 2 if kind == "t2" else 1, "amt": amt,
                                 "enrollment_transcription": CODE_NAMES[2 if kind == "t2" else 1],
                                 "company_name": o["name"], "agrmnt_num": f"Д-{o['id']}",
                                 "transaction_qty": 1, "segment_name": PAY_SEG[o["big"]],
                                 "enrollment_kind_descr": "Дополнительные" if kind == "dop" else "Основные",
                                 "market_share_flag_name": "ИП 1 чел." if kind == "ip1" else "Обычный"})
        return pd.DataFrame(rows)

    def exc_inns(self) -> set[int]:
        return {int(self.orgs[o]["inn"]) for o in self.edu | self.ev_focus}

    def _recipients(self, df: pd.DataFrame) -> dict:
        """Эталон: получатели-тройки по месяцам — логика заказчика на pandas."""
        x = df.copy()
        x["gosb"] = np.where(x["sys_tb_id"] == 38, 9038, x["sys_gosb_id"])
        relev = ((x["enrollment_kind_descr"] == "Основные") & (x["market_share_flag_name"] != "ИП 1 чел.")
                 & ~x["inn"].isin([SBER_INN, "Не определено", "0", "-1"]))
        x["pf"] = relev & x["enrollment_type"].isin(PORTFOLIO)
        x["t2"] = relev & (x["enrollment_type"] == 2)
        x = x[x["inn"].str.fullmatch(r"[0-9]{1,12}") & (x["pf"] | x["t2"])].copy()
        x["inn_n"] = x["inn"].astype("int64")
        x["amt_pf"] = np.where(x["pf"], x["amt"], 0.0)
        x["pos_pf"] = x["pf"] & (x["amt"] > 0)
        x["t2"] = x["t2"] & (x["amt"] > 0)
        g = x.groupby(["report_dt", "epk_id", "inn_n", "gosb"]).agg(
            amt_pf=("amt_pf", "sum"), pos_pf=("pos_pf", "any"), t2=("t2", "any")).reset_index()
        g["exc"] = g["inn_n"].isin(self.exc_inns())
        ok = g["t2"] | (g["pos_pf"] & (g["exc"] | (g["amt_pf"] > 2500)))
        return {M.iso(k): int(v) for k, v in g[ok].groupby("report_dt").size().items()}

    def epk(self) -> pd.DataFrame:
        rows = []
        for o in self.orgs:
            if o["big"] is None:
                continue
            rows.append({"epk_id": 9_000_000 + o["id"], "inn": o["inn"],
                         "segment_name": o["big"], "company_name": o["name"],
                         "holding_name": self._holding(o),
                         "reference_holding_name": FOCUS_HOLDING if o["id"] in self.ev_focus else None,
                         "industry_name": "Образование" if o["seg"] == "РГС" else "Торговля",
                         "status_name": "Ликвидирована" if o["liquidated"] else "Активна",
                         "tb_id": o["tb"]})
        # Одна организация с двумя ЕПК — живой и ликвидированной: жива.
        if rows:
            dup = dict(rows[0])
            dup["epk_id"] += 500_000
            dup["status_name"] = "Ликвидирована"
            rows.append(dup)
        return pd.DataFrame(rows)

    def _holding(self, o: dict):
        if o["id"] in self.ev_focus:
            return FOCUS_HOLDING
        if o["id"] in self.fio_holding_orgs:
            return HOLDING_FIO
        if o["id"] % 3:
            return None
        k = o["id"] % 40
        return HOLDING_FIO if k == 13 else f"Холдинг синтетики {k:02d}"

    def gosb(self) -> pd.DataFrame:
        rows = []
        for tb, name in TB:
            for k in range(1, 5):
                g = tb * 100 + k
                rows.append({"tb_id": tb, "tb_short_name": name, "tb_full_name": f"{name} банк",
                             "old_gosb_id": g + 9000, "new_gosb_id": g,
                             "old_gosb_name": f"{name} ГОСБ {k}", "new_gosb_name": f"{name} ГОСБ {k}"})
        return pd.DataFrame(rows)


# Прод-схема витрин и справочников (имена и типы колонок — как на проме).
DDL = """
DROP SCHEMA IF EXISTS {s} CASCADE;
CREATE SCHEMA {s};
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
"""


def _copy(conn, df: pd.DataFrame, table: str) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    cols = ", ".join(df.columns)
    with conn.connection.cursor() as cur:
        cur.copy_expert(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf)


def build(url: str | None = None, persons: int = 30000, schema: str = config.SYNTH_SCHEMA) -> dict:
    print(f"→ синтетика: {persons:,} ФЛ, {N_MONTHS} мес., схема {schema}", flush=True)
    g = Gen(persons)
    g.build_orgs()
    g.build_people()
    g.plan_events()
    g.simulate()
    pay, epk, gosb = g.payroll(), g.epk(), g.gosb()
    et = pd.DataFrame([{"enrollment_type_id": c, "enrollment_type_name": n,
                        "is_fot_enrollment": c in PORTFOLIO, "is_portfolio_enrollment": c in PORTFOLIO}
                       for c, n in CODE_NAMES.items()])
    edu = pd.DataFrame([{"inn": str(g.orgs[o]["inn"]), "company_name": g.orgs[o]["name"]}
                        for o in sorted(g.edu)])
    print(f"  ведомости: {len(pay):,} строк; организаций {len(g.orgs):,}", flush=True)
    eng = create_engine(config.db_url(url), future=True)
    with eng.connect() as conn:
        for stmt in DDL.format(s=schema).split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
        _copy(conn, gosb, f"{schema}.uzp_dim_gosb")
        _copy(conn, epk, f"{schema}.uzp_data_epk_consolidation")
        _copy(conn, pay, f"{schema}.mis_data_payroll_m")
        _copy(conn, et, f"{schema}.uzp_dim_enrollment_type")
        _copy(conn, edu, f"{schema}.uzp_dim_education_organization")
        conn.execute(text(f"ANALYZE {schema}.mis_data_payroll_m"))
        conn.commit()
    config.ensure_dirs()
    path = config.OUTPUT_DIR / "synth_expect.json"
    path.write_text(json.dumps(g.expect, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ готово; ожидания → {path}", flush=True)
    return g.expect


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None)
    ap.add_argument("--persons", type=int, default=30000)
    ap.add_argument("--schema", default=config.SYNTH_SCHEMA)
    a = ap.parse_args()
    build(a.url, a.persons, a.schema)


if __name__ == "__main__":
    main()
