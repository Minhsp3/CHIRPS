from __future__ import annotations

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

DAILY_DIR = BASE_DIR / "Daily"
DEM_DIR = BASE_DIR / "dem"
BASIN_DIR = BASE_DIR / "luu_vuc"
PMIS_DIR = BASE_DIR / "PMIS"
HYDROLOGY_DIR = BASE_DIR / "hydrology"
OUTPUT_DIR = BASE_DIR / "output"
MODEL_DIR = BASE_DIR / "models"
HOCHUA_DB_PATH = Path(
    os.getenv("HYDROAI_DB_PATH", str(PMIS_DIR / "evn_hochua_manual.db"))
).expanduser()
NSMO_WEATHER_DB_PATH = Path(r"D:\VScode\Project\LayDuLieu_NMSO\NSMO_1\nsmo_data.db")

BASIN_NAMES = {
    "BTS": "Buon_Tua_Srah",
    "BK": "Buon_Kuop",
    "SP3": "Srepok_3",
}

WATER_LEVEL_BOUNDS = {
    "BTS": (450.0, 500.0),
    "BK": (380.0, 430.0),
    "SP3": (240.0, 290.0),
}

OUTLETS = {
    "BTS": (108.06069243337487, 12.245219147474575),
    "BK": (107.933552, 12.527364),
    "SP3": (107.88876981346154, 12.754378941554707),
}

DEM_BOUNDS = (107.0, 11.2, 109.0, 13.4)  # west, south, east, north
DEM_FILE = DEM_DIR / "srtm30_srepok.tif"
STREAM_ACC_THRESHOLD = 50_000

START_YEAR = 1997
END_YEAR = 2026
START_DATE = f"{START_YEAR}-01-01"
END_DATE = f"{END_YEAR}-12-31"

CHIRPS_PATTERN = "chirps-v2.0.*.days_p05.nc"
CHIRPS_URL_TEMPLATE = (
    "https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_daily/netcdf/p05/"
    "chirps-v2.0.{year}.days_p05.nc"
)

HYDROLOGY_FILES = {
    basin_id: HYDROLOGY_DIR / f"{basin_id}_daily.csv"
    for basin_id in BASIN_NAMES
}

TARGET_COLUMN = "Q_in_m3s"
RAW_DATASET_FILE = OUTPUT_DIR / "HydroAI_Dataset_raw_v5.xlsx"
FEATURE_DATASET_FILE = OUTPUT_DIR / "HydroAI_Dataset_v5.xlsx"
ALL_BASINS_DATASET_FILE = OUTPUT_DIR / "HydroAI_All_Basins_dataset.csv"
ALL_BASINS_FEATURE_FILE = OUTPUT_DIR / "HydroAI_All_Basins_features.csv"
FORECAST_FILE = OUTPUT_DIR / "HydroAI_Forecast_All.xlsx"

TRAIN_MIN_ROWS = 730
TRAIN_RECOMMENDED_ROWS = 1500
MIN_FEATURE_COVERAGE = 0.70
MAX_FEATURE_CORRELATION = 0.98
WALK_FORWARD_START_YEAR = 2019
WALK_FORWARD_END_YEAR = 2025
RANDOM_SEARCH_ITERATIONS = 4
RANDOM_STATE = 42

RAIN_WINDOWS = [3, 7, 15, 30, 90]
RAIN_LAGS = [1, 2, 3, 5, 7, 14, 30]
FORECAST_RAIN_HORIZONS = list(range(1, 16))
Q_LAGS = [1, 2, 3, 7, 14, 30]
Q_WINDOWS = [3, 7, 15, 30]
WATER_LEVEL_LAGS = [1, 2, 3, 7, 14, 30]
WATER_LEVEL_WINDOWS = [3, 7, 15, 30]
OPERATION_LAGS = [1, 2, 3, 7, 14]
OPERATION_WINDOWS = [3, 7, 15]
TYPHOON_LAGS = [1, 2, 3, 5, 7]
TYPHOON_WINDOWS = [3, 7, 15]

ANALOG_TOP_N = 6
FORECAST_HORIZON_DAYS = 365
MAX_DAILY_WATER_CHANGE = {
    "BTS": 1.0,
    "BK": 0.75,
    "SP3": 0.75,
}

RESERVOIR_OPERATION_RULES = {
    "BTS": {
        "Name": "Buon Tua Srah annual regulation filling rule",
        "Target_Level_m": 487.5,
        "Ramp_Start": "09-01",
        "End_Nov_Target_m": 487.3,
        "End_Year_Target_m": 487.45,
        "Max_Level_m": 487.5,
    }
}

WEATHER_IMPACT_DAYS = 7
WEATHER_IMPACT_BASIN_WEIGHTS = {
    "BTS": {"mt": 0.55, "mn": 0.35, "mb": 0.10},
    "BK": {"mt": 0.55, "mn": 0.35, "mb": 0.10},
    "SP3": {"mt": 0.55, "mn": 0.35, "mb": 0.10},
}
WEATHER_RAIN_KEYWORDS = [
    "mua",
    "mưa",
    "dong",
    "dông",
    "giong",
    "giông",
    "bao",
    "bão",
    "ap thap",
    "áp thấp",
    "nhiet doi",
    "nhiệt đới",
]

DASHBOARD_PORT = 8501
API_HOST = os.getenv("HYDROAI_API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("HYDROAI_API_PORT", "8000"))


def ensure_project_dirs() -> None:
    for path in [DAILY_DIR, DEM_DIR, BASIN_DIR, PMIS_DIR, HYDROLOGY_DIR, OUTPUT_DIR, MODEL_DIR]:
        path.mkdir(parents=True, exist_ok=True)
