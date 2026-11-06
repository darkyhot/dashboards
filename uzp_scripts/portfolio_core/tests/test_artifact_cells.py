"""Артефакты: неодинаковый состав совместителей и смена ячеек организации."""
import unittest
import warnings

import numpy as np
import pandas as pd

from ptf import bench, inputs


def heterogeneous_cells():
    months = bench.sim_months()
    atoms, organizations = [], []
    for month in months:
        for tb, pf, multi in ((1, 1000., 7.), (2, 1000., 0.)):
            a = dict.fromkeys(inputs.MEAS, 0.)
            a.update(report_dt=month, seg_rep="Сегмент", sys_tb_id=tb, sys_gosb_id=tb,
                     pf_recip=pf, pf_fl=pf - multi / 2, multi_recip=multi,
                     m_base_pf=pf, single_pay_recip=pf * .2, raw_rows=pf * 1.4)
            atoms.append(a)
            o = a.copy()
            pf_org = 100. if tb == 1 else 900.
            o.update(org_id=1, pf_recip=pf_org, pf_fl=pf_org - multi / 2,
                     m_base_pf=pf_org, single_pay_recip=pf_org * .2, raw_rows=pf_org * 1.4)
            organizations.append(o)
    return inputs.from_frames(months, pd.DataFrame(atoms), pd.DataFrame(organizations))


class ArtifactCellTests(unittest.TestCase):
    def test_calendar_preserves_heterogeneous_cell_composition(self):
        p, t = heterogeneous_cells(), 30
        before = p.copy()
        with warnings.catch_warnings():
            warnings.simplefilter("error", pd.errors.PerformanceWarning)
            q = bench.inject(p, bench.Event("calendar", t, 0, q=.005))
        tail = inputs.residual_panel(q)
        r = int(p.orgs[0])
        self.assertAlmostEqual(q.x["multi_recip"][0, t], 7 * .995)
        self.assertAlmostEqual(q.org_stock.at[(r, t, 0), "multi_recip"], 7 * .995)
        self.assertEqual(tail.x["multi_recip"][0, t], 0.)
        for h in (t, t + 1):
            np.testing.assert_allclose(q.identity_gap(h), 0., atol=1e-6)
            np.testing.assert_allclose(tail.identity_gap(h), 0., atol=1e-6)
        for m in p.x:
            np.testing.assert_array_equal(p.x[m], before.x[m])
        pd.testing.assert_frame_equal(p.org_stock, before.org_stock)
        pd.testing.assert_frame_equal(p.org_flows, before.org_flows)

    def test_underload_only_removes_local_fractional_people_and_multiple_jobs(self):
        p, t = heterogeneous_cells(), 32
        q = bench.inject(p, bench.Event("underload", t, 0, tb=1, q=.08))
        tail = inputs.residual_panel(q)
        r = int(p.orgs[0])
        self.assertAlmostEqual(q.x["multi_recip"][r, t], 7 * .92)
        self.assertAlmostEqual(q.org_stock.at[(r, t, 0), "pf_fl"], 96.5 * .92)
        self.assertAlmostEqual(q.x["pf_fl"][r, t], 96.5 * .92 + 900.)
        self.assertEqual(q.x["pf_recip"][1, t], p.x["pf_recip"][1, t])
        np.testing.assert_allclose(q.identity_gap(t), 0., atol=1e-6)
        np.testing.assert_allclose(tail.identity_gap(t), 0., atol=1e-6)
        self.assertTrue((tail.x["multi_recip"] >= 0).all())

    def test_calendar_does_not_return_cohort_to_absent_next_month_cell(self):
        p, t = heterogeneous_cells(), 30
        r = int(p.orgs[0])
        removed = p.org_stock.loc[(r, t + 1, 0)].copy()
        p.org_stock = p.org_stock.drop((r, t + 1, 0))
        p.org_flows = p.org_flows.drop((r, t + 1, 0))
        for measure in p.org_stock.columns:
            p.x[measure][r, t + 1] -= removed[measure]
        before = p.org_flows.copy()
        with warnings.catch_warnings():
            warnings.simplefilter("error", pd.errors.PerformanceWarning)
            q = bench.inject(p, bench.Event("calendar", t, 0, q=.005))
        self.assertNotIn((r, t + 1, 0), q.org_flows.index)
        self.assertEqual(q.org_stock.at[(r, t, 0), "pf_recip"], 100.)
        for h in (t, t + 1):
            np.testing.assert_allclose(q.identity_gap(h), p.identity_gap(h), atol=1e-6)
        inputs.residual_panel(q)
        pd.testing.assert_frame_equal(p.org_flows, before)


if __name__ == "__main__":
    unittest.main()
