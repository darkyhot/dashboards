"""Один прогон должен сохранять полные результаты даже при непрохождении критериев."""
import json
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from ptf import acceptance, bench, watch
from test_watch import split_org_panel
from test_artifact_cells import heterogeneous_cells


class AcceptanceTests(unittest.TestCase):
    def test_completed_events_survive_artifact_failure_without_replay(self):
        p = heterogeneous_cells()
        detector, artifact = watch.detect, bench._inject_artifact

        def crash_on_artifact(panel, t):
            if getattr(panel, "failed_artifact", False):
                raise ValueError("Проверяем остановку внутри артефакта")
            return detector(panel, t)

        crash_on_artifact.__module__ = watch.__name__

        def broken_artifact(panel, event):
            result = artifact(panel, event)
            result.failed_artifact = True
            return result

        with patch.object(watch, "detect", crash_on_artifact), tempfile.TemporaryDirectory() as folder:
            signature = acceptance.method_signature()
            with patch.object(bench, "_inject_artifact", broken_artifact):
                try:
                    acceptance.run(p, out_dir=folder, compare=False, max_details=0, progress=None,
                        kinds=("codes",), per_cell=1, people_sizes=(), per_size=0)
                except ValueError as error:
                    traceback = error.__traceback__
                else:
                    self.fail("Ожидался сбой внутри артефакта")
            with patch("sys.last_traceback", traceback, create=True):
                recovered = acceptance.recover_artifact_failure(signature)
            self.assertIsNotNone(recovered)
            with patch.object(bench, "inject", wraps=bench.inject) as inject:
                result = acceptance.run(recovered["panel"], out_dir=folder, compare=False,
                    max_details=0, progress=None, completed=recovered["completed"], **recovered["params"])
            self.assertTrue(all(call.args[1].kind in bench.ARTIFACTS for call in inject.call_args_list))
            pd.testing.assert_frame_equal(result["result"]["recall"], recovered["completed"]["recall"])
            self.assertEqual(result["data"]["recovered_stages"], ["background", "events", "echo"])

    def test_recovery_is_rejected_if_selection_rule_changed(self):
        signature = acceptance.method_signature()
        with patch.object(watch, "MIN_PEOPLE", watch.MIN_PEOPLE + 1):
            self.assertIsNone(acceptance.recover_artifact_failure(signature))

    def test_saved_report_contains_metrics_cards_diagnostics_and_file_versions(self):
        p = split_org_panel()
        before = p.copy()
        with tempfile.TemporaryDirectory() as folder:
            run = acceptance.run(p, out_dir=folder, compare=True, max_details=1,
                                 progress=None, kinds=("codes",), per_cell=1,
                                 people_sizes=(1000,), per_size=1)
            saved = json.loads(run["paths"]["json"].read_text())
            page = run["paths"]["html"].read_text()
            self.assertEqual(saved["protocol"], 3)
            self.assertEqual(len(saved["recall"]), len(run["result"]["recall"]))
            self.assertEqual(len(saved["cards"]), len(run["data"]["cards"]))
            self.assertIn("baseline_checks", saved)
            self.assertIn("reason_partition", saved)
            self.assertTrue(all(row["полностью размечены причины"] == 0 for row in saved["reason_partition"]))
            self.assertIn("recode", saved["artifacts"])
            self.assertEqual(len(saved["files"]["watch.py"]), 64)
            self.assertIn("Разбор пропущенных событий", page)
            self.assertIn("Версии файлов", page)
            self.assertNotIn('<script src=', page)
            self.assertFalse(any("ложных карточек" in x["проверка"] for x in saved["checks"]))
        for name in p.x:
            np.testing.assert_array_equal(p.x[name], before.x[name])
        pd.testing.assert_frame_equal(p.org_stock, before.org_stock)


if __name__ == "__main__":
    unittest.main()
