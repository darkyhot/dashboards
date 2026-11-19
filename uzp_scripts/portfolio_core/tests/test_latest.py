"""Регрессии отчёта на дату, в том числе поиск внутри стабильной пары."""
import os
import unittest
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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


class SavedReview(unittest.TestCase):
    def test_notebook_recovers_the_saved_report_in_a_new_kernel(self):
        card = {
            "pair": "abc123", "gosb": 8369, "group": "Вся пара", "amount": 100000,
            "expected": 200000, "raw": -100000, "boundary": 0, "moved": None,
            "confirmed": -10000, "residual": -90000, "people": 30,
            "state": "Проверено", "lifecycle": "",
        }
        original = latest.card_table({"cards": [card]}).to_html(index=False)
        details = ("<details><summary>Орг. masked · ГОСБ 8369 · Пара abc123 · Вся пара</summary>"
                   "<p>Получатели: 30, ожидание 33. Выплаты в обоих месяцах получили 25.</p>"
                   "<ul><li>Нет портфельных выплат в банке — 3 чел., 90 000 ₽ в прошлом месяце</li></ul></details>")
        notebook = json.loads((Path(__file__).resolve().parents[1] / "portfolio_core.ipynb").read_text())
        source = next("".join(c["source"]) for c in notebook["cells"]
                      if "ПРОСМОТР ГОТОВОГО ОТЧЁТА" in "".join(c["source"]))
        with TemporaryDirectory() as tmp, patch.object(config, "OUTPUT_DIR", Path(tmp)):
            path = Path(tmp) / "latest" / "latest.html"
            path.parent.mkdir()
            path.write_text(original + details, encoding="utf-8")
            displays = []
            with patch.object(db, "get_engine", side_effect=AssertionError("Просмотр обратился к БД")):
                exec(compile(source, "просмотр", "exec"), {"display": displays.append})
            self.assertEqual(len(displays), 4)
            self.assertEqual(displays[0].iloc[0]["Те же получатели, ₽"], "-10 000")
            self.assertEqual(displays[1].iloc[0]["Остаток, ₽"], "-90 000")
            self.assertEqual(displays[3].iloc[0]["Выплаты в прошлом месяце, ₽"], "90 000")
            path.write_text("<h1>Последний месяц</h1><p>Карточек по условиям отбора нет.</p>", encoding="utf-8")
            self.assertEqual(latest.read_saved_review(path), {"cards": []})


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
        checked = report["organization_checks"][outgoing["pair"]]
        self.assertEqual({int(b["gosb"]): b["current_amount"] for b in checked}, {8601: 0, 8602: 1650000})
        with TemporaryDirectory() as tmp:
            path = latest.write_html(report, Path(tmp) / "latest.html")
            recovered = latest.read_saved_review(path)
            self.assertEqual(recovered["organization_checks"], report["organization_checks"])
            tables = dict(latest.notebook_tables(recovered))
            self.assertIn("Выплаты организаций по всем ГОСБ при остановке пары", tables)

        self.assertEqual(outgoing["residual"], 0)
        self.assertIsNone(incoming["expected"])
        self.assertNotIn(str(ORG_A), str(report))
        self.assertNotIn(str(ORG_B), str(report))
        self.assertTrue(any("·" in card["group"] for card in report["cards"]))
        _, decreases = latest.review_tables(report)[0]
        self.assertIn("От 200 тыс. ₽", set(decreases["Группа"]))
        self.assertTrue(decreases["Пара"].is_unique)
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

    def test_shortlist_favors_money_and_ignores_collapsed_seasonal_base(self):
        # Пара 1 раньше получала миллион, старая формула сводила ожидание к 2 тыс.
        # У пары 2 сильнее процент, у пары 3 сильнее просадка в деньгах.
        # У пары 5 снизилась численность, но рост выплат больше, чем у пары 4.
        with self.engine.begin() as c:
            c.execute(text("""
                CREATE TEMP TABLE ptf_latest_groups ON COMMIT DROP AS
                SELECT org_id, 100 AS gosb_id, -1 AS income, -1 AS history, -1 AS jobs,
                       actual, prior, year_actual, year_prior,
                       CASE WHEN org_id = 5 THEN 85 ELSE 100 END AS n_actual,
                       100 AS n_prior, 100 AS n_year_actual, 100 AS n_year_prior,
                       CASE WHEN org_id = 5 THEN 1600000 ELSE 0 END AS stay_actual,
                       CASE WHEN org_id = 5 THEN 1000000 ELSE 0 END AS stay_prior,
                       0 AS stay_year_actual, 0 AS stay_year_prior,
                       CASE WHEN org_id = 5 THEN 85 ELSE 0 END AS stay_n,
                       CASE WHEN org_id = 5 THEN 85 ELSE 0 END AS stay_year_n
                FROM (VALUES (1, 1000000, 1000000, 2000, 1000000),
                             (2, 10000, 100000, 100000, 100000),
                             (3, 750000, 1000000, 1000000, 1000000),
                             (4, 1500000, 1000000, 1000000, 1000000),
                             (5, 1600000, 1600000, 1600000, 1600000))
                       x(org_id, actual, prior, year_actual, year_prior)
            """))
            params = {"min_people": 10, "min_change": 0.15, "min_base_ratio": 0.5, "max_pairs": 1, "max_groups": 1}
            c.execute(text("CREATE TEMP TABLE ptf_latest_scored ON COMMIT DROP AS " + Q.LATEST_SCORED), params)
            scored = db.read_sql(c, "SELECT org_id, rub_change FROM ptf_latest_scored").set_index("org_id")
            chosen = db.read_sql(c, Q.latest_shortlist(), params)
        self.assertTrue(pd.isna(scored.at[1, "rub_change"]))
        self.assertEqual(set(chosen["org_id"]), {3, 5})

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
        self.assertTrue(report["organization_checks"])
        self.assertTrue(all(c["confirmed"] is None for c in report["cards"]))
