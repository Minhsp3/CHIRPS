from __future__ import annotations

import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd

from config import HOCHUA_DB_PATH, PMIS_DIR


SAN_LUONG_DIR = PMIS_DIR / "San luong"
TABLE_DAILY = "pmis_generation_daily"
TABLE_SUMMARY = "pmis_generation_import_summary"

BASIN_GROUPS = {
    "BTS": {
        "Reservoir_Name": "Buon Tua Srah",
        "Display_Name": "Buôn Tua Srah",
        "start_col": 1,
    },
    "BK": {
        "Reservoir_Name": "Buon Kuop",
        "Display_Name": "Buôn Kuốp",
        "start_col": 9,
    },
    "SP3": {
        "Reservoir_Name": "Srepok 3",
        "Display_Name": "Srêpốk 3",
        "start_col": 17,
    },
}


def normalize_text(value: object) -> str:
    text = "" if pd.isna(value) else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("đ", "d").replace("Đ", "D")
    text = re.sub(r"\s+", " ", text.lower()).strip()
    return text


def to_float(value: object) -> float | None:
    if pd.isna(value):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value in {"", "-", "--"}:
            return None
    parsed = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def parse_date(value: object, source_file: Path) -> pd.Timestamp | None:
    if isinstance(value, str) and re.match(r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*$", value):
        parsed = pd.to_datetime(value, errors="coerce", yearfirst=True)
    else:
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    date = pd.Timestamp(parsed).normalize()
    file_match = re.match(r"(?P<year>\d{4})\.(?P<month>\d{2})", source_file.name)
    if file_match:
        file_year = int(file_match.group("year"))
        file_month = int(file_match.group("month"))
        if date.year != file_year or date.month != file_month:
            # PMIS sometimes stores a stale title, but the row date is authoritative.
            if abs((date - pd.Timestamp(file_year, file_month, 1)).days) > 35:
                return None
    return date


def parse_generation_workbook(path: Path) -> pd.DataFrame:
    workbook = pd.ExcelFile(path)
    sheet_name = workbook.sheet_names[0]
    data = pd.read_excel(path, sheet_name=sheet_name, header=None)
    records: list[dict[str, object]] = []
    for row_index in range(len(data)):
        date = parse_date(data.iat[row_index, 0] if data.shape[1] > 0 else None, path)
        if date is None:
            continue
        for basin_id, meta in BASIN_GROUPS.items():
            start = int(meta["start_col"])
            values = [to_float(data.iat[row_index, start + offset]) if start + offset < data.shape[1] else None for offset in range(8)]
            if all(value is None for value in values):
                continue
            generation_terminal_mwh = values[0]
            generation_net_mwh = values[1]
            self_use_mwh = values[2]
            self_use_pct = values[3]
            running_hours = values[4]
            avg_power_mw = values[5]
            turbine_water_million_m3 = values[6]
            specific_water_m3_per_kwh = values[7]
            turbine_flow = None
            if turbine_water_million_m3 is not None:
                turbine_flow = turbine_water_million_m3 * 1_000_000.0 / 86_400.0
            records.append(
                {
                    "Date": date,
                    "Year": int(date.year),
                    "Month": int(date.month),
                    "Day": int(date.day),
                    "Basin_ID": basin_id,
                    "Reservoir_Name": meta["Reservoir_Name"],
                    "Display_Name": meta["Display_Name"],
                    "Generation_MWh": generation_terminal_mwh,
                    "Generation_Net_MWh": generation_net_mwh,
                    "Self_Use_MWh": self_use_mwh,
                    "Self_Use_pct": self_use_pct,
                    "Running_Hours": running_hours,
                    "Power_MW": avg_power_mw,
                    "Turbine_Water_Mm3": turbine_water_million_m3,
                    "Q_turbine_m3s": turbine_flow,
                    "Specific_Water_m3_per_kWh": specific_water_m3_per_kwh,
                    "Data_Source": "PMIS_SAN_LUONG_DAILY",
                    "Source_File": path.name,
                    "Source_Sheet": sheet_name,
                }
            )
    return pd.DataFrame(records)


def import_san_luong_to_sql(folder: Path = SAN_LUONG_DIR) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not folder.exists():
        raise FileNotFoundError(f"Khong tim thay folder san luong: {folder}")
    files = sorted(path for path in folder.rglob("*.xls*") if path.is_file() and not path.name.startswith("~$"))
    if not files:
        raise FileNotFoundError(f"Khong tim thay file Excel trong: {folder}")

    frames = []
    summary_rows = []
    for path in files:
        try:
            df = parse_generation_workbook(path)
            frames.append(df)
            summary_rows.append({"Source_File": path.name, "Rows": len(df), "Status": "OK", "Error": ""})
        except Exception as exc:
            summary_rows.append({"Source_File": path.name, "Rows": 0, "Status": "ERROR", "Error": str(exc)})

    daily = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not daily.empty:
        daily = daily.sort_values(["Basin_ID", "Date", "Source_File"]).drop_duplicates(
            subset=["Date", "Basin_ID"],
            keep="last",
        )
        numeric_columns = [
            "Generation_MWh",
            "Generation_Net_MWh",
            "Self_Use_MWh",
            "Self_Use_pct",
            "Running_Hours",
            "Power_MW",
            "Turbine_Water_Mm3",
            "Q_turbine_m3s",
            "Specific_Water_m3_per_kWh",
        ]
        for column in numeric_columns:
            daily[column] = pd.to_numeric(daily[column], errors="coerce")
        daily = daily.replace({np.nan: None})

    summary = pd.DataFrame(summary_rows)
    export_daily = daily.copy()
    if not export_daily.empty:
        export_daily["Date"] = pd.to_datetime(export_daily["Date"], errors="coerce").dt.strftime("%Y-%m-%d")

    with sqlite3.connect(HOCHUA_DB_PATH) as conn:
        export_daily.to_sql(TABLE_DAILY, conn, if_exists="replace", index=False)
        summary.to_sql(TABLE_SUMMARY, conn, if_exists="replace", index=False)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_DAILY}_basin_date ON {TABLE_DAILY}(Basin_ID, Date)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hydroai_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for key, value in [
            ("pmis_generation_daily_rows", str(len(export_daily))),
            ("pmis_generation_daily_files", str(len(files))),
            ("pmis_generation_daily_min_date", "" if export_daily.empty else str(export_daily["Date"].min())),
            ("pmis_generation_daily_max_date", "" if export_daily.empty else str(export_daily["Date"].max())),
        ]:
            conn.execute(
                """
                INSERT INTO hydroai_metadata(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )

    return daily, summary


if __name__ == "__main__":
    try:
        daily_df, summary_df = import_san_luong_to_sql()
    except Exception as exc:
        print("\nCHUONG TRINH DUNG DO LOI:")
        print(exc)
        sys.exit(1)
    print(f"Da nap {len(daily_df):,} dong san luong/nuoc chay may vao {TABLE_DAILY}")
    if not daily_df.empty:
        report = (
            daily_df.groupby("Basin_ID", as_index=False)
            .agg(
                Start_Date=("Date", "min"),
                End_Date=("Date", "max"),
                Rows=("Date", "count"),
                Generation_MWh=("Generation_MWh", "sum"),
                Turbine_Water_Mm3=("Turbine_Water_Mm3", "sum"),
            )
            .sort_values("Basin_ID")
        )
        print(report.to_string(index=False))
    errors = summary_df[summary_df["Status"] != "OK"]
    if not errors.empty:
        print("\nFile loi:")
        print(errors.to_string(index=False))
