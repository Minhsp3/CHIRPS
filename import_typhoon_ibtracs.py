from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from config import BASIN_NAMES, END_DATE, HOCHUA_DB_PATH, OUTLETS, OUTPUT_DIR, PMIS_DIR, START_DATE


IBTRACS_WP_URL = (
    "https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs/"
    "v04r01/access/csv/ibtracs.WP.list.v04r01.csv"
)
DEFAULT_INPUT = PMIS_DIR / "ibtracs.WP.list.v04r01.csv"
TRACK_TABLE = "typhoon_track_points"
DAILY_TABLE = "typhoon_daily_features"


def haversine_km(lon1, lat1, lon2, lat2):
    radius = 6371.0
    lon1 = np.deg2rad(lon1)
    lat1 = np.deg2rad(lat1)
    lon2 = math.radians(float(lon2))
    lat2 = math.radians(float(lat2))
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * math.cos(lat2) * np.sin(dlon / 2) ** 2
    return radius * 2 * np.arcsin(np.sqrt(a))


def read_ibtracs(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    wanted = {
        "SID",
        "SEASON",
        "NUMBER",
        "BASIN",
        "SUBBASIN",
        "NAME",
        "ISO_TIME",
        "NATURE",
        "LAT",
        "LON",
        "WMO_WIND",
        "WMO_PRES",
        "USA_WIND",
        "USA_PRES",
        "DIST2LAND",
        "LANDFALL",
    }
    df = pd.read_csv(
        path,
        skiprows=[1],
        usecols=lambda column: column in wanted,
        low_memory=False,
        na_values=["", " ", "NA", "NaN"],
    )
    df["ISO_TIME"] = pd.to_datetime(df["ISO_TIME"], errors="coerce")
    df = df.dropna(subset=["ISO_TIME", "LAT", "LON"]).copy()
    df = df[(df["ISO_TIME"] >= pd.Timestamp(start_date)) & (df["ISO_TIME"] <= pd.Timestamp(end_date) + pd.Timedelta(days=1))]

    for column in ["LAT", "LON", "WMO_WIND", "WMO_PRES", "USA_WIND", "USA_PRES", "DIST2LAND", "LANDFALL"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df["Wind_kt"] = df["WMO_WIND"].combine_first(df["USA_WIND"])
    df["Pressure_mb"] = df["WMO_PRES"].combine_first(df["USA_PRES"])
    df["NAME"] = df["NAME"].fillna("UNNAMED").astype(str).str.strip().replace("", "UNNAMED")
    df["Date"] = df["ISO_TIME"].dt.normalize()
    return df


def build_track_points(raw: pd.DataFrame) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for basin_id, (outlet_lon, outlet_lat) in OUTLETS.items():
        part = raw.copy()
        part["Basin_ID"] = basin_id
        part["Reservoir_Name"] = BASIN_NAMES[basin_id]
        part["Distance_To_Basin_km"] = haversine_km(part["LON"].to_numpy(), part["LAT"].to_numpy(), outlet_lon, outlet_lat)
        frames.append(part)

    tracks = pd.concat(frames, ignore_index=True)
    tracks = tracks[
        [
            "SID",
            "SEASON",
            "NAME",
            "ISO_TIME",
            "Date",
            "BASIN",
            "SUBBASIN",
            "NATURE",
            "LAT",
            "LON",
            "Wind_kt",
            "Pressure_mb",
            "DIST2LAND",
            "LANDFALL",
            "Basin_ID",
            "Reservoir_Name",
            "Distance_To_Basin_km",
        ]
    ].copy()
    tracks["ISO_TIME"] = tracks["ISO_TIME"].dt.strftime("%Y-%m-%d %H:%M:%S")
    tracks["Date"] = tracks["Date"].dt.strftime("%Y-%m-%d")
    return tracks


def names_join(values: pd.Series) -> str:
    names = sorted(set(str(value).strip() for value in values.dropna() if str(value).strip()))
    return "; ".join(names[:10])


def aggregate_daily(tracks: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    work = tracks.copy()
    rows = []
    for basin_id, basin in work.groupby("Basin_ID"):
        all_dates = pd.DataFrame({"Date": pd.date_range(start_date, end_date, freq="D").strftime("%Y-%m-%d")})
        all_dates["Basin_ID"] = basin_id
        daily_base = all_dates

        features = daily_base.copy()
        for threshold in [500, 1000, 1500]:
            near = basin[basin["Distance_To_Basin_km"] <= threshold]
            grouped = near.groupby(["Date", "Basin_ID"]).agg(
                **{
                    f"Typhoon_Count_{threshold}km": ("SID", "nunique"),
                    f"Typhoon_Max_Wind_{threshold}km": ("Wind_kt", "max"),
                    f"Typhoon_Min_Distance_{threshold}km": ("Distance_To_Basin_km", "min"),
                }
            ).reset_index()
            features = features.merge(grouped, on=["Date", "Basin_ID"], how="left")

        near1500 = basin[basin["Distance_To_Basin_km"] <= 1500]
        extra = near1500.groupby(["Date", "Basin_ID"]).agg(
            Typhoon_Min_Pressure_1500km=("Pressure_mb", "min"),
            Typhoon_Landfall_Near_Count=("LANDFALL", lambda s: int(pd.to_numeric(s, errors="coerce").le(100).sum())),
            Typhoon_Names_1500km=("NAME", names_join),
        ).reset_index()
        features = features.merge(extra, on=["Date", "Basin_ID"], how="left")
        rows.append(features)

    daily = pd.concat(rows, ignore_index=True)
    count_columns = [column for column in daily.columns if column.startswith("Typhoon_Count_") or column == "Typhoon_Landfall_Near_Count"]
    wind_columns = [column for column in daily.columns if column.startswith("Typhoon_Max_Wind_")]
    distance_columns = [column for column in daily.columns if column.startswith("Typhoon_Min_Distance_")]
    daily[count_columns] = daily[count_columns].fillna(0).astype(int)
    daily[wind_columns] = daily[wind_columns].fillna(0.0)
    daily[distance_columns] = daily[distance_columns].fillna(9999.0)
    daily["Typhoon_Min_Pressure_1500km"] = daily["Typhoon_Min_Pressure_1500km"].fillna(np.nan)
    daily["Typhoon_Names_1500km"] = daily["Typhoon_Names_1500km"].fillna("")
    return daily.sort_values(["Basin_ID", "Date"]).reset_index(drop=True)


def save_outputs(tracks: pd.DataFrame, daily: pd.DataFrame, db_path: Path) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    daily.to_csv(OUTPUT_DIR / "Typhoon_Daily_Features.csv", index=False, encoding="utf-8-sig")
    tracks.to_csv(OUTPUT_DIR / "Typhoon_Track_Points.csv", index=False, encoding="utf-8-sig")

    with sqlite3.connect(db_path) as conn:
        tracks.to_sql(TRACK_TABLE, conn, if_exists="replace", index=False)
        daily.to_sql(DAILY_TABLE, conn, if_exists="replace", index=False)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TRACK_TABLE}_basin_date ON {TRACK_TABLE}(Basin_ID, Date)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TRACK_TABLE}_sid ON {TRACK_TABLE}(SID)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{DAILY_TABLE}_basin_date ON {DAILY_TABLE}(Basin_ID, Date)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import NOAA IBTrACS Western Pacific typhoon data into HydroAI SQLite.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default=END_DATE)
    parser.add_argument("--db", type=Path, default=HOCHUA_DB_PATH)
    args = parser.parse_args()

    if not args.input.exists():
        raise FileNotFoundError(
            f"Thieu file {args.input}. Tai tu NOAA IBTrACS WP CSV: {IBTRACS_WP_URL}"
        )

    raw = read_ibtracs(args.input, args.start_date, args.end_date)
    tracks = build_track_points(raw)
    daily = aggregate_daily(tracks, args.start_date, args.end_date)
    save_outputs(tracks, daily, args.db)
    print(f"Imported {tracks['SID'].nunique():,} storms, {len(tracks):,} basin-track rows, {len(daily):,} daily feature rows.")
    print(f"Saved tables: {TRACK_TABLE}, {DAILY_TABLE}")


if __name__ == "__main__":
    main()
