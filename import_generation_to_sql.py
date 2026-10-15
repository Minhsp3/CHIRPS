from __future__ import annotations

import calendar
import re
import sqlite3
import unicodedata
from pathlib import Path

import pandas as pd

from config import HOCHUA_DB_PATH, PMIS_DIR


BASIN_BY_RESERVOIR = {
    "buon tua srah": ("BTS", "Buon Tua Srah"),
    "buon kuop": ("BK", "Buon Kuop"),
    "buon k uop": ("BK", "Buon Kuop"),
    "sre pok 3": ("SP3", "Srepok 3"),
    "srepok 3": ("SP3", "Srepok 3"),
}


def normalize_text(value: object) -> str:
    text = "" if pd.isna(value) else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("đ", "d").replace("Đ", "D")
    text = re.sub(r"\s+", " ", text.lower()).strip()
    return text


def find_workbook(name_contains: str) -> Path:
    matches = [
        path
        for path in PMIS_DIR.glob("*.xlsx")
        if not path.name.startswith("~$") and name_contains.lower() in normalize_text(path.name)
    ]
    if not matches:
        raise FileNotFoundError(f"Khong tim thay workbook chua: {name_contains}")
    return matches[0]


def reservoir_to_basin(value: object) -> tuple[str, str] | None:
    normalized = normalize_text(value)
    for key, basin in BASIN_BY_RESERVOIR.items():
        if key in normalized:
            return basin
    return None


def numeric_month_value(value: object) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return float(parsed)


def add_monthly_record(
    records: list[dict],
    *,
    year: int,
    month: int,
    basin_id: str,
    reservoir_name: str,
    generation_million_kwh: float,
    data_source: str,
    source_file: Path,
    source_sheet: str,
    scenario: str,
    scenario_order: int,
) -> None:
    days_in_month = calendar.monthrange(year, month)[1]
    generation_mwh = generation_million_kwh * 1000.0
    avg_daily_generation_mwh = generation_mwh / days_in_month
    records.append(
        {
            "Year": year,
            "Month": month,
            "Date": pd.Timestamp(year=year, month=month, day=1),
            "Basin_ID": basin_id,
            "Reservoir_Name": reservoir_name,
            "Generation_Million_kWh": generation_million_kwh,
            "Generation_MWh_Monthly": generation_mwh,
            "Daily_Generation_MWh_MonthAvg": avg_daily_generation_mwh,
            "Power_MW_MonthAvg": avg_daily_generation_mwh / 24.0,
            "Data_Source": data_source,
            "Source_File": source_file.name,
            "Source_Sheet": source_sheet,
            "Scenario": scenario,
            "Scenario_Order": scenario_order,
        }
    )


def parse_historical_monthly_generation(path: Path) -> pd.DataFrame:
    records: list[dict] = []
    workbook = pd.ExcelFile(path)
    year_sheets = [sheet for sheet in workbook.sheet_names if str(sheet).isdigit()]
    for sheet in year_sheets:
        year = int(sheet)
        data = pd.read_excel(path, sheet_name=sheet, header=None)
        in_generation_section = False
        for row_index in range(len(data)):
            marker = normalize_text(data.iat[row_index, 0] if data.shape[1] > 0 else "")
            if "san luong" in marker:
                in_generation_section = True
            elif marker and ("qv" in marker or "nh do" in marker or "nhiet do" in marker):
                in_generation_section = False
            reservoir_cell = data.iat[row_index, 1] if data.shape[1] > 1 else None
            basin = reservoir_to_basin(reservoir_cell)
            if not in_generation_section or basin is None:
                continue
            basin_id, reservoir_name = basin
            for month in range(1, 13):
                col = month + 1
                if col >= data.shape[1]:
                    continue
                value = numeric_month_value(data.iat[row_index, col])
                if value is None:
                    continue
                add_monthly_record(
                    records,
                    year=year,
                    month=month,
                    basin_id=basin_id,
                    reservoir_name=reservoir_name,
                    generation_million_kwh=value,
                    data_source="PMIS_GENERATION_HISTORICAL_MONTHLY",
                    source_file=path,
                    source_sheet=sheet,
                    scenario="historical_monthly",
                    scenario_order=0,
                )
    return pd.DataFrame(records)


def scenario_label(data: pd.DataFrame, header_row: int, order: int) -> str:
    labels: list[str] = []
    for row in range(max(0, header_row - 3), header_row):
        values = [str(v).strip() for v in data.iloc[row, :3].tolist() if pd.notna(v)]
        if values:
            labels.append(" ".join(values))
    label = " | ".join(labels).strip()
    return label or f"scenario_{order}"


def parse_2026_generation_plan(path: Path) -> pd.DataFrame:
    records: list[dict] = []
    data = pd.read_excel(path, sheet_name="2026", header=None)
    scenario_order = 0
    for row_index in range(len(data)):
        if normalize_text(data.iat[row_index, 0] if data.shape[1] > 0 else "") != "thang":
            continue
        nearby_rows = range(row_index + 1, min(len(data), row_index + 6))
        reservoir_rows = [(idx, reservoir_to_basin(data.iat[idx, 0])) for idx in nearby_rows]
        reservoir_rows = [(idx, basin) for idx, basin in reservoir_rows if basin is not None]
        if len(reservoir_rows) < 3:
            continue
        scenario_order += 1
        label = scenario_label(data, row_index, scenario_order)
        for idx, basin in reservoir_rows[:3]:
            basin_id, reservoir_name = basin
            for month in range(1, 13):
                if month >= data.shape[1]:
                    continue
                value = numeric_month_value(data.iat[idx, month])
                if value is None:
                    continue
                add_monthly_record(
                    records,
                    year=2026,
                    month=month,
                    basin_id=basin_id,
                    reservoir_name=reservoir_name,
                    generation_million_kwh=value,
                    data_source="PMIS_GENERATION_PLAN_2026",
                    source_file=path,
                    source_sheet="2026",
                    scenario=label,
                    scenario_order=scenario_order,
                )
    return pd.DataFrame(records)


def expand_monthly_to_daily(monthly_df: pd.DataFrame) -> pd.DataFrame:
    if monthly_df.empty:
        return monthly_df
    selected_rows = []
    for (year, month, basin_id), group in monthly_df.groupby(["Year", "Month", "Basin_ID"], sort=False):
        if int(year) == 2026:
            row = group.sort_values("Scenario_Order").iloc[-1]
        else:
            row = group.iloc[0]
        days = calendar.monthrange(int(year), int(month))[1]
        for day in range(1, days + 1):
            item = row.to_dict()
            item["Date"] = pd.Timestamp(year=int(year), month=int(month), day=day)
            selected_rows.append(item)
    daily_df = pd.DataFrame(selected_rows)
    columns = [
        "Date",
        "Basin_ID",
        "Reservoir_Name",
        "Generation_MWh_Monthly",
        "Daily_Generation_MWh_MonthAvg",
        "Power_MW_MonthAvg",
        "Data_Source",
        "Source_File",
        "Source_Sheet",
        "Scenario",
        "Scenario_Order",
    ]
    return daily_df[columns].sort_values(["Basin_ID", "Date"]).reset_index(drop=True)


def save_to_sql(monthly_df: pd.DataFrame, daily_df: pd.DataFrame) -> None:
    HOCHUA_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    monthly_export = monthly_df.copy()
    daily_export = daily_df.copy()
    for frame in [monthly_export, daily_export]:
        if "Date" in frame.columns:
            frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    with sqlite3.connect(HOCHUA_DB_PATH) as conn:
        monthly_export.to_sql("generation_monthly", conn, if_exists="replace", index=False)
        daily_export.to_sql("generation_daily_features", conn, if_exists="replace", index=False)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_generation_monthly_basin_month ON generation_monthly(Basin_ID, Year, Month)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_generation_daily_basin_date ON generation_daily_features(Basin_ID, Date)")
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
            ("generation_monthly_rows", str(len(monthly_export))),
            ("generation_daily_features_rows", str(len(daily_export))),
        ]:
            conn.execute(
                """
                INSERT INTO hydroai_metadata(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )


def import_generation_to_sql() -> tuple[pd.DataFrame, pd.DataFrame]:
    historical_path = find_workbook("du lieu huan luyen du bao san luong buon kuop")
    plan_2026_path = find_workbook("tinh toan san luong bk 2026")
    monthly_df = pd.concat(
        [
            parse_historical_monthly_generation(historical_path),
            parse_2026_generation_plan(plan_2026_path),
        ],
        ignore_index=True,
    )
    monthly_df = monthly_df.drop_duplicates(
        subset=["Year", "Month", "Basin_ID", "Source_File", "Source_Sheet", "Scenario_Order"],
        keep="last",
    )
    daily_df = expand_monthly_to_daily(monthly_df)
    save_to_sql(monthly_df, daily_df)
    return monthly_df, daily_df


if __name__ == "__main__":
    monthly, daily = import_generation_to_sql()
    print(f"Da nap {len(monthly):,} dong san luong thang vao generation_monthly")
    print(f"Da tao {len(daily):,} dong san luong/cong suat ngay vao generation_daily_features")
    if not monthly.empty:
        summary = monthly.groupby(["Basin_ID", "Year"], as_index=False)["Generation_MWh_Monthly"].sum()
        print(summary.tail(12).to_string(index=False))
