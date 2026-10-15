from __future__ import annotations

import sys
import sqlite3

import numpy as np
import pandas as pd

from config import (
    ALL_BASINS_DATASET_FILE,
    ALL_BASINS_FEATURE_FILE,
    FORECAST_RAIN_HORIZONS,
    BASIN_NAMES,
    FEATURE_DATASET_FILE,
    HOCHUA_DB_PATH,
    OPERATION_LAGS,
    OPERATION_WINDOWS,
    OUTPUT_DIR,
    Q_LAGS,
    Q_WINDOWS,
    RAIN_LAGS,
    RAIN_WINDOWS,
    TARGET_COLUMN,
    TYPHOON_LAGS,
    TYPHOON_WINDOWS,
    WATER_LEVEL_LAGS,
    WATER_LEVEL_WINDOWS,
    ensure_project_dirs,
)


def save_dataframe_to_sqlite(dataframe: pd.DataFrame, table_name: str) -> None:
    if dataframe.empty:
        return
    HOCHUA_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    export_df = dataframe.copy()
    if "Date" in export_df.columns:
        export_df["Date"] = pd.to_datetime(export_df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    with sqlite3.connect(HOCHUA_DB_PATH) as conn:
        export_df.to_sql(table_name, conn, if_exists="replace", index=False)
        date_column = "Date" if "Date" in export_df.columns else None
        if date_column and "Basin_ID" in export_df.columns:
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_basin_date ON {table_name}(Basin_ID, Date)")
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
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (f"{table_name}_rows", str(len(export_df))),
        )


def safe_to_csv(dataframe: pd.DataFrame, output_file) -> None:
    try:
        dataframe.to_csv(output_file, index=False, encoding="utf-8-sig")
    except PermissionError:
        fallback_file = output_file.with_name(f"{output_file.stem}_new{output_file.suffix}")
        dataframe.to_csv(fallback_file, index=False, encoding="utf-8-sig")
        print(f"File dang bi khoa, da xuat ban moi: {fallback_file}")


def safe_to_excel(writer_func, output_file) -> None:
    try:
        writer_func(output_file)
    except PermissionError:
        fallback_file = output_file.with_name(f"{output_file.stem}_new{output_file.suffix}")
        writer_func(fallback_file)
        print(f"File dang bi khoa, da xuat ban moi: {fallback_file}")


def numeric_series(dataframe: pd.DataFrame, column: str) -> pd.Series:
    if column not in dataframe.columns:
        return pd.Series(np.nan, index=dataframe.index, dtype="float64")
    return pd.to_numeric(dataframe[column], errors="coerce")


def add_features(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Create leakage-safe daily features for each basin."""
    df = dataframe.copy().sort_values(["Basin_ID", "Date"])
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    if "Generation_MWh" in df.columns or "Daily_Generation_MWh_MonthAvg" in df.columns:
        actual_generation = numeric_series(df, "Generation_MWh")
        monthly_generation = numeric_series(df, "Daily_Generation_MWh_MonthAvg")
        monthly_power = numeric_series(df, "Power_MW_MonthAvg")
        df["Generation_MWh_Filled"] = actual_generation.combine_first(monthly_generation)
        df["Power_MW"] = (actual_generation / 24.0).combine_first(monthly_power)
    df["Year"] = df["Date"].dt.year
    df["Month"] = df["Date"].dt.month
    df["DayOfYear"] = df["Date"].dt.dayofyear
    df["Month_sin"] = np.sin(2 * np.pi * df["Month"] / 12)
    df["Month_cos"] = np.cos(2 * np.pi * df["Month"] / 12)
    df["Day_sin"] = np.sin(2 * np.pi * df["DayOfYear"] / 366)
    df["Day_cos"] = np.cos(2 * np.pi * df["DayOfYear"] / 366)

    frames: list[pd.DataFrame] = []
    for basin_id, group in df.groupby("Basin_ID", sort=False):
        basin = group.copy().sort_values("Date")
        if "Rain_Basin_mm" in basin.columns:
            rain = pd.to_numeric(basin["Rain_Basin_mm"], errors="coerce")
            observed_rain = rain.shift(1)
            basin["Observed_Rain_Lag1"] = observed_rain.round(2)
            for window in RAIN_WINDOWS:
                basin[f"Observed_Rain_{window}d"] = observed_rain.rolling(window, min_periods=1).sum().round(2)
                basin[f"Rain_{window}d_Previous"] = basin[f"Observed_Rain_{window}d"]
                basin[f"Rain_{window}d_mm"] = basin[f"Observed_Rain_{window}d"]
            for lag in RAIN_LAGS:
                lagged = rain.shift(lag).round(2)
                basin[f"Observed_Rain_Lag{lag}"] = lagged
                basin[f"Rain_Lag{lag}_mm"] = lagged
            for horizon in FORECAST_RAIN_HORIZONS:
                column = f"Forecast_Rain_D{horizon}"
                if column not in basin.columns:
                    basin[column] = 0.0

        typhoon_columns = [
            column
            for column in [
                "Typhoon_Count_500km",
                "Typhoon_Count_1000km",
                "Typhoon_Count_1500km",
                "Typhoon_Max_Wind_500km",
                "Typhoon_Max_Wind_1000km",
                "Typhoon_Max_Wind_1500km",
                "Typhoon_Landfall_Near_Count",
            ]
            if column in basin.columns
        ]
        for column in typhoon_columns:
            values = pd.to_numeric(basin[column], errors="coerce").fillna(0)
            known_values = values.shift(1).fillna(0)
            for lag in TYPHOON_LAGS:
                basin[f"Observed_Storm_{column}_Lag{lag}"] = values.shift(lag)
                basin[f"{column}_Lag{lag}"] = values.shift(lag)
            for window in TYPHOON_WINDOWS:
                basin[f"Observed_Storm_{column}_{window}d_sum"] = known_values.rolling(window, min_periods=1).sum()
                basin[f"Observed_Storm_{column}_{window}d_max"] = known_values.rolling(window, min_periods=1).max()
                basin[f"{column}_{window}d_sum"] = basin[f"Observed_Storm_{column}_{window}d_sum"]
                basin[f"{column}_{window}d_max"] = basin[f"Observed_Storm_{column}_{window}d_max"]
            if f"Forecast_Storm_{column}_D1" not in basin.columns:
                for horizon in FORECAST_RAIN_HORIZONS:
                    basin[f"Forecast_Storm_{column}_D{horizon}"] = 0.0

        if TARGET_COLUMN in basin.columns:
            q = pd.to_numeric(basin[TARGET_COLUMN], errors="coerce")
            q_history = q.shift(1)
            for lag in Q_LAGS:
                basin[f"Q_Lag{lag}"] = q.shift(lag)
            for window in Q_WINDOWS:
                basin[f"Q_{window}d_mean"] = q_history.rolling(window, min_periods=1).mean()
                basin[f"Q_{window}d_max"] = q_history.rolling(window, min_periods=1).max()

        if "WaterLevel_m" in basin.columns:
            water = pd.to_numeric(basin["WaterLevel_m"], errors="coerce")
            water_history = water.shift(1)
            for lag in WATER_LEVEL_LAGS:
                basin[f"WaterLevel_Lag{lag}"] = water.shift(lag)
            for window in WATER_LEVEL_WINDOWS:
                basin[f"WaterLevel_{window}d_mean"] = water_history.rolling(window, min_periods=1).mean()
                basin[f"WaterLevel_{window}d_min"] = water_history.rolling(window, min_periods=1).min()
                basin[f"WaterLevel_{window}d_max"] = water_history.rolling(window, min_periods=1).max()

        release_columns = [
            column
            for column in ["Q_turbine_m3s", "Q_spill_m3s", "Q_env_m3s"]
            if column in basin.columns
        ]
        if release_columns:
            release = sum(pd.to_numeric(basin[column], errors="coerce").fillna(0) for column in release_columns)
            release = release.where(basin[release_columns].notna().any(axis=1))
            basin["Q_release_total_m3s"] = release

        for source_column in [
            "Q_release_total_m3s",
            "Q_turbine_m3s",
            "Q_spill_m3s",
            "Storage_Mm3",
            "Generation_MWh",
            "Generation_MWh_Filled",
            "Daily_Generation_MWh_MonthAvg",
            "Power_MW",
            "Power_MW_MonthAvg",
            "Generation_MWh_PMIS_Daily",
            "Generation_Net_MWh_PMIS_Daily",
            "Self_Use_MWh_PMIS_Daily",
            "Self_Use_pct_PMIS_Daily",
            "Running_Hours_PMIS_Daily",
            "Power_MW_PMIS_Daily",
            "Turbine_Water_Mm3_PMIS_Daily",
            "Q_turbine_m3s_PMIS_Daily",
            "Specific_Water_m3_per_kWh_PMIS_Daily",
        ]:
            if source_column not in basin.columns:
                continue
            values = pd.to_numeric(basin[source_column], errors="coerce")
            history = values.shift(1)
            prefix = source_column.replace("_m3s", "").replace("_Mm3", "").replace("_MWh", "")
            for lag in OPERATION_LAGS:
                basin[f"{prefix}_Lag{lag}"] = values.shift(lag)
            for window in OPERATION_WINDOWS:
                basin[f"{prefix}_{window}d_mean"] = history.rolling(window, min_periods=1).mean()

        frames.append(basin)

    return pd.concat(frames, ignore_index=True)


def build_features() -> pd.DataFrame:
    ensure_project_dirs()
    if not ALL_BASINS_DATASET_FILE.exists():
        raise FileNotFoundError(f"Thieu {ALL_BASINS_DATASET_FILE}. Hay chay 05_build_dataset.py truoc.")

    all_df = pd.read_csv(ALL_BASINS_DATASET_FILE, parse_dates=["Date"])
    try:
        with sqlite3.connect(HOCHUA_DB_PATH) as conn:
            typhoon_df = pd.read_sql_query("SELECT * FROM typhoon_daily_features", conn, parse_dates=["Date"])
        all_df = all_df.merge(typhoon_df, on=["Date", "Basin_ID"], how="left")
        for column in all_df.columns:
            if column.startswith("Typhoon_Count_") or column.startswith("Typhoon_Max_Wind_") or column == "Typhoon_Landfall_Near_Count":
                all_df[column] = pd.to_numeric(all_df[column], errors="coerce").fillna(0)
            elif column.startswith("Typhoon_Min_Distance_"):
                all_df[column] = pd.to_numeric(all_df[column], errors="coerce").fillna(9999)
    except Exception as exc:
        print(f"Bo qua feature bao: {exc}")

    try:
        with sqlite3.connect(HOCHUA_DB_PATH) as conn:
            generation_df = pd.read_sql_query("SELECT * FROM generation_daily_features", conn, parse_dates=["Date"])
        all_df = all_df.merge(generation_df, on=["Date", "Basin_ID"], how="left", suffixes=("", "_GenerationPlan"))
        for column in ["Generation_MWh_Monthly", "Daily_Generation_MWh_MonthAvg", "Power_MW_MonthAvg"]:
            if column in all_df.columns:
                all_df[column] = pd.to_numeric(all_df[column], errors="coerce")
    except Exception as exc:
        print(f"Bo qua feature san luong/cong suat: {exc}")

    try:
        with sqlite3.connect(HOCHUA_DB_PATH) as conn:
            daily_generation = pd.read_sql_query("SELECT * FROM pmis_generation_daily", conn, parse_dates=["Date"])
        rename_columns = {
            "Generation_MWh": "Generation_MWh_PMIS_Daily",
            "Generation_Net_MWh": "Generation_Net_MWh_PMIS_Daily",
            "Self_Use_MWh": "Self_Use_MWh_PMIS_Daily",
            "Self_Use_pct": "Self_Use_pct_PMIS_Daily",
            "Running_Hours": "Running_Hours_PMIS_Daily",
            "Power_MW": "Power_MW_PMIS_Daily",
            "Turbine_Water_Mm3": "Turbine_Water_Mm3_PMIS_Daily",
            "Q_turbine_m3s": "Q_turbine_m3s_PMIS_Daily",
            "Specific_Water_m3_per_kWh": "Specific_Water_m3_per_kWh_PMIS_Daily",
            "Data_Source": "Data_Source_PMIS_GenerationDaily",
            "Source_File": "Source_File_PMIS_GenerationDaily",
            "Source_Sheet": "Source_Sheet_PMIS_GenerationDaily",
        }
        keep_columns = ["Date", "Basin_ID", *rename_columns.keys()]
        daily_generation = daily_generation[[column for column in keep_columns if column in daily_generation.columns]].rename(columns=rename_columns)
        all_df = all_df.merge(daily_generation, on=["Date", "Basin_ID"], how="left")
        for column in [
            "Generation_MWh_PMIS_Daily",
            "Generation_Net_MWh_PMIS_Daily",
            "Self_Use_MWh_PMIS_Daily",
            "Self_Use_pct_PMIS_Daily",
            "Running_Hours_PMIS_Daily",
            "Power_MW_PMIS_Daily",
            "Turbine_Water_Mm3_PMIS_Daily",
            "Q_turbine_m3s_PMIS_Daily",
            "Specific_Water_m3_per_kWh_PMIS_Daily",
        ]:
            if column in all_df.columns:
                all_df[column] = pd.to_numeric(all_df[column], errors="coerce")
        if "Generation_MWh_PMIS_Daily" in all_df.columns:
            base_generation = numeric_series(all_df, "Generation_MWh")
            all_df["Generation_MWh"] = all_df["Generation_MWh_PMIS_Daily"].combine_first(base_generation)
    except Exception as exc:
        print(f"Bo qua feature san luong/nuoc chay may ngay PMIS: {exc}")

    feature_df = add_features(all_df)

    for basin_id in BASIN_NAMES:
        basin_df = feature_df[feature_df["Basin_ID"] == basin_id].copy()
        safe_to_csv(basin_df, OUTPUT_DIR / f"HydroAI_{basin_id}_features.csv")

    safe_to_csv(feature_df, ALL_BASINS_FEATURE_FILE)

    def write_excel(path):
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            feature_df.to_excel(writer, sheet_name="All_Basins", index=False)
            for basin_id in BASIN_NAMES:
                feature_df[feature_df["Basin_ID"] == basin_id].to_excel(writer, sheet_name=f"{basin_id}_Features", index=False)

    safe_to_excel(write_excel, FEATURE_DATASET_FILE)

    save_dataframe_to_sqlite(all_df, "hydroai_dataset_daily")
    save_dataframe_to_sqlite(feature_df, "hydroai_features_daily")
    print(f"Da tao dataset dac trung: {FEATURE_DATASET_FILE}")
    print(f"Da luu HydroAI dataset vao database: {HOCHUA_DB_PATH}")
    return feature_df


if __name__ == "__main__":
    try:
        build_features()
    except Exception as exc:
        print("\nCHUONG TRINH DUNG DO LOI:")
        print(exc)
        sys.exit(1)
