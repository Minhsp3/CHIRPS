from __future__ import annotations

import sys
import warnings
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
import xarray as xr
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds

warnings.filterwarnings("ignore", category=RuntimeWarning)


# ============================================================
# 1. CẤU HÌNH ĐƯỜNG DẪN
# ============================================================

NETCDF_FILE = Path(r"D:\VScode\CHIRPS\chirps-v2.0.monthly.nc")

# Có thể khai báo một hoặc nhiều lưu vực.
BASIN_FILES = {
    "Buon_Tua_Srah": Path(r"D:\VScode\CHIRPS\luu_vuc\Buon_Tua_Srah.shp"),
    "Buon_Kuop": Path(r"D:\VScode\CHIRPS\luu_vuc\Buon_Kuop.shp"),
    "Srepok_3": Path(r"D:\VScode\CHIRPS\luu_vuc\Srepok_3.shp"),
}

OUTPUT_DIR = Path(r"D:\VScode\CHIRPS\output")

# Khoảng thời gian cần xuất.
START_DATE = "1981-01-01"
END_DATE = "2025-12-31"

# Tên biến mưa thường gặp trong CHIRPS.
POSSIBLE_PRECIP_NAMES = [
    "precip",
    "precipitation",
    "rainfall",
    "pr",
]

# Giá trị CHIRPS âm thường là NoData.
MIN_VALID_PRECIP = 0.0


# ============================================================
# 2. CÁC HÀM HỖ TRỢ
# ============================================================

def find_coordinate_name(
    dataset: xr.Dataset,
    candidates: list[str],
) -> str:
    """Tìm tên tọa độ trong Dataset."""
    names = set(dataset.coords) | set(dataset.dims)

    for candidate in candidates:
        if candidate in names:
            return candidate

    lower_lookup = {name.lower(): name for name in names}

    for candidate in candidates:
        if candidate.lower() in lower_lookup:
            return lower_lookup[candidate.lower()]

    raise KeyError(
        f"Không tìm thấy tọa độ trong các tên: {candidates}. "
        f"Các tọa độ hiện có: {list(dataset.coords)}"
    )


def find_precipitation_variable(dataset: xr.Dataset) -> str:
    """Tự động tìm biến lượng mưa."""
    for name in POSSIBLE_PRECIP_NAMES:
        if name in dataset.data_vars:
            return name

    # Tìm theo thuộc tính standard_name hoặc long_name.
    for name, variable in dataset.data_vars.items():
        attributes = " ".join(
            str(value).lower()
            for value in variable.attrs.values()
        )

        if (
            "precip" in attributes
            or "rainfall" in attributes
            or "rain" in attributes
        ):
            return name

    # Nếu file chỉ có một biến dữ liệu thì dùng biến đó.
    if len(dataset.data_vars) == 1:
        return list(dataset.data_vars)[0]

    raise KeyError(
        "Không tự xác định được biến lượng mưa. "
        f"Các biến trong file: {list(dataset.data_vars)}"
    )


def inspect_netcdf(file_path: Path) -> None:
    """In thông tin cơ bản của NetCDF."""
    print("\n" + "=" * 70)
    print("KIỂM TRA FILE NETCDF")
    print("=" * 70)

    with xr.open_dataset(
        file_path,
        decode_times=True,
        chunks={},
    ) as dataset:
        print(dataset)
        print("\nCác biến dữ liệu:", list(dataset.data_vars))
        print("Các tọa độ:", list(dataset.coords))
        print("Các kích thước:", dict(dataset.sizes))


def load_basin(
    basin_path: Path,
    target_crs: str = "EPSG:4326",
) -> gpd.GeoDataFrame:
    """Đọc và chuẩn hóa shapefile lưu vực."""
    if not basin_path.exists():
        raise FileNotFoundError(
            f"Không tìm thấy shapefile: {basin_path}"
        )

    basin = gpd.read_file(basin_path)

    if basin.empty:
        raise ValueError(
            f"Shapefile không có đối tượng hình học: {basin_path}"
        )

    basin = basin[basin.geometry.notna()].copy()
    basin = basin[~basin.geometry.is_empty].copy()

    if basin.empty:
        raise ValueError(
            f"Shapefile không có geometry hợp lệ: {basin_path}"
        )

    if basin.crs is None:
        raise ValueError(
            f"Shapefile chưa có hệ tọa độ CRS: {basin_path}\n"
            "Hãy mở trong QGIS, gán đúng CRS rồi lưu lại."
        )

    basin = basin.to_crs(target_crs)

    # Sửa lỗi geometry nhẹ nếu có.
    basin["geometry"] = basin.geometry.buffer(0)

    # Gộp tất cả polygon của lưu vực thành một đối tượng.
    merged_geometry = basin.geometry.union_all()

    return gpd.GeoDataFrame(
        {"basin": [basin_path.stem]},
        geometry=[merged_geometry],
        crs=target_crs,
    )


def normalize_longitudes(
    rainfall: xr.DataArray,
    longitude_name: str,
) -> xr.DataArray:
    """
    Chuyển kinh độ 0..360 sang -180..180 nếu cần.
    """
    longitude = rainfall[longitude_name]

    if float(longitude.max()) > 180:
        rainfall = rainfall.assign_coords(
            {
                longitude_name: (
                    (longitude + 180) % 360
                ) - 180
            }
        )
        rainfall = rainfall.sortby(longitude_name)

    return rainfall


def crop_to_bounding_box(
    rainfall: xr.DataArray,
    basin: gpd.GeoDataFrame,
    latitude_name: str,
    longitude_name: str,
    padding_degrees: float = 0.10,
) -> xr.DataArray:
    """
    Cắt sơ bộ theo hình chữ nhật bao lưu vực.
    Việc này làm giảm mạnh lượng dữ liệu cần xử lý.
    """
    min_x, min_y, max_x, max_y = basin.total_bounds

    min_x -= padding_degrees
    min_y -= padding_degrees
    max_x += padding_degrees
    max_y += padding_degrees

    latitude = rainfall[latitude_name]

    # Xử lý trường hợp vĩ độ tăng hoặc giảm.
    if latitude[0] < latitude[-1]:
        latitude_slice = slice(min_y, max_y)
    else:
        latitude_slice = slice(max_y, min_y)

    subset = rainfall.sel(
        {
            longitude_name: slice(min_x, max_x),
            latitude_name: latitude_slice,
        }
    )

    if (
        subset.sizes.get(latitude_name, 0) == 0
        or subset.sizes.get(longitude_name, 0) == 0
    ):
        raise ValueError(
            "Không lấy được ô lưới CHIRPS trong phạm vi lưu vực. "
            "Hãy kiểm tra CRS và tọa độ shapefile."
        )

    return subset


def create_basin_mask(
    rainfall: xr.DataArray,
    basin: gpd.GeoDataFrame,
    latitude_name: str,
    longitude_name: str,
) -> xr.DataArray:
    """
    Tạo mặt nạ raster tại các ô lưới nằm trong lưu vực.
    """
    latitudes = rainfall[latitude_name].values
    longitudes = rainfall[longitude_name].values

    height = len(latitudes)
    width = len(longitudes)

    if height < 2 or width < 2:
        raise ValueError(
            "Phạm vi lưu vực quá nhỏ hoặc không có đủ ô lưới."
        )

    # Kích thước ô lưới.
    x_resolution = abs(
        float(longitudes[1]) - float(longitudes[0])
    )
    y_resolution = abs(
        float(latitudes[1]) - float(latitudes[0])
    )

    west = float(longitudes.min()) - x_resolution / 2
    east = float(longitudes.max()) + x_resolution / 2
    south = float(latitudes.min()) - y_resolution / 2
    north = float(latitudes.max()) + y_resolution / 2

    transform = from_bounds(
        west,
        south,
        east,
        north,
        width,
        height,
    )

    geometries = [
        geometry.__geo_interface__
        for geometry in basin.geometry
    ]

    # rasterio mặc định hàng đầu tiên ứng với phía Bắc.
    mask_north_to_south = geometry_mask(
        geometries=geometries,
        out_shape=(height, width),
        transform=transform,
        invert=True,
        all_touched=True,
    )

    # Nếu vĩ độ của xarray đi từ Nam lên Bắc,
    # đảo mặt nạ theo trục dọc.
    if latitudes[0] < latitudes[-1]:
        mask_values = np.flipud(mask_north_to_south)
    else:
        mask_values = mask_north_to_south

    return xr.DataArray(
        mask_values,
        coords={
            latitude_name: rainfall[latitude_name],
            longitude_name: rainfall[longitude_name],
        },
        dims=(latitude_name, longitude_name),
        name="basin_mask",
    )


def calculate_area_weighted_mean(
    rainfall: xr.DataArray,
    mask: xr.DataArray,
    latitude_name: str,
    longitude_name: str,
) -> xr.DataArray:
    """
    Tính lượng mưa trung bình lưu vực có trọng số diện tích.

    Vì dữ liệu nằm trên lưới kinh-vĩ độ, diện tích ô lưới giảm
    theo vĩ độ. Trọng số gần đúng là cos(vĩ độ).
    """
    valid_rainfall = rainfall.where(
        rainfall >= MIN_VALID_PRECIP
    )

    masked_rainfall = valid_rainfall.where(mask)

    latitude_weights = np.cos(
        np.deg2rad(masked_rainfall[latitude_name])
    )

    weighted_mean = masked_rainfall.weighted(
        latitude_weights
    ).mean(
        dim=[latitude_name, longitude_name],
        skipna=True,
    )

    return weighted_mean


def data_array_to_dataframe(
    monthly_rainfall: xr.DataArray,
    basin_name: str,
    time_name: str,
) -> pd.DataFrame:
    """Chuyển kết quả xarray sang bảng pandas."""
    series = monthly_rainfall.compute().to_series()

    dataframe = series.reset_index()
    dataframe.columns = ["Thoi_gian", "Mua_mm"]

    dataframe["Thoi_gian"] = pd.to_datetime(
        dataframe["Thoi_gian"]
    )

    dataframe["Nam"] = dataframe["Thoi_gian"].dt.year
    dataframe["Thang"] = dataframe["Thoi_gian"].dt.month
    dataframe["Luu_vuc"] = basin_name

    dataframe["Mua_mm"] = dataframe["Mua_mm"].round(2)

    dataframe = dataframe[
        [
            "Luu_vuc",
            "Thoi_gian",
            "Nam",
            "Thang",
            "Mua_mm",
        ]
    ]

    dataframe = dataframe.sort_values(
        ["Nam", "Thang"]
    ).reset_index(drop=True)

    return dataframe


def add_rainfall_features(
    dataframe: pd.DataFrame,
) -> pd.DataFrame:
    """
    Bổ sung các biến có ích cho mô hình dự báo thủy văn.
    """
    result = dataframe.copy()

    result["Mua_luy_ke_2T_mm"] = (
        result["Mua_mm"]
        .rolling(window=2, min_periods=1)
        .sum()
        .round(2)
    )

    result["Mua_luy_ke_3T_mm"] = (
        result["Mua_mm"]
        .rolling(window=3, min_periods=1)
        .sum()
        .round(2)
    )

    result["Mua_luy_ke_6T_mm"] = (
        result["Mua_mm"]
        .rolling(window=6, min_periods=1)
        .sum()
        .round(2)
    )

    result["Mua_T_truoc_mm"] = (
        result["Mua_mm"].shift(1).round(2)
    )

    result["Mua_2T_truoc_mm"] = (
        result["Mua_mm"].shift(2).round(2)
    )

    result["Mua_3T_truoc_mm"] = (
        result["Mua_mm"].shift(3).round(2)
    )

    return result


def make_monthly_pivot(
    dataframe: pd.DataFrame,
) -> pd.DataFrame:
    """Tạo bảng năm × 12 tháng."""
    pivot = dataframe.pivot(
        index="Nam",
        columns="Thang",
        values="Mua_mm",
    )

    pivot = pivot.rename(
        columns={
            month: f"T{month}"
            for month in pivot.columns
        }
    )

    pivot["Tong_nam_mm"] = pivot.sum(
        axis=1,
        min_count=1,
    ).round(2)

    pivot = pivot.reset_index()

    return pivot


def export_to_excel(
    basin_results: dict[str, pd.DataFrame],
    output_file: Path,
) -> None:
    """Xuất nhiều lưu vực vào một file Excel."""
    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    all_data = pd.concat(
        basin_results.values(),
        ignore_index=True,
    )

    with pd.ExcelWriter(
        output_file,
        engine="openpyxl",
    ) as writer:
        all_data.to_excel(
            writer,
            sheet_name="Tat_ca_luu_vuc",
            index=False,
        )

        for basin_name, dataframe in basin_results.items():
            safe_sheet_name = basin_name[:31]

            dataframe.to_excel(
                writer,
                sheet_name=safe_sheet_name,
                index=False,
            )

            pivot = make_monthly_pivot(dataframe)

            pivot_sheet_name = (
                f"{basin_name}_bang"
            )[:31]

            pivot.to_excel(
                writer,
                sheet_name=pivot_sheet_name,
                index=False,
            )

    print(f"\nĐã xuất Excel: {output_file}")


def create_plot(
    dataframe: pd.DataFrame,
    basin_name: str,
    output_dir: Path,
) -> None:
    """Vẽ biểu đồ lượng mưa theo thời gian."""
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    figure, axis = plt.subplots(
        figsize=(14, 6)
    )

    axis.plot(
        dataframe["Thoi_gian"],
        dataframe["Mua_mm"],
        linewidth=0.8,
    )

    axis.set_title(
        f"Lượng mưa trung bình tháng - {basin_name}"
    )
    axis.set_xlabel("Thời gian")
    axis.set_ylabel("Lượng mưa (mm/tháng)")
    axis.grid(True, alpha=0.25)

    figure.tight_layout()

    output_file = output_dir / f"{basin_name}_monthly.png"

    figure.savefig(
        output_file,
        dpi=180,
        bbox_inches="tight",
    )

    plt.close(figure)


# ============================================================
# 3. XỬ LÝ MỘT LƯU VỰC
# ============================================================

def process_one_basin(
    rainfall: xr.DataArray,
    basin_name: str,
    basin_file: Path,
    latitude_name: str,
    longitude_name: str,
    time_name: str,
) -> pd.DataFrame:
    print("\n" + "-" * 70)
    print(f"Đang xử lý: {basin_name}")
    print(f"Shapefile: {basin_file}")
    print("-" * 70)

    basin = load_basin(basin_file)

    print(
        "Phạm vi lưu vực:",
        basin.total_bounds,
    )

    subset = crop_to_bounding_box(
        rainfall=rainfall,
        basin=basin,
        latitude_name=latitude_name,
        longitude_name=longitude_name,
    )

    print(
        "Kích thước dữ liệu sau khi cắt sơ bộ:",
        dict(subset.sizes),
    )

    basin_mask = create_basin_mask(
        rainfall=subset,
        basin=basin,
        latitude_name=latitude_name,
        longitude_name=longitude_name,
    )

    number_of_cells = int(
        basin_mask.sum().values
    )

    print(
        f"Số ô lưới CHIRPS nằm trong lưu vực: "
        f"{number_of_cells}"
    )

    if number_of_cells == 0:
        raise ValueError(
            f"Không có ô CHIRPS nào trong lưu vực {basin_name}."
        )

    monthly_mean = calculate_area_weighted_mean(
        rainfall=subset,
        mask=basin_mask,
        latitude_name=latitude_name,
        longitude_name=longitude_name,
    )

    dataframe = data_array_to_dataframe(
        monthly_rainfall=monthly_mean,
        basin_name=basin_name,
        time_name=time_name,
    )

    dataframe = add_rainfall_features(dataframe)

    print("\n5 dòng đầu:")
    print(dataframe.head())

    print("\n5 dòng cuối:")
    print(dataframe.tail())

    return dataframe


# ============================================================
# 4. CHƯƠNG TRÌNH CHÍNH
# ============================================================

def main() -> None:
    if not NETCDF_FILE.exists():
        raise FileNotFoundError(
            f"Không tìm thấy file NetCDF:\n{NETCDF_FILE}"
        )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    inspect_netcdf(NETCDF_FILE)

    print("\nĐang mở CHIRPS bằng xarray...")

    dataset = xr.open_dataset(
        NETCDF_FILE,
        decode_times=True,
        chunks="auto",
        engine="netcdf4",
    )

    try:
        latitude_name = find_coordinate_name(
            dataset,
            ["latitude", "lat", "y"],
        )

        longitude_name = find_coordinate_name(
            dataset,
            ["longitude", "lon", "x"],
        )

        time_name = find_coordinate_name(
            dataset,
            ["time", "date"],
        )

        precip_name = find_precipitation_variable(
            dataset
        )

        print("\nĐã nhận diện:")
        print(f"  Biến mưa: {precip_name}")
        print(f"  Vĩ độ: {latitude_name}")
        print(f"  Kinh độ: {longitude_name}")
        print(f"  Thời gian: {time_name}")

        rainfall = dataset[precip_name]

        rainfall = normalize_longitudes(
            rainfall,
            longitude_name,
        )

        rainfall = rainfall.sel(
            {
                time_name: slice(
                    START_DATE,
                    END_DATE,
                )
            }
        )

        print(
            "\nKhoảng thời gian xử lý:",
            str(rainfall[time_name].min().values),
            "đến",
            str(rainfall[time_name].max().values),
        )

        basin_results: dict[str, pd.DataFrame] = {}

        for basin_name, basin_file in BASIN_FILES.items():
            try:
                result = process_one_basin(
                    rainfall=rainfall,
                    basin_name=basin_name,
                    basin_file=basin_file,
                    latitude_name=latitude_name,
                    longitude_name=longitude_name,
                    time_name=time_name,
                )

                basin_results[basin_name] = result

                csv_file = (
                    OUTPUT_DIR
                    / f"CHIRPS_{basin_name}_monthly.csv"
                )

                result.to_csv(
                    csv_file,
                    index=False,
                    encoding="utf-8-sig",
                )

                create_plot(
                    dataframe=result,
                    basin_name=basin_name,
                    output_dir=OUTPUT_DIR,
                )

                print(f"Đã xuất CSV: {csv_file}")

            except Exception as error:
                print(
                    f"\nLỖI khi xử lý {basin_name}: {error}"
                )

        if not basin_results:
            raise RuntimeError(
                "Không có lưu vực nào được xử lý thành công."
            )

        excel_file = (
            OUTPUT_DIR
            / "CHIRPS_luong_mua_cac_luu_vuc.xlsx"
        )

        export_to_excel(
            basin_results=basin_results,
            output_file=excel_file,
        )

        print("\n" + "=" * 70)
        print("HOÀN THÀNH")
        print("=" * 70)

    finally:
        dataset.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\nCHƯƠNG TRÌNH DỪNG DO LỖI:")
        print(exc)
        sys.exit(1)