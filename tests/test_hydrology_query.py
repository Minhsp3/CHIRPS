from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
QUERY_APP_DIR = ROOT / "streamlit_deploy" / "truyvan_thuyvan"
sys.path.insert(0, str(QUERY_APP_DIR))

from data_utils import (
    _parse_annual_production_report,
    annual_inflow_projection_table,
    annual_projection_table,
    load_analog_training_workbook,
    merge_daily_hydrology,
    merge_current_calendar_month_actuals,
    merge_monthly_actuals,
    merge_upload_file_sets,
    resolve_production_actual_date,
    seven_day_inflow_generation_projection,
)


class HydrologyQueryTests(unittest.TestCase):
    def test_current_month_annual_total_uses_previous_day_as_cutoff(self) -> None:
        monthly = pd.DataFrame(
            [
                {
                    "year": 2026,
                    "month": 11,
                    "plant": "Buôn Kuốp",
                    "production": 2.3,
                    "flow": None,
                }
            ]
        )

        result = resolve_production_actual_date(
            pd.Timestamp("2026-10-31"),
            monthly,
            pd.DataFrame(columns=["date", "plant", "production"]),
            2026,
            11,
            today=pd.Timestamp("2026-11-02"),
        )

        self.assertEqual(result, pd.Timestamp("2026-11-01"))

    def test_partial_month_projection_adds_analog_remainder(self) -> None:
        rows = []
        for plant in ("Buôn Kuốp", "Buôn Tua Srah", "Srêpốk 3"):
            for month in range(1, 13):
                rows.append(
                    {
                        "year": 2025,
                        "month": month,
                        "plant": plant,
                        "production": 30.0,
                        "flow": 100.0,
                    }
                )
            for month in range(1, 12):
                rows.append(
                    {
                        "year": 2026,
                        "month": month,
                        "plant": plant,
                        "production": 3.0 if month == 11 else 30.0,
                        "flow": 100.0,
                    }
                )
        data = pd.DataFrame(rows)

        result = annual_projection_table(
            data,
            2026,
            11,
            pd.Timestamp("2026-11-01"),
            pd.DataFrame(columns=["date", "plant", "production"]),
        ).set_index("Nhà máy")

        self.assertAlmostEqual(result.at["Buôn Kuốp", "T11"], 32.0)

    def test_new_upload_keeps_other_report_types_and_replaces_same_family(self) -> None:
        saved = (
            ("BCSX_Thang (50).xls", b"old-month"),
            ("BCTV_LUYKE (9).xls", b"hydrology"),
            ("BCSX_Nam (10).xls", b"old-year"),
        )
        current = (("BCSX_Nam (11).xls", b"new-year"),)

        result = merge_upload_file_sets(saved, current)

        self.assertEqual(
            [name for name, _ in result],
            ["BCSX_Thang (50).xls", "BCTV_LUYKE (9).xls", "BCSX_Nam (11).xls"],
        )
        self.assertEqual(dict(result)["BCSX_Nam (11).xls"], b"new-year")

    def test_annual_production_report_uses_gross_mwh_as_monthly_actuals(self) -> None:
        raw = pd.DataFrame([[None] * 26 for _ in range(6)])
        raw.iat[0, 0] = "BÁO CÁO SẢN XUẤT NĂM 2026"
        raw.iat[2, 0] = "Tháng"
        raw.iat[2, 1] = "Buôn Tua Srah"
        raw.iat[2, 9] = "Buôn Kuốp"
        raw.iat[2, 17] = "Srepok 3"
        for column in (1, 9, 17):
            raw.iat[3, column] = "Đầu cực (MWh)"
        raw.iat[4, 0] = 1
        raw.iat[4, 1] = 26021.2938
        raw.iat[4, 9] = 103249.1049
        raw.iat[4, 17] = 76804.7064
        raw.iat[5, 0] = 2
        raw.iat[5, 1] = 0
        raw.iat[5, 9] = 0
        raw.iat[5, 17] = 0

        class FakeExcel:
            sheet_names = ["BCSX Năm"]

        with patch("data_utils.pd.ExcelFile", return_value=FakeExcel()), patch(
            "data_utils.pd.read_excel", return_value=raw
        ):
            result = _parse_annual_production_report("annual.xls")

        self.assertEqual(set(result["month"]), {1})
        self.assertAlmostEqual(result["production"].sum(), 206.0751051, places=7)
        self.assertAlmostEqual(
            result.loc[result["plant"] == "Buôn Kuốp", "production"].iloc[0],
            103.2491049,
            places=7,
        )

    def test_pmis_training_workbook_supplies_observed_history_from_1977(self) -> None:
        workbook = (
            ROOT
            / "PMIS"
            / "Dữ liệu huấn luyện dự báo sản lượng Buon Kuop qua các năm.xlsx"
        )

        result = load_analog_training_workbook(workbook)

        self.assertEqual(int(result["year"].min()), 1977)
        self.assertEqual(int(result["year"].max()), 2024)
        self.assertEqual(result["plant"].nunique(), 3)
        self.assertEqual(len(result[result["year"] == 1977]), 36)
        self.assertTrue(
            result[result["year"] == 1977][["production", "flow"]]
            .notna()
            .all()
            .all()
        )

    def test_new_monthly_actual_overrides_stale_value_and_keeps_old_months(self) -> None:
        history = pd.DataFrame(
            [
                {"year": 2026, "month": 9, "plant": "Buôn Kuốp", "production": 120.0, "flow": 80.0},
                {"year": 2026, "month": 10, "plant": "Buôn Kuốp", "production": 135.678, "flow": 90.0},
            ]
        )
        uploaded = pd.DataFrame(
            [
                {"year": 2026, "month": 10, "plant": "Buon Kuop", "production": 176.538, "flow": None},
                {"year": 2026, "month": 11, "plant": "Buôn Kuốp", "production": 12.0, "flow": 100.0},
            ]
        )

        result = merge_monthly_actuals(history, uploaded).set_index(["year", "month", "plant"])

        self.assertEqual(result.at[(2026, 9, "Buôn Kuốp"), "production"], 120.0)
        self.assertEqual(result.at[(2026, 10, "Buôn Kuốp"), "production"], 176.538)
        self.assertEqual(result.at[(2026, 10, "Buôn Kuốp"), "flow"], 90.0)
        self.assertEqual(result.at[(2026, 11, "Buôn Kuốp"), "production"], 12.0)

    def test_completed_month_is_not_replaced_by_partial_daily_total(self) -> None:
        monthly = pd.DataFrame([
            {"year": 2026, "month": 10, "plant": "Buôn Kuốp", "production": 176.538, "flow": 90.0},
        ])
        partial = pd.DataFrame([
            {"year": 2026, "month": 10, "plant": "Buôn Kuốp", "production": 159.9, "flow": 88.0},
        ])

        result = merge_current_calendar_month_actuals(
            monthly,
            partial,
            pd.Timestamp("2026-10-26"),
            today=pd.Timestamp("2026-11-02"),
        ).set_index(["year", "month", "plant"])

        self.assertEqual(result.at[(2026, 10, "Buôn Kuốp"), "production"], 176.538)
        self.assertEqual(result.at[(2026, 10, "Buôn Kuốp"), "flow"], 90.0)

    def test_partial_daily_total_updates_current_calendar_month(self) -> None:
        monthly = pd.DataFrame([
            {"year": 2026, "month": 11, "plant": "Buôn Kuốp", "production": 20.0, "flow": 90.0},
        ])
        partial = pd.DataFrame([
            {"year": 2026, "month": 11, "plant": "Buôn Kuốp", "production": 25.0, "flow": 95.0},
        ])

        result = merge_current_calendar_month_actuals(
            monthly,
            partial,
            pd.Timestamp("2026-11-02"),
            today=pd.Timestamp("2026-11-02"),
        ).set_index(["year", "month", "plant"])

        self.assertEqual(result.at[(2026, 11, "Buôn Kuốp"), "production"], 25.0)
        self.assertEqual(result.at[(2026, 11, "Buôn Kuốp"), "flow"], 95.0)

    def test_api_values_override_excel_and_keep_old_fallback(self) -> None:
        excel = pd.DataFrame(
            [
                {"date": "2026-09-30", "plant": "Buôn Kuốp", "flow": 90.0},
                {"date": "2026-10-01", "plant": "Buôn Kuốp", "flow": 100.0},
            ]
        )
        api = pd.DataFrame(
            [
                {"date": "2026-10-01", "plant": "Buon Kuop", "flow": 110.0},
                {"date": "2026-10-02", "plant": "Buôn Kuốp", "flow": 120.0},
                {"date": "2026-10-03", "plant": "Buôn Kuốp", "flow": None},
            ]
        )

        result = merge_daily_hydrology(excel, api).set_index("date")

        self.assertEqual(result.loc[pd.Timestamp("2026-09-30"), "flow"], 90.0)
        self.assertEqual(result.loc[pd.Timestamp("2026-10-01"), "flow"], 110.0)
        self.assertEqual(result.loc[pd.Timestamp("2026-10-02"), "flow"], 120.0)
        self.assertNotIn(pd.Timestamp("2026-10-03"), result.index)

    def test_seven_day_projection_applies_bts_filling_rule(self) -> None:
        dates = pd.date_range("2026-10-20", "2026-10-26")
        flows = {"Buôn Kuốp": 100.0, "Buôn Tua Srah": 120.0, "Srêpốk 3": 80.0}
        daily = pd.DataFrame(
            [
                {"date": date, "plant": plant, "flow": flow}
                for date in dates
                for plant, flow in flows.items()
            ]
        )
        status = pd.DataFrame(
            [
                {
                    "date": "2026-10-26",
                    "plant": "Buôn Tua Srah",
                    "water_level": 485.0,
                    "mndbt": 487.5,
                }
            ]
        )

        result = seven_day_inflow_generation_projection(
            daily, pd.Timestamp("2026-10-26"), status
        ).set_index("Nhà máy")

        self.assertEqual(result.at["Buôn Tua Srah", "Q TB 7 ngày"], 120.0)
        self.assertEqual(result.at["Buôn Tua Srah", "Q tính sản lượng"], 40.0)
        self.assertAlmostEqual(
            result.at["Buôn Kuốp", "Nước về ngày"], 8.64
        )
        self.assertAlmostEqual(
            result.at["Buôn Kuốp", "Sản lượng ngày"],
            100.0 * 86_400 / 4.063 / 1_000_000,
        )
        self.assertEqual(result.at["Buôn Kuốp", "Số ngày còn lại"], 66)

    def test_bts_near_full_uses_all_mean_inflow(self) -> None:
        daily = pd.DataFrame(
            [
                {"date": date, "plant": "Buôn Tua Srah", "flow": 120.0}
                for date in pd.date_range("2026-10-20", "2026-10-26")
            ]
        )
        status = pd.DataFrame(
            [{"date": "2026-10-26", "plant": "Buôn Tua Srah", "water_level": 486.8, "mndbt": 487.5}]
        )

        result = seven_day_inflow_generation_projection(
            daily, pd.Timestamp("2026-10-26"), status
        ).iloc[0]

        self.assertEqual(result["Q tính sản lượng"], 120.0)
        self.assertIn("gần đầy", result["Chế độ tính"])

    def test_annual_inflow_projection_uses_remaining_calendar_days(self) -> None:
        data = pd.DataFrame(
            [
                {"year": 2026, "month": month, "plant": plant, "production": 10.0, "flow": 1.0}
                for plant in ("Buôn Kuốp", "Buôn Tua Srah", "Srêpốk 3")
                for month in range(1, 11)
            ]
        )
        inflow = pd.DataFrame(
            [{"Nhà máy": plant, "Sản lượng ngày": 2.0} for plant in ("Buôn Kuốp", "Buôn Tua Srah", "Srêpốk 3")]
        )

        result = annual_inflow_projection_table(
            data, 2026, 10, pd.Timestamp("2026-10-26"), inflow
        ).set_index("Nhà máy")

        self.assertEqual(result.at["Buôn Kuốp", "T10"], 20.0)
        self.assertEqual(result.at["Buôn Kuốp", "T11"], 60.0)
        self.assertEqual(result.at["Buôn Kuốp", "T12"], 62.0)


if __name__ == "__main__":
    unittest.main()
