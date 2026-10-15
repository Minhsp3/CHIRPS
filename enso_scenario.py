from __future__ import annotations

import sqlite3

import pandas as pd


SOURCE_NAME = "Ban tin nhan dinh xu the KTTV 7 thang ngay 15/09/2026"

# Sai lech luu luong thang so voi trung binh nhieu nam trong ban tin KTTV.
# SP3 khong co dong rieng trong bang nguon, nen dung chuoi KG-BK lam bien dai dien
# cho xu the dong chay ha bac thang Srepok va duoc danh dau ro la proxy.
MONTHLY_Q_ANOMALY_PCT = {
    "BTS": {
        "2026-10": -17.0,
        "2026-11": -5.0,
        "2026-12": 2.0,
        "2027-01": -3.0,
        "2027-02": -2.0,
        "2027-03": -9.0,
        "2027-04": -21.0,
    },
    "BK": {
        "2026-10": -27.0,
        "2026-11": -35.0,
        "2026-12": -33.0,
        "2027-01": -4.0,
        "2027-02": -4.0,
        "2027-03": -6.0,
        "2027-04": -14.0,
    },
    "SP3": {
        "2026-10": -34.0,
        "2026-11": -10.0,
        "2026-12": -12.0,
        "2027-01": -11.0,
        "2027-02": 2.0,
        "2027-03": -13.0,
        "2027-04": -3.0,
    },
}


def scenario_dataframe() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for basin_id, monthly_values in MONTHLY_Q_ANOMALY_PCT.items():
        for year_month, anomaly_pct in monthly_values.items():
            rows.append(
                {
                    "YearMonth": year_month,
                    "Basin_ID": basin_id,
                    "Q_Anomaly_pct": anomaly_pct,
                    "ENSO_Phase": "El Nino",
                    "Scenario": "KTTV_2026_09_15",
                    "Source": SOURCE_NAME,
                    "Method": "Official reservoir row" if basin_id != "SP3" else "KG-BK cascade proxy",
                }
            )
    return pd.DataFrame(rows).sort_values(["YearMonth", "Basin_ID"]).reset_index(drop=True)


def save_scenario_to_database(database_path) -> pd.DataFrame:
    scenario = scenario_dataframe()
    with sqlite3.connect(database_path) as conn:
        scenario.to_sql("enso_hydrology_scenario_monthly", conn, if_exists="replace", index=False)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_enso_hydrology_scenario_monthly "
            "ON enso_hydrology_scenario_monthly(Basin_ID, YearMonth)"
        )
    return scenario


def anomaly_for_date(basin_id: str, date: pd.Timestamp) -> float | None:
    value = MONTHLY_Q_ANOMALY_PCT.get(basin_id, {}).get(pd.Timestamp(date).strftime("%Y-%m"))
    return None if value is None else float(value)
