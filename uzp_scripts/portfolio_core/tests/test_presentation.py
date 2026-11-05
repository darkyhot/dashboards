"""Номера организаций не попадают в экран/артефакт; просмотр не запускает стенд."""
import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from ptf import acceptance, bench, diagnostics, presentation, watch
from test_watch import split_org_panel


class PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = split_org_panel()
        cls.panel.meta.loc[cls.panel.orgs, "org_id"] = 1_000_000_001 + np.arange(len(cls.panel.orgs))

    def test_mask_covers_legacy_tables_and_preserves_numbers_and_input(self):
        p = self.panel
        r = int(p.orgs[1])
        number = int(p.meta.at[r, "org_id"])
        frame = pd.DataFrame({"где": [f"организация {number} (ГОСБ 200)"],
                              "org_id": [number], "факт": [12_345_678_901.25]})
        source = {"cards": frame, "detail": [{"показатель": "id орг", "значение": number}],
                  "links": {"from_org_id": number, "to_org_id": number + 1}}
        result = presentation.mask(source, p)
        alias = presentation.org_code(r)
        self.assertIn(alias, result["cards"].iloc[0]["где"])
        self.assertEqual(result["cards"].iloc[0]["org_id"], alias)
        self.assertEqual(result["detail"][0]["значение"], alias)
        self.assertEqual(result["links"]["from_org_id"], alias)
        self.assertNotEqual(result["links"]["from_org_id"], result["links"]["to_org_id"])
        self.assertEqual(result["cards"].iloc[0]["факт"], 12_345_678_901.25)
        self.assertEqual(source["cards"].iloc[0]["org_id"], number)
        self.assertEqual(source["detail"][0]["значение"], number)
        self.assertNotIn(str(number), json.dumps(acceptance._plain(result), ensure_ascii=False))

    def test_reorganization_receiver_photo_uses_incoming_flow(self):
        ev = bench.Event("reorg", 0, 3000, row=1, row2=2)
        data = {"input": {"months": ["2026-09-30"]}, "missed_details": [{
            "event": ev, "target": 2, "size_sigma": 6., "where": "организация 1000000001",
            "table": pd.DataFrame([{"когда": "после", "поток": "out_other", "факт": 12.},
                                   {"когда": "после", "поток": "in_other", "факт": 3012.}])}]}
        view = presentation.photo_page(data, "пропуски")
        self.assertEqual(view["blocks"][0][1]["Факт"].tolist(), [3012.])
        self.assertNotIn("1000000001", repr(view))

    def test_new_reports_and_photo_pages_are_masked(self):
        p = self.panel.copy()
        before = p.meta.copy()
        with tempfile.TemporaryDirectory() as folder:
            result = acceptance.run(p, folder, compare=True, max_details=1, progress=None,
                                    kinds=("codes",), per_cell=1, people_sizes=(1000,), per_size=1)
            raw = result["paths"]["json"].read_text()
            html = result["paths"]["html"].read_text()
            for number in p.meta.loc[p.orgs, "org_id"]:
                self.assertNotIn(str(number), raw)
                self.assertNotIn(str(number), html)
            saved = json.loads(raw)
            for data in (result["data"], saved):
                for section in ("итог", "карточки", "пропуски", "календарь", "качество"):
                    first = presentation.photo_page(data, section, panel=p)
                    for page in range(1, first["pages"] + 1):
                        view = presentation.photo_page(data, section, page, panel=p)
                        self.assertTrue(all(len(frame) <= 12 for _, frame in view["blocks"]))
            with patch.object(watch, "detect", side_effect=AssertionError("Просмотр запустил стенд")):
                shown, output = [], io.StringIO()
                with contextlib.redirect_stdout(output):
                    presentation.show(saved, shown.append, section="карточки", panel=p)
                self.assertTrue(shown)
                for number in p.meta.loc[p.orgs, "org_id"]:
                    self.assertNotIn(str(number), output.getvalue() + repr(shown))
            with self.assertRaises(ValueError):
                presentation.photo_page(saved, "карточки", 0)
            with self.assertRaises(ValueError):
                presentation.photo_page(saved, "карточки", 999)
        pd.testing.assert_frame_equal(before, p.meta)

    def test_threshold_trace_does_not_change_detection(self):
        p, t = self.panel.copy(), 32
        for i, r in enumerate(p.orgs[:4]):
            p.x["m_out_codes"][r, 16 + i:23 + i:6] = 5000
        obs = {f: watch.flow_obs(p, f) for f in watch.FLOWS}
        plain = watch.scan_rows(p, p.orgs, t, obs)
        detailed = watch.scan_rows(p, p.orgs, t, obs, details=True)
        self.assertEqual(watch.pick(plain, p.orgs), watch.pick(detailed, p.orgs))
        for flow in watch.FLOWS:
            for field in plain[flow]:
                np.testing.assert_array_equal(plain[flow][field], detailed[flow][field])
            trace = detailed[flow]["calibration"]
            self.assertAlmostEqual(detailed[flow]["zs"], max(trace["normal_z"], trace["empirical_z"]))
            contributors = trace["contributors"]
            if trace["empirical_z"] > trace["normal_z"]:
                selected = next(r for r in contributors if r["задаёт порог"])
                self.assertAlmostEqual(selected["модуль z"] + 1e-9, trace["empirical_z"])
        self.assertTrue(detailed["out_codes"]["calibration"]["contributors"])

    def test_snapshot_runs_one_scan_in_memory(self):
        p = self.panel
        data = {"missed_details": [{"event": bench.Event("hire", 29, 3000, row=int(p.orgs[1])),
                 "target": int(p.orgs[1]), "measure": "m_in_from_outside",
                 "table": [{"когда": "после", "поток": "in_outside", "порог z": 1000.}]}]}
        with patch.object(watch, "scan_rows", wraps=watch.scan_rows) as scan:
            snapshot = diagnostics.threshold_snapshot(p, data)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(snapshot["month"], p.months[29])
        self.assertEqual(snapshot["level"], "org")
        self.assertEqual(len(snapshot["summary"]), len(watch.FLOWS))
        self.assertEqual(snapshot["sources"][0]["flow"], "in_outside")
        for page in (1, 2):
            view = presentation.photo_page({"threshold_snapshot": snapshot}, "пороги", page)
            self.assertTrue(view["blocks"])
        other = {"input": {"months": ["2000-01-31"], "atoms": len(p.atoms), "organizations": len(p.orgs)}}
        with self.assertRaisesRegex(ValueError, "разным поколениям"):
            diagnostics.threshold_snapshot(p, other)


if __name__ == "__main__":
    unittest.main()
