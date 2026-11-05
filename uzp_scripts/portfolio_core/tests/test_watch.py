"""Регрессии сторожа: python3 -m unittest discover -s tests -v (из portfolio_core)."""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from ptf import bench, inputs, watch
from ptf.bench import Card


def split_org_panel():
    """Организация в двух ГОСБ; весь её уход больше ухода в главном атоме."""
    months = bench.sim_months()
    atoms, orgs = [], []
    for t, month in enumerate(months):
        for tb in range(8):
            for cell in range(2):
                row = dict.fromkeys(inputs.MEAS, 0.0)
                row.update(report_dt=month, seg_rep="Сегмент", sys_tb_id=tb + 1,
                           sys_gosb_id=100 * (tb + 1) + cell,
                           pf_recip=50_000., pf_fl=50_000., m_base_pf=50_000., raw_rows=70_000.)
                for m in inputs.IN_M + inputs.OUT_M:
                    row[m] = 200.
                if t == len(months) - 1 and tb == 0:
                    row["m_out_codes"] = 4864. if cell == 0 else 5566.
                atoms.append(row)
            for cell, share in ((0, .6), (1, .4)) if tb == 0 else ((0, 1.),):
                row = dict.fromkeys(inputs.MEAS, 0.0)
                row.update(report_dt=month, org_id=tb + 1, seg_rep="Сегмент", sys_tb_id=tb + 1,
                           sys_gosb_id=100 * (tb + 1) + cell,
                           pf_recip=20_000. * share, pf_fl=20_000. * share, m_base_pf=20_000. * share,
                           raw_rows=28_000. * share)
                for m in inputs.IN_M + inputs.OUT_M:
                    row[m] = 20. * share
                if t == len(months) - 1 and tb == 0:
                    row["m_out_codes"] = 4864. if cell == 0 else 5366.
                orgs.append(row)
    return inputs.from_frames(months, pd.DataFrame(atoms), pd.DataFrame(orgs))


class WatchRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = split_org_panel()

    def test_fewer_departures_are_not_labelled_as_organization_stop(self):
        p, r = self.panel.copy(), int(self.panel.orgs[1])
        p.x["m_out_left_bank"][r] = 2000
        p.x["m_out_left_bank"][r, -1] = 0
        card = next(c for c in watch.detect(p, 32) if c.level == "org" and c.key == r)
        self.assertEqual(card.measure, "out_left")
        self.assertGreater(card.delta, 0)
        self.assertIsNone(card.label)

    def test_split_org_does_not_create_reverse_event_or_change_observations(self):
        p = self.panel.copy()
        before = {m: a.copy() for m, a in p.x.items()}
        scan = watch.scan_rows
        seen_atoms = []

        def inspect(panel, rows, t, obs):
            if np.array_equal(rows, panel.atoms):
                seen_atoms.append(obs["out_codes"].copy())
            return scan(panel, rows, t, obs)

        with patch.object(watch, "scan_rows", side_effect=inspect):
            cards = watch.detect(p, len(p.months) - 1)
        org = int(p.orgs[0])
        self.assertTrue(any(c.level == "org" and c.key == org and c.delta < -10_000
                            for c in cards))
        for c in cards:
            if (c.level == "atom" and c.key in (0, 1) and c.measure == "out_codes"
                    and c.info.get("scope", "all") == "all"):
                self.assertLess(c.delta, 0)
                self.assertEqual(c.obs, p.x["m_out_codes"][c.key, -1])
        self.assertTrue(seen_atoms)
        np.testing.assert_array_equal(seen_atoms[0], before["m_out_codes"])
        for m, a in before.items():
            np.testing.assert_array_equal(p.x[m], a)

    def test_split_org_is_not_assigned_entirely_to_main_atom(self):
        p, r = self.panel, int(self.panel.orgs[0])
        c = Card("org", r, "out_codes", -10_000)
        self.assertEqual(p.org_scopes["atom"][r, -1], -1)
        self.assertEqual(p.org_scopes["gosb"][r, -1], -1)
        self.assertTrue(watch._contained(p, 32, c, "tb", 1, "out_codes"))
        self.assertFalse(watch._covered(p, 32, "atom", 0, "out_codes", 4500, [c]))
        self.assertFalse(watch._covered(p, 32, "gosb", 100, "out_codes", 4500, [c]))
        self.assertTrue(watch._covered(p, 32, "tb", 1, "out_codes", 10_000, [c]))

    def test_duplicate_needs_same_direction_and_known_scope(self):
        p, r = self.panel.copy(), int(self.panel.orgs[1])
        a = int(p.meta.at[r, "atom"])
        c = Card("org", r, "out_codes", -1000)
        self.assertTrue(watch._covered(p, 32, "atom", a, "out_codes", 1100, [c]))
        self.assertFalse(watch._covered(p, 32, "atom", a, "out_codes", -1100, [c]))
        p.org_scopes = {}
        self.assertFalse(watch._covered(p, 32, "atom", a, "out_codes", 1100, [c]))

    def test_overlapping_levels_are_not_added_twice(self):
        p, r = self.panel, int(self.panel.orgs[1])
        a = int(p.meta.at[r, "atom"])
        cards = [Card("org", r, "out_codes", -400), Card("atom", a, "out_codes", -400)]
        self.assertFalse(watch._covered(p, 32, "tb", 2, "out_codes", 1000, cards))
        same = [Card("org", r, "out_codes", -400), Card("org", r, "out_codes", -400)]
        self.assertFalse(watch._covered(p, 32, "tb", 2, "out_codes", 1000, same))

    def test_independent_remainder_is_not_hidden_by_large_org_event(self):
        p, r = self.panel, int(self.panel.orgs[1])
        a = int(p.meta.at[r, "atom"])
        c = Card("org", r, "out_codes", -11_000)
        self.assertFalse(watch._covered(p, 32, "atom", a, "out_codes", 15_000, [c],
                                        score=14_000, minimum=100))
        self.assertTrue(watch._covered(p, 32, "atom", a, "out_codes", 11_500, [c],
                                       score=10_500, minimum=100))

    def test_loader_keeps_historical_locations(self):
        months = ["2026-08-31", "2026-09-30"]
        rows = []
        for month in months:
            for gosb in (100, 101):
                row = dict.fromkeys(inputs.MEAS, 0.0)
                row.update(report_dt=month, seg_rep="Сегмент", sys_tb_id=1,
                           sys_gosb_id=gosb, pf_recip=1000.)
                rows.append(row)
        orgs = pd.DataFrame([rows[0], rows[3]]).assign(org_id=1)
        p = inputs.from_frames(months, pd.DataFrame(rows), orgs)
        r = int(p.orgs[0])
        np.testing.assert_array_equal(p.org_scopes["atom"][r], [0, 1])
        self.assertEqual(p.meta.at[r, "atom"], 1)
        c = Card("org", r, "out_codes", -1000)
        self.assertTrue(watch._contained(p, 0, c, "atom", 0, "out_codes"))
        self.assertFalse(watch._contained(p, 0, c, "atom", 1, "out_codes"))

    def test_previous_month_scope_matters_for_returns(self):
        p, r = self.panel.copy(), int(self.panel.orgs[1])
        a = int(p.meta.at[r, "atom"])
        c = Card("org", r, "in_outside", 1000)
        p.x["m_out_temp"][r, -2] = 50
        p.org_scopes["atom"][r, -2] = 0
        self.assertFalse(watch._contained(p, 32, c, "atom", a, "in_outside"))
        self.assertTrue(watch._contained(p, 32, c, "atom", a, "out_codes"))

    def test_frame_shows_raw_fact_and_comparable_expectation(self):
        p = self.panel.copy()
        p.x["m_in_from_outside"][0, -1] = 120
        c = Card("atom", 0, "in_outside", -30, obs=70, exp=100, score=20)
        row = watch.cards_frame(p, [c], 32).iloc[0]
        self.assertEqual(row["факт"], 120)
        self.assertEqual(row["ожидание"], 150)
        self.assertEqual(row["отклонение вклада в портфель"], -30)
        self.assertIn("меньше ожидания", row["что"])
        self.assertIn("месяц", watch.cards_frame(p, [], 32).columns)

    def test_copy_and_injection_preserve_scope_safety(self):
        p, r = self.panel, int(self.panel.orgs[1])
        old = p.org_scopes["atom"].copy()
        dst = int(p.meta.at[r, "atom"]) + 1
        q = bench.inject(p, bench.Event("recode", 32, 1000, row=r, row2=dst))
        self.assertEqual(q.org_scopes["atom"][r, -1], -1)
        self.assertEqual(q.org_scopes["tb"][r, -1], 2)
        np.testing.assert_array_equal(p.org_scopes["atom"], old)
        self.assertEqual(q.org_flows.at[(r, 32, dst), "in_other"], 1000)
        self.assertNotIn((r, 32, dst), p.org_flows.index)

    def test_recode_uses_actual_cells_and_leaves_unrelated_cell_alone(self):
        p, r = self.panel, int(self.panel.orgs[1])
        src = int(p.meta.at[r, "atom"])
        dst = src + 1
        q = bench.inject(p, bench.Event("recode", 32, 10_000, row=r, row2=dst))
        old_flows = q.org_flows.copy()
        cards = watch.detect(q, 32)
        recodes = [c for c in cards if c.level == "org" and c.key == r and c.label == "recode"]
        self.assertEqual(len(recodes), 1)
        self.assertFalse(any(c.level == "atom" and c.key == src and c.measure == "out_other"
                             and c.label not in bench.ARTIFACTS for c in cards))
        self.assertFalse(any(c.level == "atom" and c.key == dst and c.measure == "in_other"
                             and c.label not in bench.ARTIFACTS for c in cards))
        c = recodes[0]
        d = c.info["explains"]["in_other"]
        self.assertGreater(watch._recode_part(q, 32, c, "atom", dst, "in_other", d), 9000)
        # Недобор относительно ожидания в старой ячейке не уменьшает приход в новую.
        self.assertEqual(watch._recode_part(q, 32, c, "atom", dst, "in_other", d - 500), 10_000)
        self.assertEqual(watch._recode_part(q, 32, c, "atom", dst + 1, "in_other", d), 0)
        self.assertEqual(bench._extra_cards(cards, watch.detect(p, 32)), 0)
        pd.testing.assert_frame_equal(q.org_flows, old_flows)
        # Независимый приход в ту же принимающую ячейку нельзя спрятать за сменой ГОСБ.
        q.x["m_in_from_other_org"][dst, 32] += 4000
        q.x["pf_recip"][dst, 32] += 4000
        extra = watch.detect(q, 32)
        self.assertTrue(any(c.level == "atom" and c.key == dst and c.measure == "in_other"
                            and c.label not in bench.ARTIFACTS for c in extra))
        # Даже если независимый дополнительный приход относится к той же организации,
        # узкая погрешность источника не должна объяснить лишние 4000 человек.
        q.x["m_in_from_other_org"][r, 32] += 4000
        q.x["pf_recip"][r, 32] += 4000
        q.x["pf_fl"][r, 32] += 4000
        q.org_flows.at[(r, 32, dst), "in_other"] += 4000
        own_extra = watch.detect(q, 32)
        self.assertTrue(any(c.level == "atom" and c.key == dst and c.measure == "in_other"
                            and c.label not in bench.ARTIFACTS for c in own_extra))

    def test_recode_allows_source_estimation_error_without_inventing_arrival(self):
        p, r = self.panel.copy(), int(self.panel.orgs[1])
        src = int(p.meta.at[r, "atom"])
        for t in range(20, 32):
            out = 320 + (-100, -60, -20, 20, 60, 100)[(t - 20) % 6]
            delta = out - 20
            p.org_flows.at[(r, t, src), "out_other"] = out
            for row in (r, src):
                p.x["m_out_to_other_org"][row, t] += delta
                p.x["m_in_from_outside"][row, t] += delta
        q = bench.inject(p, bench.Event("recode", 32, 2000, row=r, row2=src + 1))
        evidence = next(c for c in watch._cell_recodes(q, 32) if c.key == r)
        self.assertEqual(evidence.info["cell_explains"]["in_other"][src + 1], 2000)
        cards = watch.detect(q, 32)
        self.assertFalse(any(c.level == "atom" and c.key == src + 1 and c.measure == "in_other"
                             and c.label not in bench.ARTIFACTS for c in cards))

    def test_calendar_correction_keeps_local_dip(self):
        p, t = self.panel, 31
        q = bench.inject(p, bench.Event("calendar", t, 0, q=.005))
        np.testing.assert_allclose(watch.flow_obs(q, "out_left")[:, t],
                                   watch.flow_obs(p, "out_left")[:, t])
        r = int(p.orgs[1])
        local = bench.inject(p, bench.Event("dip", t, 1000, row=r))
        self.assertEqual(watch.calendar_component(local)[:, t].sum(), 0)
        self.assertEqual(watch.flow_obs(local, "out_left")[r, t] - p.x["m_out_left_bank"][r, t], 1000)

    def test_independent_code_event_does_not_hide_recode_evidence(self):
        p, r = self.panel, int(self.panel.orgs[1])
        src = int(p.meta.at[r, "atom"])
        q = bench.inject(p, bench.Event("recode", 32, 1000, row=r, row2=src + 1))
        q = bench.inject(q, bench.Event("codes", 32, 15_000, row=r))
        cards = watch.detect(q, 32)
        self.assertTrue(any(c.level == "org" and c.key == r and c.measure == "out_codes" for c in cards))
        self.assertFalse(any(c.level == "atom" and c.key in (src, src + 1)
                             and c.measure in ("out_other", "in_other")
                             and c.label not in bench.ARTIFACTS for c in cards))

    def test_future_stock_does_not_change_current_detection(self):
        p, t = self.panel, 29
        q = p.copy()
        q.x["pf_recip"][p.orgs, t + 1:] *= 100
        np.testing.assert_array_equal(watch._groups(p, p.orgs, t), watch._groups(q, q.orgs, t))
        obs = {f: watch.flow_obs(p, f) for f in watch.FLOWS}
        left = watch.scan_rows(p, p.orgs, t, obs)
        right = watch.scan_rows(q, q.orgs, t, obs)
        for f in watch.FLOWS:
            np.testing.assert_array_equal(left[f]["s"], right[f]["s"])
            np.testing.assert_array_equal(left[f]["E"], right[f]["E"])

    def test_local_recode_is_not_blocked_by_noisy_whole_org(self):
        p = self.panel.copy()
        r, branch = int(p.orgs[0]), 15
        p.x["m_out_codes"][r, -1] = 20
        p.org_flows.at[(r, 32, 0), "out_codes"] = 12.
        p.org_flows.at[(r, 32, 1), "out_codes"] = 8.
        p.x["m_out_codes"][:2, -1] = 200
        for m in ("pf_recip", "pf_fl", "m_base_pf"):
            p.x[m][r] += 1_000_000
            p.x[m][branch] += 1_000_000
        for t in range(len(p.months)):
            value = 2000 + (t % 5) * 20_000
            for f, col in (("in_other", "m_in_from_other_org"), ("out_other", "m_out_to_other_org")):
                p.x[col][r, t] += value
                p.x[col][branch, t] += value
                key = (r, t, branch)
                if key not in p.org_flows.index:
                    p.org_flows.loc[key, :] = 0.
                p.org_flows.at[key, f] = value
            p.org_stock.loc[(r, t, branch), :] = [1_000_000., 1_000_000., 1_000_000., 0.]
        p.org_flows.sort_index(inplace=True)
        p.org_stock.sort_index(inplace=True)
        for scope in p.org_scopes.values():
            scope[r] = -1
        q = bench.inject(p, bench.Event("recode", 32, 1000, row=r, row2=1,
                                        extra={"source_atom": 0}))
        evidence = watch._cell_recodes(q, 32)
        self.assertTrue(any(c.key == r for c in evidence))
        cards = watch.detect(q, 32)
        self.assertFalse(any(c.level == "atom" and c.key in (0, 1)
                             and c.measure in ("out_other", "in_other")
                             and c.label not in bench.ARTIFACTS for c in cards))


if __name__ == "__main__":
    unittest.main()
