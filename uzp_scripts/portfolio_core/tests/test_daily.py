"""Граница месяца по дневной витрине: сдвинутый месяц (16-е … 15-е) против календарного.

Расчётные функции проверяются без БД. Полный путь через SQL — на локальном Postgres,
если задан PTF_TEST_PG_URL (в закрытом контуре тест пропускается).
"""
import os
import unittest
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ptf import config, daily, presentation
from ptf import months as M

PG_URL = os.environ.get("PTF_TEST_PG_URL", "")
SCHEMA = "test_daily"
FIRST, LAST = date(2024, 6, 1), date(2026, 9, 30)
ORG_A, ORG_B, ORG_T = 7_000_000_001, 7_000_000_002, 7_000_000_003


def pay_day_first(y: int, m: int) -> date:
    """Выплата 1-го числа; в выходной — в предыдущую пятницу."""
    d = date(y, m, 1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def payments() -> pd.DataFrame:
    """A: 100 человек 5-го числа. B: 50 человек 1-го числа (выходной → пятница раньше).
    T: организация ТИК — исключается. Плюс строка «Дополнительные» — не отбирается."""
    rows = []
    y, m = FIRST.year, FIRST.month
    while date(y, m, 1) <= LAST:
        for i in range(100):
            rows.append((date(y, m, 5), 1000 + i, ORG_A, 50_000.0, "Основные"))
        d = pay_day_first(y, m)
        if FIRST <= d <= LAST:
            for i in range(50):
                rows.append((d, 2000 + i, ORG_B, 40_000.0, "Основные"))
            rows.append((d, 3000, ORG_T, 40_000.0, "Основные"))
        rows.append((date(y, m, 10), 4000, ORG_A, 90_000.0, "Дополнительные"))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return pd.DataFrame(rows, columns=["pay_dt", "epk_id", "org", "amt", "kind"])


def reference(pays: pd.DataFrame) -> pd.DataFrame:
    """Независимый эталон: число троек по календарному и сдвинутому месяцу."""
    p = pays[(pays["kind"] == "Основные") & (pays["org"] != ORG_T)].copy()
    p["cal"] = [M.iso(d) for d in p["pay_dt"]]
    p["sh"] = [M.iso(d if d.day <= 15 else M.shift(d, 1)) for d in p["pay_dt"]]
    out = {}
    for col in ("cal", "sh"):
        s = p.groupby([col, "epk_id", "org"])["amt"].sum()
        s = s[s > 2500].reset_index().groupby(col).size()
        out["n_" + col] = s
    return pd.DataFrame(out)


class Windows(unittest.TestCase):
    def test_shifted_month_bounds(self):
        w = daily.windows("2026-08")
        self.assertEqual((w["s_from"], w["s_to"]), ("2026-07-16", "2026-08-15"))
        self.assertEqual((w["c_from"], w["c_to"]), ("2026-08-01", "2026-08-31"))
        self.assertEqual(daily.windows("2026-01")["s_from"], "2025-12-16")

    def test_needed_months(self):
        self.assertEqual(daily.needed(["2026-08"]),
                         ["2025-07-31", "2025-08-31", "2026-07-31", "2026-08-31"])


def frame_from(ref: pd.DataFrame, monthly_shift: dict | None = None) -> pd.DataFrame:
    f = ref.rename(columns={"n_cal": "n_cal", "n_sh": "n_sh"}).reset_index(names="report_dt")
    f["seg"], f["sys_tb_id"], f["n_both"] = "РГС", 52, np.nan
    f["pf"] = f["n_cal"].astype(float)
    for m, v in (monthly_shift or {}).items():
        f.loc[f["report_dt"] == m, "pf"] += v
    return f


class Report(unittest.TestCase):
    def setUp(self):
        self.ref = reference(payments()).fillna(0)

    def test_weekend_first_is_calendar(self):
        """1 августа 2026 — суббота: выплата B ушла на 31 июля; в 2025 году 1-е — пятница."""
        rep = daily.report(frame_from(self.ref), ["2026-08"])
        row = rep["bank"].iloc[0]
        self.assertEqual(row["Месячная сводка"], -50)
        self.assertEqual(row["Остаток (16-е – 15-е)"], 0)
        self.assertEqual(row["Граница месяца"], -50)
        self.assertEqual(row["Расхождение витрин"], 0)
        comp = rep["details"][0]["components"].set_index("Счёт")
        self.assertEqual(comp.loc["Календарный месяц (дни)", "Сверх прошлого года"], -50)
        self.assertIn("календарь, а не потеря людей", rep["verdicts"][0])

    def test_source_gap_is_visible(self):
        """Уровни сходятся в допуске, но отличие месячной сводки иное — разница видна строкой."""
        rep = daily.report(frame_from(self.ref, {"2026-08-31": -0.2}), ["2026-08"])
        row = rep["bank"].iloc[0]
        self.assertAlmostEqual(row["Расхождение витрин"], -0.2)
        self.assertAlmostEqual(row["Месячная сводка"],
                               row["Граница месяца"] + row["Остаток (16-е – 15-е)"] + row["Расхождение витрин"])
        self.assertIn("расхождение витрин", rep["verdicts"][0])

    def test_monthly_mismatch_blocks_conclusion(self):
        rep = daily.report(frame_from(self.ref, {"2026-07-31": 10.}), ["2026-08"])
        self.assertIn("расходится с месячной", rep["verdicts"][0])

    def test_missing_month_is_unknown(self):
        f = frame_from(self.ref)
        f = f[f["report_dt"] != "2025-07-31"]
        rep = daily.report(f, ["2026-08"])
        self.assertTrue(np.isnan(rep["bank"].iloc[0]["Граница месяца"]))
        self.assertIn("нельзя", rep["verdicts"][0])

    def test_pages_fit(self):
        rep = daily.report(frame_from(self.ref), ["2026-08", "2026-02"])
        data = {"calendar": rep}
        view = presentation.photo_page(data, "граница месяца", 1)
        self.assertEqual(view["pages"], 5)
        for page in range(1, 6):
            v = presentation.photo_page(data, "граница месяца", page)
            for _, frame in v["blocks"]:
                self.assertLessEqual(len(frame), 12)


@unittest.skipUnless(PG_URL, "PTF_TEST_PG_URL не задан: SQL дневной витрины проверяется на локальном Postgres")
class Sql(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, text

        from ptf import synth

        cls.pays = payments()
        cls.ref = reference(cls.pays).fillna(0)
        eng = create_engine(PG_URL, future=True)
        with eng.connect() as c:
            for stmt in synth.DDL.format(s=SCHEMA).split(";"):
                if stmt.strip():
                    c.execute(text(stmt))
            c.execute(text(f"CREATE TABLE {SCHEMA}.mis_data_payroll_d (LIKE {SCHEMA}.mis_data_payroll_m)"))
            c.execute(text(f"""CREATE TABLE {SCHEMA}.yva_ptf_month_org (
                report_dt date, org_id bigint, sys_gosb_id integer, sys_tb_id integer,
                seg_rep varchar, pf_recip bigint)"""))
            synth._copy(c, pd.DataFrame([{"enrollment_type_id": k, "enrollment_type_name": v[0],
                                          "is_portfolio_enrollment": v[1], "is_fot_enrollment": v[2]}
                                         for k, v in synth.CODES.items()]),
                        f"{SCHEMA}.uzp_dim_enrollment_type")
            synth._copy(c, pd.DataFrame([{"inn": ORG_T, "company_name": "ТИК", "is_tik_cik": True}]),
                        f"{SCHEMA}.yva_tik_cik")
            d = cls.pays
            synth._copy(c, pd.DataFrame({
                "report_dt": [x.isoformat() for x in d["pay_dt"]], "epk_id": d["epk_id"],
                "inn": d["org"].astype(str), "sys_gosb_id": 8601, "sys_tb_id": 52,
                "enrollment_type": 1, "amt": d["amt"], "segment_name": "Средний бизнес",
                "enrollment_kind_descr": d["kind"], "market_share_flag_name": "Прочие",
                "transaction_qty": 1}), f"{SCHEMA}.mis_data_payroll_d")
            # Месячная сводка — эталонный календарный месяц (для сверки).
            p = d[(d["kind"] == "Основные") & (d["org"] != ORG_T)].copy()
            p["report_dt"] = [M.iso(x) for x in p["pay_dt"]]
            mo = p.groupby(["report_dt", "org", "epk_id"])["amt"].sum()
            mo = mo[mo > 2500].reset_index().groupby(["report_dt", "org"]).size().reset_index(name="pf_recip")
            synth._copy(c, pd.DataFrame({"report_dt": mo["report_dt"], "org_id": mo["org"],
                                         "sys_gosb_id": 8601, "sys_tb_id": 52,
                                         "seg_rep": np.where(mo["org"] == ORG_A, "РГС", "ММБ"),
                                         "pf_recip": mo["pf_recip"]}), f"{SCHEMA}.yva_ptf_month_org")
            c.commit()
        cls.saved = (config.SCHEMA, config.SCHEMA_T)
        config.setup(SCHEMA, SCHEMA)
        cls.engine = eng

    @classmethod
    def tearDownClass(cls):
        config.SCHEMA, config.SCHEMA_T = cls.saved

    def test_sql_matches_reference_and_caches(self):
        from ptf import db

        logs = []
        with db.session(self.engine) as c:
            rep = daily.run(c, months=["2026-08", "2026-02"], progress=logs.append)
            frame = daily.load(c, daily.needed(["2026-08", "2026-02"]))
            again = daily.run(c, months=["2026-08", "2026-02"], with_days=False, progress=logs.append)
        got = frame.groupby("report_dt")[["n_cal", "n_sh", "pf"]].sum()
        for m, row in got.iterrows():
            self.assertEqual(row["n_cal"], self.ref.loc[m, "n_cal"], m)
            self.assertEqual(row["n_sh"], self.ref.loc[m, "n_sh"], m)
            self.assertEqual(row["pf"], row["n_cal"], m)
        bank = rep["bank"].set_index("Месяц")
        self.assertEqual(bank.loc["2026-08", "Граница месяца"], -50)
        self.assertEqual(bank.loc["2026-08", "Остаток (16-е – 15-е)"], 0)
        self.assertEqual(bank.loc["2026-08", "Сверка уровней, %"], 0)
        self.assertTrue(any("уже посчитаны" in s for s in logs))
        pd.testing.assert_frame_equal(rep["bank"], again["bank"])
        days = rep["details"][0]["days"]
        self.assertEqual(days.loc[days["Этот год"] == "31.07 пт", "Строк выплат"].iloc[0], 50)
        segs = rep["details"][0]["segments"].set_index("Сегмент")
        self.assertEqual(segs.loc["ММБ", "Граница месяца"], -50)

    def test_notebook_cell_shows_pages_without_generation(self):
        import json
        import re
        from pathlib import Path
        from unittest.mock import patch

        from ptf import inputs

        notebook = json.loads((Path(__file__).resolve().parents[1] / "portfolio_core.ipynb").read_text())
        source = next("".join(c["source"]) for c in notebook["cells"]
                      if "БИЗНЕС-РАЗБОР ПОРТФЕЛЯ" in "".join(c["source"]))
        for name, value in (("BUSINESS_PAGE", "[]"), ("BUSINESS_EXTRA_MONTHS", "[]"),
                            ("SLICE_SOURCES", "False"), ("CALENDAR", "True"),
                            ("CALENDAR_MONTHS", '["2026-08", "2026-01"]'),
                            ("CALENDAR_PAGE", "[1, 2, 3, 4, 5]")):
            source = re.sub(rf"(?m)^{name} = .*$", f"{name} = {value}", source)
        printed = []
        scope = {"display": printed.append, "CONN": PG_URL, "SCHEMA": SCHEMA, "SCHEMA_T": SCHEMA}
        with patch.object(inputs, "load", side_effect=AssertionError("Поколение")):
            exec(compile(source, "notebook_business", "exec"), scope)
        self.assertNotIn("panel", scope)
        self.assertEqual(len(scope["calendar_report"]["details"]), 2)
        self.assertGreaterEqual(len(printed), 9)

    def test_auto_months_use_monthly_summary(self):
        from ptf import db

        with db.session(self.engine) as c:
            got = daily.auto_months(c, top=3)
        self.assertEqual(len(got), 3)
        self.assertTrue(all(m >= "2025-07-31" for m in got))


if __name__ == "__main__":
    unittest.main()
