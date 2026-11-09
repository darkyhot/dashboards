"""Границы положительных счётчиков, точный остаток и независимость совокупностей."""
import math
import unittest
from statistics import NormalDist

import numpy as np
import pandas as pd

from ptf import bench, count_model, inputs, watch
from ptf.bench import Card
from test_calibration import sparse_transfers


class PositiveCountTests(unittest.TestCase):
    def test_tail_bound_and_combined_rule_respect_false_alarm_budget(self):
        grid = np.linspace(0, 9, 100)
        exact = np.array([.5 * math.erfc(x / math.sqrt(2)) for x in grid])
        self.assertTrue((count_model.normal_tail_bound(grid) >= exact - 1e-15).all())
        n = 400
        normal = NormalDist().inv_cdf(1 - 1 / (2 * n))
        center, sigma = np.linspace(np.log(100), np.log(100000), n), np.linspace(.05, .5, n)
        base = np.linspace(50000, 1000000, n)
        scans = {"flow": {"center": center, "log_s": sigma, "zs": normal,
                           "base": base, "d": np.zeros(n)}}
        critical, bound = count_model.materiality_threshold(scans, normal, 1.)
        self.assertLessEqual(bound, 1.)
        rng = np.random.default_rng(7001)
        observation = np.maximum(np.expm1(center + sigma * rng.standard_normal((5000, n))), 0.)
        upper, lower = count_model.positive_exp(center + critical * sigma), count_model.positive_exp(center - critical * sigma)
        alarms = ((observation - upper >= watch.floor_of(base))
                  | (lower - observation >= watch.floor_of(base)))
        self.assertLessEqual(float(alarms.sum(axis=1).mean()), 1.1)

    def test_large_active_historical_events_do_not_become_common_threshold(self):
        p, t = sparse_transfers(), 30
        obs = {f: watch.flow_obs(p, f) for f in watch.FLOWS}
        active = p.orgs[1:9]
        for h, row in zip(range(18, 25), active):
            obs["in_other"][row, h] += 35044.
        old = watch.scan_rows(p, p.orgs, t, obs, legacy_replay=True)["in_other"]
        new = watch.scan_rows(p, p.orgs, t, obs, details=True)["in_other"]
        self.assertGreater(old["zs"], 100.)
        self.assertLess(new["zs"], 10.)
        self.assertFalse(any(r["задаёт порог"] for r in new["calibration"]["contributors"]))
        obs["in_other"][int(p.orgs[2]), t] += 1000.
        self.assertIn(int(p.orgs[2]), {r for r, *_ in watch.pick(
            watch.scan_rows(p, p.orgs, t, obs), p.orgs)})

    def test_intervals_respect_count_support_and_excess_is_in_people(self):
        p, t = sparse_transfers(), 30
        obs = {f: watch.flow_obs(p, f) for f in watch.FLOWS}
        scan = watch.scan_rows(p, p.orgs, t, obs)
        for s in scan.values():
            good = np.isfinite(s["d"])
            self.assertTrue((s["lower"][good] >= 0).all())
            self.assertTrue((s["lower"][good] <= s["E"][good]).all())
            self.assertTrue((s["E"][good] <= s["upper"][good]).all())
        row = int(p.orgs[1])
        obs["in_other"][row, t] = 1000.
        s = watch.scan_rows(p, p.orgs, t, obs)["in_other"]
        i = int(np.flatnonzero(p.orgs == row)[0])
        self.assertAlmostEqual(watch.excess(s)[i], 1000. - s["upper"][i])


class ResidualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = bench.spread_organizations(bench.simulate(seed=623, n_orgs=40,
                                                 gosb_per_tb=2), fraction=.8)

    def test_subtracts_actual_cells_and_preserves_raw_flow_identity(self):
        p = self.panel
        q = inputs.residual_panel(p)
        for f, m in inputs.CELL_MEAS.items():
            known = p.org_flows[f].groupby(level=["atom", "t"]).sum(min_count=1)
            for (a, t), value in known.items():
                np.testing.assert_allclose(q.x[m][a, t], p.x[m][a, t] - value, atol=1e-6)
        for t in range(len(p.months)):
            np.testing.assert_allclose(q.identity_gap(t), 0., atol=1e-6)

    def test_unknown_component_is_unknown_and_negative_is_not_clipped(self):
        p = self.panel.copy()
        key = p.org_flows.index[0]
        p.org_flows.at[key, "in_codes"] = np.nan
        q = inputs.residual_panel(p)
        self.assertTrue(np.isnan(q.x["m_in_codes"][key[2], key[1]]))
        p.org_flows.at[key, "in_codes"] = p.x["m_in_codes"][key[2], key[1]] + 1
        with self.assertRaisesRegex(ValueError, "остаток|Остаток"):
            inputs.residual_panel(p)
        q = inputs.residual_panel(self.panel)
        q.x["m_out_temp_left"][:, 30] = q.x["m_out_left_bank"][:, 30] + 1
        with self.assertRaisesRegex(ValueError, "больше общего ухода"):
            watch.flow_obs(q, "out_left", calendar_reference=self.panel)

    def test_recode_and_organization_gap_do_not_invent_tail_flows(self):
        p = self.panel
        a = inputs.residual_panel(p)
        recode = bench.pick_recode(p, np.random.default_rng(123), 30)
        self.assertIsNotNone(recode)
        for event in (recode, bench.Event("dip", 29, 100, row=int(p.orgs[0]))):
            b = inputs.residual_panel(bench.inject(p, event))
            for m in inputs.IN_M + inputs.OUT_M + ["m_in_temp_outside", "m_out_temp_left"]:
                np.testing.assert_allclose(a.x[m], b.x[m], atol=1e-6, equal_nan=True)

    def test_artifacts_keep_cell_stock_and_flows_consistent(self):
        p = self.panel
        for ev in (bench.Event("calendar", 30, 0, q=.005),
                   bench.Event("underload", 32, 0, tb=int(p.meta.at[0, "sys_tb_id"]), q=.08)):
            q = inputs.residual_panel(bench.inject(p, ev))
            for t in range(len(p.months)):
                np.testing.assert_allclose(q.identity_gap(t), 0., atol=1e-6)
            for f in watch.FLOWS:
                normalized = watch.flow_obs(q, f, calendar_reference=p)
                self.assertFalse((normalized < -1e-6).any())

    def test_atom_cohort_cannot_remove_people_of_unchanged_organizations(self):
        months = bench.sim_months()
        rows = []
        for m in months:
            v = dict.fromkeys(inputs.MEAS, 0.)
            v.update(report_dt=m, seg_rep="Сегмент", sys_tb_id=1, sys_gosb_id=1,
                     pf_recip=1000., pf_fl=1000., m_base_pf=1000., raw_rows=1000.)
            rows.append(v)
        atoms = pd.DataFrame(rows)
        orgs = atoms.copy().assign(org_id=1)
        for m in ("pf_recip", "pf_fl", "m_base_pf", "raw_rows"):
            orgs[m] = 990.
        p = inputs.from_frames(months, atoms, orgs)
        with self.assertRaisesRegex(ValueError, "превышает доступный остаток"):
            bench.inject(p, bench.Event("leave", 30, 100, row=0))
        q = bench.inject(p, bench.Event("leave", 30, 5, row=0))
        self.assertEqual(inputs.residual_panel(q).x["pf_recip"][0, 30], 5.)
        np.testing.assert_array_equal(p.x["pf_recip"][p.orgs], q.x["pf_recip"][p.orgs])

    def test_matching_compares_same_population(self):
        p, atom = self.panel, 0
        def tail(value):
            return Card("atom", atom, "in_outside", value, info={"scope": "other_orgs"})
        whole = Card("atom", atom, "in_outside", 2000.)
        self.assertEqual(bench.match(p, [tail(400)], atom, 1, [whole, tail(200)],
                                    "m_in_from_outside", 100., 30), "atom")
        self.assertIsNone(bench.match(p, [tail(400)], atom, 1, [tail(400), whole],
                                     "m_in_from_outside", 100., 30))
        self.assertEqual(len(bench.extra_cards([tail(400)], [whole])), 1)

    def test_coverage_adds_disjoint_populations_without_double_counting_full_atom(self):
        p = self.panel
        r = next(int(r) for r in p.orgs if p.org_scopes["atom"][r, 30] >= 0)
        atom = int(p.org_scopes["atom"][r, 30])
        tb = int(p.meta.at[atom, "sys_tb_id"])
        org = Card("org", r, "out_codes", -400.)
        tail = Card("atom", atom, "out_codes", -400., info={"scope": "other_orgs"})
        self.assertTrue(watch._covered(p, 30, "tb", tb, "out_codes", 1000., [org, tail], 850.))
        full = Card("atom", atom, "out_codes", -900.)
        self.assertFalse(watch._covered(p, 30, "tb", tb, "out_codes", 2000., [org, tail, full], 1800.))

    def test_tail_card_displays_tail_fact_and_original_significance_floor(self):
        p = self.panel
        q = inputs.residual_panel(p)
        a, t = int(p.org_stock.index[0][2]), 30
        raw = q.x["m_in_from_outside"][a, t]
        normalized = raw - q.x["m_in_temp_outside"][a, t]
        c = Card("atom", a, "in_outside", 150., obs=normalized, exp=normalized - 150.,
                 score=100.9, info={"scope": "other_orgs"})
        frame = watch.cards_frame(p, [c], t)
        self.assertEqual(frame.iloc[0]["факт"], round(raw))
        self.assertEqual(frame.iloc[0]["ожидание"], round(raw - 150.))
        self.assertIn("прочие организации", frame.iloc[0]["где"])
        self.assertEqual(frame.iloc[0]["сверх шума, не меньше"], 100)
        self.assertNotEqual(round(raw), round(p.x["m_in_from_outside"][a, t]))
        scans = watch.scan_rows(q, q.atoms, t,
            {f: watch.flow_obs(q, f, calendar_reference=p) for f in watch.FLOWS}, details=True)
        for sc in scans.values():
            np.testing.assert_array_equal(sc["base"], p.x["m_base_pf"][p.atoms, t])
            self.assertEqual(sc["calibration"]["budget"], watch.FALSE_PER_LEVEL / 2)
            self.assertLessEqual(sc["calibration"]["expected_false_bound"], watch.FALSE_PER_LEVEL / 2)


if __name__ == "__main__":
    unittest.main()
