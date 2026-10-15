from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from pysheds.grid import Grid
from rasterio.features import shapes
from shapely.geometry import shape

from config import BASIN_DIR, BASIN_NAMES, DEM_FILE, OUTLETS, STREAM_ACC_THRESHOLD, ensure_project_dirs

if not hasattr(np, "in1d"):
    np.in1d = np.isin


def mask_to_polygon(mask: np.ndarray, transform, crs) -> gpd.GeoDataFrame:
    geometries = [
        shape(geom)
        for geom, value in shapes(mask.astype("uint8"), mask=mask, transform=transform)
        if int(value) == 1
    ]
    if not geometries:
        raise RuntimeError("Khong tao duoc polygon luu vuc.")

    basin = gpd.GeoDataFrame(geometry=geometries, crs=crs)
    basin = basin.dissolve().explode(index_parts=False).reset_index(drop=True)
    basin["geometry"] = basin.geometry.buffer(0)
    return basin


def delineate_one(basin_id: str) -> Path:
    ensure_project_dirs()
    if not DEM_FILE.exists():
        raise FileNotFoundError(f"Thieu DEM: {DEM_FILE}. Hay chay 02_download_DEM.py truoc.")
    if basin_id not in OUTLETS:
        raise KeyError(f"Khong co cau hinh outlet cho basin_id={basin_id}")

    lon, lat = OUTLETS[basin_id]
    print(f"Phan dinh luu vuc {basin_id}...")

    grid = Grid.from_raster(str(DEM_FILE))
    dem = grid.read_raster(str(DEM_FILE))
    dem = grid.resolve_flats(grid.fill_depressions(grid.fill_pits(dem)))

    dirmap = (64, 128, 1, 2, 4, 8, 16, 32)
    flow_direction = grid.flowdir(dem, dirmap=dirmap)
    accumulation = grid.accumulation(flow_direction, dirmap=dirmap)
    snap_lon, snap_lat = grid.snap_to_mask(accumulation > STREAM_ACC_THRESHOLD, (lon, lat))
    catchment = grid.catchment(x=snap_lon, y=snap_lat, fdir=flow_direction, dirmap=dirmap, xytype="coordinate")

    with rasterio.open(DEM_FILE) as src:
        basin = mask_to_polygon(np.asarray(catchment, dtype=bool), src.transform, src.crs)

    basin = basin.to_crs("EPSG:4326")
    basin["Basin_ID"] = basin_id
    basin["OutletLon"] = lon
    basin["OutletLat"] = lat
    basin["SnapLon"] = snap_lon
    basin["SnapLat"] = snap_lat

    output_file = BASIN_DIR / f"{BASIN_NAMES[basin_id]}.shp"
    basin.to_file(output_file, driver="ESRI Shapefile", encoding="UTF-8")

    area_km2 = basin.to_crs("EPSG:32648").area.sum() / 1_000_000
    print(f"Outlet snap: {snap_lon:.6f}, {snap_lat:.6f}; dien tich so bo: {area_km2:.2f} km2")
    print(f"Da xuat: {output_file}")
    return output_file


def create_all() -> dict[str, Path]:
    return {basin_id: delineate_one(basin_id) for basin_id in OUTLETS}


if __name__ == "__main__":
    try:
        create_all()
    except Exception as exc:
        print("\nCHUONG TRINH DUNG DO LOI:")
        print(exc)
        sys.exit(1)
