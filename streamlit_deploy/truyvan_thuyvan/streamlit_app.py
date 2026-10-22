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
API_CONNECT_TIMEOUT = 5
API_READ_TIMEOUT = 45
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from data_utils import (
    PLANTS,
    analog_similarity_series,
    annual_projection_table,
    comparison_table,
    current_month_actuals,
    current_period,
    daily_remaining_production_forecast,
    latest_complete_actual_date,
    load_current_hydrology_reports,
    load_daily_folders,
    load_folder,
    monthly_cumulative_production,
    prepare_analog_comparison_data,
    rank_analog_years,
    read_cumulative_hydrology_report,
    read_daily_production_workbook,
    read_workbook,
    update_historical_workbooks,
)


class UploadStoreError(RuntimeError):
    pass


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
) -> tuple[tuple[tuple[str, bytes], ...], str | None]:
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
    return tuple(files), payload.get("saved_at")


def save_uploads_to_server(
    api_url: str, api_key: str, files: tuple[tuple[str, bytes], ...]
) -> dict:
    payload = {
        "files": [
            {
                "name": name,
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
            for name, content in files
        ]
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

    monthly_data = (
        pd.concat(monthly_frames, ignore_index=True)
        .groupby(["year", "month", "plant"], as_index=False)[["production", "flow"]]
        .mean()
        .sort_values(["year", "month", "plant"])
        if monthly_frames
        else pd.DataFrame()
    )
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
daily_production, daily_hydrology = load_daily_data(str(APP_DIR))
reported_hydrology = load_current_reports(str(APP_DIR))
api_url = setting("HYDROAI_API_URL")
api_key = setting("HYDROAI_API_KEY")
upload_refresh_token = int(st.session_state.get("upload_refresh_token", 0))
saved_uploads: tuple[tuple[str, bytes], ...] = ()
saved_at: str | None = None
saved_upload_error: str | None = None
if api_url and api_key:
    try:
        saved_uploads, saved_at = load_saved_uploads(
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
    save_disabled = not current_uploads or not api_url or not api_key
    if st.button(
        ":material/save: Lưu dữ liệu mới nhất",
        type="primary",
        width="stretch",
        disabled=save_disabled,
        help="Lưu bộ Excel đang chọn về máy chủ HydroAI để dùng lại sau khi Streamlit khởi động lại.",
    ):
        try:
            result = save_uploads_to_server(api_url, api_key, current_uploads)
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

    if local_files:
        st.success(f"Đã đọc {len(local_files)} tệp trong thư mục.")
        for name in local_files:
            st.caption(name)

uploaded_data = pd.DataFrame()
uploaded_daily_production = pd.DataFrame(columns=["date", "plant", "production"])
uploaded_reported_hydrology = pd.DataFrame(columns=["date", "plant", "flow"])
active_uploads = current_uploads or saved_uploads
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

data = pd.concat([local_data, uploaded_data], ignore_index=True) if not local_data.empty or not uploaded_data.empty else pd.DataFrame()
if not data.empty:
    data = (
        data.groupby(["year", "month", "plant"], as_index=False)[["production", "flow"]]
        .mean()
        .sort_values(["year", "month", "plant"])
    )

current_actuals = current_month_actuals(
    daily_production, daily_hydrology, reported_hydrology, data_as_of
)
if data_as_of is not None and not current_actuals.empty:
    if data.empty:
        data = current_actuals.copy()
    else:
        same_period = (data["year"] == data_as_of.year) & (data["month"] == data_as_of.month)
        data = pd.concat([data.loc[~same_period], current_actuals], ignore_index=True).sort_values(
            ["year", "month", "plant"]
        )

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
if data_as_of is not None and data_as_of.year == year and data_as_of.month == month:
    st.success(f"Dữ liệu thực tế cập nhật đến ngày {data_as_of.strftime('%d/%m/%Y')}.")
    summary_title = f"Tổng hợp đến ngày {data_as_of.strftime('%d/%m/%Y')}"
    production_label = f"Sản lượng lũy kế đến {data_as_of.strftime('%d/%m')}"
    flow_label = f"Lưu lượng TB đến {data_as_of.strftime('%d/%m')}"
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

period = data[(data["year"] == year) & (data["month"] == month)].set_index("plant")
comparison_year: int | None = None
comparison_production_label: str | None = None
comparison_production = pd.DataFrame()
comparison_index = pd.DataFrame()
if data_as_of is not None and data_as_of.year == year and data_as_of.month == month:
    comparison_year = year - 1
    comparison_production_label = f"Sản lượng lũy kế cùng kỳ {comparison_year}"
    comparison_production = monthly_cumulative_production(
        daily_production,
        comparison_year,
        month,
        data_as_of.day,
    )
    comparison_index = comparison_production.set_index("plant")

summary_rows: list[dict[str, object]] = []
best_by_plant: dict[str, int | None] = {}
for plant in PLANTS:
    ranked = rank_analog_years(analog_data, year, analog_month, plant)
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

if comparison_year is not None and data_as_of is not None:
    st.caption(
        f"Sản lượng cùng kỳ {comparison_year} là tổng từ "
        f"01–{data_as_of.day:02d}/{month:02d}/{comparison_year}."
    )
    incomplete_plants = comparison_production.loc[
        comparison_production["days"] < data_as_of.day, "plant"
    ].tolist()
    if incomplete_plants:
        st.warning(
            f"Dữ liệu cùng kỳ {comparison_year} chưa đủ {data_as_of.day} ngày cho: "
            + ", ".join(incomplete_plants)
            + "."
        )

with st.container(horizontal=True, horizontal_alignment="left", vertical_alignment="center"):
    if st.button(":material/query_stats: Truy vấn cả 3 nhà máy", type="primary"):
        st.session_state["selected_plant"] = "Tất cả"
    if is_partial_month and data_as_of is not None:
        st.caption(
            f"Năm đồng dạng được tính từ T1–T{month}. T{month} so sánh cùng kỳ "
            f"01–{data_as_of.day:02d}/{month:02d}: sản lượng lũy kế và lưu lượng trung bình."
        )
    else:
        st.caption(
            f"Năm đồng dạng được xác định từ quy luật sản lượng và lưu lượng tháng 1–{analog_month}; "
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

comparison_suffix = (
    f" (T{month} tính đến {data_as_of.day}/{month})"
    if is_partial_month and data_as_of is not None
    else ""
)
analog_period_label = (
    f"T1–{data_as_of.day:02d}/{month:02d}"
    if is_partial_month and data_as_of is not None
    else f"T1–T{analog_month}"
)
st.subheader(f"So sánh mức tương đồng đến tháng {analog_month}{comparison_suffix}")
chart_columns = st.columns(3, gap="medium")
for chart_column, plant in zip(chart_columns, PLANTS):
    ranking = rank_analog_years(analog_data, year, analog_month, plant)
    with chart_column.container(border=True, height="stretch"):
        st.markdown(f"**{plant}**")
        if ranking.empty:
            st.info("Chưa đủ dữ liệu để so sánh.")
            continue
        analog_year = int(ranking.iloc[0]["year"])
        similarity = float(ranking.iloc[0]["similarity"])
        st.metric(
            f"{year} so với {analog_year}",
            f"{similarity:.1%}",
            border=False,
        )
        chart_data = analog_similarity_series(
            analog_data, year, analog_month, plant, analog_year
        )
        chart_encoding = {
            "x": alt.X("Tháng:O", title="Tháng", axis=alt.Axis(labelAngle=0)),
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
        if is_partial_month:
            completed = chart_data[chart_data["Tháng"] < month]
            partial_segment = chart_data[chart_data["Tháng"] >= max(1, month - 1)]
            solid_line = alt.Chart(completed).mark_line(strokeWidth=2.5).encode(
                **chart_encoding
            )
            dashed_line = alt.Chart(partial_segment).mark_line(
                strokeWidth=2.5, strokeDash=[7, 5]
            ).encode(**chart_encoding)
            points = alt.Chart(chart_data).mark_point(filled=True, size=55).encode(
                **chart_encoding, tooltip=tooltip
            )
            similarity_chart = alt.layer(solid_line, dashed_line, points).properties(
                height=230
            )
        else:
            similarity_chart = (
                alt.Chart(chart_data)
                .mark_line(point=True, strokeWidth=2.5)
                .encode(**chart_encoding, tooltip=tooltip)
                .properties(height=230)
            )
        st.altair_chart(similarity_chart)
st.caption(
    "Chỉ số chuẩn hóa kết hợp sản lượng và lưu lượng; mức 100 tương ứng với bình quân "
    f"của năm {year} trong giai đoạn {analog_period_label}. Hai đường càng gần nhau thì quy luật càng tương đồng."
    + (f" Đoạn nối T{month - 1}–T{month} dùng nét đứt vì T{month} chưa kết thúc." if is_partial_month else "")
)

selected = st.session_state.get("selected_plant")
if selected:
    plant_filter = None if selected == "Tất cả" else selected
    ranking = rank_analog_years(analog_data, year, analog_month, plant_filter)
    title = "cả 3 nhà máy" if plant_filter is None else plant_filter
    st.divider()
    st.subheader(f"Kết quả năm đồng dạng {analog_period_label} — {title}")
    if ranking.empty:
        st.warning("Chưa đủ dữ liệu lịch sử từ tháng 1 đến kỳ hiện tại để xác định năm đồng dạng.")
    else:
        analog_year = int(ranking.iloc[0]["year"])
        similarity = float(ranking.iloc[0]["similarity"])
        with st.container(horizontal=True):
            st.metric("Năm đồng dạng nhất", str(analog_year), border=True)
            st.metric("Mức tương đồng", f"{similarity:.1%}", border=True)
            st.metric("Số điểm dữ liệu so sánh", str(int(ranking.iloc[0]["comparisons"])), border=True)

        details = comparison_table(
            analog_data, year, analog_month, analog_year, plant_filter
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
        if is_partial_month and data_as_of is not None:
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
projection = annual_projection_table(
    data, year, month, data_as_of, daily_production, analog_data
)
forecast_columns = [f"T{forecast_month}" for forecast_month in range(month + 1, 13)]

if is_partial_month and data_as_of is not None:
    projection_by_plant = projection.set_index("Nhà máy")
    current_month = data[(data["year"] == year) & (data["month"] == month)].set_index("plant")
    remainder_rows = []
    for plant in PLANTS:
        actual_value = (
            float(current_month.at[plant, "production"])
            if plant in current_month.index and pd.notna(current_month.at[plant, "production"])
            else float("nan")
        )
        projected_month = float(projection_by_plant.at[plant, f"T{month}"])
        later_months = sum(
            float(projection_by_plant.at[plant, f"T{future_month}"])
            for future_month in range(month + 1, 13)
        )
        remainder_rows.append(
            {
                "Nhà máy": plant,
                f"Thực tế 1–{data_as_of.day}/{month}": actual_value,
                f"Dự kiến {data_as_of.day + 1}–{data_as_of.days_in_month}/{month}": projected_month - actual_value,
                f"Dự kiến T{month}": projected_month,
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
    st.caption(
        f"Đơn vị: triệu kWh. Phần còn lại của tháng {month} được cộng theo sản lượng "
        "từng ngày tương ứng của năm đồng dạng."
    )
    daily_forecast = daily_remaining_production_forecast(
        data, daily_production, year, month, data_as_of, analog_data
    )
    if not daily_forecast.empty:
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
    if is_partial_month:
        styles.loc[~total_mask, f"T{month}"] = (
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
    actual_end_month = month - 1 if is_partial_month else month
    st.caption(f"🔵 Thực tế: sản lượng {year} từ T1 đến T{actual_end_month}")
    if is_partial_month and data_as_of is not None:
        st.caption(
            f"🟠 T{month}: thực tế đến {data_as_of.day}/{month} + phần còn lại theo năm quy luật"
        )
    if forecast_columns:
        st.caption(
            f"🔴 Dự kiến: sản lượng năm quy luật từ T{month + 1} đến T12"
        )
if is_partial_month:
    st.info(
        f"Sản lượng và lưu lượng hiện tại được tổng hợp từ dữ liệu ngày đến "
        f"{data_as_of.strftime('%d/%m/%Y')}. Năm đồng dạng dùng dữ liệu T1–T{month}; "
        f"T{month} được ước từ sản lượng bình quân ngày và lưu lượng trung bình hiện có. "
        f"Dự kiến phần còn lại của T{month} lấy từng ngày của năm đồng dạng."
    )
