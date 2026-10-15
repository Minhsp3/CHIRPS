from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    HOCHUA_DB_PATH,
    HYDROLOGY_DIR,
    HYDROLOGY_FILES,
    OUTPUT_DIR,
    PMIS_DIR,
    WATER_LEVEL_BOUNDS,
    ensure_project_dirs,
)

SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".csv"}

STANDARD_COLUMNS = [
    "Date",
    "Q_in_m3s",
    "WaterLevel_m",
    "Storage_Mm3",
    "Generation_MWh",
    "Q_turbine_m3s",
    "Q_spill_m3s",
    "Q_env_m3s",
    "Data_Source",
    "Source_File",
    "QC_Flag",
]

BASIN_KEYWORDS = {
    "BTS": ["bts", "buon tua srah", "buon_tua_srah", "buontuasrah", "tua srah"],
    "BK": ["bk", "buon kuop", "buon_kuop", "buonkuop"],
    "SP3": ["sp3", "srepok 3", "srepok_3", "srepok3", "sre pok 3"],
}

HOCHUA_RESERVOIR_PATTERNS = {
    "BTS": "Buôn Tua Srah%",
    "BK": "Buôn Kuốp%",
    "SP3": "Srêpốk 3%",
}

COLUMN_ALIASES = {
    "Date": ["date", "ngay", "ngay_thang", "thoi_gian", "datetime", "timestamp", "time", "ngay_gio", "ngaygio"],
    "Q_in_m3s": ["q_in_m3s", "q_in", "qve", "q_ve", "q ve", "q den", "luu_luong_ve", "luu luong ve", "inflow"],
    "WaterLevel_m": ["waterlevel_m", "water_level", "muc_nuoc", "muc nuoc", "mnh", "z ho", "cao_trinh_muc_nuoc"],
    "Storage_Mm3": ["storage_mm3", "storage", "dung_tich", "dung tich", "w ho", "v ho"],
    "Generation_MWh": ["generation_mwh", "generation", "san_luong", "san luong", "dien_luong", "mwh", "dien nang"],
    "Q_turbine_m3s": ["q_turbine_m3s", "q_turbine", "q_phat", "luu_luong_qua_may", "qtm"],
    "Q_spill_m3s": ["q_spill_m3s", "q_spill", "q_xa_tran", "xa_tran", "qtran", "q_tran"],
    "Q_env_m3s": ["q_env_m3s", "q_env", "q_moi_truong", "xa_moi_truong", "q duy tri"],
}


def remove_accents(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_text(value: object) -> str:
    text = remove_accents(value).lower().strip()
    text = re.sub(r"[\(\)\[\]{}]", " ", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


NORMALIZED_ALIASES = {
    standard: {normalize_text(alias) for alias in aliases}
    for standard, aliases in COLUMN_ALIASES.items()
}


def detect_basin(file_path: Path) -> str | None:
    searchable = normalize_text(file_path)
    for basin_id, keywords in BASIN_KEYWORDS.items():
        if any(normalize_text(keyword) in searchable for keyword in keywords):
            return basin_id
    return None


def find_header_row(raw: pd.DataFrame, max_rows: int = 25) -> int:
    best_row, best_score = 0, -1
    for row_index in range(min(max_rows, len(raw))):
        values = [normalize_text(value) for value in raw.iloc[row_index].tolist()]
        score = sum(any(value in aliases for aliases in NORMALIZED_ALIASES.values()) for value in values)
        if score > best_score:
            best_row, best_score = row_index, score
    return best_row


def read_file_smart(file_path: Path) -> list[pd.DataFrame]:
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        for encoding in ["utf-8-sig", "utf-8", "cp1258", "latin1"]:
            try:
                raw = pd.read_csv(file_path, header=None, encoding=encoding)
                header_row = find_header_row(raw)
                df = pd.read_csv(file_path, header=header_row, encoding=encoding)
                df["_Sheet_Name"] = "CSV"
                return [df]
            except Exception:
                continue
        raise RuntimeError(f"Khong doc duoc CSV: {file_path}")

    engine = "xlrd" if suffix == ".xls" else None
    excel = pd.ExcelFile(file_path, engine=engine)
    frames: list[pd.DataFrame] = []
    for sheet_name in excel.sheet_names:
        try:
            raw = pd.read_excel(file_path, sheet_name=sheet_name, header=None, engine=engine)
            if raw.empty:
                continue
            header_row = find_header_row(raw)
            df = pd.read_excel(file_path, sheet_name=sheet_name, header=header_row, engine=engine)
            if not df.empty:
                df["_Sheet_Name"] = sheet_name
                frames.append(df)
        except Exception as exc:
            print(f"  Bo qua sheet '{sheet_name}': {exc}")
    return frames


def map_columns(dataframe: pd.DataFrame) -> dict[object, str]:
    mapping: dict[object, str] = {}
    normalized_columns = {
        column: normalize_text(column)
        for column in dataframe.columns
        if not str(column).startswith("_")
    }
    for standard, aliases in NORMALIZED_ALIASES.items():
        exact = [original for original, normalized in normalized_columns.items() if normalized in aliases]
        if exact:
            mapping[exact[0]] = standard
            continue
        partial = [
            original
            for original, normalized in normalized_columns.items()
            if any(len(alias) >= 4 and (alias in normalized or normalized in alias) for alias in aliases)
        ]
        if partial:
            mapping[partial[0]] = standard
    return mapping


def manual_hydrology_report_mapping(dataframe: pd.DataFrame) -> dict[object, str]:
    normalized_columns = {
        column: normalize_text(column)
        for column in dataframe.columns
        if not str(column).startswith("_")
    }

    def pick(*patterns: str) -> object | None:
        for pattern in patterns:
            for original, normalized in normalized_columns.items():
                if pattern in normalized:
                    return original
        return None

    date_col = pick("ngay")
    q_in_col = pick("luu_luong_ve_ho", "luu_luong_ve")
    if date_col is None or q_in_col is None:
        return {}

    selections = {
        "Date": date_col,
        "Q_in_m3s": q_in_col,
        "WaterLevel_m": pick("muc_nuoc_thuong_luu", "muc_nuoc_cuoi_ngay"),
        "Storage_Mm3": pick("dt_muc_nuoc_ho", "dung_tich_huu_ich"),
        "Generation_MWh": pick("san_luong"),
        "Q_turbine_m3s": pick("luu_luong_chay_may"),
        "Q_spill_m3s": pick("tong_luu_luong_xa"),
        "Q_env_m3s": pick("ll_xa_van_moi_truong", "xa_van_moi_truong"),
    }
    return {source: standard for standard, source in selections.items() if source is not None}


def parse_numeric(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    cleaned = series.astype(str).str.strip().str.replace(" ", "", regex=False).str.replace("\u00a0", "", regex=False)
    both = cleaned.str.contains(r"\.", regex=True) & cleaned.str.contains(",", regex=False)
    cleaned.loc[both] = cleaned.loc[both].str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    comma_only = cleaned.str.contains(",", regex=False) & ~cleaned.str.contains(r"\.", regex=True)
    cleaned.loc[comma_only] = cleaned.loc[comma_only].str.replace(",", ".", regex=False)
    cleaned = cleaned.str.replace(r"[^0-9eE+\-.]", "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def parse_dates(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", dayfirst=True)
    numeric = pd.to_numeric(series, errors="coerce")
    serial_mask = parsed.isna() & numeric.between(20_000, 80_000)
    if serial_mask.any():
        parsed.loc[serial_mask] = pd.to_datetime(numeric.loc[serial_mask], unit="D", origin="1899-12-30", errors="coerce")
    return parsed


def standardize_sheet(dataframe: pd.DataFrame, basin_id: str, file_path: Path) -> pd.DataFrame:
    mapping = manual_hydrology_report_mapping(dataframe) or map_columns(dataframe)
    if "Date" not in mapping.values():
        return pd.DataFrame()

    result = dataframe.rename(columns=mapping).copy()
    result["DateTime"] = parse_dates(result["Date"])
    result = result.dropna(subset=["DateTime"]).copy()

    numeric_columns = [
        "Q_in_m3s",
        "WaterLevel_m",
        "Storage_Mm3",
        "Generation_MWh",
        "Q_turbine_m3s",
        "Q_spill_m3s",
        "Q_env_m3s",
    ]
    for column in numeric_columns:
        if column in result.columns:
            result[column] = parse_numeric(result[column])

    keep = ["DateTime"] + [column for column in numeric_columns if column in result.columns]
    result = result[keep].copy()
    result["Basin_ID"] = basin_id
    result["Data_Source"] = "PMIS"
    result["Source_File"] = file_path.name
    result["QC_Flag"] = "OK"
    return result


def aggregate_to_daily(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe
    df = dataframe.copy().sort_values("DateTime")
    df["Date"] = df["DateTime"].dt.normalize()
    aggregation: dict[str, object] = {}
    for column in ["Q_in_m3s", "Q_turbine_m3s", "Q_spill_m3s", "Q_env_m3s", "WaterLevel_m"]:
        if column in df.columns:
            aggregation[column] = "mean"
    if "Storage_Mm3" in df.columns:
        aggregation["Storage_Mm3"] = "last"
    if "Generation_MWh" in df.columns:
        aggregation["Generation_MWh"] = "sum"
    aggregation.update({
        "Basin_ID": "last",
        "Data_Source": "last",
        "Source_File": lambda values: "; ".join(sorted(set(str(v) for v in values if pd.notna(v)))),
        "QC_Flag": "last",
    })
    return df.groupby("Date", as_index=False).agg(aggregation)


def merge_with_existing(new_data: pd.DataFrame, output_file: Path) -> pd.DataFrame:
    existing = pd.DataFrame()
    if output_file.exists():
        try:
            existing = pd.read_csv(output_file) if output_file.suffix.lower() == ".csv" else pd.read_excel(output_file)
            if "Date" in existing.columns:
                existing["Date"] = pd.to_datetime(existing["Date"], errors="coerce").dt.normalize()
                existing = existing.dropna(subset=["Date"])
            else:
                existing = pd.DataFrame()
        except Exception as exc:
            print(f"  Khong doc duoc file hien co {output_file.name}: {exc}")

    combined = pd.concat([existing, new_data], ignore_index=True) if not existing.empty else new_data.copy()
    if combined.empty:
        combined = pd.DataFrame(columns=STANDARD_COLUMNS)
    else:
        combined = combined.sort_values("Date").drop_duplicates(subset=["Date"], keep="last").reset_index(drop=True)

    for column in STANDARD_COLUMNS:
        if column not in combined.columns:
            combined[column] = np.nan
    return combined[STANDARD_COLUMNS]


def overlay_hydrology(base: pd.DataFrame, update: pd.DataFrame) -> pd.DataFrame:
    if base.empty and update.empty:
        result = pd.DataFrame(columns=STANDARD_COLUMNS)
    elif base.empty:
        result = update.copy()
    elif update.empty:
        result = base.copy()
    else:
        left = base.copy()
        right = update.copy()
        left["Date"] = pd.to_datetime(left["Date"], errors="coerce").dt.normalize()
        right["Date"] = pd.to_datetime(right["Date"], errors="coerce").dt.normalize()
        merged = left.merge(right, on="Date", how="outer", suffixes=("_base", "_update"))
        result = pd.DataFrame({"Date": merged["Date"]})

        numeric_columns = [
            "Q_in_m3s",
            "WaterLevel_m",
            "Storage_Mm3",
            "Generation_MWh",
            "Q_turbine_m3s",
            "Q_spill_m3s",
            "Q_env_m3s",
        ]
        for column in numeric_columns:
            base_col = f"{column}_base"
            update_col = f"{column}_update"
            base_values = merged[base_col] if base_col in merged else pd.Series(np.nan, index=merged.index)
            update_values = merged[update_col] if update_col in merged else pd.Series(np.nan, index=merged.index)
            result[column] = update_values.combine_first(base_values)

        for column in ["Basin_ID", "Data_Source", "Source_File", "QC_Flag"]:
            base_col = f"{column}_base"
            update_col = f"{column}_update"
            base_values = merged[base_col] if base_col in merged else pd.Series(np.nan, index=merged.index)
            update_values = merged[update_col] if update_col in merged else pd.Series(np.nan, index=merged.index)
            result[column] = update_values.combine_first(base_values)

    for column in STANDARD_COLUMNS:
        if column not in result.columns:
            result[column] = np.nan
    result["Date"] = pd.to_datetime(result["Date"], errors="coerce").dt.normalize()
    result = result.dropna(subset=["Date"]).sort_values("Date").drop_duplicates("Date", keep="last")
    return result[STANDARD_COLUMNS].reset_index(drop=True)


def write_hydrology_file(dataframe: pd.DataFrame, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        if output_file.suffix.lower() == ".csv":
            dataframe.to_csv(output_file, index=False, encoding="utf-8-sig")
        else:
            dataframe.to_excel(output_file, index=False)
    except PermissionError:
        timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        fallback_file = output_file.with_name(f"{output_file.stem}_new_{timestamp}{output_file.suffix}")
        if output_file.suffix.lower() == ".csv":
            dataframe.to_csv(fallback_file, index=False, encoding="utf-8-sig")
        else:
            dataframe.to_excel(fallback_file, index=False)
        print(f"File dang bi khoa, da xuat ban moi: {fallback_file}")


def read_hochua_daily(hochua_db_path: Path, basin_id: str) -> pd.DataFrame:
    if not hochua_db_path.exists():
        return pd.DataFrame()
    pattern = HOCHUA_RESERVOIR_PATTERNS[basin_id]
    query = """
        WITH ranked AS (
            SELECT
                observed_at,
                h_tl,
                q_ve,
                q_xt,
                q_xm,
                sum_qx,
                requested_at_last_seen,
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY observed_at
                    ORDER BY requested_at_last_seen DESC, id DESC
                ) AS rn
            FROM reservoir_readings
            WHERE reservoir_name LIKE ?
              AND observed_at IS NOT NULL
        )
        SELECT
            DATE(observed_at) AS Date,
            AVG(q_ve) AS Q_in_m3s,
            AVG(h_tl) AS WaterLevel_m,
            AVG(q_xm) AS Q_turbine_m3s,
            AVG(q_xt) AS Q_spill_m3s,
            AVG(sum_qx) AS Q_total_release_m3s,
            COUNT(*) AS Samples_Per_Day
        FROM ranked
        WHERE rn = 1
        GROUP BY DATE(observed_at)
        ORDER BY DATE(observed_at)
    """
    with sqlite3.connect(hochua_db_path) as conn:
        df = pd.read_sql_query(query, conn, params=(pattern,))
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["Date"]).copy()
    low, high = WATER_LEVEL_BOUNDS[basin_id]
    df.loc[~df["WaterLevel_m"].between(low, high), "WaterLevel_m"] = np.nan
    df["Basin_ID"] = basin_id
    df["Storage_Mm3"] = np.nan
    df["Generation_MWh"] = np.nan
    df["Q_env_m3s"] = np.nan
    df["Data_Source"] = "EVN_HOCHUA_SQLITE"
    df["Source_File"] = hochua_db_path.name
    df["QC_Flag"] = np.where(df["Samples_Per_Day"] >= 18, "OK", "LOW_SAMPLES")
    return df


def import_hochua_sqlite(hochua_db_path: Path = HOCHUA_DB_PATH) -> dict[str, pd.DataFrame]:
    results: dict[str, pd.DataFrame] = {}
    if not hochua_db_path.exists():
        print(f"Khong tim thay database ho chua: {hochua_db_path}")
        return results

    print(f"Doc database ho chua: {hochua_db_path}")
    for basin_id, output_file in HYDROLOGY_FILES.items():
        daily = read_hochua_daily(hochua_db_path, basin_id)
        if daily.empty:
            print(f"  {basin_id}: khong co du lieu trong database.")
            results[basin_id] = pd.DataFrame(columns=STANDARD_COLUMNS)
            continue
        for column in STANDARD_COLUMNS:
            if column not in daily.columns:
                daily[column] = np.nan
        daily = daily[STANDARD_COLUMNS].sort_values("Date").reset_index(drop=True)
        write_hydrology_file(daily, output_file)
        results[basin_id] = daily
        print(
            f"  {basin_id}: {len(daily)} ngay, "
            f"{daily['Date'].min().date()} den {daily['Date'].max().date()}"
        )
    return results


def quality_report(dataframe: pd.DataFrame, basin_id: str) -> dict[str, object]:
    if dataframe.empty or dataframe["Date"].dropna().empty:
        return {"Basin_ID": basin_id, "Rows": 0, "Completeness_pct": 0.0}
    start, end = dataframe["Date"].min(), dataframe["Date"].max()
    expected = (end - start).days + 1

    def availability(column: str) -> float:
        return round(dataframe[column].notna().mean() * 100, 2) if column in dataframe.columns else 0.0

    return {
        "Basin_ID": basin_id,
        "Start_Date": start,
        "End_Date": end,
        "Rows": len(dataframe),
        "Expected_Days": expected,
        "Completeness_pct": round(len(dataframe) / expected * 100, 2) if expected else 0.0,
        "Q_Available_pct": availability("Q_in_m3s"),
        "WaterLevel_Available_pct": availability("WaterLevel_m"),
        "Generation_Available_pct": availability("Generation_MWh"),
    }


def discover_files(pmis_dir: Path) -> list[Path]:
    if not pmis_dir.exists():
        pmis_dir.mkdir(parents=True, exist_ok=True)
        raise FileNotFoundError(f"Da tao thu muc PMIS nhung chua co du lieu: {pmis_dir}")
    files = sorted(
        file
        for file in pmis_dir.rglob("*")
        if file.is_file() and file.suffix.lower() in SUPPORTED_EXTENSIONS and not file.name.startswith("~$")
    )
    if not files:
        raise FileNotFoundError(f"Khong tim thay file Excel/CSV trong: {pmis_dir}")
    return files


def import_pmis(pmis_dir: Path = PMIS_DIR) -> dict[str, pd.DataFrame]:
    ensure_project_dirs()
    HYDROLOGY_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    sqlite_results = import_hochua_sqlite(HOCHUA_DB_PATH)

    files = discover_files(pmis_dir)
    print(f"Tim thay {len(files)} file PMIS Excel/CSV.")

    collected = {basin_id: [] for basin_id in HYDROLOGY_FILES}
    skipped_files: list[dict[str, str]] = []

    for file_path in files:
        basin_id = detect_basin(file_path)
        if basin_id is None:
            skipped_files.append({"File": str(file_path), "Reason": "Khong nhan dien duoc BTS/BK/SP3"})
            continue
        print(f"\nDoc {basin_id}: {file_path}")
        try:
            successful = False
            for sheet in read_file_smart(file_path):
                standardized = standardize_sheet(sheet, basin_id, file_path)
                if standardized.empty:
                    continue
                collected[basin_id].append(standardized)
                successful = True
            if not successful:
                skipped_files.append({"File": str(file_path), "Reason": "Khong tim thay cot ngay hoac du lieu phu hop"})
        except Exception as exc:
            print(f"  Loi: {exc}")
            skipped_files.append({"File": str(file_path), "Reason": str(exc)})

    results: dict[str, pd.DataFrame] = {}
    reports: list[dict[str, object]] = []
    for basin_id, output_file in HYDROLOGY_FILES.items():
        raw = pd.concat(collected[basin_id], ignore_index=True) if collected[basin_id] else pd.DataFrame()
        if not raw.empty:
            if "Basin_ID" not in raw.columns:
                raw["Basin_ID"] = basin_id
            if "Data_Source" not in raw.columns:
                raw["Data_Source"] = "PMIS"
            if "Source_File" not in raw.columns:
                raw["Source_File"] = ""
            if "QC_Flag" not in raw.columns:
                raw["QC_Flag"] = "OK"
        daily = aggregate_to_daily(raw) if not raw.empty else pd.DataFrame()
        combined = overlay_hydrology(sqlite_results.get(basin_id, pd.DataFrame()), daily)
        write_hydrology_file(combined, output_file)
        results[basin_id] = combined
        reports.append(quality_report(combined, basin_id))
        print(f"Da cap nhat {output_file}: {len(combined)} ngay du lieu.")

    report_file = OUTPUT_DIR / "PMIS_Import_Report.xlsx"
    with pd.ExcelWriter(report_file, engine="openpyxl") as writer:
        pd.DataFrame(reports).to_excel(writer, sheet_name="Data_Quality", index=False)
        pd.DataFrame(skipped_files).to_excel(writer, sheet_name="Skipped_Files", index=False)
    print(f"Da xuat bao cao: {report_file}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Import va chuan hoa du lieu PMIS cho HydroAI v5.")
    parser.add_argument("--pmis-dir", type=Path, default=PMIS_DIR)
    parser.add_argument("--build-dataset", action="store_true", help="Tu chay lai 05_build_dataset.py sau khi import.")
    args = parser.parse_args()

    import_pmis(args.pmis_dir)
    if args.build_dataset:
        build_script = Path(__file__).with_name("05_build_dataset.py")
        subprocess.run([sys.executable, str(build_script)], check=True, cwd=Path(__file__).parent)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\nCHUONG TRINH DUNG DO LOI:")
        print(exc)
        sys.exit(1)
