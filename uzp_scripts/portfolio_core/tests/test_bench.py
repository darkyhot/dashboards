"""Проверки честности стенда и физической допустимости виртуальных когорт."""
import unittest

import numpy as np

from ptf import bench
from ptf.bench import Card, Event
from test_watch import split_org_panel


class BenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = split_org_panel()

    def test_existing_unrelated_card_is_not_a_found_event(self):
        p = self.panel
        r = int(p.orgs[1])
        old = Card("org", r, "out_left", -2000)
        self.assertIsNone(bench.match(p, [old], r, -1, [old], "m_out_codes", 500))
        self.assertIsNone(bench.match(p, [old], r, -1, [old], "m_out_left_bank", 500))
        grown = Card("org", r, "out_left", -3000)
        self.assertEqual(bench.match(p, [grown], r, -1, [old], "m_out_left_bank", 500), "org")

    def test_new_measure_on_same_entity_counts_as_artifact_leak(self):
        old = Card("gosb", 100, "out_left", -1000)
        new = Card("gosb", 100, "out_codes", -1000)
        self.assertEqual(bench._extra_cards([new], [old]), 1)
        self.assertEqual(bench._extra_cards([old], [old]), 0)
        self.assertEqual(bench._extra_cards([Card("gosb", 100, "out_left", -2000)], [old]), 1)

    def test_relocalization_of_unchanged_signal_is_not_an_artifact_leak(self):
        p = self.panel
        r = int(p.orgs[1])
        old = Card("org", r, "out_left", -1000)
        parent = Card("gosb", 200, "out_left", -1100)
        self.assertEqual(bench.extra_cards([parent], [old], p, p.copy(), 32), [])
        changed = p.copy()
        changed.x["m_out_left_bank"][2, -1] += 1000
        self.assertEqual(len(bench.extra_cards([parent], [old], p, changed, 32)), 1)
        unrelated = Card("org", r, "out_codes", -1000)
        self.assertEqual(len(bench.extra_cards([parent], [unrelated], p, p, 32)), 1)

    def test_split_cohort_changes_real_cells_and_preserves_identity(self):
        p = self.panel
        r = int(p.orgs[0])
        q = bench.inject(p, Event("leave", 32, 1000, row=r))
        self.assertAlmostEqual(p.x["pf_recip"][0, -1] - q.x["pf_recip"][0, -1], 600)
        self.assertAlmostEqual(p.x["pf_recip"][1, -1] - q.x["pf_recip"][1, -1], 400)
        self.assertAlmostEqual(p.x["pf_recip"][r, -1] - q.x["pf_recip"][r, -1], 1000)
        np.testing.assert_allclose(q.identity_gap(32), p.identity_gap(32))

    def test_impossible_cohort_and_segment_change_are_rejected(self):
        p = self.panel.copy()
        r = int(p.orgs[1])
        with self.assertRaises(ValueError):
            bench.inject(p, Event("leave", 32, 100_000, row=r))
        dst = int(p.meta.at[r, "atom"]) + 1
        p.meta.at[dst, "seg_rep"] = "Другой сегмент"
        with self.assertRaisesRegex(ValueError, "Смена ГОСБ"):
            bench.inject(p, Event("recode", 32, 1000, row=r, row2=dst))

    def test_actual_sigma_is_recorded_when_size_floor_applies(self):
        p = self.panel
        events = bench.pick_events(p, np.random.default_rng(1), "codes", 3., 32, 1)
        self.assertTrue(events)
        self.assertGreater(events[0].extra["effective_k"], 3)
        self.assertGreaterEqual(events[0].n, 300)

    def test_baseline_outgoing_flow_has_negative_portfolio_effect(self):
        p = self.panel.copy()
        r = int(p.orgs[1])
        p.x["m_out_codes"][r, -1] += 1000
        cards = bench.baseline(p, 32)
        card = next(c for c in cards if c.level == "org" and c.key == r)
        self.assertEqual(card.measure, "m_out_codes")
        self.assertEqual(card.delta, -1000)

    def test_varying_reasons_preserves_partition_and_aggregation(self):
        p = bench.simulate(seed=13, n_orgs=100, gosb_per_tb=2)
        q = bench.vary_departure_reasons(p, seed=14)
        np.testing.assert_array_equal(q.x["m_out_left_bank"], p.x["m_out_left_bank"])
        np.testing.assert_allclose(sum(q.x[m] for m in ("m_lb_seasonal", "m_lb_org_stopped",
                                                      "m_lb_one_time", "m_lb_unknown")),
                                   q.x["m_out_left_bank"])
        for m in ("m_lb_unknown", "m_lb_one_time"):
            self.assertGreaterEqual(np.min(q.x[m]), 0.)
            residual = q.x[m][q.atoms].copy()
            np.subtract.at(residual, q.meta.loc[q.orgs, "atom"].to_numpy(dtype=int), q.x[m][q.orgs])
            self.assertGreaterEqual(np.min(residual), -1e-6)
        self.assertGreater(float(np.std(q.x["m_lb_unknown"] / q.x["m_out_left_bank"])), .03)
        for m in p.x:
            if m not in ("m_lb_unknown", "m_lb_one_time"):
                np.testing.assert_array_equal(q.x[m], p.x[m])

    def test_spreading_preserves_bank_totals_and_identity(self):
        p = self.panel
        q = bench.spread_organizations(p, fraction=1.)
        for m in p.x:
            np.testing.assert_allclose(np.nansum(q.x[m][q.atoms], axis=0),
                                       np.nansum(p.x[m][p.atoms], axis=0), atol=1e-7)
        for t in (20, 32):
            np.testing.assert_allclose(q.identity_gap(t), p.identity_gap(t), atol=1e-7)

    def test_stop_removes_fractional_stock_and_multiple_jobs_together(self):
        p = self.panel.copy()
        r = int(p.orgs[1])
        a = int(p.meta.at[r, "atom"])
        p.x["pf_fl"][r, -1] = 18_000
        p.x["multi_recip"][r, -1] = 4000
        p.x["multi_recip"][a, -1] = 8000
        p.x["pf_fl"][a, -1] = 46_000
        p.org_stock.at[(r, 32, a), "pf_fl"] = 18_000
        p.org_stock.at[(r, 32, a), "multi_recip"] = 4000
        q = bench.inject(p, Event("stop", 32, 20_000, row=r))
        self.assertEqual(q.x["pf_recip"][r, -1], 0)
        self.assertEqual(q.x["pf_fl"][r, -1], 0)
        self.assertEqual(q.x["multi_recip"][r, -1], 0)
        self.assertEqual(q.x["multi_recip"][a, -1], 4000)
        self.assertEqual(q.x["pf_fl"][a, -1], 28_000)


if __name__ == "__main__":
    unittest.main()
