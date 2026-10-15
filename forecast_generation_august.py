from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from config import BASIN_NAMES, HOCHUA_DB_PATH, MODEL_DIR, OUTPUT_DIR, ensure_project_dirs


YEAR = 2026
MONTH = 8
TARGET = "Generation_MWh"
TABLE_DAILY = "hydroai_generation_forecast_daily"
TABLE_SUMMARY = "hydroai_generation_forecast_summary"


def safe_to_excel(sheets: dict[str, pd.DataFrame], output_file: Path) -> Path:
    try:
        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            for sheet_name, data in sheets.items():
                data.to_excel(writer, sheet_name=sheet_name, index=False)
        return output_file
    except PermissionError:
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback_file = output_file.with_name(f"{output_file.stem}_new_{suffix}{output_file.suffix}")
        with pd.ExcelWriter(fallback_file, engine="openpyxl") as writer:
            for sheet_name, data in sheets.items():
                data.to_excel(writer, sheet_name=sheet_name, index=False)
        print(f"File Excel dang bi khoa, da luu ban moi: {fallback_file}")
        return fallback_file


def safe_to_csv(data: pd.DataFrame, output_file: Path) -> Path:
    try:
        data.to_csv(output_file, index=False, encoding="utf-8-sig")
        return output_file
    except PermissionError:
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback_file = output_file.with_name(f"{output_file.stem}_new_{suffix}{output_file.suffix}")
        data.to_csv(fallback_file, index=False, encoding="utf-8-sig")
        print(f"File CSV dang bi khoa, da luu ban moi: {fallback_file}")
        return fallback_file


def load_model_bundle(basin_id: str) -> dict[str, object]:
    model_file = MODEL_DIR / f"model_{basin_id}_Generation.joblib"
    if not model_file.exists():
        raise FileNotFoundError(f"Thieu model san luong: {model_file}")
    return joblib.load(model_file)


def load_residual_bounds(basin_id: str) -> tuple[float, float]:
    backtest_file = OUTPUT_DIR / f"Backtest_{basin_id}_Generation.csv"
    if not backtest_file.exists():
        return (-0.15, 0.15)
    backtest = pd.read_csv(backtest_file, parse_dates=["Date"])
    pred_col = "Generation_Predicted"
    if TARGET not in backtest.columns or pred_col not in backtest.columns:
        return (-0.15, 0.15)
    month_rows = backtest[backtest["Date"].dt.month == MONTH].copy()
    sample = month_rows if len(month_rows) >= 20 else backtest
    residual = pd.to_numeric(sample[pred_col], errors="coerce") - pd.to_numeric(sample[TARGET], errors="coerce")
    residual = residual.dropna()
    if residual.empty:
        return (-0.15, 0.15)
    return (float(residual.quantile(0.10)), float(residual.quantile(0.90)))


def weighted_value(components: dict[str, tuple[float | None, float]]) -> tuple[float, str]:
    values = {name: (value, weight) for name, (value, weight) in components.items() if value is not None and np.isfinite(value)}
    if not values:
        return (0.0, "")
    total_weight = sum(weight for _, weight in values.values())
    if total_weight <= 0:
        return (0.0, "")
    blended = sum(float(value) * weight for value, weight in values.values()) / total_weight
    return (max(0.0, float(blended)), "+".join(values.keys()))


def climatology_for_date(history: pd.DataFrame, date: pd.Timestamp) -> float | None:
    sample = history[(history["Date"].dt.month == date.month) & (history["Date"].dt.day == date.day)]
    values = pd.to_numeric(sample[TARGET], errors="coerce").dropna()
    if values.empty:
        month_values = pd.to_numeric(history[history["Date"].dt.month == date.month][TARGET], errors="coerce").dropna()
        values = month_values
    return None if values.empty else float(values.median())


def trailing_value(series: pd.Series, date: pd.Timestamp, lag: int) -> float:
    target_date = date - pd.Timedelta(days=lag)
    if target_date not in series.index or pd.isna(series.loc[target_date]):
        return np.nan
    return float(series.loc[target_date])


def trailing_mean(series: pd.Series, date: pd.Timestamp, window: int) -> float:
    values = series[series.index < date].tail(window).dropna()
    return np.nan if values.empty else float(values.mean())


def build_model_input(
    df: pd.DataFrame,
    features: list[str],
    date: pd.Timestamp,
    series: pd.Series,
) -> pd.DataFrame:
    rows = df[df["Date"].eq(date)]
    if rows.empty:
        model_input = pd.DataFrame([{feature: np.nan for feature in features}])
    else:
        model_input = rows.iloc[[0]][features].copy()

    updates: dict[str, float] = {
        "Year": float(date.year),
        "Month": float(date.month),
        "DayOfYear": float(date.dayofyear),
        "Month_sin": float(np.sin(2 * np.pi * date.month / 12)),
        "Month_cos": float(np.cos(2 * np.pi * date.month / 12)),
        "Day_sin": float(np.sin(2 * np.pi * date.dayofyear / 366)),
        "Day_cos": float(np.cos(2 * np.pi * date.dayofyear / 366)),
    }

    for lag in [1, 2, 3, 7, 14, 30]:
        generation = trailing_value(series, date, lag)
        updates[f"Generation_Lag{lag}"] = generation
        updates[f"Generation_Filled_Lag{lag}"] = generation
        updates[f"Power_MW_PMIS_Daily_Lag{lag}"] = generation / 24.0 if np.isfinite(generation) else np.nan
    for window in [3, 7, 15, 30]:
        generation_mean = trailing_mean(series, date, window)
        updates[f"Generation_{window}d_mean"] = generation_mean
        updates[f"Generation_Filled_{window}d_mean"] = generation_mean
        updates[f"Power_MW_PMIS_Daily_{window}d_mean"] = generation_mean / 24.0 if np.isfinite(generation_mean) else np.nan

    for column in features:
        if column.startswith("Forecast_Storm_"):
            updates.setdefault(column, 0.0)
        if column.startswith("Observed_Storm_") and column not in updates:
            updates.setdefault(column, 0.0)

    for column, value in updates.items():
        if column in model_input.columns:
            model_input.loc[:, column] = value
    return model_input


def forecast_basin(basin_id: str) -> tuple[pd.DataFrame, dict[str, object]]:
    feature_file = OUTPUT_DIR / f"HydroAI_{basin_id}_features.csv"
    if not feature_file.exists():
        raise FileNotFoundError(f"Thieu feature file: {feature_file}")

    df = pd.read_csv(feature_file, parse_dates=["Date"], low_memory=False).sort_values("Date")
    if TARGET not in df.columns:
        raise KeyError(f"{basin_id} chua co cot {TARGET}")

    bundle = load_model_bundle(basin_id)
    model = bundle["model"]
    features = list(bundle["features"])
    for column in features:
        if column not in df.columns:
            df[column] = np.nan

    month_start = pd.Timestamp(YEAR, MONTH, 1)
    month_end = pd.Timestamp(YEAR, MONTH, calendar.monthrange(YEAR, MONTH)[1])
    dates = pd.date_range(month_start, month_end, freq="D")

    actual_mask = df[TARGET].notna()
    latest_actual = df.loc[actual_mask, "Date"].max() if actual_mask.any() else pd.NaT
    history = df[df["Date"] < month_start][["Date", TARGET]].copy()
    series = df[["Date", TARGET]].copy().set_index("Date")[TARGET].astype(float)
    residual_p10, residual_p90 = load_residual_bounds(basin_id)

    rows: list[dict[str, object]] = []
    for date in dates:
        day_row = df[df["Date"].eq(date)]
        actual = float(day_row[TARGET].iloc[0]) if not day_row.empty and pd.notna(day_row[TARGET].iloc[0]) else None
        if actual is not None:
            forecast_value = actual
            source = "Actual_PMIS"
            model_value = None
            plan_value = None
            analog_value = None
            recent_value = None
        else:
            model_value = None
            plan_value = None
            analog_value = climatology_for_date(df[df["Date"] < date][["Date", TARGET]], date)
            recent_values = series[series.index < date].tail(7).dropna()
            recent_value = None if recent_values.empty else float(recent_values.mean())

            model_input = build_model_input(df, features, date, series)
            model_value = max(0.0, float(model.predict(model_input)[0]))
            if not day_row.empty and "Daily_Generation_MWh_MonthAvg" in day_row:
                plan_raw = pd.to_numeric(day_row["Daily_Generation_MWh_MonthAvg"], errors="coerce").iloc[0]
                plan_value = None if pd.isna(plan_raw) else float(plan_raw)

            lead_day = max(1, int((date - latest_actual).days)) if pd.notna(latest_actual) else 8
            recent_weight = max(0.05, 0.45 * max(0, 8 - lead_day) / 7)
            plan_weight = min(0.45, 0.20 + 0.25 * lead_day / 31)
            model_weight = 0.35
            analog_weight = 0.20
            forecast_value, source = weighted_value(
                {
                    "AI": (model_value, model_weight),
                    "Plan": (plan_value, plan_weight),
                    "Analog": (analog_value, analog_weight),
                    "Recent7d": (recent_value, recent_weight),
                }
            )
            series.loc[date] = forecast_value

        p10 = max(0.0, forecast_value + residual_p10) if actual is None else forecast_value
        p90 = max(0.0, forecast_value + residual_p90) if actual is None else forecast_value
        rows.append(
            {
                "Date": date,
                "Year": YEAR,
                "Month": MONTH,
                "Basin_ID": basin_id,
                "Reservoir_Name": BASIN_NAMES.get(basin_id, basin_id),
                "Generation_MWh": actual,
                "Generation_Forecast_MWh": forecast_value,
                "Generation_P10_MWh": min(p10, p90),
                "Generation_P90_MWh": max(p10, p90),
                "Forecast_Power_MW": forecast_value / 24.0,
                "Model_Prediction_MWh": model_value,
                "Plan_Daily_MWh": plan_value,
                "Analog_Daily_MWh": analog_value,
                "Recent7d_MWh": recent_value,
                "Value_Type": "Actual" if actual is not None else "Forecast",
                "Blend_Source": source,
                "Actual_Cutoff": None if pd.isna(latest_actual) else latest_actual.strftime("%Y-%m-%d"),
            }
        )

    forecast = pd.DataFrame(rows)
    actual_mtd = float(forecast.loc[forecast["Value_Type"].eq("Actual"), "Generation_Forecast_MWh"].sum())
    forecast_rest = float(forecast.loc[forecast["Value_Type"].eq("Forecast"), "Generation_Forecast_MWh"].sum())
    summary = {
        "Basin_ID": basin_id,
        "Reservoir_Name": BASIN_NAMES.get(basin_id, basin_id),
        "Actual_Cutoff": None if pd.isna(latest_actual) else latest_actual.strftime("%Y-%m-%d"),
        "Actual_Days_In_August": int(forecast["Value_Type"].eq("Actual").sum()),
        "Forecast_Days_In_August": int(forecast["Value_Type"].eq("Forecast").sum()),
        "Actual_MTD_MWh": actual_mtd,
        "Forecast_Remaining_MWh": forecast_rest,
        "Forecast_August_Total_MWh": actual_mtd + forecast_rest,
        "Forecast_August_Avg_MW": (actual_mtd + forecast_rest) / (len(dates) * 24.0),
    }
    return forecast, summary


def save_to_sql(daily: pd.DataFrame, summary: pd.DataFrame) -> None:
    daily_sql = daily.copy()
    daily_sql["Date"] = pd.to_datetime(daily_sql["Date"]).dt.strftime("%Y-%m-%d")
    with sqlite3.connect(HOCHUA_DB_PATH) as conn:
        daily_sql.to_sql(TABLE_DAILY, conn, if_exists="replace", index=False)
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
        metadata = {
            "generation_forecast_month": f"{YEAR}-{MONTH:02d}",
            "generation_forecast_rows": str(len(daily_sql)),
            "generation_forecast_created_at": datetime.now().isoformat(timespec="seconds"),
        }
        for key, value in metadata.items():
            conn.execute(
                """
                INSERT INTO hydroai_metadata(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )


def main() -> None:
    ensure_project_dirs()
    daily_frames = []
    summary_rows = []
    for basin_id in BASIN_NAMES:
        daily, summary = forecast_basin(basin_id)
        daily_frames.append(daily)
        summary_rows.append(summary)

    daily_all = pd.concat(daily_frames, ignore_index=True)
    summary = pd.DataFrame(summary_rows)
    csv_file = safe_to_csv(daily_all, OUTPUT_DIR / "HydroAI_Generation_Forecast_August_2026.csv")
    excel_file = safe_to_excel(
        {"Daily": daily_all, "Summary": summary},
        OUTPUT_DIR / "HydroAI_Generation_Forecast_August_2026.xlsx",
    )
    save_to_sql(daily_all, summary)

    print(f"Da luu du bao san luong thang 8: {csv_file}")
    print(f"Da luu Excel: {excel_file}")
    print(summary.to_string(index=False))
    print(json.dumps({"csv": str(csv_file), "excel": str(excel_file)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
