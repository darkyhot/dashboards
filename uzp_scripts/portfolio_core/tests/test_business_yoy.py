"""Год к году: повторяющийся сезон сокращается, отличие раскладывается точно."""
import unittest

import numpy as np
import pandas as pd

from ptf import bench, business, inputs, presentation


def seasonal_panel(extra_month="2026-05", extra=30.):
    """Два атома: каждый январь −100 в атоме 0 (возврат в феврале), один раз −extra в атоме 1."""
    months = bench.sim_months()
    meta = pd.DataFrame({"lvl": ["atom", "atom"], "org_id": [-1, -1], "seg_rep": ["A", "B"],
                         "sys_tb_id": [1, 1], "sys_gosb_id": [100, 101], "atom": [0, 1]})
    pf = np.full((2, len(months)), 1000.)
    for t, m in enumerate(months):
        if m[5:7] == "01":
            pf[0, t] -= 100.
        if m.startswith(extra_month):
            pf[1, t] -= extra
    x = {m: np.zeros((2, len(months))) for m in inputs.MEAS}
    x["pf_recip"], x["pf_fl"] = pf.copy(), pf.copy()
    x["m_base_pf"][:, 1:], x["m_base_pf"][:, 0] = pf[:, :-1], pf[:, 0]
    diff = np.zeros_like(pf)
    diff[:, 1:] = pf[:, 1:] - pf[:, :-1]
    for name in ("m_in_from_outside", "m_fl_in"):
        x[name] = np.maximum(diff, 0.)
    for name in ("m_out_left_bank", "m_lb_unknown", "m_fl_out"):
        x[name] = np.maximum(-diff, 0.)
    # Каждый провал возвращается в следующем месяце: временный отток и возврат после него.
    x["m_out_temp_left"] = x["m_out_left_bank"].copy()
    x["m_out_temp_left"][:, -1] = np.nan
    x["m_in_temp_outside"] = x["m_in_from_outside"].copy()
    x["m_in_temp_outside"][:, :2] = np.nan
    p = inputs.Panel(months, meta, x)
    for t in range(len(months)):
        np.testing.assert_allclose(p.identity_gap(t), 0.)
    return p


class YearOverYearTests(unittest.TestCase):
    def test_repeated_january_cancels_and_real_difference_is_selected(self):
        p = seasonal_panel()
        self.assertIn(business.analyze(p)["month"][5:7], ("01", "02"))  # старый выбор — сезон
        report = business.yoy(p)
        self.assertEqual(report["month"][:7], "2026-05")
        self.assertEqual(report["excess"], -30.)
        january = report["history"].set_index("Месяц").loc["2026-01-31"]
        self.assertEqual(january["Сверх прошлого года"], 0.)
        self.assertEqual(report["slices"]["segments"].iloc[0]["Сегмент"], "B")

    def test_flows_add_up_to_bank_difference(self):
        report = business.yoy(seasonal_panel(), "2026-06")
        self.assertEqual(report["excess"], 30.)
        self.assertAlmostEqual(report["flows"]["Сверх прошлого года"].sum(), report["excess"])
        self.assertTrue(report["summary"]["Проверено"].all())

    def test_missing_reason_stays_unknown(self):
        p = seasonal_panel()
        t = p.months.index("2026-05-31")
        p.x["m_lb_one_time"][1, t] = np.nan
        report = business.yoy(p, "2026-05")
        row = report["reasons"].set_index("Признак").loc["Ушли из банка: были разовыми в прошлом месяце"]
        self.assertTrue(np.isnan(row["Этот год"]))
        self.assertTrue(any("Неизвестно" in issue for issue in report["issues"]))

    def test_needs_thirteen_previous_months(self):
        p = seasonal_panel()
        short = inputs.Panel(p.months[:13], p.meta, {k: v[:, :13] for k, v in p.x.items()})
        with self.assertRaisesRegex(ValueError, "год к году"):
            business.yoy(short)
        with self.assertRaisesRegex(ValueError, "год к году"):
            business.yoy(p, "2024-06")

    def test_photo_pages_fit(self):
        report = business.yoy(seasonal_panel())
        for page in range(1, 9):
            view = presentation.photo_page({"business_yoy": report}, "год к году", page)
            self.assertTrue(all(len(frame) <= 12 for _, frame in view["blocks"]))

    def test_explanation_adds_up_and_names_temporary_outflow(self):
        report = business.yoy(seasonal_panel(), "2026-05")
        frame = report["explanation"].set_index("Составляющая")["Сверх прошлого года"]
        self.assertAlmostEqual(frame.sum(), report["excess"])
        self.assertEqual(frame["Временный отток: ушли и вернулись в следующем месяце"], -30.)
        self.assertEqual(frame["Постоянный уход из банка"], 0.)
        self.assertEqual(frame["Невязка расчёта"], 0.)
        self.assertTrue(any("Признак сдвига выплат" in line for line in report["verdict"]))
        back = business.yoy(seasonal_panel(), "2026-06")
        self.assertEqual(back["explanation"].set_index("Составляющая")["Сверх прошлого года"][
            "Возврат после временного оттока прошлого месяца"], 30.)

    def test_last_month_temporary_outflow_is_unknown_not_zero(self):
        p = seasonal_panel("2026-09")
        report = business.yoy(p, "2026-09")
        frame = report["explanation"].set_index("Составляющая")["Сверх прошлого года"]
        self.assertTrue(np.isnan(frame["Временный отток: ушли и вернулись в следующем месяце"]))
        self.assertTrue(any("неизвестен" in line for line in report["verdict"]))

    def test_absent_organization_is_marked(self):
        p = seasonal_panel()
        meta = pd.concat([p.meta, pd.DataFrame({"lvl": ["org"], "org_id": [1_000_000_001], "seg_rep": ["A"],
                          "sys_tb_id": [1], "sys_gosb_id": [100], "atom": [0]})], ignore_index=True)
        x = {k: np.vstack([v, v[:1]]) for k, v in p.x.items()}
        t = p.months.index("2026-01-31")
        x["pf_recip"][2, t - 1:] = 0.
        q = inputs.Panel(p.months, meta, x)
        orgs = business.yoy(q, "2026-01")["organizations"].set_index("Организация")
        self.assertEqual(orgs["Примечание"].iloc[0], "нет в этом году")
        self.assertEqual(orgs["Сверх прошлого года"].iloc[0], 100.)


if __name__ == "__main__":
    unittest.main()
