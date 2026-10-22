from __future__ import annotations

import importlib.util
import base64
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
API_FILE = ROOT / "10_api.py"


def create_test_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE reservoir_readings (
                id INTEGER PRIMARY KEY,
                observed_at TEXT NOT NULL,
                requested_at_last_seen TEXT NOT NULL,
                reservoir_name TEXT NOT NULL,
                h_tl REAL,
                h_dbt REAL,
                h_c REAL,
                q_ve REAL,
                sum_qx REAL,
                q_xt REAL,
                q_xm REAL
            );
            CREATE INDEX idx_test_readings ON reservoir_readings(reservoir_name, observed_at);

            CREATE TABLE hydroai_forecast_daily (
                Date TEXT, Basin_ID TEXT, Forecast_Type TEXT,
                ENSO_Q_Anomaly_pct REAL, ENSO_Scenario_Source TEXT,
                Rain_Basin_mm REAL, Q_P10_m3s REAL, Q_P50_m3s REAL,
                Q_P90_m3s REAL, WaterLevel_P10_m REAL,
                WaterLevel_P50_m REAL, WaterLevel_P90_m REAL
            );
            CREATE TABLE pmis_generation_daily (
                Date TEXT, Basin_ID TEXT, Reservoir_Name TEXT, Display_Name TEXT,
                Generation_MWh REAL, Generation_Net_MWh REAL, Self_Use_MWh REAL,
                Running_Hours REAL, Power_MW REAL, Turbine_Water_Mm3 REAL,
                Q_turbine_m3s REAL, Specific_Water_m3_per_kWh REAL
            );
            CREATE TABLE hydroai_generation_forecast_daily (
                Date TEXT, Basin_ID TEXT, Reservoir_Name TEXT,
                Generation_MWh REAL, Generation_Forecast_MWh REAL,
                Generation_P10_MWh REAL, Generation_P90_MWh REAL,
                Forecast_Power_MW REAL, Value_Type TEXT,
                Blend_Source TEXT, Actual_Cutoff TEXT
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO reservoir_readings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "2026-10-15 00:00:00", "2026-10-15 00:05:00", "Buôn Tua Srah", 480, 487.5, 465, 100, 90, 0, 90),
                (2, "2026-10-15 12:00:00", "2026-10-15 12:05:00", "Buôn Tua Srah", 482, 487.5, 465, 120, 110, 0, 110),
                (3, "2026-10-15 12:00:00", "2026-10-15 12:05:00", "Buôn Kuốp", 411, 412, 409, 80, 75, 0, 75),
                (4, "2026-10-15 12:00:00", "2026-10-15 12:05:00", "Trị An", 61.7, 62, 50, 700, 650, 0, 650),
            ],
        )
        connection.execute(
            "INSERT INTO hydroai_forecast_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-10-16", "BTS", "daily", -10, "test", 5, 90, 110, 140, 480, 481, 482),
        )
        connection.execute(
            "INSERT INTO pmis_generation_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-10-15", "BTS", "Buôn Tua Srah", "BTS", 1000, 990, 10, 20, 50, 1, 12, 8.58),
        )
        connection.execute(
            "INSERT INTO hydroai_generation_forecast_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-10-16", "BTS", "Buôn Tua Srah", None, 1100, 900, 1300, 55, "forecast", "test", "2026-10-15"),
        )


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test.db"
        create_test_database(cls.db_path)
        os.environ["HYDROAI_DB_PATH"] = str(cls.db_path)
        os.environ["HYDROAI_API_KEY"] = "test-secret"
        os.environ["HYDROAI_UPLOAD_DIR"] = str(Path(cls.temp_dir.name) / "uploads")
        spec = importlib.util.spec_from_file_location("hydroai_api_test", API_FILE)
        assert spec and spec.loader
        cls.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.api)
        cls.client = TestClient(cls.api.app)
        cls.headers = {"X-API-Key": "test-secret"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls.temp_dir.cleanup()

    def test_health_is_public(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["database_available"])

    def test_data_requires_api_key(self) -> None:
        self.assertEqual(self.client.get("/basins").status_code, 401)
        self.assertEqual(self.client.get("/history", headers={"X-API-Key": "wrong"}).status_code, 401)
        self.assertEqual(self.client.get("/hydrology-uploads").status_code, 401)

    def test_hydrology_uploads_are_persisted_and_returned(self) -> None:
        content = b"test excel bytes"
        response = self.client.post(
            "/hydrology-uploads",
            headers=self.headers,
            json={
                "files": [
                    {
                        "name": "BCTV_LUYKE_2026-10-21.xls",
                        "content_base64": base64.b64encode(content).decode("ascii"),
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["files"][0]["name"], "BCTV_LUYKE_2026-10-21.xls")

        saved = self.client.get("/hydrology-uploads", headers=self.headers)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(len(saved.json()["files"]), 1)
        self.assertEqual(
            base64.b64decode(saved.json()["files"][0]["content_base64"]),
            content,
        )

    def test_hydrology_upload_rejects_non_excel(self) -> None:
        response = self.client.post(
            "/hydrology-uploads",
            headers=self.headers,
            json={
                "files": [
                    {
                        "name": "payload.txt",
                        "content_base64": base64.b64encode(b"bad").decode("ascii"),
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_history_filters_and_aggregates(self) -> None:
        response = self.client.get(
            "/history",
            params={"basin_id": "BTS", "start_date": "2026-10-15", "end_date": "2026-10-15", "limit": 10},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        rows = response.json()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Basin_ID"], "BTS")
        self.assertAlmostEqual(rows[0]["WaterLevel_m"], 481.0)
        self.assertAlmostEqual(rows[0]["Q_in_m3s"], 110.0)

    def test_forecast_and_generation_use_sqlite(self) -> None:
        forecast = self.client.get(
            "/forecast",
            params={"basin_id": "BTS", "start_date": "2026-10-16", "end_date": "2026-10-16"},
            headers=self.headers,
        )
        self.assertEqual(forecast.status_code, 200)
        self.assertEqual(forecast.json()["rows"][0]["Q_P50_m3s"], 110.0)

        generation = self.client.get(
            "/generation",
            params={"basin_id": "BTS", "start_date": "2026-10-15", "end_date": "2026-10-16"},
            headers=self.headers,
        )
        self.assertEqual(generation.status_code, 200)
        self.assertEqual(generation.json()["actual_rows"][0]["Generation_MWh"], 1000.0)
        self.assertEqual(generation.json()["forecast_rows"][0]["Generation_Forecast_MWh"], 1100.0)

    def test_all_reservoirs_are_listed_and_history_is_queryable(self) -> None:
        basins = self.client.get("/basins", headers=self.headers)
        self.assertEqual(basins.status_code, 200)
        items = basins.json()["basins"]
        tri_an = next(item for item in items if item["reservoir_name"] == "Trị An")
        self.assertEqual(tri_an["id"], "RESERVOIR:Trị An")
        self.assertIsNone(tri_an["model_basin_id"])

        history = self.client.get(
            "/history",
            params={
                "basin_id": tri_an["id"],
                "start_date": "2026-10-15",
                "end_date": "2026-10-15",
            },
            headers=self.headers,
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["rows"][0]["Reservoir_Name"], "Trị An")

        forecast = self.client.get(
            "/forecast",
            params={"basin_id": tri_an["id"]},
            headers=self.headers,
        )
        self.assertEqual(forecast.status_code, 200)
        self.assertEqual(forecast.json()["rows"], [])

    def test_database_connection_is_read_only(self) -> None:
        with closing(self.api.open_database()) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE should_fail(id INTEGER)")


if __name__ == "__main__":
    unittest.main()
