from __future__ import annotations

import sys

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds

from config import (
    ALL_BASINS_DATASET_FILE,
    BASIN_DIR,
    BASIN_NAMES,
    CHIRPS_PATTERN,
    DAILY_DIR,
    END_DATE,
    HYDROLOGY_FILES,
    OUTPUT_DIR,
    RAW_DATASET_FILE,
    START_DATE,
    WATER_LEVEL_BOUNDS,
    ensure_project_dirs,
)

ALIASES = {
    "ngay": "Date",
    "date": "Date",
    "thoi_gian": "Date",
    "q_ve": "Q_in_m3s",
    "luu_luong_ve": "Q_in_m3s",
    "q_in_m3s": "Q_in_m3s",
    "muc_nuoc": "WaterLevel_m",
    "dung_tich": "Storage_Mm3",
    "san_luong": "Generation_MWh",
}


def normalize_column(name: object) -> str:
    key = str(name).strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
    return ALIASES.get(key, str(name).strip())


def parse_dates_smart(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip()
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    iso_mask = text.str.match(r"^\d{4}-\d{1,2}-\d{1,2}")
    if iso_mask.any():
        parsed.loc[iso_mask] = pd.to_datetime(text.loc[iso_mask], errors="coerce")
    if (~iso_mask).any():
        parsed.loc[~iso_mask] = pd.to_datetime(text.loc[~iso_mask], errors="coerce", dayfirst=True)
    return parsed


def find_name(ds: xr.Dataset, candidates: list[str]) -> str:
    names = set(ds.coords) | set(ds.dims)
    lookup = {name.lower(): name for name in names}
    for candidate in candidates:
        if candidate in names:
            return candidate
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    raise KeyError(f"Khong tim thay toa do {candidates}")


def make_mask(da: xr.DataArray, basin: gpd.GeoDataFrame, lat_name: str, lon_name: str) -> xr.DataArray:
    lat = da[lat_name].values
    lon = da[lon_name].values
    dx = abs(float(lon[1] - lon[0]))
    dy = abs(float(lat[1] - lat[0]))
    transform = from_bounds(
        float(lon.min()) - dx / 2,
        float(lat.min()) - dy / 2,
        float(lon.max()) + dx / 2,
        float(lat.max()) + dy / 2,
        len(lon),
        len(lat),
    )
    mask = geometry_mask(
        [geometry.__geo_interface__ for geometry in basin.geometry],
        out_shape=(len(lat), len(lon)),
        transform=transform,
        invert=True,
        all_touched=True,
    )
    if lat[0] < lat[-1]:
        mask = np.flipud(mask)
    return xr.DataArray(mask, coords={lat_name: da[lat_name], lon_name: da[lon_name]}, dims=(lat_name, lon_name))


def read_hydrology(path) -> pd.DataFrame:
    df = pd.read_csv(path) if path.suffix.lower() == ".csv" else pd.read_excel(path)
    df = df.rename(columns={column: normalize_column(column) for column in df.columns})
    if "Date" not in df.columns:
        raise KeyError(f"{path.name} thieu cot Date/Ngay")
    df["Date"] = parse_dates_smart(df["Date"]).dt.normalize()
    return df.dropna(subset=["Date"])


def extract_rain_one(rain: xr.DataArray, basin_id: str, lat_name: str, lon_name: str) -> pd.DataFrame:
    shapefile = BASIN_DIR / f"{BASIN_NAMES[basin_id]}.shp"
    if not shapefile.exists():
        raise FileNotFoundError(f"Thieu {shapefile}; hay chay 03_create_basin.py truoc.")

    basin = gpd.read_file(shapefile).to_crs("EPSG:4326")
    minx, miny, maxx, maxy = basin.total_bounds
    lat = rain[lat_name]
    lat_slice = slice(miny - 0.1, maxy + 0.1) if float(lat[0]) < float(lat[-1]) else slice(maxy + 0.1, miny - 0.1)
    subset = rain.sel({lon_name: slice(minx - 0.1, maxx + 0.1), lat_name: lat_slice})

    mask = make_mask(subset, basin, lat_name, lon_name)
    valid = subset.where(subset >= 0).where(mask)
    weights = np.cos(np.deg2rad(valid[lat_name]))
    mean = valid.weighted(weights).mean(dim=[lat_name, lon_name], skipna=True)

    df = mean.compute().to_series().reset_index()
    df.columns = ["Date", "Rain_Basin_mm"]
    df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
    df["Basin_ID"] = basin_id
    df["Rain_Basin_mm"] = df["Rain_Basin_mm"].round(2)
    return df


def extract_chirps_daily() -> dict[str, pd.DataFrame]:
    files = sorted(DAILY_DIR.glob(CHIRPS_PATTERN))
    if not files:
        raise FileNotFoundError(f"Khong co CHIRPS Daily trong {DAILY_DIR}. Hay chay 01_download_CHIRPS.py truoc.")

    print(f"Tim thay {len(files)} file CHIRPS.")
    ds = xr.open_mfdataset(
        [str(file) for file in files],
        combine="by_coords",
        chunks="auto",
        engine="netcdf4",
        parallel=False,
        data_vars="minimal",
        coords="minimal",
        compat="override",
    )
    try:
        lat_name = find_name(ds, ["latitude", "lat", "y"])
        lon_name = find_name(ds, ["longitude", "lon", "x"])
        time_name = find_name(ds, ["time", "date"])
        precip_name = "precip" if "precip" in ds.data_vars else list(ds.data_vars)[0]
        rain = ds[precip_name].sel({time_name: slice(START_DATE, END_DATE)})

        results = {}
        for basin_id in BASIN_NAMES:
            print(f"Trich mua {basin_id}...")
            df = extract_rain_one(rain, basin_id, lat_name, lon_name)
            results[basin_id] = df
            df.to_csv(OUTPUT_DIR / f"CHIRPS_{basin_id}_daily.csv", index=False, encoding="utf-8-sig")
        pd.concat(results.values(), ignore_index=True).to_csv(OUTPUT_DIR / "CHIRPS_All_Basins_daily.csv", index=False, encoding="utf-8-sig")
        return results
    finally:
        ds.close()


def build_one(basin_id: str, rain: pd.DataFrame) -> pd.DataFrame:
    hydrology_file = HYDROLOGY_FILES[basin_id]
    candidates = sorted(
        [hydrology_file, *hydrology_file.parent.glob(f"{hydrology_file.stem}_new*{hydrology_file.suffix}")],
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    )
    hydrology_file = next((candidate for candidate in candidates if candidate.exists()), hydrology_file)
    if hydrology_file.exists():
        hydro = read_hydrology(hydrology_file)
        df = rain.merge(hydro, on="Date", how="outer", suffixes=("", "_hydro"))
    else:
        df = rain.copy()
    if "WaterLevel_m" in df.columns:
        low, high = WATER_LEVEL_BOUNDS[basin_id]
        df.loc[~pd.to_numeric(df["WaterLevel_m"], errors="coerce").between(low, high), "WaterLevel_m"] = np.nan
    df["Basin_ID"] = basin_id
    return df.sort_values("Date")


def build_all() -> dict[str, pd.DataFrame]:
    ensure_project_dirs()
    rain_by_basin = extract_chirps_daily()
    results = {basin_id: build_one(basin_id, rain) for basin_id, rain in rain_by_basin.items()}

    for basin_id, df in results.items():
        df.to_csv(OUTPUT_DIR / f"HydroAI_{basin_id}_dataset.csv", index=False, encoding="utf-8-sig")

    all_df = pd.concat(results.values(), ignore_index=True)
    all_df.to_csv(ALL_BASINS_DATASET_FILE, index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(RAW_DATASET_FILE, engine="openpyxl") as writer:
        all_df.to_excel(writer, sheet_name="All_Basins", index=False)
        for basin_id, df in results.items():
            df.to_excel(writer, sheet_name=f"{basin_id}_Daily", index=False)

    print(f"Da tao dataset tho: {RAW_DATASET_FILE}")
    return results


if __name__ == "__main__":
    try:
        build_all()
    except Exception as exc:
        print("\nCHUONG TRINH DUNG DO LOI:")
        print(exc)
        sys.exit(1)
