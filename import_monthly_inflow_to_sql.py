from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from config import HOCHUA_DB_PATH, PMIS_DIR


SOURCE_FILE = PMIS_DIR / "Chuoi so lieu luu luong nuoc ve cap nhat het 2024.xlsx"

SHEET_MAP = {
    "Phu luc 1": ("BTS", "Buôn Tua Srah"),
    "Phu luc 2": ("BK", "Buôn Kuốp"),
    "Phu luc 3": ("SP3", "Srêpốk 3"),
}


def find_table_bounds(raw: pd.DataFrame) -> tuple[int, int]:
    header_row = None
    for index, row in raw.iterrows():
        values = [str(value).strip() for value in row.tolist() if pd.notna(value)]
        if "Năm" in values and any("Tháng 1" == value for value in values):
            header_row = int(index)
            break
    if header_row is None:
        raise ValueError("Khong tim thay dong header co cot Nam/Thang 1.")

    data_start = header_row + 1
    data_end = data_start
    while data_end < len(raw):
        year = pd.to_numeric(pd.Series([raw.iat[data_end, 1]]), errors="coerce").iloc[0]
        if pd.isna(year) or int(year) < 1900:
            break
        data_end += 1
    return data_start, data_end


def parse_workbook(path: Path, start_year: int, end_year: int) -> pd.DataFrame:
    excel = pd.ExcelFile(path)
    frames: list[pd.DataFrame] = []

    for sheet_name in excel.sheet_names:
        match = next((prefix for prefix in SHEET_MAP if sheet_name.startswith(prefix)), None)
        if not match:
            continue

        basin_id, reservoir_name = SHEET_MAP[match]
        raw = pd.read_excel(path, sheet_name=sheet_name, header=None)
        data_start, data_end = find_table_bounds(raw)

        table = raw.iloc[data_start:data_end, 0:15].copy()
        table.columns = [
            "STT",
            "Year",
            *[f"Month_{month}" for month in range(1, 13)],
            "Annual_Average_m3s",
        ]
        table["Year"] = pd.to_numeric(table["Year"], errors="coerce").astype("Int64")
        table = table[table["Year"].between(start_year, end_year)].copy()

        monthly = table.melt(
            id_vars=["Year", "Annual_Average_m3s"],
            value_vars=[f"Month_{month}" for month in range(1, 13)],
            var_name="Month_Name",
            value_name="Q_in_m3s",
        )
        monthly["Month"] = monthly["Month_Name"].str.extract(r"(\d+)").astype(int)
        monthly["Q_in_m3s"] = pd.to_numeric(monthly["Q_in_m3s"], errors="coerce")
        monthly["Annual_Average_m3s"] = pd.to_numeric(monthly["Annual_Average_m3s"], errors="coerce")
        monthly["Date"] = pd.to_datetime(
            {
                "year": monthly["Year"].astype(int),
                "month": monthly["Month"].astype(int),
                "day": 1,
            }
        )
        monthly["Basin_ID"] = basin_id
        monthly["Reservoir_Name"] = reservoir_name
        monthly["Source_File"] = path.name
        monthly["Source_Sheet"] = sheet_name
        monthly["Data_Source"] = "PMIS_MONTHLY_INFLOW"
        frames.append(monthly)

    if not frames:
        raise RuntimeError(f"Khong tim thay sheet phu luc trong {path}.")

    result = pd.concat(frames, ignore_index=True)
    result = result.dropna(subset=["Year", "Month", "Q_in_m3s"])
    result["Year"] = result["Year"].astype(int)
    result["Month"] = result["Month"].astype(int)
    result["Date"] = result["Date"].dt.strftime("%Y-%m-%d")
    columns = [
        "Date",
        "Year",
        "Month",
        "Basin_ID",
        "Reservoir_Name",
        "Q_in_m3s",
        "Annual_Average_m3s",
        "Data_Source",
        "Source_File",
        "Source_Sheet",
    ]
    return result[columns].sort_values(["Basin_ID", "Year", "Month"]).reset_index(drop=True)


def save_to_sql(dataframe: pd.DataFrame, db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hydroai_monthly_inflow (
                Date TEXT NOT NULL,
                Year INTEGER NOT NULL,
                Month INTEGER NOT NULL,
                Basin_ID TEXT NOT NULL,
                Reservoir_Name TEXT NOT NULL,
                Q_in_m3s REAL,
                Annual_Average_m3s REAL,
                Data_Source TEXT NOT NULL,
                Source_File TEXT NOT NULL,
                Source_Sheet TEXT NOT NULL,
                Imported_At TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (Basin_ID, Year, Month)
            )
            """
        )
        conn.execute("DELETE FROM hydroai_monthly_inflow WHERE Data_Source = 'PMIS_MONTHLY_INFLOW'")
        dataframe.to_sql("hydroai_monthly_inflow", conn, if_exists="append", index=False)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hydroai_monthly_inflow_date ON hydroai_monthly_inflow(Date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hydroai_monthly_inflow_basin_date ON hydroai_monthly_inflow(Basin_ID, Date)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hydroai_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO hydroai_metadata(key, value, updated_at)
            VALUES ('hydroai_monthly_inflow_rows', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (str(len(dataframe)),),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import monthly reservoir inflow series into HydroAI SQLite database.")
    parser.add_argument("--file", type=Path, default=SOURCE_FILE)
    parser.add_argument("--start-year", type=int, default=1977)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--db", type=Path, default=HOCHUA_DB_PATH)
    args = parser.parse_args()

    data = parse_workbook(args.file, args.start_year, args.end_year)
    save_to_sql(data, args.db)
    print(f"Imported {len(data):,} monthly inflow rows into {args.db}")
    print(f"Years: {data['Year'].min()}-{data['Year'].max()}; basins: {', '.join(sorted(data['Basin_ID'].unique()))}")


if __name__ == "__main__":
    main()
