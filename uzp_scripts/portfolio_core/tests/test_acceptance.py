"""Один прогон должен сохранять полные результаты даже при непрохождении критериев."""
import json
import tempfile
import unittest

import numpy as np
import pandas as pd

from ptf import acceptance
from test_watch import split_org_panel


class AcceptanceTests(unittest.TestCase):
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
