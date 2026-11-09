"""Диагностика должна воспроизводить случай полного стенда без его повторного прогона."""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from ptf import bench, diagnostics, watch
from test_watch import split_org_panel


class DiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = split_org_panel()

    def test_replay_matches_evaluate_draws(self):
        for params in ({}, {"seed": 19, "per_cell": 3, "months_back": 3,
                           "sizes": (3., 6.), "kinds": ("hire", "reorg", "stop")}):
            captured = []

            def capture(panel, event):
                if event.kind == "recode":
                    captured.append(event)
                return panel  # проверяем жеребьёвку, детектор здесь намеренно пустой

            with patch.object(bench, "inject", side_effect=capture):
                bench.evaluate(lambda panel, t: [], self.panel, **params)
            self.assertEqual(diagnostics.recode_event(self.panel, **params), captured[0])

    def test_report_only_runs_two_detections_and_preserves_panel(self):
        p = self.panel.copy()
        original = p.copy()
        with patch.object(watch, "detect", wraps=watch.detect) as detector:
            report = diagnostics.recode_report(p, per_cell=2)
            self.assertEqual(detector.call_count, 2)
        self.assertEqual(len(report), 4)
        scenario, scans, org_card, extra = list(report.values())
        facts = scenario.set_index("показатель")["значение"]
        self.assertEqual(facts["лишних по правилу стенда"], len(extra))
        self.assertEqual(len(scans), 2 * len(watch.FLOWS))
        for name in p.x:
            np.testing.assert_array_equal(p.x[name], original.x[name])
        for name in p.org_scopes:
            np.testing.assert_array_equal(p.org_scopes[name], original.org_scopes[name])
        pd.testing.assert_frame_equal(p.org_flows, original.org_flows)


if __name__ == "__main__":
    unittest.main()
