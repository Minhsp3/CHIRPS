from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from threadpoolctl import threadpool_limits

from config import (
    BASIN_NAMES,
    MAX_FEATURE_CORRELATION,
    MIN_FEATURE_COVERAGE,
    MODEL_DIR,
    OUTPUT_DIR,
    RANDOM_SEARCH_ITERATIONS,
    RANDOM_STATE,
    TRAIN_MIN_ROWS,
    TRAIN_RECOMMENDED_ROWS,
    WALK_FORWARD_END_YEAR,
    WALK_FORWARD_START_YEAR,
    ensure_project_dirs,
)

DEFAULT_TARGET_COLUMNS = ["Q_in_m3s", "WaterLevel_m"]
TARGET_COLUMNS = [
    target.strip()
    for target in os.environ.get("HYDROAI_TARGETS", ",".join(DEFAULT_TARGET_COLUMNS)).split(",")
    if target.strip()
]
EXCLUDE = {
    "Date",
    "Basin_ID",
    "Q_in_m3s",
    "WaterLevel_m",
    "Storage_Mm3",
    "Generation_MWh",
    "Q_turbine_m3s",
    "Q_spill_m3s",
    "Q_env_m3s",
}
GENERATION_SAME_DAY_EXCLUDE = {
    "Generation_MWh",
    "Generation_MWh_Filled",
    "Generation_MWh_PMIS_Daily",
    "Generation_Net_MWh_PMIS_Daily",
    "Self_Use_MWh_PMIS_Daily",
    "Self_Use_pct_PMIS_Daily",
    "Running_Hours_PMIS_Daily",
    "Power_MW",
    "Power_MW_PMIS_Daily",
    "Turbine_Water_Mm3_PMIS_Daily",
    "Q_turbine_m3s",
    "Q_turbine_m3s_PMIS_Daily",
    "Specific_Water_m3_per_kWh_PMIS_Daily",
}


def target_slug(target: str) -> str:
    if target == "Q_in_m3s":
        return "Q"
    if target == "WaterLevel_m":
        return "WaterLevel"
    if target == "Generation_MWh":
        return "Generation"
    return re.sub(r"[^A-Za-z0-9]+", "_", target).strip("_") or "Target"


def excluded_columns_for_target(target: str) -> set[str]:
    excluded = set(EXCLUDE)
    if target == "Generation_MWh":
        excluded.update(GENERATION_SAME_DAY_EXCLUDE)
    return excluded


def timestamp_suffix() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def safe_joblib_dump(bundle: dict[str, object], output_file: Path) -> Path:
    try:
        joblib.dump(bundle, output_file)
        return output_file
    except PermissionError:
        fallback_file = output_file.with_name(f"{output_file.stem}_new_{timestamp_suffix()}{output_file.suffix}")
        joblib.dump(bundle, fallback_file)
        print(f"File model dang bi khoa, da luu ban moi: {fallback_file}")
        return fallback_file


def safe_write_json(data: dict[str, object], output_file: Path) -> Path:
    try:
        with open(output_file, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        return output_file
    except PermissionError:
        fallback_file = output_file.with_name(f"{output_file.stem}_new_{timestamp_suffix()}{output_file.suffix}")
        with open(fallback_file, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        print(f"File metrics dang bi khoa, da luu ban moi: {fallback_file}")
        return fallback_file


def safe_to_csv(dataframe: pd.DataFrame, output_file: Path) -> Path:
    try:
        dataframe.to_csv(output_file, index=False, encoding="utf-8-sig")
        return output_file
    except PermissionError:
        fallback_file = output_file.with_name(f"{output_file.stem}_new_{timestamp_suffix()}{output_file.suffix}")
        dataframe.to_csv(fallback_file, index=False, encoding="utf-8-sig")
        print(f"File backtest dang bi khoa, da luu ban moi: {fallback_file}")
        return fallback_file


def safe_to_excel(dataframe: pd.DataFrame, output_file: Path) -> Path:
    try:
        dataframe.to_excel(output_file, index=False)
        return output_file
    except PermissionError:
        fallback_file = output_file.with_name(f"{output_file.stem}_new_{timestamp_suffix()}{output_file.suffix}")
        dataframe.to_excel(fallback_file, index=False)
        print(f"File metrics tong hop dang bi khoa, da luu ban moi: {fallback_file}")
        return fallback_file


def hydrology_metrics(actual: pd.Series, predicted: np.ndarray, dates: pd.Series) -> dict[str, float]:
    """Return hydrology-focused deterministic forecast metrics."""
    y = pd.to_numeric(actual, errors="coerce").astype(float)
    p = pd.Series(predicted, index=y.index, dtype="float64")
    mask = y.notna() & p.notna()
    y = y[mask]
    p = p[mask]
    dates = pd.to_datetime(dates.loc[mask], errors="coerce")
    if y.empty:
        return {}
    residual = p - y
    denom = float(((y - y.mean()) ** 2).sum())
    nse = 1.0 - float(((y - p) ** 2).sum()) / denom if denom else np.nan
    corr = float(y.corr(p)) if len(y) > 1 else np.nan
    beta = float(p.mean() / y.mean()) if y.mean() else np.nan
    gamma = float((p.std() / p.mean()) / (y.std() / y.mean())) if y.mean() and p.mean() and y.std() else np.nan
    kge = 1.0 - float(((corr - 1) ** 2 + (beta - 1) ** 2 + (gamma - 1) ** 2) ** 0.5) if np.isfinite(corr) else np.nan
    pbias = float(100.0 * (p - y).sum() / y.sum()) if y.sum() else np.nan
    peak_idx_actual = y.idxmax()
    peak_idx_pred = p.idxmax()
    peak_error = float(p.loc[peak_idx_pred] - y.loc[peak_idx_actual])
    peak_timing_error_days = float((dates.loc[peak_idx_pred] - dates.loc[peak_idx_actual]).days)
    monthly = pd.DataFrame({"Date": dates, "Actual": y, "Predicted": p}).dropna(subset=["Date"])
    monthly["YearMonth"] = monthly["Date"].dt.to_period("M")
    month_sum = monthly.groupby("YearMonth")[["Actual", "Predicted"]].sum()
    monthly_volume_error_pct = float((100.0 * (month_sum["Predicted"] - month_sum["Actual"]) / month_sum["Actual"].replace(0, np.nan)).abs().mean())
    return {
        "NSE": float(nse),
        "KGE": float(kge),
        "PBIAS": float(pbias),
        "Peak_Error": peak_error,
        "Peak_Timing_Error_Days": peak_timing_error_days,
        "Monthly_Volume_Error_pct": monthly_volume_error_pct,
        "Correlation": corr,
    }


def drop_bad_features(train_df: pd.DataFrame, features: list[str]) -> list[str]:
    """Filter features using only the training subset."""
    if not features:
        return []
    coverage = train_df[features].notna().mean()
    usable = coverage[coverage >= MIN_FEATURE_COVERAGE].index.tolist()
    if not usable:
        return []
    variances = train_df[usable].var(numeric_only=True)
    usable = variances[variances.fillna(0) > 0].index.tolist()
    if not usable:
        return []
    signature = train_df[usable].T.drop_duplicates().T
    usable = signature.columns.tolist()
    corr = train_df[usable].corr(numeric_only=True).abs()
    upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
    drop = [column for column in upper.columns if any(upper[column] > MAX_FEATURE_CORRELATION)]
    return [column for column in usable if column not in drop]


def select_features(
    df: pd.DataFrame,
    target_mask: pd.Series,
    target: str,
    min_coverage: float = MIN_FEATURE_COVERAGE,
) -> list[str]:
    excluded = excluded_columns_for_target(target)
    candidates = [column for column in df.select_dtypes(include=[np.number]).columns if column not in excluded]
    covered = [column for column in candidates if df.loc[target_mask, column].notna().mean() >= min_coverage]
    return drop_bad_features(df.loc[target_mask], covered)


def candidate_params() -> list[dict[str, object]]:
    rng = np.random.default_rng(RANDOM_STATE)
    grid = []
    for _ in range(RANDOM_SEARCH_ITERATIONS):
        grid.append(
            {
                "learning_rate": float(rng.choice([0.025, 0.035, 0.05, 0.075, 0.1])),
                "max_iter": int(rng.choice([200, 300, 500])),
                "max_leaf_nodes": int(rng.choice([15, 31, 63])),
                "max_depth": None if rng.random() < 0.35 else int(rng.choice([4, 6, 8, 10])),
                "l2_regularization": float(rng.choice([0.0, 0.1, 1.0, 3.0])),
                "min_samples_leaf": int(rng.choice([20, 35, 50, 80])),
                "random_state": RANDOM_STATE,
            }
        )
    return grid


def walk_forward_splits(model_df: pd.DataFrame) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    splits = []
    years = set(model_df["Date"].dt.year.dropna().astype(int))
    for test_year in range(WALK_FORWARD_START_YEAR, WALK_FORWARD_END_YEAR + 1):
        if test_year not in years:
            continue
        train_df = model_df[model_df["Date"].dt.year < test_year]
        test_df = model_df[model_df["Date"].dt.year == test_year]
        if len(train_df) >= TRAIN_MIN_ROWS and len(test_df) >= 30:
            splits.append((train_df, test_df))
    return splits


def fit_model(train_df: pd.DataFrame, target: str, features: list[str], params: dict[str, object]) -> HistGradientBoostingRegressor:
    model = HistGradientBoostingRegressor(**params)
    with threadpool_limits(limits=1):
        model.fit(train_df[features], train_df[target])
    return model


def score_params(model_df: pd.DataFrame, target: str, base_features: list[str], params: dict[str, object]) -> tuple[float, list[dict[str, object]]]:
    fold_rows = []
    scores = []
    for fold, (train_df, test_df) in enumerate(walk_forward_splits(model_df), start=1):
        features = drop_bad_features(train_df, base_features)
        if not features:
            continue
        model = fit_model(train_df, target, features, params)
        prediction = model.predict(test_df[features])
        rmse = float(mean_squared_error(test_df[target], prediction) ** 0.5)
        scores.append(rmse)
        row = {
            "Fold": fold,
            "Train_Start": train_df["Date"].min().strftime("%Y-%m-%d"),
            "Train_End": train_df["Date"].max().strftime("%Y-%m-%d"),
            "Test_Year": int(test_df["Date"].dt.year.iloc[0]),
            "Train_Rows": int(len(train_df)),
            "Test_Rows": int(len(test_df)),
            "Feature_Count": int(len(features)),
            "MAE": float(mean_absolute_error(test_df[target], prediction)),
            "RMSE": rmse,
            "R2": float(r2_score(test_df[target], prediction)),
        }
        row.update(hydrology_metrics(test_df[target], prediction, test_df["Date"]))
        fold_rows.append(row)
    return (float(np.mean(scores)) if scores else float("inf")), fold_rows


def train_target(basin_id: str, target: str) -> dict[str, object]:
    feature_file = OUTPUT_DIR / f"HydroAI_{basin_id}_features.csv"
    if not feature_file.exists():
        raise FileNotFoundError(f"Thieu {feature_file}. Hay chay 06_feature_engineering.py truoc.")

    df = pd.read_csv(feature_file, parse_dates=["Date"], low_memory=False)
    if target not in df.columns:
        raise KeyError(f"{basin_id} chua co cot muc tieu {target}.")

    target_mask = df[target].notna()
    features = select_features(df, target_mask, target)
    if not features:
        raise ValueError(f"{basin_id}/{target}: khong co feature nao du do phu de huan luyen.")

    model_df = df[["Date", target] + features].dropna(subset=[target]).sort_values("Date")
    if len(model_df) < TRAIN_MIN_ROWS:
        raise ValueError(f"{basin_id}/{target}: can toi thieu {TRAIN_MIN_ROWS} ngay co du lieu de huan luyen.")
    warning = ""
    if len(model_df) < TRAIN_RECOMMENDED_ROWS:
        warning = f"Khuyen nghi bo sung du lieu: moi co {len(model_df)} ngay, nen >{TRAIN_RECOMMENDED_ROWS} ngay."

    best_params: dict[str, object] | None = None
    best_score = float("inf")
    best_fold_rows: list[dict[str, object]] = []
    for params in candidate_params():
        score, fold_rows = score_params(model_df, target, features, params)
        if score < best_score:
            best_score = score
            best_params = params
            best_fold_rows = fold_rows
    if best_params is None or not best_fold_rows:
        best_params = {
            "learning_rate": 0.05,
            "max_iter": 500,
            "max_leaf_nodes": 31,
            "max_depth": None,
            "l2_regularization": 1.0,
            "min_samples_leaf": 20,
            "random_state": RANDOM_STATE,
        }

    split = int(len(model_df) * 0.8)
    train_df = model_df.iloc[:split]
    test_df = model_df.iloc[split:]
    final_features = drop_bad_features(model_df, features)
    if not final_features:
        raise ValueError(f"{basin_id}/{target}: khong con feature nao sau loc coverage/correlation.")
    model = fit_model(model_df, target, final_features, best_params)
    eval_model = fit_model(train_df, target, drop_bad_features(train_df, final_features), best_params)
    eval_features = eval_model.feature_names_in_.tolist()
    prediction = eval_model.predict(test_df[eval_features])

    slug = target_slug(target)
    metrics = {
        "Basin_ID": basin_id,
        "Target": target,
        "Rows": int(len(model_df)),
        "Train_Rows": int(len(train_df)),
        "Test_Rows": int(len(test_df)),
        "MAE": float(mean_absolute_error(test_df[target], prediction)),
        "RMSE": float(mean_squared_error(test_df[target], prediction) ** 0.5),
        "R2": float(r2_score(test_df[target], prediction)),
        "N_WalkForward_Folds": int(len(best_fold_rows)),
        "WalkForward_RMSE_Mean": float(np.mean([row["RMSE"] for row in best_fold_rows])) if best_fold_rows else np.nan,
        "Best_Params": best_params,
        "Warning": warning,
        "Model_Note": "Water level model kept for dashboard compatibility; v6 target design should compute water level from water balance when Z-V curves are available." if target == "WaterLevel_m" else "",
        "Features": final_features,
    }
    metrics.update(hydrology_metrics(test_df[target], prediction, test_df["Date"]))

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    bundle = {"model": model, "features": final_features, "target": target, "best_params": best_params}
    safe_joblib_dump(bundle, MODEL_DIR / f"model_{basin_id}_{slug}.joblib")
    if target == "Q_in_m3s":
        safe_joblib_dump(bundle, MODEL_DIR / f"model_{basin_id}.joblib")

    metric_file = MODEL_DIR / f"metrics_{basin_id}_{slug}.json"
    safe_write_json(metrics, metric_file)
    if target == "Q_in_m3s":
        safe_write_json(metrics, MODEL_DIR / f"metrics_{basin_id}.json")

    backtest = test_df[["Date", target]].copy()
    backtest[f"{slug}_Predicted"] = prediction
    safe_to_csv(backtest, OUTPUT_DIR / f"Backtest_{basin_id}_{slug}.csv")
    if best_fold_rows:
        safe_to_csv(pd.DataFrame(best_fold_rows), OUTPUT_DIR / f"WalkForward_{basin_id}_{slug}.csv")

    print(
        f"{basin_id}/{target}: MAE={metrics['MAE']:.2f}; RMSE={metrics['RMSE']:.2f}; "
        f"NSE={metrics.get('NSE', np.nan):.3f}; KGE={metrics.get('KGE', np.nan):.3f}; "
        f"Features={len(final_features)}"
    )
    return metrics


def train_all() -> list[dict[str, object]]:
    ensure_project_dirs()
    rows: list[dict[str, object]] = []
    for basin_id in BASIN_NAMES:
        for target in TARGET_COLUMNS:
            try:
                rows.append(train_target(basin_id, target))
            except Exception as exc:
                print(f"Khong huan luyen {basin_id}/{target}: {exc}")

    if rows:
        summary = [{key: value for key, value in row.items() if key != "Features"} for row in rows]
        if TARGET_COLUMNS == DEFAULT_TARGET_COLUMNS:
            metrics_xlsx = OUTPUT_DIR / "Model_Metrics.xlsx"
            metrics_json = OUTPUT_DIR / "metrics.json"
        else:
            suffix = "_".join(target_slug(target) for target in TARGET_COLUMNS)
            metrics_xlsx = OUTPUT_DIR / f"Model_Metrics_{suffix}.xlsx"
            metrics_json = OUTPUT_DIR / f"metrics_{suffix}.json"
        safe_to_excel(pd.DataFrame(summary), metrics_xlsx)
        safe_write_json({"models": rows, "created_at": datetime.now().isoformat(timespec="seconds")}, metrics_json)
    return rows


if __name__ == "__main__":
    try:
        train_all()
    except Exception as exc:
        print("\nCHUONG TRINH DUNG DO LOI:")
        print(exc)
        sys.exit(1)
