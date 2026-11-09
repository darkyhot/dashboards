"""Разбор слабых целей: те же цели, что в приёмке; запас — тем же отбором, что у сторожа."""
import json
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from ptf import acceptance, bench, presentation, watch
from test_watch import split_org_panel


def all_weak(r):
    """Делаем все цели «слабыми»: на маленькой панели точных 3 шумов может не быть."""
    big, _ = ACCEPTANCE_GROUPS(r)
    return big, pd.Series(True, index=r.index)


ACCEPTANCE_GROUPS = bench.acceptance_groups


class WeakMissesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = split_org_panel()
        cls.panel.meta.loc[cls.panel.orgs, "org_id"] = 1_000_000_001 + np.arange(len(cls.panel.orgs))
        cls.folder = tempfile.TemporaryDirectory()
        cls.report = acceptance.run(cls.panel, out_dir=cls.folder.name, compare=False, max_details=1,
                                 progress=None, kinds=("codes", "hire"), per_cell=1,
                                 people_sizes=(1000,), per_size=1)
        # На маленькой панели все цели находятся; часть помечаем пропущенной, чтобы
        # проверить и подробные страницы. Разбор берёт флаг found из recall.
        recall = cls.report["result"]["recall"].copy()
        recall.loc[recall.index[::2], "found"] = False
        cls.report = dict(cls.report, result=dict(cls.report["result"], recall=recall))
        saved = json.loads(cls.report["paths"]["json"].read_text())
        saved["recall"] = json.loads(recall.to_json(orient="records", force_ascii=False))
        cls.saved = {"data": saved}

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_groups_match_verdict(self):
        r = self.report["result"]["recall"]
        big, mid = bench.acceptance_groups(r)
        checks = {name: detail for name, _, detail in bench.verdict(self.report["result"])}
        self.assertTrue(checks["полнота: ≥ 4 шумов и ≥ 2 тыс. чел."].endswith(f"из {int(big.sum())}")
                        or not big.any())
        self.assertIn(f"из {int(mid.sum())}", checks["полнота: 3 шума, от 300 чел."] if mid.any() else "из 0")

    def test_memory_and_saved_report_give_same_review_without_detector(self):
        with patch.object(bench, "acceptance_groups", all_weak), \
                patch.object(watch, "detect", side_effect=AssertionError("Разбор запустил сторож")):
            memory = acceptance.weak_misses(self.panel, self.report, progress=None)
            saved = acceptance.weak_misses(self.panel, self.saved, progress=None)
        r = self.report["result"]["recall"]
        self.assertEqual(len(memory["overview"]), len(r))
        self.assertEqual(len(memory["details"]), int((~r["found"]).sum()))
        self.assertGreater(len(memory["details"]), 0)
        pd.testing.assert_frame_equal(memory["overview"], saved["overview"])
        raw = json.dumps(acceptance._plain(memory), ensure_ascii=False)
        for number in self.panel.meta.loc[self.panel.orgs, "org_id"]:
            self.assertNotIn(str(number), raw)

    def test_margin_is_the_detector_excess(self):
        r = self.report["result"]["recall"]
        row = r.iloc[0]
        ev = acceptance._event_of(self.report["result"]["plan"], row)
        target = int(row["row"])
        scan = acceptance._target_scan(self.panel, ev, target, {})
        q = bench.inject(self.panel, ev)
        rows = self.panel.orgs if self.panel.meta.at[target, "lvl"] == "org" else self.panel.atoms
        i = int(np.flatnonzero(rows == target)[0])
        scans = watch.scan_rows(q, rows, ev.t, {f: watch.flow_obs(q, f) for f in watch.FLOWS})
        after = scan["table"].query("когда == 'после'").set_index("поток")
        for flow, s in scans.items():
            expected = watch.excess(s)[i]
            if np.isfinite(expected):
                self.assertAlmostEqual(after.at[flow, "сверх шума"], round(float(expected), 2), places=2)
        picked = {rr: f for rr, f, *_ in watch.pick(scans, rows)}.get(target)
        self.assertEqual(after.index[after["выбран"]].tolist(), [picked] if picked else [])

    def test_photo_pages_fit_and_cover_every_miss(self):
        with patch.object(bench, "acceptance_groups", all_weak):
            weak = acceptance.weak_misses(self.panel, self.report, progress=None)
        data = {"weak_misses": weak}
        first = presentation.photo_page(data, "слабые", panel=self.panel)
        self.assertEqual(first["pages"], max(1, -(-len(weak["overview"]) // 8)) + len(weak["details"]))
        for page in range(1, first["pages"] + 1):
            view = presentation.photo_page(data, "слабые", page, panel=self.panel)
            self.assertTrue(all(len(frame) <= 12 for _, frame in view["blocks"]))
        with self.assertRaises(ValueError):
            presentation.photo_page({}, "слабые")

    def test_saved_report_of_other_generation_is_rejected(self):
        other = {"data": dict(self.saved["data"], input=dict(self.saved["data"]["input"], run_id="другое"))}
        self.panel.run_id = "это"
        try:
            with self.assertRaisesRegex(ValueError, "разным поколениям"):
                acceptance.weak_misses(self.panel, other, progress=None)
        finally:
            self.panel.run_id = None


if __name__ == "__main__":
    unittest.main()
