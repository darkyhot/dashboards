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
from sqlalchemy.exc import DBAPIError, OperationalError

from ptf import config, db, latest, latest_html, months as M, sql as Q, synth
from test_daily import ORG_A, ORG_B, ORG_T, payments

PG_URL = os.environ.get("PTF_TEST_PG_URL", "")
SCHEMA = "test_latest_month"
MONTH = "2026-08-31"
ORG_C = 7_000_000_004
ORG_D = 7_000_000_005
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
            self.assertEqual(displays[1].iloc[0]["Пока без объяснения, ₽"], "-90 000")
            self.assertEqual(displays[3].iloc[0]["Выплаты в прошлом месяце, ₽"], "90 000")
            self.assertEqual(displays[3].iloc[0]["Доля выплат выбывшим"], "100.0%")
            path.write_text("<h1>Последний месяц</h1><p>Карточек по условиям отбора нет.</p>", encoding="utf-8")
            self.assertEqual(latest.read_saved_review(path), {"cards": []})


class ReportCoverage(unittest.TestCase):
    def setUp(self):
        self.cards = [
            {"pair": f"pair-{i}", "gosb": 8601, "group": "Вся пара", "state": "Нужна проверка",
             "confirmed": -1_000_000 if i == 0 else None, "residual": -(100_000 - i)}
            for i in range(8)
        ]
        self.report = {
            "cards": self.cards,
            "organization_ids": {c["pair"]: str(7_000_000_000 + i) for i, c in enumerate(self.cards)},
            "totals": {"signals": 1000},
        }

    def test_confirmed_table_keeps_two_groups_of_the_same_pair(self):
        self.cards.append({**self.cards[0], "group": "Один работодатель", "confirmed": -500_000})
        findings = latest_html._findings(self.report)
        self.assertIn("найдено 1 000 сигналов", findings)
        self.assertIn("9 карточек по 8 парам", findings)
        self.assertEqual(findings.count("<tr data-impact="), 2)
        self.assertIn('data-impact="500000"', findings)

    def test_open_and_stopped_tables_keep_more_than_six_rows(self):
        self.report["organization_checks"] = {
            c["pair"]: [{"stopped_gosb": 8601, "previous_amount": 100_000 - i, "current_amount": 0}]
            for i, c in enumerate(self.cards)
        }
        explanations = latest_html._explanations(self.report)
        self.assertIn("Организации с остановкой выплат в ГОСБ (8)", explanations)
        self.assertIn("Причину снижения ещё предстоит проверить (8)", explanations)
        self.assertEqual(explanations.count("<td>7000000007</td>"), 2)


class BonusTiming(unittest.TestCase):
    def test_same_bonus_in_a_different_month_is_not_a_pay_change(self):
        amounts = (1_000_000, 800_000, 800_000, 1_000_000)
        bonuses = (200_000, 0, 0, 200_000)
        row = dict(zip(latest.CURRENCIES + latest.STAYS + latest.BONUSES, amounts + amounts + bonuses))
        row.update({name: 10 for name in latest.COUNTS})
        row.update(tb_id=52, rub_change=0.5, stay_change=0, stay_n=10, stay_year_n=10)
        windows = pd.DataFrame([(10, amt, amt, 0, bonus) for amt, bonus in zip(amounts, bonuses) for _ in range(2)],
                               columns=["n", "amount", "stay", "moved", "bonus"])
        result = latest._calendar_values(row, windows, set(), 0.15, True, 0.5)
        self.assertEqual((result["raw"], result["bonus"], result["confirmed"], result["residual"]),
                         (400_000, 400_000, None, 0))


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
        moved["amt"] = 55000
        moved.loc[moved["pay_dt"].map(M.iso) >= MONTH, "gosb_id"] = 8602
        gone = d[is_a & d["epk_id"].between(1080, 1082) & ~august].copy()
        gone["org"] = ORG_D
        gone["epk_id"] += 4000
        gone["amt"] = 45000
        d = pd.concat([d, moved, gone], ignore_index=True)
        raw = pd.DataFrame({
            "report_dt": d["pay_dt"], "inn": d["org"].map(lambda org: f"{org:012d}" if org == ORG_C else str(org)),
            "epk_id": d["epk_id"],
            "amt": d["amt"], "sys_tb_id": 52, "sys_gosb_id": d["gosb_id"], "enrollment_type": 1,
            "segment_name": "Средний бизнес", "enrollment_kind_descr": d["kind"],
            "market_share_flag_name": "Прочие", "transaction_qty": 1,
        })
        bonus = raw[(raw["inn"] == str(ORG_A)) & raw["epk_id"].lt(1020)
                    & raw["report_dt"].map(M.iso).isin(["2025-07-31", MONTH])]
        raw = pd.concat([raw, bonus.assign(amt=100_000, enrollment_type=18)], ignore_index=True)
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
            c.execute(text(f"INSERT INTO {SCHEMA}.uzp_dim_enrollment_type VALUES "
                           "(1, 'Зарплата', true, true), (18, 'Премия', true, true)"))
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

    def test_daily_halves_apply_portfolio_threshold_after_joining_windows(self):
        with self.engine.connect() as c, c.begin():
            c.execute(text("CREATE TEMP TABLE t_exc (inn_exc bigint) ON COMMIT DROP"))
            c.execute(text("""CREATE TEMP TABLE ptf_latest_pairs ON COMMIT DROP AS
                            SELECT 10 AS org_id, 8601 AS gosb_id"""))
            c.execute(text("""
                CREATE TEMP TABLE ptf_latest_day_halves ON COMMIT DROP AS
                SELECT *, 10 AS org_id, 8601 AS gosb_id, false AS pos_t2, 0 AS a_t2, 0 AS a_bonus
                FROM (VALUES (0, 1, 1500, true, 1500), (1, 1, 1500, true, 1500),
                             (0, 2, 3500, true, 3500), (1, 2, -1500, false, 0),
                             (0, 3, 1500, true, 1500), (3, 3, 1500, true, 1500))
                    h(mi, epk_id, sum_pf, pos_pf, a_pf)
            """))
            result = c.execute(text(Q.latest_daily_triples((0,))), {"amt_min": 2500})
            actual = {(r.mi, r.epk_id): r.pf_amt for r in result}
        self.assertEqual(actual, {(0, 1): 3000, (1, 2): 3500, (1, 3): 3000})

    def test_anomaly_mart_preserves_scope_and_replaces_the_month(self):
        def run():
            with db.session(self.engine) as c:
                return latest.write_anomaly_month(c, today=date(2026, 9, 15))

        result = run()
        self.assertEqual(result["month"], MONTH)
        self.assertGreater(result["confirmed"], 0)
        self.assertEqual(len(result["checked_examples"]), 7)
        self.assertTrue(all(case["matches"] for case in result["checked_examples"]))
        self.assertTrue(all("org_id" not in case and "org_number" not in case for case in result["checked_examples"]))
        with db.session(self.engine) as c:
            stored = db.read_sql(c, f"SELECT * FROM {SCHEMA}.yva_ptf_anomaly_month")
        self.assertNotIn("epk_id", stored.columns)
        self.assertTrue(stored.loc[stored["detected_scope"] == "pair", "detected_group"].isna().all())
        self.assertTrue(stored.loc[stored["detected_scope"] == "group", "detected_group"].notna().all())
        top = stored[(stored["org_id"] == ORG_A) & (stored["metric_code"] == "continuing_pay")
                     & (stored["group_key"] == "5:-1:-1")].iloc[0]
        self.assertEqual(top["confirmed_impact_rub"], -2_000_000)
        self.assertTrue(top["has_deviation"])
        self.assertEqual(top["detected_scope"], "group")
        self.assertFalse(top["pair_has_deviation"])
        self.assertTrue(pd.isna(top["contributor_group"]))
        self.assertIn("Подтверждено", top["case_text"])
        pair = stored[(stored["org_id"] == ORG_A) & (stored["metric_code"] == "fot")
                      & (stored["group_key"] == "all")].iloc[0]
        self.assertTrue(pair["pair_has_deviation"])
        self.assertEqual(pair["detected_scope"], "pair")
        self.assertEqual(pair["contributor_group"], "Один работодатель")
        self.assertGreater(pair["contributor_people"], 0)
        moved = stored[(stored["org_id"] == ORG_C) & (stored["gosb_id"] == 8601)
                       & (stored["metric_code"] == "portfolio_credits") & (stored["group_key"] == "all")].iloc[0]
        self.assertEqual(moved["org_number"], f"{ORG_C:012d}")
        self.assertEqual(moved["check_status"], "explained")
        self.assertFalse(moved["has_deviation"])
        self.assertIn("Вклад переходов между ГОСБ", moved["case_text"])
        self.assertEqual(set(stored["metric_code"]), {"fot", "portfolio_credits", "recipients", "continuing_pay", "turnover"})
        self.assertEqual(run(), result)
        before = len(stored)
        insert_sql = Q.insert_anomaly_month()
        read_sql = db.read_sql

        def read_with_staged_examples_only(c, sql, params=None):
            if sql == Q.CHECK_ANOMALY_EXAMPLES:
                c.execute(text("DROP TABLE ptf_latest_groups"))
            return read_sql(c, sql, params)

        with patch.object(Q, "insert_anomaly_month", return_value=insert_sql + "; DROP TABLE ptf_latest_events"), \
                patch.object(db, "read_sql", side_effect=read_with_staged_examples_only):
            self.assertEqual(run(), result)
        with patch.object(Q, "insert_anomaly_month", return_value="SELECT 1 / 0"):
            with self.assertRaises(DBAPIError):
                run()
        alter_example = f""";
            UPDATE {Q.ANOMALY_MART}
            SET actual_value = actual_value + 100, expected_value = expected_value + 100
            WHERE report_dt = CAST(:report_dt AS date) AND has_deviation
        """
        with patch.object(Q, "insert_anomaly_month", return_value=insert_sql + alter_example):
            with self.assertRaisesRegex(RuntimeError, "Контроль по месячным агрегатам не прошёл"):
                run()
        duplicate_insert = insert_sql + f""";
            INSERT INTO {Q.ANOMALY_MART}
            SELECT * FROM {Q.ANOMALY_MART} WHERE report_dt = CAST(:report_dt AS date) LIMIT 1
        """
        with patch.object(Q, "insert_anomaly_month", return_value=duplicate_insert):
            with self.assertRaisesRegex(RuntimeError, "ключей с дублями"):
                run()
        with db.session(self.engine) as c:
            restored = db.read_sql(c, f"SELECT * FROM {SCHEMA}.yva_ptf_anomaly_month")
        self.assertEqual(len(restored), before)
        key = ["report_dt", "org_id", "gosb_id", "metric_code", "group_key"]
        for field in ("actual_value", "expected_value"):
            pd.testing.assert_series_equal(stored.set_index(key)[field].sort_index(),
                                           restored.set_index(key)[field].sort_index())

    def test_anomaly_mart_creation_failure_stops_before_reading_payroll(self):
        with patch.object(Q, "CREATE_ANOMALY_MART", "SELECT 1 / 0"), patch.object(latest, "_monthly") as monthly:
            with db.session(self.engine) as c, self.assertRaises(DBAPIError):
                latest.write_anomaly_month(c, today=date(2026, 9, 15))
            monthly.assert_not_called()

    def test_latest_from_ready_data_with_no_future_and_two_mechanisms(self):
        with db.session(self.engine) as c:
            report = latest.run(c, today=date(2026, 9, 15), alias_salt=TEST_SALT)
        self.assertEqual(report["month"], MONTH)
        self.assertEqual(report["totals"]["pairs"], 5)
        changes = report["portfolio"]["changes"]
        self.assertEqual((changes["net"], changes["left"], changes["entered"], changes["early"]), (-53, 53, 0, 50))
        self.assertEqual(changes["early_date"], "2026-07-31")
        route = next(r for r in report["routes"] if r["source_gosb"] == 8601 and r["target_gosb"] == 8602)
        self.assertEqual((route["people"], route["amount"]), (30, 1650000))

        def whole_pair(org, gosb):
            key = latest.pair_alias(org, gosb, TEST_SALT)
            return next(card for card in report["cards"] if card["group"] == "Вся пара" and card["pair"] == key)

        boundary = whole_pair(ORG_B, 8601)
        self.assertEqual(boundary["raw"], -4_000_000)
        self.assertEqual(boundary["boundary"], -4_000_000)
        self.assertEqual(boundary["residual"], 0)
        top = next(card for card in report["cards"] if card["group"] == "От 200 тыс. ₽")
        self.assertEqual(top["confirmed"], -2_000_000)
        self.assertEqual(top["bonus"], 4_000_000)
        self.assertEqual(top["bonus_months"], [2_000_000, 0, 0, 2_000_000])
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
            body = path.read_text(encoding="utf-8")
            self.assertIn(f"{ORG_C:012d}", body)
            self.assertIn("ФОТ по ведомостям", body)
            self.assertIn("Как изменилась структура", body)
            self.assertIn("Почему изменилось число людей", body)
            self.assertIn("найден перенос выплат на 31.07", body)
            self.assertIn("Изменились премии у тех же получателей", body)
            self.assertIn("Премии по календарным месяцам", body)
            real = body.split('<section id="findings">', 1)[1].split("</section>", 1)[0]
            self.assertNotIn(f"{ORG_C:012d}", real)
            self.assertIn(f"{ORG_C:012d}", body.split('<section id="explanations">', 1)[1])
            self.assertNotIn("organization_ids", recovered)
            self.assertNotIn(str(ORG_C), str(recovered))
            self.assertEqual(recovered["organization_checks"], report["organization_checks"])
            tables = dict(latest.notebook_tables(recovered))
            self.assertIn("Выплаты организаций по всем ГОСБ при остановке пары", tables)

        self.assertEqual(outgoing["residual"], 0)
        self.assertIsNone(incoming["expected"])
        self.assertNotIn(str(ORG_A), str(report["cards"]))
        self.assertNotIn(str(ORG_B), str(report["cards"]))
        self.assertTrue(all(str(ORG_C) not in str(table) for _, table in latest.notebook_tables(report)))
        current_bands = [b for b in report["portfolio"]["bands"] if b["mi"] == 0]
        self.assertEqual(sum(b["people"] for b in current_bands), 100)
        self.assertEqual(sum(b["amount"] for b in current_bands), report["portfolio"]["periods"][0]["amount"])
        self.assertTrue(any("·" in card["group"] for card in report["cards"]))
        _, decreases = latest.review_tables(report)[0]
        self.assertIn("От 200 тыс. ₽", set(decreases["Группа"]))
        self.assertTrue(decreases["Пара"].is_unique)
        for card in report["cards"]:
            if card["raw"] is not None:
                self.assertAlmostEqual(card["raw"], (card["boundary"] or 0) + (card["moved"] or 0)
                                       + (card["bonus"] or 0) + (card["confirmed"] or 0) + card["residual"])
        with db.session(self.engine) as c:
            replay = latest.run(c, "2026-08", today=date(2026, 10, 1), alias_salt=TEST_SALT)
        self.assertEqual(replay["latest"], "2026-09-30")
        self.assertEqual(replay["cards"], report["cards"])
        self.assertEqual(replay["portfolio"]["changes"], changes)

    def test_pair_audit_disconnect_returns_the_monthly_report_without_confirming_causes(self):
        def read_sql(connection, query, params=None):
            if "FROM changed" in query:
                self.assertNotIn("FROM ptf_latest_tr", query)
                raise OperationalError(query, None, Exception("server closed the connection unexpectedly"))
            frame = read_actual(connection, query, params)
            if query == Q.LATEST_MONTHS:
                frame.loc[frame["report_dt"].map(M.iso).eq(MONTH), "fot_amt"] += 99
            return frame

        read_actual = db.read_sql
        with db.session(self.engine) as c, patch.object(db, "read_sql", side_effect=read_sql):
            report = latest.run(c, month=MONTH, today=date(2026, 9, 15), alias_salt=TEST_SALT)
        self.assertIsNone(report["stale_pairs"])
        self.assertIn("Сверка отобранных пар", report["pair_audit_warning"])
        self.assertTrue(report["organization_checks"])
        self.assertTrue(report["cards"])
        self.assertTrue(all(c["confirmed"] is None and "не закончилась" in c["state"] for c in report["cards"]))

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
                       0 AS bonus_actual, 0 AS bonus_prior, 0 AS bonus_year_actual, 0 AS bonus_year_prior,
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
