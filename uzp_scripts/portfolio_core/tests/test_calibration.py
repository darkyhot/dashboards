"""Проверки калибровки на редких потоках и неполной истории."""
import unittest
import warnings

import numpy as np
import pandas as pd

from ptf import bench, diagnostics, inputs, watch


def sparse_transfers():
    months = bench.sim_months()
    cells = []
    for month in months:
        for tb in range(8):
            for j in range(10):
                v = dict.fromkeys(inputs.MEAS, 0.)
                v.update(report_dt=month, org_id=1000 + tb * 10 + j,
                         seg_rep="Сегмент", sys_tb_id=tb, sys_gosb_id=100 + tb,
                         pf_recip=10000., pf_fl=10000., m_base_pf=10000., raw_rows=15000.,
                         single_pay_recip=1000., m_in_from_outside=200., m_out_left_bank=200.,
                         m_lb_unknown=200., m_in_codes=50., m_out_codes=50.,
                         m_fl_in=200., m_fl_out=200.,
                         m_in_from_other_org=0. if j == 0 else 100.,
                         m_out_to_other_org=0. if j == 0 else 100.)
                cells.append(v)
    organizations = pd.DataFrame(cells)
    atoms = organizations.groupby(["report_dt", *inputs.ATOM_KEYS])[inputs.MEAS].sum().reset_index()
    p = inputs.from_frames(months, atoms, organizations)
    # Семь настоящих переходов после долгой истории нулей; это не выборка обычного шума.
    for k in range(7):
        p = bench.inject(p, bench.Event("reorg", 18 + k, 1900 + 100 * k,
            row=int(p.orgs[-9 + k]), row2=int(p.orgs[k * 10])))
    for t in range(len(months)):
        np.testing.assert_allclose(p.identity_gap(t), 0., atol=1e-6)
    return p


class CalibrationTests(unittest.TestCase):
    def test_low_mean_transfers_do_not_raise_threshold_for_ordinary_rows(self):
        p, t = sparse_transfers(), 30
        obs = {f: watch.flow_obs(p, f) for f in watch.FLOWS}
        old = watch.scan_rows(p, p.orgs, t, obs, details=True, legacy_replay=True)["in_other"]
        new = watch.scan_rows(p, p.orgs, t, obs, details=True)["in_other"]
        self.assertGreater(old["zs"], 100.)
        self.assertLess(new["zs"], old["zs"] / 10.)
        self.assertGreaterEqual(new["zs"], watch.z_star(len(p.orgs) * len(watch.FLOWS), []))
        self.assertGreaterEqual(new["calibration"]["excluded_count"], 7)
        self.assertTrue(all(r["ожидание"] < watch.MIN_NULL_MEAN
                            for r in new["calibration"]["excluded"]))
        target = int(p.orgs[1])
        q = bench.inject(p, bench.Event("reorg", t, 700, row=int(p.orgs[-1]), row2=target))
        obs = {f: watch.flow_obs(q, f) for f in watch.FLOWS}
        old = watch.scan_rows(q, q.orgs, t, obs, legacy_replay=True)
        new = watch.scan_rows(q, q.orgs, t, obs)
        self.assertNotIn(target, {r for r, *_ in watch.pick(old, q.orgs)})
        self.assertIn(target, {r for r, *_ in watch.pick(new, q.orgs)})

    def test_unknown_history_is_not_replaced_with_unit_noise(self):
        obs = np.full((2, 20), np.nan)
        expected = np.full_like(obs, np.nan)
        base = np.full_like(obs, np.nan)
        obs[:, 19], expected[:, 19], base[:, 19] = 3000., .02, 10000.
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            new = watch.score(obs, expected, base, np.zeros(2), 19)
            old = watch.score(obs, expected, base, np.zeros(2), 19, legacy_replay=True)
        self.assertTrue(np.isnan(new["z"]).all())
        self.assertTrue(np.isnan(new["s"]).all())
        self.assertTrue((new["n_history"] == 0).all())
        self.assertTrue(new["unsupported_material"].all())
        self.assertTrue((old["z"] > 1000.).all())

    def test_empty_medians_preserve_unknowns_without_runtime_warnings(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            for shape, axis in (((3, 0), 1), ((2, 3, 4), 1), ((0, 4), 0)):
                result = watch._nanmedian(np.full(shape, np.nan), axis=axis)
                self.assertTrue(np.isnan(result).all())
            a = np.array([[np.nan, np.nan], [1., 3.]])
            np.testing.assert_allclose(watch._nanmedian(a, axis=1), [np.nan, 2.], equal_nan=True)

    def test_old_generation_comparison_is_masked_and_keeps_old_model_replay(self):
        p = sparse_transfers()
        del p.x["m_in_temp_outside"]
        del p.x["m_out_temp_left"]
        old = diagnostics.threshold_snapshot(p, {})
        self.assertTrue(old["legacy_replay"])
        self.assertEqual(old["calibration_revision"], 2)
        self.assertEqual(len(old["comparison"]), len(watch.FLOWS))
        table = old["comparison"].set_index("Поток")
        # Последний месяц уже вне окна семи событий, сравнение доступно без полной приёмки.
        self.assertTrue(np.isfinite(table["Было z"]).all())
        self.assertTrue(np.isfinite(table["Стало z"]).all())
        self.assertTrue((table["Стало z"] <= table["Было z"]).all())
        for source in old["sources"]:
            self.assertTrue(all(row["ряд"].startswith("Орг. R")
                                for row in source["contributors"].to_dict("records")))


if __name__ == "__main__":
    unittest.main()
