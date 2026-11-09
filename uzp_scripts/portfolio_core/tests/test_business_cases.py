"""C01: единицы, неполные меры и просмотр без повторного стенда."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from ptf import acceptance, bench, build, business, config, db, diagnostics, inputs, presentation, watch


def secondary_job_gap():
    months = bench.sim_months()
    meta = pd.DataFrame({"lvl": ["atom", "atom", "org", "org"],
        "org_id": [-1, -1, 1_000_000_001, 1_000_000_002], "seg_rep": ["A", "B", "A", "B"],
        "sys_tb_id": [1] * 4, "sys_gosb_id": [100, 101, 100, 101], "atom": [0, 1, 0, 1]})
    x = {m: np.zeros((4, len(months))) for m in inputs.MEAS}
    august, july, september = [next(i for i, d in enumerate(months) if d.startswith(m))
                               for m in ("2026-08", "2026-07", "2026-09")]
    x["pf_recip"][:] = 1.
    x["pf_fl"][:] = .5
    x["multi_recip"][:] = 1.
    x["single_pay_recip"][1] = 1.
    x["single_pay_recip"][1, [july, august]] = 0.
    x["pf_recip"][1, august] = 0.
    x["pf_fl"][:2, august] = [1., 0.]
    x["multi_recip"][:2, august] = 0.
    x["m_base_pf"][:] = 1.
    x["m_base_pf"][1, september] = 0.
    x["m_out_to_other_org"][1, august] = 1.
    x["m_in_from_other_org"][1, september] = 1.
    x["m_out_temp"][1, august] = 1.
    x["m_out_temp"][:, -1] = np.nan
    x["m_fl_conc"][:2, august] = [.5, -.5]
    x["m_fl_conc"][:2, september] = [-.5, .5]
    for measure in x:
        x[measure][2:] = x[measure][:2]
    p = inputs.Panel(months, meta, x)
    for t in range(len(months)):
        np.testing.assert_allclose(p.identity_gap(t), 0.)
    return p


class BusinessCaseTests(unittest.TestCase):
    def test_secondary_job_does_not_mean_loss_of_person_or_two_extra_jobs(self):
        p = secondary_job_gap()
        before = {k: v.copy() for k, v in p.x.items()}
        with patch.object(watch, "detect", side_effect=AssertionError("Стенд")), \
             patch.object(db, "get_engine", side_effect=AssertionError("БД")):
            case = diagnostics.august_calendar_case(p, year=2026)
        august = case["stocks"].set_index("Месяц").loc["2026-08"]
        self.assertEqual(august["Получатели (тройки)"], 1.)
        self.assertEqual(august["ФЛ (доли)"], 1.)
        self.assertEqual(august["Совмест. (тройки)"], 0.)
        changes = case["stock_changes"].set_index("Месяц").loc["2026-08"]
        self.assertEqual(changes["Получатели (тройки)"], -1.)
        self.assertEqual(changes["ФЛ (доли)"], 0.)
        self.assertEqual(changes["Совмест. (тройки)"], -2.)
        self.assertIn("01.08.2026 — сб", case["calendar"])
        self.assertIn("31.07.2026 — пт", case["calendar"])
        for k, value in p.x.items():
            np.testing.assert_array_equal(value, before[k])

    def test_missing_measure_is_not_a_partial_total_or_zero(self):
        p = secondary_job_gap()
        august = next(i for i, m in enumerate(p.months) if m.startswith("2026-08"))
        p.x["single_pay_recip"][0, august] = np.nan
        case = diagnostics.august_calendar_case(p)
        row = case["indicators"].set_index("Месяц").loc["2026-08"]
        self.assertTrue(np.isnan(row["Одна выплата (тройки)"]))
        issue = case["availability"].query("Мера == 'single_pay_recip'").iloc[0]
        self.assertEqual(issue["Нет значений"], 1)
        last = case["indicators"].set_index("Месяц").loc["2026-09"]
        self.assertTrue(np.isnan(last["Пропуск и возврат в М+1"]))

    def test_absent_period_and_all_pages_are_explicit_and_masked(self):
        p = secondary_job_gap()
        for year in (2026, 2028):
            case = diagnostics.august_calendar_case(p, year=year)
            data = {"calendar_case": case}
            first = presentation.photo_page(data, "кейс-август")
            for page in range(1, first["pages"] + 1):
                view = presentation.photo_page(data, "кейс-август", page)
                self.assertTrue(all(len(f) <= 8 for _, f in view["blocks"]))
            for number in p.meta.loc[p.orgs, "org_id"]:
                self.assertNotIn(str(number), json.dumps(acceptance._plain(case), ensure_ascii=False))
            if year == 2028:
                self.assertTrue(case["stocks"].drop(columns="Месяц").isna().all().all())


class BusinessReportTests(unittest.TestCase):
    def test_exact_return_does_not_subtract_secondary_job_from_outside_arrivals(self):
        p = secondary_job_gap()
        t = p.months.index("2026-09-30")
        self.assertEqual(watch.flow_obs(p, "in_outside")[1, t], 0.)
        p.x["m_in_from_outside"][1, t] = 25.
        p.x["m_in_temp_outside"][1, t] = 10.
        p.x["m_out_temp"][1, t - 1] = 100.
        self.assertEqual(watch.flow_obs(p, "in_outside")[1, t], 15.)
        p.x["m_in_temp_outside"][1, t] = 26.
        with self.assertRaisesRegex(ValueError, "больше общего прихода"):
            watch.flow_obs(p, "in_outside")
        p.x["m_in_temp_outside"][1, t] = np.nan
        self.assertTrue(np.isnan(watch.flow_obs(p, "in_outside")[1, t]))

    def test_old_generation_is_only_replayed_in_readonly_threshold_diagnostics(self):
        p = secondary_job_gap()
        del p.x["m_in_temp_outside"]
        del p.x["m_out_temp_left"]
        with self.assertRaisesRegex(ValueError, "обновлённая сборка"):
            watch.flow_obs(p, "in_outside")
        # Для сравнения со старым JSON нужен прежний расчёт, но не запуск нового сторожа.
        with patch.object(watch, "flow_obs", side_effect=AssertionError("Новый расчёт")):
            snapshot = diagnostics.threshold_snapshot(p, {})
        self.assertTrue(snapshot["legacy_replay"])
        view = presentation.photo_page({"threshold_snapshot": snapshot}, "пороги")
        self.assertTrue(any("только диагностика" in n for n in view["notes"]))

    def test_photo_cell_recovers_old_report_and_generation_without_running_acceptance(self):
        notebook = json.loads((Path(__file__).resolve().parents[1] / "portfolio_core.ipynb").read_text())
        source = next("".join(c["source"]) for c in notebook["cells"]
                      if "СТОРОЖ: РЕЗУЛЬТАТЫ ДЛЯ ФОТОГРАФИРОВАНИЯ" in "".join(c["source"]))
        source = source.replace('PHOTO_SECTION = "итог"', 'PHOTO_SECTION = "пороги"')
        p = secondary_job_gap()
        del p.x["m_in_temp_outside"]
        del p.x["m_out_temp_left"]
        p.run_id = 1
        saved = {"input": {"months": p.months, "atoms": len(p.atoms),
                            "organizations": len(p.orgs), "run_id": 1}}
        shown = []
        scope = {"display": shown.append, "CONN": "postgresql://example/example"}
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "OUTPUT_DIR", Path(tmp)), \
             patch("importlib.reload", side_effect=lambda module: module), \
             patch.object(db, "get_engine"), patch.object(db, "session"), \
             patch.object(inputs, "load", return_value=p) as load, \
             patch.object(build, "run", side_effect=AssertionError("Сборка")), \
             patch.object(bench, "evaluate", side_effect=AssertionError("Стенд")):
            folder = Path(tmp) / "watch_acceptance"
            folder.mkdir()
            (folder / "report.json").write_text(json.dumps(saved))
            exec(compile(source, "notebook_photo", "exec"), scope)
            exec(compile(source, "notebook_photo", "exec"), scope)
            self.assertEqual(load.call_count, 1)
            self.assertEqual(load.call_args.kwargs["run_id"], 1)
            self.assertTrue(scope["res_acceptance"]["threshold_snapshot"]["legacy_replay"])
            self.assertEqual(len(shown), 2)

    def test_finds_internal_reorganization_even_when_bank_and_atom_are_flat(self):
        months = bench.sim_months("2026-01", "2026-04")
        meta = pd.DataFrame({"lvl": ["atom", "org", "org"], "org_id": [-1, 1_000_000_001, 1_000_000_002],
            "seg_rep": ["A"] * 3, "sys_tb_id": [1] * 3, "sys_gosb_id": [100] * 3, "atom": [0] * 3})
        x = {m: np.zeros((3, 4)) for m in inputs.MEAS}
        x["pf_recip"][:] = [[100.] * 4, [40., 40., 10., 10.], [60., 60., 90., 90.]]
        x["pf_fl"][:] = x["pf_recip"]
        x["m_base_pf"][:] = x["pf_recip"]
        x["m_base_pf"][:, 2] = x["pf_recip"][:, 1]
        x["m_out_to_other_org"][[0, 1], 2] = 30.
        x["m_in_from_other_org"][[0, 2], 2] = 30.
        x["m_fl_conc"][[1, 2], 2] = [-30., 30.]
        p = inputs.Panel(months, meta, x)
        report = business.analyze(p)
        self.assertEqual(report["month"][:7], "2026-03")
        self.assertEqual(report["bank_change"], 0.)
        self.assertTrue(report["summary"]["Проверено"].all())
        self.assertEqual(report["organizations"]["Вклад (тройки)"].abs().sum(), 60.)
        self.assertEqual(report["slices"]["segments"]["Вклад (тройки)"].sum(), 0.)
        self.assertEqual(report["history"]["Объём изменений"].max(), 30.)

    def test_finds_secondary_job_gap_and_keeps_bank_people_unchanged(self):
        p = secondary_job_gap()
        before = {k: v.copy() for k, v in p.x.items()}
        with patch.object(watch, "detect", side_effect=AssertionError("Стенд")), \
             patch.object(db, "get_engine", side_effect=AssertionError("БД")), \
             patch.object(watch, "flow_obs", side_effect=AssertionError("Коррекция")):
            report = business.analyze(p)
        self.assertEqual(report["month"][:7], "2026-08")
        summary = report["summary"].set_index("Мера")
        self.assertEqual(summary.at["Получатели (тройки)", "Изменение"], -1.)
        self.assertEqual(summary.at["ФЛ (доли)", "Изменение"], 0.)
        self.assertTrue(summary["Проверено"].all())
        self.assertEqual(report["movements"]["Вклад (тройки)"].sum(), -1.)
        self.assertEqual(report["fractional"]["Вклад (доли ФЛ)"].sum(), 0.)
        self.assertEqual(report["issues"], [])
        for k, value in p.x.items():
            np.testing.assert_array_equal(value, before[k])

    def test_equal_and_opposite_errors_cannot_pass_bank_identity(self):
        p = secondary_job_gap()
        t = p.months.index("2026-07-31")
        p.x["m_in_from_outside"][0, t] = 10.
        p.x["m_out_left_bank"][1, t] = 10.
        report = business.analyze(p, "2026-07")
        total = report["summary"].iloc[0]
        self.assertEqual(total["Невязка"], 0.)
        self.assertFalse(total["Проверено"])
        history = report["history"].set_index("Месяц").loc[p.months[t]]
        self.assertEqual(history["Ошибок тождества в атомах"], 2)

    def test_missing_flow_does_not_become_zero_or_partial_bank_total(self):
        p = secondary_job_gap()
        t = p.months.index("2026-08-31")
        p.x["m_out_to_other_org"][0, t] = np.nan
        report = business.analyze(p, "2026-08")
        self.assertFalse(report["summary"].iloc[0]["Проверено"])
        flow = report["movements"].set_index("Движение").loc["Ушли в другие организации"]
        self.assertTrue(np.isnan(flow["Вклад (тройки)"]))
        self.assertTrue(np.isnan(report["summary"].iloc[0]["Невязка"]))
        self.assertTrue(report["issues"])

    def test_missing_whole_atom_is_not_silently_treated_as_departure(self):
        p = secondary_job_gap()
        t = p.months.index("2026-08-31")
        for value in p.x.values():
            value[1, t] = np.nan
        report = business.analyze(p, "2026-08")
        self.assertTrue(np.isnan(report["summary"].iloc[0]["Стало"]))
        self.assertFalse(report["summary"].iloc[0]["Проверено"])

    def test_resegmentation_is_separate_from_movement_and_zero_total(self):
        p = secondary_job_gap()
        t = p.months.index("2026-07-31")
        p.x["pf_recip"][:2, t] = [0., 2.]
        p.x["m_base_pf"][:2, t] = [0., 2.]
        report = business.analyze(p, "2026-07")
        self.assertTrue(report["summary"]["Проверено"].all())
        segments = report["slices"]["segments"].set_index("Сегмент")
        self.assertEqual(segments.at["A", "Вклад (тройки)"], -1.)
        self.assertEqual(segments.at["B", "Вклад (тройки)"], 1.)
        np.testing.assert_allclose(segments["Вклад (тройки)"], segments["Различие баз"])
        self.assertEqual(report["movements"]["Вклад (тройки)"].sum(), 0.)

    def test_no_duplicate_bank_sum_and_small_slices_remain_in_remainder(self):
        p = secondary_job_gap()
        report = business.analyze(p)
        # Строки организаций дублируют атомы, но банк содержит ровно две тройки до провала.
        self.assertEqual(report["summary"].iloc[0]["Было"], 2.)
        for frame in report["slices"].values():
            small = business.compact(frame, limit=1)
            self.assertEqual(small["Вклад (тройки)"].sum(), -1.)
            self.assertEqual(small["Приход"].sum() - small["Уход"].sum(), -1.)
        organization_view = business.compact(report["organizations"], limit=1, total=-1.)
        self.assertEqual(organization_view["Вклад (тройки)"].sum(), -1.)

    def test_report_html_and_photo_pages_are_masked_and_compact(self):
        p = secondary_job_gap()
        report = business.analyze(p)
        data = {"business": report}
        first = presentation.photo_page(data, "бизнес")
        for page in range(1, first["pages"] + 1):
            view = presentation.photo_page(data, "бизнес", page)
            self.assertTrue(all(len(frame) <= 8 for _, frame in view["blocks"]))
        with tempfile.TemporaryDirectory() as tmp:
            path = business.write_html(report, Path(tmp) / "report.html")
            exported = path.read_text()
            for number in p.meta.loc[p.orgs, "org_id"]:
                self.assertNotIn(str(number), exported)
                self.assertNotIn(str(number), json.dumps(acceptance._plain(report), ensure_ascii=False))
            self.assertIn("Орг. R", exported)

    def test_rejects_missing_neighbor_duplicate_months_and_negative_counts(self):
        p = secondary_job_gap()
        p.months[-1] = "2026-10-31"
        with self.assertRaisesRegex(ValueError, "соседнего"):
            business.analyze(p, "2026-10")
        p.months[-1] = p.months[-2]
        with self.assertRaisesRegex(ValueError, "повторений"):
            business.analyze(p)
        p = secondary_job_gap()
        t = p.months.index("2026-08-31")
        p.x["m_in_from_outside"][0, t] = -100.
        report = business.analyze(p, "2026-08")
        self.assertFalse(report["summary"].iloc[0]["Проверено"])
        self.assertTrue(any("Отрицательные" in message for message in report["issues"]))
        p.x["m_in_from_outside"][0, t] = np.inf
        report = business.analyze(p, "2026-08")
        self.assertFalse(report["summary"].iloc[0]["Проверено"])
        self.assertTrue(np.isnan(report["movements"].iloc[0]["Вклад (тройки)"]))

    def test_notebook_runs_without_acceptance_and_loads_only_if_panel_is_absent(self):
        notebook = json.loads((Path(__file__).resolve().parents[1] / "portfolio_core.ipynb").read_text())
        cells = [c for c in notebook["cells"] if "БИЗНЕС-РАЗБОР ПОРТФЕЛЯ" in "".join(c["source"])]
        self.assertEqual(len(cells), 1)
        source = "".join(cells[0]["source"])
        for have_panel in (True, False):
            p = secondary_job_gap()
            shown = []
            scope = {"display": shown.append, "CONN": "postgresql://example/example"}
            if have_panel:
                scope["panel"] = p
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(config, "OUTPUT_DIR", Path(tmp)), \
                 patch.object(db, "get_engine") as engine, patch.object(db, "session"), \
                 patch.object(inputs, "load", return_value=p) as load, \
                 patch.object(build, "run", side_effect=AssertionError("Сборка")), \
                 patch.object(bench, "evaluate", side_effect=AssertionError("Стенд")):
                exec(compile(source, "notebook_business", "exec"), scope)
                self.assertEqual(load.call_count, 0 if have_panel else 1)
                self.assertEqual(engine.call_count, 0 if have_panel else 1)
                self.assertEqual(scope["business_report"]["month"][:7], "2026-08")
                self.assertTrue((Path(tmp) / "business" / "report.html").exists())
                self.assertEqual(len(shown), 2)


if __name__ == "__main__":
    unittest.main()
