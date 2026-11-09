"""Вариант верхней границы и тихие NaN-свёртки: принятая версия не меняется."""
import unittest
import warnings

import numpy as np

from ptf import bench, count_model, watch
from test_watch import split_org_panel


class UpperBoundTests(unittest.TestCase):
    def setUp(self):
        self.saved = count_model.UPPER_BOUND

    def tearDown(self):
        count_model.UPPER_BOUND = self.saved

    def scan(self, variant):
        count_model.UPPER_BOUND = variant
        p = split_org_panel()
        obs = {f: watch.flow_obs(p, f) for f in watch.FLOWS}
        return watch.scan_rows(p, p.orgs, len(p.months) - 1, obs)

    def test_default_is_accepted_log_bound(self):
        self.assertEqual(count_model.UPPER_BOUND, "log")

    def test_hybrid_only_tightens_upper_and_keeps_everything_else(self):
        log, hybrid = self.scan("log"), self.scan("hybrid")
        for flow in watch.FLOWS:
            a, b = log[flow], hybrid[flow]
            finite = np.isfinite(a["upper"])
            self.assertTrue(np.all(b["upper"][finite] <= a["upper"][finite] + 1e-9))
            ok = np.isfinite(a["E"])
            self.assertTrue(np.all(b["upper"][ok] >= a["E"][ok] - 1e-9))
            bound = a["E"] + a["zs"] * a["s"]
            np.testing.assert_allclose(b["upper"][ok], np.maximum(np.fmin(a["upper"][ok], bound[ok]), a["E"][ok]))
            for field in ("lower", "E", "s", "zs", "obs", "base"):
                np.testing.assert_array_equal(a[field], b[field])


class QuietNanTests(unittest.TestCase):
    def test_empty_slice_gives_nan_without_warning_and_other_warnings_stay(self):
        data = np.array([[np.nan, np.nan], [1., 3.]])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            np.testing.assert_array_equal(bench.quiet_nan(np.nanmin, data, axis=1), [np.nan, 1.])
            np.testing.assert_array_equal(bench.quiet_nan(np.nanmean, data, axis=1), [np.nan, 2.])
            with self.assertRaises(RuntimeWarning):
                bench.quiet_nan(lambda: warnings.warn("другое", RuntimeWarning))



class TailShapeTests(unittest.TestCase):
    def test_tail_shape_runs_on_panel_and_fits_photo(self):
        from unittest.mock import patch
        from ptf import diagnostics, presentation

        p = split_org_panel()
        with patch.object(watch, "detect", side_effect=AssertionError("Разбор запустил сторож")):
            shape = diagnostics.tail_shape(p)
        frame = shape["table"]
        self.assertEqual(len(frame), 2 * len(watch.FLOWS))
        self.assertTrue((frame["точек"] >= 0).all())
        self.assertTrue((frame["лишних отборов/мес"] >= 0).all())
        view = presentation.photo_page({"tail_shape": shape}, "хвосты")
        self.assertTrue(all(len(f) <= 12 for _, f in view["blocks"]))
        with self.assertRaises(ValueError):
            presentation.photo_page({}, "хвосты")


if __name__ == "__main__":
    unittest.main()
