"""Регрессии отчёта на дату, в том числе поиск внутри стабильной пары."""
import os
import unittest
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from ptf import config, db, latest, months as M, sql as Q, synth
from test_daily import ORG_A, ORG_B, ORG_T, payments

PG_URL = os.environ.get("PTF_TEST_PG_URL", "")
SCHEMA = "test_latest_month"
MONTH = "2026-08-31"
ORG_C = 7_000_000_004
TEST_SALT = "fixture"


class Period(unittest.TestCase):
    def test_latest_ready_month_and_replay(self):
        journal = pd.DataFrame({"report_dt": M.span("2024-01", "2026-09")})
        newest, _, _ = latest._choose_month(journal, None)
        replay, _, slots = latest._choose_month(journal, "2026-08")
        self.assertEqual(newest, "2026-09-30")
        self.assertEqual(replay, MONTH)
        self.assertEqual(max(slots), MONTH)

    def test_no_silent_fallback_to_older_report(self):
        dates = M.span("2025-01", MONTH)
        dates.remove(M.parse("2025-06"))
        with self.assertRaisesRegex(ValueError, "сопоставимая сводка"):
            latest._choose_month(pd.DataFrame({"report_dt": dates}), None)


@unittest.skipUnless(PG_URL, "PTF_TEST_PG_URL не задан")
class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (config.SCHEMA, config.SCHEMA_T, config.PAYROLL, config.PAYROLL_D)
        config.setup(SCHEMA, SCHEMA, "mis_data_payroll_m", payroll_d="mis_data_payroll_d")
        cls.engine = create_engine(PG_URL)
        d = payments().query("org != @ORG_T and kind == 'Основные'").copy()
        is_a = d["org"] == ORG_A
        top = is_a & (d["epk_id"] < 1020)
        august = d["pay_dt"].map(M.iso) == MONTH
        d.loc[top, "amt"] = np.where(august[top], 150000, 250000)
        d.loc[is_a & ~top & august, "amt"] = 75000
        d["gosb_id"] = 8601
        moved = d[is_a & d["epk_id"].between(1050, 1079)].copy()
        moved["org"] = ORG_C
        moved["epk_id"] += 3000
        moved["amt"] = 55000
        moved.loc[moved["pay_dt"].map(M.iso) >= MONTH, "gosb_id"] = 8602
        d = pd.concat([d, moved], ignore_index=True)
        raw = pd.DataFrame({
            "report_dt": d["pay_dt"], "inn": d["org"].astype(str), "epk_id": d["epk_id"],
            "amt": d["amt"], "sys_tb_id": 52, "sys_gosb_id": d["gosb_id"], "enrollment_type": 1,
            "segment_name": "Средний бизнес", "enrollment_kind_descr": d["kind"],
            "market_share_flag_name": "Прочие", "transaction_qty": 1,
        })
        monthly = raw.assign(report_dt=raw["report_dt"].map(M.iso))
        monthly = monthly[monthly["report_dt"] >= "2024-01-31"]
        portfolio = monthly.groupby(["report_dt", "inn", "sys_gosb_id"], as_index=False).agg(
            n=("epk_id", "nunique"), rub=("amt", "sum"))
        portfolio["org_id"] = portfolio.pop("inn").astype(int)
        portfolio["tb"] = 52
        portfolio["fot"] = portfolio["rub"]
        portfolio["any_r"] = portfolio["n"]
        journal = portfolio.groupby("report_dt", as_index=False).agg(pf_recip=("n", "sum"), fot_amt=("fot", "sum"))
        journal["slice_type"] = "bank"
        journal["run_id"] = 1
        journal["checks_ok"] = True
        journal["def_ver"] = Q.DEF_VER

        with cls.engine.begin() as c:
            for statement in synth.DDL.format(s=SCHEMA).split(";"):
                if statement.strip():
                    c.execute(text(statement))
            c.execute(text(f"CREATE TABLE {SCHEMA}.mis_data_payroll_d (LIKE {SCHEMA}.mis_data_payroll_m)"))
            c.execute(text(f"""CREATE TABLE {SCHEMA}.yva_ptf_month_org (
                report_dt date, org_id bigint, sys_gosb_id integer, sys_tb_id integer,
                pf_recip bigint, pf_amt numeric, fot_amt numeric, any_recip bigint)"""))
            c.execute(text(db.render(Q.CREATE_JOURNAL)))
            c.execute(text(f"INSERT INTO {SCHEMA}.uzp_dim_enrollment_type VALUES (1, 'Зарплата', true, true)"))
            synth._copy(c, raw, f"{SCHEMA}.mis_data_payroll_d")
            synth._copy(c, monthly, f"{SCHEMA}.mis_data_payroll_m")
            portfolio = portfolio.rename(columns={"n": "pf_recip", "rub": "pf_amt", "tb": "sys_tb_id",
                                                   "fot": "fot_amt", "any_r": "any_recip"})
            synth._copy(c, portfolio, f"{SCHEMA}.yva_ptf_month_org")
            synth._copy(c, journal, f"{SCHEMA}.yva_ptf_journal")

    @classmethod
    def tearDownClass(cls):
        config.setup(cls.saved[0], cls.saved[1], cls.saved[2], payroll_d=cls.saved[3])
        cls.engine.dispose()

    def test_latest_from_ready_data_with_no_future_and_two_mechanisms(self):
        with db.session(self.engine) as c:
            report = latest.run(c, today=date(2026, 9, 15), alias_salt=TEST_SALT)
        self.assertEqual(report["month"], MONTH)
        self.assertEqual(report["totals"]["pairs"], 4)

        def whole_pair(org, gosb):
            key = latest.pair_alias(org, gosb, TEST_SALT)
            return next(card for card in report["cards"] if card["group"] == "Вся пара" and card["pair"] == key)

        boundary = whole_pair(ORG_B, 8601)
        self.assertEqual(boundary["raw"], -4_000_000)
        self.assertEqual(boundary["boundary"], -4_000_000)
        self.assertEqual(boundary["residual"], 0)
        top = next(card for card in report["cards"] if card["group"] == "От 200 тыс. ₽")
        self.assertEqual(top["confirmed"], -2_000_000)
        self.assertEqual(top["residual"], 0)
        outgoing, incoming = whole_pair(ORG_C, 8601), whole_pair(ORG_C, 8602)
        self.assertEqual(outgoing["moved"], -1_650_000)
        self.assertEqual(incoming["moved"], 1_650_000)
        self.assertEqual(outgoing["org"], incoming["org"])
        self.assertEqual(outgoing["residual"], 0)
        self.assertIsNone(incoming["expected"])
        self.assertNotIn(str(ORG_A), str(report))
        self.assertNotIn(str(ORG_B), str(report))
        self.assertTrue(any("·" in card["group"] for card in report["cards"]))
        for card in report["cards"]:
            if card["raw"] is not None:
                self.assertAlmostEqual(card["raw"], (card["boundary"] or 0) + (card["moved"] or 0)
                                       + (card["confirmed"] or 0) + card["residual"])
        with db.session(self.engine) as c:
            replay = latest.run(c, "2026-08", today=date(2026, 10, 1), alias_salt=TEST_SALT)
        self.assertEqual(replay["latest"], "2026-09-30")
        self.assertEqual(replay["cards"], report["cards"])

    def test_stale_monthly_totals_keep_the_report_and_flag_only_changed_pairs(self):
        update = text(f"""UPDATE {SCHEMA}.yva_ptf_month_org
            SET pf_recip = pf_recip - 1, pf_amt = pf_amt - 42000, fot_amt = fot_amt - 42000
            WHERE report_dt = :month AND org_id = :org""")
        restore = text(f"""UPDATE {SCHEMA}.yva_ptf_month_org
            SET pf_recip = pf_recip + 1, pf_amt = pf_amt + 42000, fot_amt = fot_amt + 42000
            WHERE report_dt = :month AND org_id = :org""")
        params = {"month": MONTH, "org": ORG_A}
        with self.engine.begin() as c:
            c.execute(update, params)
        try:
            with db.session(self.engine) as c:
                report = latest.run(c, today=date(2026, 9, 15), alias_salt=TEST_SALT)
        finally:
            with self.engine.begin() as c:
                c.execute(restore, params)
        audit = report["monthly_audit"][0]
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["live_n"] - audit["saved_n"], 1)
        self.assertEqual(audit["live_n"], audit["journal_n"])
        self.assertEqual(audit["amount_delta"], 42000)
        self.assertEqual(audit["journal_fot_delta"], -42000)
        self.assertEqual(len(latest.audit_table(report["monthly_audit"])), 1)
        self.assertEqual(report["stale_pairs"], 1)
        a = latest.pair_alias(ORG_A, 8601, TEST_SALT)
        affected = [card for card in report["cards"] if card["pair"] == a]
        self.assertTrue(affected)
        self.assertTrue(all(card["confirmed"] is None and "расходятся" in card["state"] for card in affected))
        b = latest.pair_alias(ORG_B, 8601, TEST_SALT)
        calendar = next(card for card in report["cards"] if card["pair"] == b and card["group"] == "Вся пара")
        self.assertEqual(calendar["boundary"], -4_000_000)

    def test_missing_daily_keeps_monthly_findings(self):
        with db.session(self.engine) as c:
            config.setup(payroll_d="absent_daily_table")
            try:
                report = latest.run(c, "2026-08", today=date(2026, 10, 1))
            finally:
                config.setup(payroll_d="mis_data_payroll_d")
            count = c.execute(text("SELECT count(*) FROM pg_class WHERE relname = 'ptf_latest_tr' "
                                   "AND relnamespace = pg_my_temp_schema()")).scalar_one()
        self.assertEqual(count, 0)
        self.assertIsNotNone(report["calendar_warning"])
        self.assertGreater(len(report["cards"]), 0)
        self.assertTrue(all(c["confirmed"] is None for c in report["cards"]))
