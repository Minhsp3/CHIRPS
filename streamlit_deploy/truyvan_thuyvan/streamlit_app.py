from __future__ import annotations

import base64
import io
import os
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st


APP_DIR = Path(__file__).resolve().parent
ANALOG_TRAINING_WORKBOOK = (
    APP_DIR.parents[1]
    / "PMIS"
    / "Dữ liệu huấn luyện dự báo sản lượng Buon Kuop qua các năm.xlsx"
)
API_CONNECT_TIMEOUT = 5
API_READ_TIMEOUT = 45
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from data_utils import (
    PLANTS,
    analog_similarity_series,
    annual_inflow_projection_table,
    annual_projection_table,
    comparison_table,
    current_month_actuals,
    current_period,
    daily_analog_similarity_series,
    daily_remaining_production_forecast,
    latest_complete_actual_date,
    load_current_hydrology_reports,
    load_daily_folders,
    load_folder,
    load_analog_training_workbook,
    merge_current_calendar_month_actuals,
    merge_monthly_actuals,
    merge_upload_file_sets,
    merge_daily_hydrology,
    monthly_cumulative_production,
    prepare_analog_comparison_data,
    rank_analog_years,
    read_cumulative_hydrology_report,
    read_daily_production_workbook,
    read_workbook,
    resolve_production_actual_date,
    seven_day_inflow_generation_projection,
    update_historical_workbooks,
)


class UploadStoreError(RuntimeError):
    pass


PLANT_BASINS = {
    "Buôn Kuốp": "BK",
    "Buôn Tua Srah": "BTS",
    "Srêpốk 3": "SP3",
}


def setting(name: str, default: str = "") -> str:
    short_name = name.removeprefix("HYDROAI_")
    aliases = (name, short_name)
    for alias in aliases:
        value = os.getenv(alias)
        if value:
            return value.strip()
    try:
        for alias in aliases:
            secret = st.secrets.get(alias)
            if secret:
                return str(secret).strip()
        group = st.secrets.get("hydroai", {})
        if hasattr(group, "get"):
            for alias in (name, short_name, name.lower(), short_name.lower()):
                secret = group.get(alias)
                if secret:
                    return str(secret).strip()
    except Exception:
        pass
    return default


def checked_api_response(response: requests.Response) -> dict:
    if response.status_code == 401:
        raise UploadStoreError("Khóa API không hợp lệ.")
    try:
        response.raise_for_status()
        payload = response.json()
    except (requests.HTTPError, ValueError) as exc:
        detail = response.text[:300] if response.text else str(exc)
        raise UploadStoreError(f"Máy chủ trả về lỗi {response.status_code}: {detail}") from exc
    if not isinstance(payload, dict):
        raise UploadStoreError("Máy chủ trả về dữ liệu không đúng định dạng.")
    return payload


@st.cache_data(ttl="1m", show_spinner=False)
def load_saved_uploads(
    api_url: str, api_key: str, refresh_token: int
) -> tuple[tuple[tuple[str, bytes], ...], str | None, tuple[dict, ...]]:
    del refresh_token
    try:
        response = requests.get(
            f"{api_url.rstrip('/')}/hydrology-uploads",
            headers={"X-API-Key": api_key},
            timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT),
        )
    except requests.RequestException as exc:
        raise UploadStoreError("Không kết nối được kho dữ liệu trên máy chủ HydroAI.") from exc
    payload = checked_api_response(response)
    files: list[tuple[str, bytes]] = []
    for item in payload.get("files", []):
        try:
            files.append(
                (
                    str(item["name"]),
                    base64.b64decode(str(item["content_base64"]), validate=True),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    monthly_actuals = tuple(
        item for item in payload.get("monthly_actuals", []) if isinstance(item, dict)
    )
    return tuple(files), payload.get("saved_at"), monthly_actuals


@st.cache_data(ttl="5m", show_spinner="Đang đồng bộ lưu lượng từ máy chủ...")
def load_api_daily_hydrology(
    api_url: str, api_key: str, refresh_token: int
) -> pd.DataFrame:
    del refresh_token
    headers = {"X-API-Key": api_key}
    try:
        basins_response = requests.get(
            f"{api_url.rstrip('/')}/basins",
            headers=headers,
            timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT),
        )
        basin_payload = checked_api_response(basins_response)
    except requests.RequestException as exc:
        raise UploadStoreError("Không kết nối được dữ liệu hồ chứa trên máy chủ HydroAI.") from exc

    basin_rows = {
        str(item.get("model_basin_id") or item.get("id")): item
        for item in basin_payload.get("basins", [])
        if isinstance(item, dict)
    }
    frames: list[pd.DataFrame] = []
    for plant, basin_id in PLANT_BASINS.items():
        basin = basin_rows.get(basin_id, {})
        max_date = pd.to_datetime(basin.get("max_date"), errors="coerce")
        if pd.isna(max_date):
            continue
        start_date = max_date - pd.Timedelta(days=399)
        try:
            response = requests.get(
                f"{api_url.rstrip('/')}/history",
                headers=headers,
                params={
                    "basin_id": basin_id,
                    "start_date": start_date.strftime("%Y-%m-%d"),
                    "end_date": max_date.strftime("%Y-%m-%d"),
                    "limit": 1_000,
                },
                timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT),
            )
            payload = checked_api_response(response)
        except requests.RequestException as exc:
            raise UploadStoreError(
                f"Không đọc được lưu lượng API của {plant}."
            ) from exc
        frame = pd.DataFrame(payload.get("rows", []))
        if frame.empty or "Date" not in frame or "Q_in_m3s" not in frame:
            continue
        frame = frame.rename(
            columns={
                "Date": "date",
                "Q_in_m3s": "flow",
                "WaterLevel_m": "water_level",
                "MNDBT_m": "mndbt",
            }
        )
        frame["plant"] = plant
        for column in ("water_level", "mndbt"):
            if column not in frame:
                frame[column] = float("nan")
        frames.append(frame[["date", "plant", "flow", "water_level", "mndbt"]])
    if not frames:
        return pd.DataFrame(
            columns=["date", "plant", "flow", "water_level", "mndbt"]
        )
    result = pd.concat(frames, ignore_index=True)
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.normalize()
    for column in ("flow", "water_level", "mndbt"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result.dropna(subset=["date", "plant"]).sort_values(
        ["date", "plant"]
    ).reset_index(drop=True)


def save_uploads_to_server(
    api_url: str,
    api_key: str,
    files: tuple[tuple[str, bytes], ...],
    monthly_actuals: pd.DataFrame,
) -> dict:
    monthly_records = (
        monthly_actuals.astype(object)
        .where(pd.notna(monthly_actuals), None)
        .to_dict("records")
    )
    payload = {
        "files": [
            {
                "name": name,
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
            for name, content in files
        ],
        "monthly_actuals": monthly_records,
    }
    try:
        response = requests.post(
            f"{api_url.rstrip('/')}/hydrology-uploads",
            headers={"X-API-Key": api_key},
            json=payload,
            timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT),
        )
    except requests.RequestException as exc:
        raise UploadStoreError("Không kết nối được máy chủ để lưu dữ liệu.") from exc
    return checked_api_response(response)


@st.cache_data(ttl="5m", show_spinner="Đang đọc dữ liệu Excel...")
def load_local_data(folder: str) -> tuple[pd.DataFrame, list[str]]:
    return load_folder(folder)


@st.cache_data(ttl="1h", show_spinner="Đang đọc bộ lịch sử năm đồng dạng...")
def load_analog_training_data(path: str) -> pd.DataFrame:
    return load_analog_training_workbook(path)


@st.cache_data(ttl="5m", show_spinner="Đang đọc dữ liệu sản lượng và thủy văn ngày...")
def load_daily_data(folder: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    return load_daily_folders(folder)


@st.cache_data(ttl="5m", show_spinner="Đang đọc báo cáo thủy văn lũy kế...")
def load_current_reports(folder: str) -> pd.DataFrame:
    return load_current_hydrology_reports(folder)


@st.cache_data(ttl="5m", show_spinner="Đang đọc tệp đã tải lên...")
def load_uploaded_data(
    files: tuple[tuple[str, bytes], ...]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    monthly_frames: list[pd.DataFrame] = []
    production_frames: list[pd.DataFrame] = []
    report_frames: list[pd.DataFrame] = []
    for _, content in files:
        monthly = read_workbook(io.BytesIO(content))
        daily_production = read_daily_production_workbook(io.BytesIO(content))
        report = read_cumulative_hydrology_report(io.BytesIO(content))
        if not monthly.empty:
            monthly_frames.append(monthly)
        if not daily_production.empty:
            production_frames.append(daily_production)
        if not report.empty:
            report_frames.append(report)

    monthly_data = pd.DataFrame()
    for frame in monthly_frames:
        monthly_data = merge_monthly_actuals(monthly_data, frame)
    daily_data = (
        pd.concat(production_frames, ignore_index=True)
        .groupby(["date", "plant"], as_index=False)["production"]
        .mean()
        if production_frames
        else pd.DataFrame(columns=["date", "plant", "production"])
    )
    report_data = (
        pd.concat(report_frames, ignore_index=True)
        .groupby(["date", "plant"], as_index=False)["flow"]
        .mean()
        if report_frames
        else pd.DataFrame(columns=["date", "plant", "flow"])
    )
    return monthly_data, daily_data, report_data


def remember_plant() -> None:
    click = st.session_state.get("analog_click")
    if click is not None:
        st.session_state["selected_plant"] = PLANTS[click["row"]]


st.title("Sản lượng và lưu lượng tháng hiện tại")
st.caption("Dữ liệu được đọc trực tiếp từ Excel. Đơn vị giữ nguyên theo nguồn: sản lượng triệu kWh, lưu lượng m³/s.")

local_data, local_files = load_local_data(str(APP_DIR))
analog_training_data = load_analog_training_data(str(ANALOG_TRAINING_WORKBOOK))
daily_production, daily_hydrology = load_daily_data(str(APP_DIR))
reported_hydrology = load_current_reports(str(APP_DIR))
api_url = setting("HYDROAI_API_URL")
api_key = setting("HYDROAI_API_KEY")
upload_refresh_token = int(st.session_state.get("upload_refresh_token", 0))
api_daily_hydrology = pd.DataFrame(columns=["date", "plant", "flow"])
api_hydrology_error: str | None = None
if api_url and api_key:
    try:
        api_daily_hydrology = load_api_daily_hydrology(
            api_url, api_key, upload_refresh_token
        )
        daily_hydrology = merge_daily_hydrology(daily_hydrology, api_daily_hydrology)
    except UploadStoreError as exc:
        api_hydrology_error = str(exc)
saved_uploads: tuple[tuple[str, bytes], ...] = ()
saved_at: str | None = None
saved_monthly_actuals: tuple[dict, ...] = ()
saved_upload_error: str | None = None
if api_url and api_key:
    try:
        saved_uploads, saved_at, saved_monthly_actuals = load_saved_uploads(
            api_url, api_key, upload_refresh_token
        )
    except UploadStoreError as exc:
        saved_upload_error = str(exc)

with st.sidebar:
    st.header("Nguồn dữ liệu")
    uploads = st.file_uploader(
        "Chọn Excel nếu tệp chưa nằm trong thư mục",
        type=["xlsx", "xls", "xlsm"],
        accept_multiple_files=True,
        help="Ứng dụng tự nhận dạng bảng dài hoặc workbook có mỗi sheet là một năm.",
    )
    current_uploads = tuple((file.name, file.getvalue()) for file in uploads)
    combined_uploads = merge_upload_file_sets(saved_uploads, current_uploads)
    save_disabled = not current_uploads or not api_url or not api_key
    if st.button(
        ":material/save: Lưu dữ liệu mới nhất",
        type="primary",
        width="stretch",
        disabled=save_disabled,
        help="Lưu bộ Excel đang chọn về máy chủ HydroAI để dùng lại sau khi Streamlit khởi động lại.",
    ):
        try:
            monthly_to_save = pd.DataFrame(saved_monthly_actuals)
            if saved_uploads:
                saved_monthly_from_files, _, _ = load_uploaded_data(saved_uploads)
                monthly_to_save = merge_monthly_actuals(
                    monthly_to_save, saved_monthly_from_files
                )
            current_monthly, _, _ = load_uploaded_data(current_uploads)
            monthly_to_save = merge_monthly_actuals(
                monthly_to_save, current_monthly
            )
            result = save_uploads_to_server(
                api_url, api_key, combined_uploads, monthly_to_save
            )
            st.session_state["upload_refresh_token"] = upload_refresh_token + 1
            st.session_state["saved_upload_notice"] = (
                f"Đã lưu {len(result.get('files', []))} tệp làm bộ dữ liệu mới nhất."
            )
            load_saved_uploads.clear()
            st.rerun()
        except UploadStoreError as exc:
            st.error(f"Không thể lưu dữ liệu: {exc}")

    saved_notice = st.session_state.pop("saved_upload_notice", None)
    if saved_notice:
        st.success(saved_notice)
    if saved_uploads:
        saved_date = pd.to_datetime(saved_at, errors="coerce")
        saved_label = (
            saved_date.tz_convert("Asia/Ho_Chi_Minh").strftime("%d/%m/%Y %H:%M")
            if pd.notna(saved_date) and saved_date.tzinfo is not None
            else "gần nhất"
        )
        st.success(f"Đang dùng {len(saved_uploads)} tệp đã lưu lúc {saved_label}.")
        for name, _ in saved_uploads:
            st.caption(name)
    elif saved_upload_error:
        st.warning(saved_upload_error)
    elif not api_url or not api_key:
        st.info("Cần cấu hình API URL và API key để lưu dữ liệu bền vững.")

    if not api_daily_hydrology.empty:
        latest_api_date = api_daily_hydrology["date"].max().strftime("%d/%m/%Y")
        st.success(f"Lưu lượng 3 hồ đã đồng bộ từ API đến {latest_api_date}.")
    elif api_hydrology_error:
        st.warning(f"Chưa đồng bộ được lưu lượng từ API: {api_hydrology_error}")

    if local_files:
        st.success(f"Đã đọc {len(local_files)} tệp trong thư mục.")
        for name in local_files:
            st.caption(name)
    if not analog_training_data.empty:
        st.success(
            "Bộ năm đồng dạng: "
            f"{int(analog_training_data['year'].min())}–"
            f"{int(analog_training_data['year'].max())}, 3 nhà máy."
        )

uploaded_data = pd.DataFrame()
uploaded_daily_production = pd.DataFrame(columns=["date", "plant", "production"])
uploaded_reported_hydrology = pd.DataFrame(columns=["date", "plant", "flow"])
active_uploads = merge_upload_file_sets(saved_uploads, current_uploads)
if active_uploads:
    uploaded_data, uploaded_daily_production, uploaded_reported_hydrology = load_uploaded_data(
        active_uploads
    )
if not uploaded_daily_production.empty:
    daily_production = (
        pd.concat([daily_production, uploaded_daily_production], ignore_index=True)
        .groupby(["date", "plant"], as_index=False)["production"]
        .mean()
    )
if not uploaded_reported_hydrology.empty:
    reported_hydrology = (
        pd.concat([reported_hydrology, uploaded_reported_hydrology], ignore_index=True)
        .groupby(["date", "plant"], as_index=False)["flow"]
        .mean()
    )

data_as_of = latest_complete_actual_date(
    daily_production, daily_hydrology, reported_hydrology
)
cutoff_path = APP_DIR / "data_cutoff.txt"
if data_as_of is None and cutoff_path.exists():
    try:
        data_as_of = pd.Timestamp(cutoff_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        data_as_of = None

persisted_actuals = pd.DataFrame(saved_monthly_actuals)
data = merge_monthly_actuals(analog_training_data, local_data)
data = merge_monthly_actuals(data, persisted_actuals)
data = merge_monthly_actuals(data, uploaded_data)

current_actuals = current_month_actuals(
    daily_production, daily_hydrology, reported_hydrology, data_as_of
)
data = merge_current_calendar_month_actuals(data, current_actuals, data_as_of)

with st.sidebar:
    if st.button(":material/refresh: Đọc lại dữ liệu", width="stretch"):
        st.session_state["refresh_and_sync"] = True
        st.session_state["upload_refresh_token"] = upload_refresh_token + 1
        st.cache_data.clear()
        st.rerun()

    if st.session_state.pop("refresh_and_sync", False):
        try:
            if data_as_of is None or current_actuals.empty:
                raise ValueError("Chưa xác định được ngày có đủ dữ liệu của cả 3 nhà máy.")
            updates, backup_folder = update_historical_workbooks(
                APP_DIR, current_actuals, data_as_of.year, data_as_of.month
            )
            if updates:
                st.session_state["refresh_notice"] = (
                    f"Đã kiểm tra 3 file lịch sử và cập nhật {len(updates)} file có thay đổi "
                    f"cho T{data_as_of.month}/{data_as_of.year} đến ngày "
                    f"{data_as_of.strftime('%d/%m/%Y')}. "
                    f"Bản sao lưu: {backup_folder.name}."
                )
            else:
                st.session_state["refresh_notice"] = (
                    "Dữ liệu đã mới nhất; không có ô nào cần thay đổi trong 3 file lịch sử."
                )
        except Exception as exc:
            st.session_state["refresh_error"] = f"Không thể cập nhật file lịch sử: {exc}"
        st.cache_data.clear()
        st.rerun()

    notice = st.session_state.pop("refresh_notice", None)
    error = st.session_state.pop("refresh_error", None)
    if notice:
        st.success(notice)
    if error:
        st.error(error)

if data.empty:
    st.info(
        "Chưa tìm thấy dữ liệu phù hợp. Hãy đặt file Excel vào cùng thư mục với `streamlit_app.py` "
        "hoặc chọn file ở thanh bên."
    )
    st.markdown(
        "**Định dạng được hỗ trợ:** bảng có các cột Năm, Tháng, Nhà máy, Sản lượng, Lưu lượng; "
        "hoặc workbook có các sheet mang tên năm và 12 cột tháng."
    )
    st.stop()

year, month, is_calendar_current = current_period(data)
data_as_of = resolve_production_actual_date(
    data_as_of, data, daily_production, year, month
)
is_partial_month = bool(
    data_as_of is not None
    and data_as_of.year == year
    and data_as_of.month == month
    and data_as_of.day < data_as_of.days_in_month
)
analog_month = month - 1 if is_partial_month and month > 1 else month
analog_data = prepare_analog_comparison_data(
    data,
    year,
    month,
    data_as_of,
    daily_production,
    daily_hydrology,
)
if is_partial_month:
    analog_month = month

comparison_view_state = st.session_state.get("historical_comparison_view", "Năm")
saved_comparison_month = st.session_state.get("historical_comparison_month", analog_month)
summary_month = (
    int(saved_comparison_month)
    if comparison_view_state == "Năm"
    and isinstance(saved_comparison_month, (int, float))
    and 1 <= int(saved_comparison_month) <= analog_month
    else month
)
summary_as_of = (
    data_as_of
    if summary_month == month
    else pd.Timestamp(year=year, month=summary_month, day=1) + pd.offsets.MonthEnd(0)
)
summary_is_partial = bool(
    summary_as_of is not None
    and summary_as_of.year == year
    and summary_as_of.month == summary_month
    and summary_as_of.day < summary_as_of.days_in_month
)

if data_as_of is not None and data_as_of.year == year and data_as_of.month == month:
    st.success(f"Dữ liệu thực tế cập nhật đến ngày {data_as_of.strftime('%d/%m/%Y')}.")
    summary_title = f"Tổng hợp đến ngày {summary_as_of.strftime('%d/%m/%Y')}"
    production_label = f"Sản lượng lũy kế đến {summary_as_of.strftime('%d/%m')}"
    flow_label = f"Lưu lượng TB đến {summary_as_of.strftime('%d/%m')}"
elif not is_calendar_current:
    st.warning(
        f"Excel chưa có dữ liệu cho tháng hiện tại. Trang đang hiển thị kỳ mới nhất tìm thấy: tháng {month}/{year}."
    )
    summary_title = f"Tổng hợp tháng {month}/{year}"
    production_label = "Sản lượng tháng hiện tại"
    flow_label = "Lưu lượng hiện tại"
else:
    st.success(f"Kỳ dữ liệu hiện tại: tháng {month}/{year}")
    summary_title = f"Tổng hợp tháng {month}/{year}"
    production_label = "Sản lượng tháng hiện tại"
    flow_label = "Lưu lượng hiện tại"

period = data[(data["year"] == year) & (data["month"] == summary_month)].set_index("plant")
comparison_year: int | None = None
comparison_production_label: str | None = None
comparison_production = pd.DataFrame()
comparison_index = pd.DataFrame()
if summary_as_of is not None and summary_as_of.year == year:
    comparison_year = year - 1
    comparison_production_label = f"Sản lượng lũy kế cùng kỳ {comparison_year}"
    if summary_is_partial:
        comparison_production = monthly_cumulative_production(
            daily_production,
            comparison_year,
            summary_month,
            summary_as_of.day,
        )
    else:
        comparison_production = (
            data[
                (data["year"] == comparison_year)
                & (data["month"] == summary_month)
            ][["plant", "production"]]
            .assign(days=summary_as_of.days_in_month)
            .reset_index(drop=True)
        )
    comparison_index = comparison_production.set_index("plant")

summary_rows: list[dict[str, object]] = []
best_by_plant: dict[str, int | None] = {}
for plant in PLANTS:
    ranked = rank_analog_years(analog_data, year, summary_month, plant)
    analog_year = int(ranked.iloc[0]["year"]) if not ranked.empty else None
    best_by_plant[plant] = analog_year
    current_production = period.at[plant, "production"] if plant in period.index else None
    historical_production = (
        comparison_index.at[plant, "production"]
        if not comparison_index.empty and plant in comparison_index.index
        else None
    )
    row: dict[str, object] = {
        "Nhà máy": plant,
        production_label: current_production,
    }
    if comparison_production_label is not None:
        row[comparison_production_label] = historical_production
    row[flow_label] = period.at[plant, "flow"] if plant in period.index else None
    row["Năm đồng dạng / Truy vấn"] = (
        f":material/search: {analog_year} · Xem" if analog_year else "Chưa đủ dữ liệu"
    )
    summary_rows.append(row)

summary = pd.DataFrame(summary_rows)
summary_column_config: dict[str, object] = {
    "Nhà máy": st.column_config.TextColumn("Nhà máy", pinned=True, width="medium"),
    production_label: st.column_config.NumberColumn(
        f"{production_label} (triệu kWh)", format="%.3f", width="medium"
    ),
    flow_label: st.column_config.NumberColumn(
        f"{flow_label} (m³/s)", format="%.3f", width="medium"
    ),
    "Năm đồng dạng / Truy vấn": st.column_config.ButtonColumn(
        "Năm đồng dạng / Truy vấn",
        type="primary",
        on_click=remember_plant,
        key="analog_click",
        width="medium",
    ),
}
if comparison_production_label is not None:
    summary_column_config[comparison_production_label] = st.column_config.NumberColumn(
        f"{comparison_production_label} (triệu kWh)", format="%.3f", width="medium"
    )
st.subheader(summary_title)
st.dataframe(
    summary,
    hide_index=True,
    row_height=48,
    column_config=summary_column_config,
)

if comparison_year is not None and summary_as_of is not None:
    st.caption(
        f"Sản lượng cùng kỳ {comparison_year} là tổng từ "
        f"01–{summary_as_of.day:02d}/{summary_month:02d}/{comparison_year}."
    )
    incomplete_plants = comparison_production.loc[
        comparison_production["days"] < summary_as_of.day, "plant"
    ].tolist()
    if incomplete_plants:
        st.warning(
            f"Dữ liệu cùng kỳ {comparison_year} chưa đủ {summary_as_of.day} ngày cho: "
            + ", ".join(incomplete_plants)
            + "."
        )

with st.container(horizontal=True, horizontal_alignment="left", vertical_alignment="center"):
    if st.button(":material/query_stats: Truy vấn cả 3 nhà máy", type="primary"):
        st.session_state["selected_plant"] = "Tất cả"
    if summary_is_partial and summary_as_of is not None:
        st.caption(
            f"Năm đồng dạng được tính từ T1–T{summary_month}. T{summary_month} so sánh cùng kỳ "
            f"01–{summary_as_of.day:02d}/{summary_month:02d}: sản lượng lũy kế và lưu lượng trung bình."
        )
    else:
        st.caption(
            f"Năm đồng dạng được xác định từ quy luật sản lượng và lưu lượng tháng 1–{summary_month}; "
            "năm lịch sử thiếu dữ liệu trong giai đoạn này sẽ không được xếp hạng."
        )

if is_partial_month and data_as_of is not None:
    actual_month = current_actuals.set_index("plant")
    partial_average_rows = []
    for plant in PLANTS:
        partial_average_rows.append(
            {
                "Nhà máy": plant,
                f"Sản lượng lũy kế 1–{data_as_of.day}/{month}": float(
                    actual_month.at[plant, "production"]
                ),
                f"Lưu lượng TB 1–{data_as_of.day}/{month}": float(
                    actual_month.at[plant, "flow"]
                ),
            }
        )
    with st.expander(f"Giá trị T{month} dùng để tìm năm đồng dạng"):
        partial_average_table = pd.DataFrame(partial_average_rows)
        st.dataframe(
            partial_average_table,
            hide_index=True,
            column_config={
                "Nhà máy": st.column_config.TextColumn(pinned=True),
                **{
                    column: st.column_config.NumberColumn(column, format="%.3f")
                    for column in partial_average_table.columns
                    if column != "Nhà máy"
                },
            },
        )

comparison_heading = st.empty()
comparison_view = st.segmented_control(
    "Chế độ hiển thị",
    options=["Năm", "Tháng"],
    default="Năm",
    key="historical_comparison_view",
)
year_control, month_control = st.columns([2, 1], gap="medium")
available_comparison_months = (
    list(range(1, 13)) if comparison_view == "Tháng" else list(range(1, analog_month + 1))
)
if st.session_state.get("historical_comparison_month") not in available_comparison_months:
    st.session_state["historical_comparison_month"] = analog_month
with month_control:
    comparison_month = st.selectbox(
        "Chọn tháng hiển thị" if comparison_view == "Tháng" else "So sánh đến tháng",
        options=available_comparison_months,
        format_func=lambda value: f"Tháng {value}",
        key="historical_comparison_month",
        help=(
            "Biểu đồ tháng hiển thị từng ngày; ngày tương lai dùng nét đứt."
            if comparison_view == "Tháng"
            else "Tính mức tương đồng từ tháng 1 đến hết tháng được chọn."
        ),
    )

ranking_month = min(comparison_month, analog_month)
historical_years = (
    sorted(
        analog_data.loc[analog_data["year"] < year, "year"]
        .dropna()
        .astype(int)
        .unique()
        .tolist(),
        reverse=True,
    )
    if not analog_data.empty
    else []
)
if "historical_comparison_auto" not in st.session_state:
    st.session_state["historical_comparison_auto"] = (
        st.session_state.get("historical_comparison_year") is None
    )
if historical_years:
    saved_history_year = st.session_state.get(
        "historical_comparison_year_input",
        st.session_state.get("historical_comparison_year", max(historical_years)),
    )
    if saved_history_year not in historical_years:
        saved_history_year = max(historical_years)
    st.session_state["historical_comparison_year_input"] = int(saved_history_year)
with year_control:
    if historical_years:
        st.caption(
            f"Có {len(historical_years)} năm lịch sử, từ {min(historical_years)} "
            f"đến {max(historical_years)}. Tắt Tự động để nhập trực tiếp năm cần xem."
        )
    auto_history_year = st.toggle(
        "Tự động chọn năm đồng dạng nhất",
        help=(
            "Bật để mỗi nhà máy tự chọn năm có sai lệch bình quân nhỏ nhất. "
            "Tắt để nhập một năm lịch sử dùng chung cho cả ba biểu đồ."
        ),
        key="historical_comparison_auto",
    )
    if historical_years:
        history_choice = st.number_input(
            "Nhập năm lịch sử",
            min_value=min(historical_years),
            max_value=max(historical_years),
            step=1,
            disabled=auto_history_year,
            key="historical_comparison_year_input",
        )
    else:
        history_choice = None
        st.warning("Chưa đọc được bộ dữ liệu năm lịch sử.")
manual_analog_year = (
    None if auto_history_year or history_choice is None else int(history_choice)
)
manual_uses_completed_months = bool(
    manual_analog_year is not None
    and comparison_view == "Năm"
    and is_partial_month
    and comparison_month == month
    and month > 1
)
if manual_uses_completed_months:
    ranking_month = month - 1
comparison_is_partial = bool(
    comparison_view == "Năm"
    and is_partial_month
    and comparison_month == month
    and not manual_uses_completed_months
)
comparison_suffix = (
    f" (T{month} tính đến {data_as_of.day}/{month})"
    if comparison_is_partial and data_as_of is not None
    else f" (dữ liệu hoàn tất đến T{ranking_month})"
    if manual_uses_completed_months
    else ""
)
analog_period_label = (
    f"T1–{data_as_of.day:02d}/{month:02d}"
    if comparison_is_partial and data_as_of is not None
    else f"T1–T{ranking_month}"
)
if comparison_view == "Tháng":
    comparison_heading.subheader(
        f"So sánh theo ngày trong tháng {comparison_month}/{year}"
    )
else:
    comparison_heading.subheader(
        f"So sánh mức tương đồng đến tháng "
        f"{ranking_month if manual_uses_completed_months else comparison_month}"
        f"{comparison_suffix}"
    )

selected_analog_years: dict[str, int] = {}
chart_columns = st.columns(3, gap="medium")
for chart_column, plant in zip(chart_columns, PLANTS):
    ranking = rank_analog_years(analog_data, year, ranking_month, plant)
    with chart_column.container(border=True, height="stretch"):
        st.markdown(f"**{plant}**")
        if ranking.empty:
            st.info("Chưa đủ dữ liệu để so sánh.")
            continue
        selected_ranking = (
            ranking.iloc[[0]]
            if manual_analog_year is None
            else ranking[ranking["year"] == manual_analog_year]
        )
        if selected_ranking.empty:
            st.info(f"Năm {manual_analog_year} chưa đủ dữ liệu để so sánh.")
            continue
        analog_year = int(selected_ranking.iloc[0]["year"])
        selected_analog_years[plant] = analog_year
        similarity = float(selected_ranking.iloc[0]["similarity"])
        st.metric(
            f"{year} so với {analog_year}",
            f"{similarity:.1%}",
            border=False,
        )
        if comparison_view == "Tháng":
            chart_data = daily_analog_similarity_series(
                daily_production,
                daily_hydrology,
                year,
                comparison_month,
                plant,
                analog_year,
                data_as_of,
            )
            if chart_data.empty:
                st.info("Chưa đủ dữ liệu ngày để hiển thị tháng này.")
                continue
            last_day = pd.Period(
                f"{year}-{comparison_month:02d}", freq="M"
            ).days_in_month
            day_ticks = sorted({1, 5, 10, 15, 20, 25, last_day})
            chart_encoding = {
                "x": alt.X(
                    "Ngày:O",
                    title="Ngày",
                    scale=alt.Scale(domain=list(range(1, last_day + 1))),
                    axis=alt.Axis(labelAngle=0, values=day_ticks),
                ),
                "y": alt.Y(
                    "Chỉ số tương đồng:Q",
                    title="Chỉ số chuẩn hóa",
                    scale=alt.Scale(zero=False),
                ),
                "color": alt.Color(
                    "Năm:N",
                    title=None,
                    scale=alt.Scale(
                        domain=[str(year), str(analog_year)],
                        range=["#0b78a5", "#e45756"],
                    ),
                ),
                "strokeDash": alt.StrokeDash(
                    "Trạng thái:N",
                    title=None,
                    scale=alt.Scale(
                        domain=["Thực tế", "Quá khứ", "Dự kiến"],
                        range=[[1, 0], [1, 0], [7, 5]],
                    ),
                    legend=None,
                ),
                "detail": alt.Detail("Trạng thái:N"),
            }
            tooltip = [
                alt.Tooltip("Năm:N"),
                alt.Tooltip("Ngày:O"),
                alt.Tooltip("Trạng thái:N"),
                alt.Tooltip("Sản lượng:Q", format=".3f"),
                alt.Tooltip("Lưu lượng:Q", format=".2f"),
                alt.Tooltip("Chỉ số tương đồng:Q", format=".1f"),
            ]
            similarity_chart = (
                alt.Chart(chart_data)
                .mark_line(point=alt.OverlayMarkDef(filled=True, size=45), strokeWidth=2.5)
                .encode(**chart_encoding, tooltip=tooltip)
                .properties(height=230)
            )
        else:
            chart_data = analog_similarity_series(
                analog_data, year, comparison_month, plant, analog_year
            )
            chart_encoding = {
                "x": alt.X(
                    "Tháng:O",
                    title="Tháng",
                    scale=alt.Scale(domain=list(range(1, 13))),
                    axis=alt.Axis(labelAngle=0, values=list(range(1, 13))),
                ),
                "y": alt.Y(
                    "Chỉ số tương đồng:Q",
                    title="Chỉ số chuẩn hóa",
                    scale=alt.Scale(zero=False),
                ),
                "color": alt.Color(
                    "Năm:N",
                    title=None,
                    scale=alt.Scale(
                        domain=[str(year), str(analog_year)],
                        range=["#0b78a5", "#e45756"],
                    ),
                ),
            }
            tooltip = [
                alt.Tooltip("Năm:N"),
                alt.Tooltip("Tháng:O"),
                alt.Tooltip("Chỉ số tương đồng:Q", format=".1f"),
            ]
            if comparison_is_partial:
                current_year_data = chart_data[chart_data["Năm"] == str(year)]
                historical_data = chart_data[chart_data["Năm"] == str(analog_year)]
                completed = current_year_data[current_year_data["Tháng"] < month]
                partial_segment = current_year_data[
                    current_year_data["Tháng"] >= max(1, month - 1)
                ]
                current_line = alt.Chart(completed).mark_line(strokeWidth=2.5).encode(
                    **chart_encoding
                )
                current_partial_line = alt.Chart(partial_segment).mark_line(
                    strokeWidth=2.5, strokeDash=[7, 5]
                ).encode(**chart_encoding)
                historical_line = alt.Chart(historical_data).mark_line(
                    strokeWidth=2.5
                ).encode(**chart_encoding)
                points = alt.Chart(chart_data).mark_point(filled=True, size=55).encode(
                    **chart_encoding, tooltip=tooltip
                )
                similarity_chart = alt.layer(
                    historical_line,
                    current_line,
                    current_partial_line,
                    points,
                ).properties(height=230)
            else:
                similarity_chart = (
                    alt.Chart(chart_data)
                    .mark_line(point=True, strokeWidth=2.5)
                    .encode(**chart_encoding, tooltip=tooltip)
                    .properties(height=230)
                )
        st.altair_chart(similarity_chart)
if comparison_view == "Tháng":
    st.caption(
        "Trục ngang hiển thị ngày. Năm tương đồng dùng nét liền màu đỏ; dữ liệu thực tế "
        f"năm {year} dùng nét liền màu xanh; các ngày tương lai dùng nét đứt màu xanh "
        "và kế thừa quy luật cùng ngày của năm tương đồng. Mức tương đồng phía trên vẫn "
        f"được tính từ tháng 1 đến tháng {ranking_month}."
    )
else:
    st.caption(
        "Chỉ số chuẩn hóa kết hợp sản lượng và lưu lượng; mức 100 tương ứng với bình quân "
        f"của năm {year} trong giai đoạn {analog_period_label}. Hai đường càng gần nhau thì quy luật càng tương đồng."
        " Năm lịch sử được hiển thị đủ T1–T12; các tháng sau kỳ hiện tại chỉ để tham khảo "
        "và không tham gia tính tương đồng."
        + (f" Đoạn nối T{month - 1}–T{month} dùng nét đứt vì T{month} chưa kết thúc." if comparison_is_partial else "")
    )

with st.expander("Giải thích cách tính"):
    st.markdown("#### 1. Mức tương đồng (%)")
    st.write(
        "Với từng năm lịch sử, ứng dụng tính sai lệch tuyệt đối bình quân của "
        "sản lượng và lưu lượng, rồi chuẩn hóa theo giá trị bình quân của năm hiện tại."
    )
    st.latex(
        r"E_{SL}=\frac{TB\left(\left|SL_{LS,t}-SL_{HT,t}\right|\right)}"
        r"{TB\left(\left|SL_{HT,t}\right|\right)}"
    )
    st.latex(
        r"E_Q=\frac{TB\left(\left|Q_{LS,t}-Q_{HT,t}\right|\right)}"
        r"{TB\left(\left|Q_{HT,t}\right|\right)}"
    )
    st.latex(
        r"Tuong\ dong=\max\left(0,1-\frac{E_{SL}+E_Q}{2}\right)\times100\%"
    )
    st.write(
        "Năm có sai lệch bình quân nhỏ nhất là năm đồng dạng nhất. Khi truy vấn "
        "cả ba nhà máy, phép tính lấy bình quân các chuỗi sản lượng và lưu lượng "
        "của cả ba nhà máy."
    )

    st.divider()
    st.markdown("#### 2. Điểm chuẩn hóa trên đồ thị")
    st.write(
        "Sản lượng và lưu lượng có đơn vị khác nhau nên được đưa về cùng thang điểm. "
        f"Mẫu số luôn là bình quân của năm {year} trong giai đoạn {analog_period_label}."
    )
    st.latex(
        r"I_{y,t}=\frac{1}{2}\left("
        r"\frac{SL_{y,t}}{TB(SL_{HT})}\times100+"
        r"\frac{Q_{y,t}}{TB(Q_{HT})}\times100\right)"
    )
    st.info(
        "Điểm chuẩn hóa là mức tổng hợp dùng để vẽ đường, không phải phần trăm "
        "tương đồng. Khoảng cách giữa hai điểm càng nhỏ thì quy luật càng gần nhau.",
        icon=":material/info:",
    )

    example_plant = st.session_state.get("selected_plant")
    if example_plant not in PLANTS:
        example_plant = PLANTS[0]
    example_ranking = rank_analog_years(
        analog_data, year, ranking_month, example_plant
    )
    example_year = manual_analog_year
    if example_year is None and not example_ranking.empty:
        example_year = int(example_ranking.iloc[0]["year"])

    example_scope = analog_data[
        (analog_data["plant"] == example_plant)
        & (analog_data["year"].isin([year, example_year]))
        & (analog_data["month"].between(1, ranking_month))
    ]
    current_scope = example_scope[example_scope["year"] == year]
    example_month_rows = example_scope[example_scope["month"] == ranking_month]
    if (
        example_year is not None
        and not current_scope.empty
        and set(example_month_rows["year"].dropna().astype(int)) == {year, example_year}
    ):
        production_scale = float(current_scope["production"].abs().mean())
        flow_scale = float(current_scope["flow"].abs().mean())
        example_rows = []
        normalized_rows: dict[int, tuple[float, float, float]] = {}
        for comparison_year_value in (year, example_year):
            source = example_month_rows[
                example_month_rows["year"] == comparison_year_value
            ].iloc[0]
            production_score = float(source["production"]) / production_scale * 100
            flow_score = float(source["flow"]) / flow_scale * 100
            combined_score = (production_score + flow_score) / 2
            normalized_rows[int(comparison_year_value)] = (
                production_score,
                flow_score,
                combined_score,
            )
            example_rows.append(
                {
                    "Năm": int(comparison_year_value),
                    "Sản lượng": float(source["production"]),
                    "Lưu lượng": float(source["flow"]),
                    "Điểm SL": production_score,
                    "Điểm lưu lượng": flow_score,
                    "Điểm tổng hợp": combined_score,
                }
            )

        st.markdown(
            f"**Ví dụ: {example_plant}, tháng {ranking_month} "
            f"({year} so với {example_year})**"
        )
        st.caption(
            f"Giá trị chuẩn: sản lượng bình quân {year} = {production_scale:.3f}; "
            f"lưu lượng bình quân {year} = {flow_scale:.3f}."
        )
        example_table = pd.DataFrame(example_rows)
        st.dataframe(
            example_table,
            hide_index=True,
            column_config={
                "Năm": st.column_config.NumberColumn(format="%d"),
                **{
                    column: st.column_config.NumberColumn(format="%.3f")
                    for column in example_table.columns
                    if column != "Năm"
                },
            },
        )
        current_points = normalized_rows[year]
        historical_points = normalized_rows[example_year]
        point_distance = abs(current_points[2] - historical_points[2])
        month_similarity = max(
            0.0,
            1.0
            - (
                abs(current_points[0] - historical_points[0])
                + abs(current_points[1] - historical_points[1])
            )
            / 200,
        )
        st.write(
            f"Khoảng cách hai điểm chuẩn hóa: **{point_distance:.2f} điểm**. "
            f"Mức tương đồng nếu chỉ xét riêng tháng {ranking_month}: "
            f"**{month_similarity:.2%}**."
        )

selected = st.session_state.get("selected_plant")
if selected:
    plant_filter = None if selected == "Tất cả" else selected
    ranking = rank_analog_years(analog_data, year, ranking_month, plant_filter)
    title = "cả 3 nhà máy" if plant_filter is None else plant_filter
    st.divider()
    result_label = (
        "Kết quả năm đồng dạng"
        if manual_analog_year is None
        else f"Kết quả so sánh với năm {manual_analog_year}"
    )
    st.subheader(f"{result_label} {analog_period_label} — {title}")
    if ranking.empty:
        st.warning("Chưa đủ dữ liệu lịch sử từ tháng 1 đến kỳ hiện tại để xác định năm đồng dạng.")
    else:
        selected_ranking = (
            ranking.iloc[[0]]
            if manual_analog_year is None
            else ranking[ranking["year"] == manual_analog_year]
        )
        if selected_ranking.empty:
            st.warning(f"Năm {manual_analog_year} chưa đủ dữ liệu để so sánh.")
            st.stop()
        analog_year = int(selected_ranking.iloc[0]["year"])
        similarity = float(selected_ranking.iloc[0]["similarity"])
        with st.container(horizontal=True):
            st.metric("Năm đồng dạng nhất", str(analog_year), border=True)
            st.metric("Mức tương đồng", f"{similarity:.1%}", border=True)
            st.metric("Số điểm dữ liệu so sánh", str(int(ranking.iloc[0]["comparisons"])), border=True)

        details = comparison_table(
            analog_data, year, ranking_month, analog_year, plant_filter
        )
        detail_columns = {
            column: st.column_config.NumberColumn(column, format="%.3f")
            for column in details.columns
            if column not in {"Nhà máy", "Tháng"}
        }
        detail_columns["Tháng"] = st.column_config.NumberColumn(
            "Tháng", format="%d", width="small"
        )
        st.dataframe(
            details,
            hide_index=True,
            column_config=detail_columns,
        )
        if comparison_is_partial and data_as_of is not None:
            st.caption(
                f"T{month} của cả {year} và {analog_year} đều dùng cùng kỳ 01–"
                f"{data_as_of.day:02d}/{month:02d}: sản lượng lũy kế và lưu lượng trung bình."
            )
        with st.expander("Xem xếp hạng các năm lịch sử"):
            display_ranking = ranking.rename(
                columns={
                    "year": "Năm",
                    "score": "Sai lệch bình quân",
                    "similarity": "Mức tương đồng",
                    "comparisons": "Số điểm dữ liệu",
                }
            )
            st.dataframe(
                display_ranking,
                hide_index=True,
                column_config={
                    "Năm": st.column_config.NumberColumn(format="%d"),
                    "Sai lệch bình quân": st.column_config.NumberColumn(format="percent"),
                    "Mức tương đồng": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent"),
                },
            )

st.divider()
st.subheader(f"Dự kiến sản lượng còn lại năm {year}")
projection_cutoff_month = comparison_month if comparison_view == "Năm" else month
projection_as_of = (
    data_as_of
    if projection_cutoff_month == month
    else pd.Timestamp(year=year, month=projection_cutoff_month, day=1)
    + pd.offsets.MonthEnd(0)
)
projection_is_partial = bool(
    projection_as_of is not None
    and projection_as_of.year == year
    and projection_as_of.month == projection_cutoff_month
    and projection_as_of.day < projection_as_of.days_in_month
)
projection_method = st.segmented_control(
    "Phương pháp tính",
    options=["Năm đồng dạng", "Lưu lượng thực tế TB 7 ngày"],
    default="Năm đồng dạng",
    key="remaining_generation_method",
    help="Chọn cách dự kiến sản lượng cho các ngày còn lại đến cuối năm.",
)
if projection_method == "Lưu lượng thực tế TB 7 ngày":
    projection_cutoff_month = month
    projection_as_of = data_as_of
    projection_is_partial = is_partial_month
inflow_projection = pd.DataFrame()
if data_as_of is not None:
    inflow_projection = seven_day_inflow_generation_projection(
        daily_hydrology,
        data_as_of,
        api_daily_hydrology,
    )
if projection_method == "Lưu lượng thực tế TB 7 ngày" and not inflow_projection.empty:
    projection = annual_inflow_projection_table(
        data, year, month, data_as_of, inflow_projection
    )
else:
    projection = annual_projection_table(
        data,
        year,
        projection_cutoff_month,
        projection_as_of,
        daily_production,
        analog_data,
        selected_analog_years,
    )
    if projection_method == "Lưu lượng thực tế TB 7 ngày":
        st.warning("Chưa đủ lưu lượng và mực nước để tính theo trung bình 7 ngày; đang dùng năm đồng dạng.")
forecast_columns = [
    f"T{forecast_month}" for forecast_month in range(projection_cutoff_month + 1, 13)
]

if projection_is_partial and projection_as_of is not None:
    projection_by_plant = projection.set_index("Nhà máy")
    current_month = data[
        (data["year"] == year) & (data["month"] == projection_cutoff_month)
    ].set_index("plant")
    remainder_rows = []
    for plant in PLANTS:
        actual_value = (
            float(current_month.at[plant, "production"])
            if plant in current_month.index and pd.notna(current_month.at[plant, "production"])
            else float("nan")
        )
        projected_month = float(
            projection_by_plant.at[plant, f"T{projection_cutoff_month}"]
        )
        later_months = sum(
            float(projection_by_plant.at[plant, f"T{future_month}"])
            for future_month in range(projection_cutoff_month + 1, 13)
        )
        remainder_rows.append(
            {
                "Nhà máy": plant,
                f"Thực tế 1–{projection_as_of.day}/{projection_cutoff_month}": actual_value,
                f"Dự kiến {projection_as_of.day + 1}–{projection_as_of.days_in_month}/{projection_cutoff_month}": projected_month - actual_value,
                f"Dự kiến T{projection_cutoff_month}": projected_month,
                "Dự kiến các tháng sau": later_months,
                f"Sản lượng còn lại {year}": projected_month - actual_value + later_months,
                f"Dự kiến cả năm {year}": float(projection_by_plant.at[plant, "Tổng năm"]),
            }
        )
    remainder = pd.DataFrame(remainder_rows)
    st.dataframe(
        remainder,
        hide_index=True,
        row_height=42,
        column_config={
            "Nhà máy": st.column_config.TextColumn(pinned=True, width="medium"),
            **{
                column: st.column_config.NumberColumn(column, format="%.3f", width="medium")
                for column in remainder.columns
                if column != "Nhà máy"
            },
        },
    )
    if projection_method == "Lưu lượng thực tế TB 7 ngày" and not inflow_projection.empty:
        st.caption(
            "Đơn vị: triệu kWh. Sản lượng ngày = Q tính sản lượng × 86.400 / suất hao nước / 1.000.000; "
            "giữ không đổi đến cuối năm."
        )
        with st.expander("Xem lưu lượng 7 ngày, mực nước và cách tính", expanded=True):
            st.dataframe(
                inflow_projection,
                hide_index=True,
                column_config={
                    "Từ ngày": st.column_config.DateColumn(format="DD/MM/YYYY"),
                    "Đến ngày": st.column_config.DateColumn(format="DD/MM/YYYY"),
                    "Q TB 7 ngày": st.column_config.NumberColumn(format="%.2f"),
                    "Nước về ngày": st.column_config.NumberColumn(
                        "Nước về ngày (triệu m³)", format="%.3f"
                    ),
                    "Mực nước": st.column_config.NumberColumn(format="%.2f"),
                    "MNDBT": st.column_config.NumberColumn(format="%.2f"),
                    "Chênh MNDBT": st.column_config.NumberColumn(format="%.2f"),
                    "Q tính sản lượng": st.column_config.NumberColumn(format="%.2f"),
                    "Suất hao nước": st.column_config.NumberColumn(format="%.3f"),
                    "Sản lượng ngày": st.column_config.NumberColumn(format="%.3f"),
                    "Sản lượng còn lại": st.column_config.NumberColumn(format="%.3f"),
                },
            )
            st.info(
                "Buôn Tua Srah: nếu MNDBT - mực nước < 1 m, Q chạy máy được lấy bằng Q về TB 7 ngày. "
                "Nếu chênh từ 1 m trở lên trong thời kỳ tích nước, dùng Q chạy máy tối thiểu 40 m³/s."
            )
    else:
        st.caption(
            f"Đơn vị: triệu kWh. Phần còn lại của tháng {projection_cutoff_month} "
            "được cộng theo sản lượng "
            "từng ngày tương ứng của năm đồng dạng."
        )
    daily_forecast = daily_remaining_production_forecast(
        data,
        daily_production,
        year,
        projection_cutoff_month,
        projection_as_of,
        analog_data,
    )
    if projection_method == "Năm đồng dạng" and not daily_forecast.empty:
        with st.expander("Xem dự kiến từng ngày còn lại trong tháng"):
            st.dataframe(
                daily_forecast,
                hide_index=True,
                column_config={
                    "Ngày dự kiến": st.column_config.DateColumn(format="DD/MM/YYYY"),
                    "Năm quy luật": st.column_config.NumberColumn(format="%d"),
                    "Sản lượng dự kiến": st.column_config.NumberColumn(
                        "Sản lượng dự kiến (triệu kWh)", format="%.3f"
                    ),
                },
            )

st.markdown("**Bảng sản lượng theo tháng**")


def style_projection(frame: pd.DataFrame) -> pd.DataFrame:
    styles = pd.DataFrame("", index=frame.index, columns=frame.columns)
    total_mask = frame["Nhà máy"].eq("Tổng cộng")
    if forecast_columns:
        styles.loc[~total_mask, forecast_columns] = (
            "color: #c62828; background-color: #fff1f1; font-weight: 600"
        )
    if projection_is_partial:
        styles.loc[~total_mask, f"T{projection_cutoff_month}"] = (
            "color: #8a5200; background-color: #fff4d6; font-weight: 600"
        )
    styles.loc[total_mask, :] = "background-color: #eaf2f7; font-weight: 700"
    return styles


projection_columns = {
    "Nhà máy": st.column_config.TextColumn("Nhà máy", pinned=True, width="medium"),
    "Năm quy luật": st.column_config.NumberColumn("Năm quy luật", format="%d", width="small"),
    "Tổng năm": st.column_config.NumberColumn("Tổng năm", format="%.1f", width="small"),
}
projection_columns.update(
    {
        f"T{projection_month}": st.column_config.NumberColumn(
            f"T{projection_month}", format="%.1f", width="small"
        )
        for projection_month in range(1, 13)
    }
)

st.dataframe(
    projection.style.apply(style_projection, axis=None),
    hide_index=True,
    row_height=42,
    column_config=projection_columns,
)
with st.container(horizontal=True, horizontal_alignment="left"):
    actual_end_month = (
        projection_cutoff_month - 1
        if projection_is_partial
        else projection_cutoff_month
    )
    st.caption(f"🔵 Thực tế: sản lượng {year} từ T1 đến T{actual_end_month}")
    if projection_is_partial and projection_as_of is not None:
        st.caption(
            f"🟠 T{projection_cutoff_month}: thực tế đến "
            f"{projection_as_of.day}/{projection_cutoff_month} + phần còn lại theo "
            + ("Q TB 7 ngày" if projection_method == "Lưu lượng thực tế TB 7 ngày" else "năm quy luật")
        )
    if forecast_columns:
        st.caption(
            "🔴 Dự kiến: "
            + ("Q TB 7 ngày giữ không đổi" if projection_method == "Lưu lượng thực tế TB 7 ngày" else "sản lượng năm quy luật")
            + f" từ T{projection_cutoff_month + 1} đến T12"
        )
if projection_is_partial and projection_method == "Năm đồng dạng":
    st.info(
        f"Sản lượng và lưu lượng hiện tại được tổng hợp từ dữ liệu ngày đến "
        f"{projection_as_of.strftime('%d/%m/%Y')}. Năm đồng dạng dùng dữ liệu "
        f"T1–T{projection_cutoff_month}; T{projection_cutoff_month} được ước từ "
        "sản lượng bình quân ngày và lưu lượng trung bình hiện có. "
        f"Dự kiến phần còn lại của T{projection_cutoff_month} lấy từng ngày của năm đồng dạng."
    )
elif projection_is_partial:
    st.info(
        f"Dự kiến từ ngày {(projection_as_of + pd.Timedelta(days=1)).strftime('%d/%m/%Y')} đến 31/12/{year} "
        "dùng lưu lượng về trung bình 7 ngày gần nhất và suất hao nước từng nhà máy. "
        "Kết quả là ước tính theo giả định lưu lượng giữ nguyên, không phải kế hoạch huy động chính thức."
    )
