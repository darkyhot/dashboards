"""Лента: точное разложение «отчётное = реальное + внутри банка + учёт + спорное + неизвестно»,
перебор сравнений, сезонный фильтр, склейка эха и одинаковых срезов.

Расчёт — без БД на рядах-заготовках. SQL и ячейка тетрадки — на локальном Postgres
(PTF_TEST_PG_URL) по синтетической сводке.
"""
import json
import os
import re
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from ptf import config, lenta, presentation
from ptf import months as M
from ptf import sql as Q

PG_URL = os.environ.get("PTF_TEST_PG_URL", "")


def frame_of(series: dict, months: list[str]) -> pd.DataFrame:
    """Длинная таблица срезов: {(тип, ключ): {мера: массив по месяцам}}. Остальные меры — 0."""
    rows = []
    for (kind, key), meas in series.items():
        for t, m in enumerate(months):
            row = {"slice_type": kind, "slice_key": key, "report_dt": m}
            for c in Q.LENTA_MEAS:
                v = meas.get(c, np.zeros(len(months)))[t]
                row[c] = 0. if np.isnan(v) else v
                row[f"nn_{c}"] = 1 if np.isnan(v) else 0
            rows.append(row)
    return pd.DataFrame(rows)


def seasonal(n=36, jump_month="2026-05", jump=-3000., noise_seed=3):
    """Банк: каждый январь уходят 1 000 и возвращаются в феврале (временный отток);
    в jump_month насовсем уходят ещё |jump|. Небольшой шум прихода/ухода."""
    months = [M.iso(M.shift("2023-11", k)) for k in range(n)]
    rng = np.random.default_rng(noise_seed)
    came = 300. + rng.integers(-20, 21, n)
    left = 300. + rng.integers(-20, 21, n)
    temp = np.array([1000. if m[5:7] == "01" else 0. for m in months])
    back = np.roll(temp, 1)
    back[0] = 0.
    extra = np.array([-jump if m.startswith(jump_month) else 0. for m in months])
    out_left = left + temp + extra
    pf = np.zeros(n)
    pf[0] = 100_000.
    for t in range(1, n):
        pf[t] = pf[t - 1] + came[t] + back[t] - out_left[t]
    base = np.r_[np.nan, pf[:-1]]
    temp_left = temp.copy()
    temp_left[-1] = np.nan                       # последний месяц: возврат ещё не виден
    meas = {"pf_recip": pf, "pf_fl": pf, "m_base_pf": base,
            "m_in_from_outside": came + back, "m_in_temp_outside": back,
            "m_out_left_bank": out_left, "m_out_temp_left": temp_left}
    return months, meas


class Decomposition(unittest.TestCase):
    def setUp(self):
        self.months, meas = seasonal()
        self.s = lenta.Series(frame_of({("Банк", "банк"): meas}, self.months))
        self.c = lenta.components(self.s)

    def test_parts_sum_to_reported_exactly(self):
        parts = sum(self.c[k] for k in lenta.COMPONENTS)
        ok = np.isfinite(self.c["reported"])
        np.testing.assert_allclose(parts[ok], self.c["reported"][ok])
        self.assertTrue(np.allclose(np.nan_to_num(self.c["gap"]), 0))

    def test_january_is_accounting_not_real(self):
        t = self.months.index("2026-01-31")
        self.assertEqual(self.c["temp_out"][0, t], -1000.)
        self.assertGreater(abs(self.c["left"][0, t]), 0)
        self.assertLess(abs(self.c["left"][0, t]), 400)

    def test_last_month_departures_are_unknown_not_real(self):
        self.assertEqual(self.c["left"][0, -1], 0.)
        self.assertLess(self.c["left_unk"][0, -1], 0.)
        self.assertEqual(self.c["temp_out"][0, -1], 0.)

    def test_first_month_is_unknown(self):
        self.assertTrue(np.isnan(self.c["reported"][0, 0]))

    def test_window_sum_and_bases(self):
        w = lenta.window(np.arange(1., 6.)[None, :], 3)
        np.testing.assert_allclose(w[0, 2:], [6., 9., 12.])
        self.assertTrue(np.isnan(w[0, :2]).all())
        a = np.arange(30.)[None, :]
        self.assertEqual(lenta.base_of(a, "y1")[0, 20], 8.)
        self.assertEqual(lenta.base_of(a, "avg")[0, 28], (16. + 4.) / 2)


class Scan(unittest.TestCase):
    def test_finds_real_departure_and_skips_known_season(self):
        months, meas = seasonal()
        rep = lenta.report(frame_of({("Банк", "банк"): meas}, months), months_back=12)
        st = rep["stories"]
        may = st[st["month"] == "2026-05-31"]
        self.assertFalse(may.empty)
        self.assertAlmostEqual(may.iloc[0]["g_real"], may.iloc[0]["excess"], delta=60)
        # Январь повторяется каждый год — не сюжет ни в каком сравнении месяца.
        jan = st[(st["month"] == "2026-01-31") & (st["L"] == 1)]
        self.assertTrue(jan.empty, jan)

    def test_echo_of_one_event_is_one_story(self):
        months, meas = seasonal()
        rep = lenta.report(frame_of({("Банк", "банк"): meas}, months), months_back=12)
        st = rep["stories"]
        neg = st[st["excess"] < 0]
        spans = [(r.t - r.L + 1, r.t) for r in neg.itertuples()]
        for i, (a, b) in enumerate(spans):
            for c, d in spans[i + 1:]:
                self.assertTrue(b < c or d < a, "перекрывающиеся окна одного знака склеиваются")

    def test_identical_child_slice_is_not_repeated(self):
        months, meas = seasonal()
        rep = lenta.report(frame_of({("Банк", "банк"): meas, ("Сегмент", "РГС"): meas,
                                     ("Сегмент × отрасль", "РГС / Связь"): meas}, months), months_back=12)
        self.assertEqual(set(rep["stories"]["slice_type"]), {"Банк"})

    def test_card_groups_sum_and_pages_fit(self):
        months, meas = seasonal()
        rep = lenta.report(frame_of({("Банк", "банк"): meas}, months), months_back=12,
                           checks=[("Банк", "банк", "2026-05", 1, "y1"), ("Сегмент", "нет", "2026-05", 1, "y1")])
        c = rep["checks"][0]
        self.assertAlmostEqual(sum(c["groups"].values()), c["reported"])
        self.assertIn("Нет данных", rep["checks"][1]["text"])
        n = 2 + len(rep["pages"]) + len(rep["cards"]) + len(rep["checks"])
        for page in range(1, n + 1):
            view = presentation.photo_page({"lenta": rep}, "лента", page)
            for _, frame in view["blocks"]:
                self.assertLessEqual(len(frame), 12)


@unittest.skipUnless(PG_URL, "PTF_TEST_PG_URL не задан: SQL ленты проверяется на локальном Postgres")
class Sql(unittest.TestCase):
    SCHEMA = "synth_lenta"

    @classmethod
    def setUpClass(cls):
        from ptf import build, synth

        cls.saved = (config.SCHEMA, config.SCHEMA_T)
        synth.build(PG_URL, persons=600, schema=cls.SCHEMA)
        res = build.run(conn=PG_URL, report_month="2026-10", schema=cls.SCHEMA, schema_t=cls.SCHEMA,
                        n_months=36, verbose=False)
        assert res["ok"], res["checks"]

    @classmethod
    def tearDownClass(cls):
        config.SCHEMA, config.SCHEMA_T = cls.saved

    def test_series_match_summary_and_notebook_cell_runs(self):
        from sqlalchemy import create_engine

        from ptf import db

        config.setup(self.SCHEMA, self.SCHEMA)
        eng = create_engine(PG_URL, future=True)
        with db.session(eng) as c:
            frame = lenta.load(c, hold_min=10, org_min=20, progress=lambda *_: None)
            total = db.read_sql(c, "SELECT report_dt, sum(pf_recip) AS pf, sum(m_out_left_bank) AS lb "
                                   "FROM {schema_t}.yva_ptf_month_org GROUP BY report_dt")
        bank = frame[frame["slice_type"] == "Банк"].set_index("report_dt")
        total = total.set_index("report_dt")
        np.testing.assert_allclose(bank.loc[total.index, "pf_recip"].astype(float), total["pf"].astype(float))
        for kind in ("Сегмент", "ТБ", "Размер организации"):
            part = frame[frame["slice_type"] == kind].groupby("report_dt")["pf_recip"].sum()
            np.testing.assert_allclose(part.loc[total.index].astype(float), total["pf"].astype(float))
        self.assertFalse(frame["slice_key"].str.contains(r"\d{9,}").any(), "номера организаций не выводятся")
        s = lenta.Series(frame)
        comp = lenta.components(s)
        ok = np.isfinite(comp["reported"])
        np.testing.assert_allclose(np.nan_to_num(comp["gap"][ok]), 0, atol=1e-6)

        notebook = json.loads((Path(__file__).resolve().parents[1] / "portfolio_core.ipynb").read_text())
        source = next("".join(c["source"]) for c in notebook["cells"]
                      if "ЛЕНТА: ОТКЛОНЕНИЯ" in "".join(c["source"]))
        source = re.sub(r"(?m)^LENTA_PAGE = .*$", "LENTA_PAGE = list(range(1, 30))", source)
        shown = []
        scope = {"display": shown.append, "CONN": PG_URL, "SCHEMA": self.SCHEMA, "SCHEMA_T": self.SCHEMA}
        exec(compile(source, "notebook_lenta", "exec"), scope)
        rep = scope["lenta_report"]
        self.assertEqual(len(rep["checks"]), 3)
        self.assertTrue(Path(scope["lenta_path"]).exists())
        self.assertGreaterEqual(len(shown), 2 + len(rep["pages"]) + len(rep["cards"]) + 3)


if __name__ == "__main__":
    unittest.main()


class LoadErrors(unittest.TestCase):
    def test_failed_slice_type_is_one_line_and_others_load(self):
        from unittest.mock import patch

        from ptf import db

        months, meas = seasonal(n=3)
        good = frame_of({("Банк", "банк"): meas}, months)

        class Orig(Exception):
            pass

        def fake_read(connection, sql, params=None):
            if "Отрасль" in sql:
                err = Exception("SELECT ... длинный запрос")
                err.orig = Orig("column \"ind\" does not exist\nLINE 1: ...")
                raise err
            return good.copy()

        logs = []
        with patch.object(db, "is_greenplum", return_value=True), patch.object(db, "execute", return_value=5), \
             patch.object(db, "read_sql", side_effect=fake_read), patch.object(db, "rollback"):
            frame = lenta.load(object(), progress=logs.append)
        self.assertEqual(frame.attrs["failed"], [("Отрасль", 'Orig: column "ind" does not exist')])
        self.assertTrue(any("пропущен — Orig: column" in s for s in logs))
        self.assertNotIn("SELECT", " ".join(logs))


class Rules(unittest.TestCase):
    def test_monthly_change_without_base_is_not_a_story_when_year_ago_exists(self):
        months, meas = seasonal()
        rep = lenta.report(frame_of({("Банк", "банк"): meas}, months), months_back=12)
        st = rep["stories"]
        self.assertFalse(((st["base"] == "none") & (st["L"] < 12)).any(), st[["month", "L", "base"]])

    def test_real_page_holds_departure_not_calendar(self):
        months, meas = seasonal()
        rep = lenta.report(frame_of({("Банк", "банк"): meas}, months), months_back=12)
        name, table, n = rep["pages"][0]
        self.assertTrue(name.startswith("Главное реальное"))
        self.assertIn("2026-05", set(table["Месяц"]))
        self.assertNotIn("2026-01", set(table["Месяц"]))

    def test_year_ago_stock_card_has_people_rows(self):
        months, meas = seasonal()
        meas = {**meas, "y_fl_in": np.full(len(months), 3000.), "y_fl_out": np.full(len(months), 2500.)}
        rep = lenta.report(frame_of({("Банк", "банк"): meas}, months), months_back=12,
                           checks=[("Банк", "банк", "2026-08", 12, "none")])
        t = rep["checks"][0]["table"].set_index("Составляющая")
        self.assertEqual(t.loc["к году назад: люди пришли в банк", "Окно"], 3000.)
        self.assertEqual(t.loc["к году назад: люди ушли из банка", "Окно"], -2500.)
        total = t.loc["Итого отчётное", "Окно"]
        self.assertAlmostEqual(t.loc["к году назад: прочее (совместительство, переходы, сегмент)", "Окно"],
                               total - 500.)
        view = presentation.photo_page({"lenta": rep}, "лента", 2 + len(rep["pages"]) + len(rep["cards"]) + 1)
        self.assertEqual(len(view["blocks"]), 2)
