from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta
from typing import Any

import altair as alt
import pandas as pd
import requests
import streamlit as st


CACHE_TTL_SECONDS = 300
DEFAULT_CONNECT_TIMEOUT = 5
DEFAULT_READ_TIMEOUT = 30
COLORS = {"actual": "#16a34a", "forecast": "#dc2626"}


class ApiError(RuntimeError):
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


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def api_get(
    api_url: str,
    api_key: str,
    path: str,
    params_items: tuple[tuple[str, Any], ...],
    refresh_token: int,
) -> dict[str, Any]:
    del refresh_token
    try:
        response = requests.get(
            f"{api_url.rstrip('/')}{path}",
            headers={"X-API-Key": api_key},
            params=dict(params_items),
            timeout=(DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
        )
    except requests.Timeout as exc:
        raise ApiError("Máy chủ HydroAI phản hồi quá thời gian cho phép.") from exc
    except requests.ConnectionError as exc:
        raise ApiError("Không kết nối được máy chủ HydroAI. Hãy kiểm tra URL API và trạng thái máy chủ.") from exc
    except requests.RequestException as exc:
        raise ApiError(f"Lỗi kết nối API: {exc}") from exc

    if response.status_code == 401:
        raise ApiError("Khóa API không hợp lệ. Hãy kiểm tra HYDROAI_API_KEY trong Streamlit Secrets.")
    if response.status_code == 503:
        try:
            detail = response.json().get("detail", "API hoặc database chưa sẵn sàng")
        except ValueError:
            detail = "API hoặc database chưa sẵn sàng"
        raise ApiError(str(detail))
    try:
        response.raise_for_status()
        payload = response.json()
    except (requests.HTTPError, ValueError) as exc:
        detail = response.text[:300] if response.text else str(exc)
        raise ApiError(f"API trả về lỗi {response.status_code}: {detail}") from exc
    if not isinstance(payload, dict):
        raise ApiError("API trả về dữ liệu không đúng định dạng.")
    return payload


def frame_from_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if "Date" in frame.columns:
        frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
        frame = frame.dropna(subset=["Date"]).sort_values("Date")
    return frame


def query_token(auto_refresh: bool, interval_seconds: int) -> int:
    manual = int(st.session_state.get("api_refresh_nonce", 0))
    if not auto_refresh:
        return manual
    return manual + int(time.time() // interval_seconds)


def request_payload(
    api_url: str,
    api_key: str,
    path: str,
    params: dict[str, Any],
    token: int,
) -> dict[str, Any]:
    clean_params = {
        key: value.isoformat() if isinstance(value, (date, datetime)) else value
        for key, value in params.items()
        if value is not None
    }
    return api_get(
        api_url,
        api_key,
        path,
        tuple(sorted(clean_params.items())),
        token,
    )


def metric_value(value: Any, unit: str) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return "--"
    text = f"{float(number):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{text} {unit}"


def hydro_chart(
    actual: pd.DataFrame,
    forecast: pd.DataFrame,
    actual_column: str,
    p10_column: str,
    p50_column: str,
    p90_column: str,
    title: str,
    unit: str,
) -> None:
    layers: list[alt.Chart] = []
    if not actual.empty and actual_column in actual:
        history = actual[["Date", actual_column]].dropna().rename(columns={actual_column: "Value"})
        layers.append(
            alt.Chart(history).mark_line(color=COLORS["actual"], strokeWidth=2.5).encode(
                x=alt.X("Date:T", title="Ngày"),
                y=alt.Y("Value:Q", title=unit, scale=alt.Scale(zero=False)),
                tooltip=[
                    alt.Tooltip("Date:T", title="Ngày", format="%d/%m/%Y"),
                    alt.Tooltip("Value:Q", title=unit, format=",.2f"),
                ],
            )
        )
    required = {"Date", p10_column, p50_column, p90_column}
    if not forecast.empty and required.issubset(forecast.columns):
        future = forecast[["Date", p10_column, p50_column, p90_column]].dropna(subset=[p50_column])
        if not future.empty:
            layers.append(
                alt.Chart(future).mark_area(color=COLORS["forecast"], opacity=0.12).encode(
                    x=alt.X("Date:T", title="Ngày"),
                    y=alt.Y(f"{p10_column}:Q", title=unit, scale=alt.Scale(zero=False)),
                    y2=f"{p90_column}:Q",
                )
            )
            layers.append(
                alt.Chart(future).mark_line(
                    color=COLORS["forecast"], strokeDash=[7, 5], strokeWidth=2.5
                ).encode(
                    x=alt.X("Date:T", title="Ngày"),
                    y=alt.Y(f"{p50_column}:Q", title=unit, scale=alt.Scale(zero=False)),
                    tooltip=[
                        alt.Tooltip("Date:T", title="Ngày", format="%d/%m/%Y"),
                        alt.Tooltip(f"{p50_column}:Q", title="Dự báo P50", format=",.2f"),
                    ],
                )
            )
    st.subheader(title)
    if not layers:
        st.info("Chưa có dữ liệu trong khoảng đã chọn.")
        return
    st.altair_chart(alt.layer(*layers).properties(height=330).interactive())


def generation_chart(actual: pd.DataFrame, forecast: pd.DataFrame) -> None:
    st.subheader("Sản lượng phát điện")
    layers: list[alt.Chart] = []
    if not actual.empty and "Generation_MWh" in actual:
        daily = actual.groupby("Date", as_index=False)["Generation_MWh"].sum(min_count=1)
        layers.append(
            alt.Chart(daily).mark_line(color=COLORS["actual"], strokeWidth=2.5).encode(
                x=alt.X("Date:T", title="Ngày"),
                y=alt.Y("Generation_MWh:Q", title="MWh", scale=alt.Scale(zero=False)),
                tooltip=[
                    alt.Tooltip("Date:T", title="Ngày", format="%d/%m/%Y"),
                    alt.Tooltip("Generation_MWh:Q", title="MWh", format=",.2f"),
                ],
            )
        )
    if not forecast.empty and "Generation_Forecast_MWh" in forecast:
        future = forecast.groupby("Date", as_index=False)["Generation_Forecast_MWh"].sum(min_count=1)
        layers.append(
            alt.Chart(future).mark_line(
                color=COLORS["forecast"], strokeDash=[7, 5], strokeWidth=2.5
            ).encode(
                x=alt.X("Date:T", title="Ngày"),
                y=alt.Y("Generation_Forecast_MWh:Q", title="MWh", scale=alt.Scale(zero=False)),
                tooltip=[
                    alt.Tooltip("Date:T", title="Ngày", format="%d/%m/%Y"),
                    alt.Tooltip("Generation_Forecast_MWh:Q", title="Dự báo MWh", format=",.2f"),
                ],
            )
        )
    if not layers:
        st.info("Chưa có dữ liệu sản lượng trong khoảng đã chọn.")
        return
    st.altair_chart(alt.layer(*layers).properties(height=300).interactive())


def render_dashboard(
    api_url: str,
    api_key: str,
    basin_id: str,
    start_date: date,
    end_date: date,
    show_forecast: bool,
    auto_refresh: bool,
    interval_seconds: int,
) -> None:
    token = query_token(auto_refresh, interval_seconds)
    params = {
        "basin_id": basin_id,
        "start_date": start_date,
        "end_date": end_date,
        "limit": 10_000,
    }
    try:
        history_payload = request_payload(api_url, api_key, "/history", params, token)
        forecast_payload = (
            request_payload(api_url, api_key, "/forecast", params, token)
            if show_forecast
            else {"rows": []}
        )
        generation_payload = request_payload(
            api_url,
            api_key,
            "/generation",
            {**params, "include_forecast": show_forecast},
            token,
        )
    except ApiError as exc:
        st.error(str(exc), icon=":material/cloud_off:")
        st.info("Dashboard sẽ giữ nguyên bộ lọc. Hãy kiểm tra máy chủ API rồi bấm Làm mới dữ liệu.")
        return

    history = frame_from_rows(history_payload.get("rows", []))
    forecast = frame_from_rows(forecast_payload.get("rows", []))
    generation = frame_from_rows(generation_payload.get("actual_rows", []))
    generation_forecast = frame_from_rows(generation_payload.get("forecast_rows", []))

    if history_payload.get("truncated") or forecast_payload.get("truncated") or generation_payload.get("truncated"):
        st.warning("Kết quả đã chạm giới hạn 10.000 dòng. Hãy chọn khoảng ngày ngắn hơn.")

    if not history.empty:
        latest = history.iloc[-1]
        columns = st.columns(3)
        columns[0].metric("Mực nước mới nhất", metric_value(latest.get("WaterLevel_m"), "m"))
        columns[1].metric("Lưu lượng về mới nhất", metric_value(latest.get("Q_in_m3s"), "m³/s"))
        columns[2].metric("Mốc dữ liệu", latest["Date"].strftime("%d/%m/%Y"))

    operations_tab, data_tab = st.tabs(["Vận hành hồ", "Dữ liệu API"])
    with operations_tab:
        hydro_chart(
            history,
            forecast,
            "WaterLevel_m",
            "WaterLevel_P10_m",
            "WaterLevel_P50_m",
            "WaterLevel_P90_m",
            "Mực nước thượng lưu",
            "m",
        )
        hydro_chart(
            history,
            forecast,
            "Q_in_m3s",
            "Q_P10_m3s",
            "Q_P50_m3s",
            "Q_P90_m3s",
            "Lưu lượng về",
            "m³/s",
        )
        generation_chart(generation, generation_forecast)
    with data_tab:
        st.subheader("Lịch sử hồ chứa")
        st.dataframe(history, width="stretch", hide_index=True)
        if show_forecast:
            st.subheader("Dự báo AI")
            st.dataframe(forecast, width="stretch", hide_index=True)
        st.subheader("Sản lượng")
        st.dataframe(generation, width="stretch", hide_index=True)

    now_text = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    mode = f"tự làm mới mỗi {interval_seconds // 60} phút" if auto_refresh else "làm mới thủ công"
    st.caption(f"Đã đọc API lúc {now_text}; chế độ {mode}. Cache tối đa {CACHE_TTL_SECONDS // 60} phút.")


def main() -> None:
    st.set_page_config(page_title="HydroAI", page_icon="💧", layout="wide")
    st.title("HydroAI - Dashboard hồ chứa")
    st.caption("Dữ liệu được đọc trực tiếp từ máy chủ HydroAI qua API bảo mật.")

    api_url = setting("HYDROAI_API_URL")
    api_key = setting("HYDROAI_API_KEY")
    if not api_url or not api_key:
        missing = []
        if not api_url:
            missing.append("HYDROAI_API_URL")
        if not api_key:
            missing.append("HYDROAI_API_KEY")
        st.error(f"Thiếu cấu hình Streamlit Secrets: {', '.join(missing)}.")
        st.markdown(
            "Mở **Manage app → Settings → Secrets**, nhập đúng hai dòng dưới đây, "
            "sau đó bấm **Save** và khởi động lại ứng dụng. URL phải là địa chỉ HTTPS "
            "công khai của API đang chạy trên máy chủ HydroAI."
        )
        st.code(
            'HYDROAI_API_URL = "https://ten-mien-api-cua-ban"\n'
            'HYDROAI_API_KEY = "cung-khoa-HYDROAI_API_KEY-tren-may-chu"',
            language="toml",
        )
        st.caption(
            "Không dùng http://127.0.0.1:8000 trong Streamlit Community Cloud vì đó là "
            "máy của Streamlit, không phải máy chủ HydroAI."
        )
        st.stop()

    if "api_refresh_nonce" not in st.session_state:
        st.session_state.api_refresh_nonce = 0

    try:
        basins_payload = request_payload(
            api_url,
            api_key,
            "/basins",
            {},
            int(st.session_state.api_refresh_nonce),
        )
    except ApiError as exc:
        st.error(str(exc), icon=":material/cloud_off:")
        st.stop()

    basins = [item for item in basins_payload.get("basins", []) if item.get("id")]
    if not basins:
        st.warning("API chưa trả về danh sách lưu vực.")
        st.stop()
    basin_by_id = {item["id"]: item for item in basins}

    with st.sidebar:
        st.header("Bộ lọc")
        basin_id = st.selectbox(
            "Lưu vực",
            options=list(basin_by_id),
            format_func=lambda value: basin_by_id[value].get("reservoir_name") or value,
        )
        selected = basin_by_id[basin_id]
        min_date = date.fromisoformat(selected["min_date"]) if selected.get("min_date") else date.today() - timedelta(days=365)
        actual_max_date = date.fromisoformat(selected["max_date"]) if selected.get("max_date") else date.today()
        forecast_max_date = (
            date.fromisoformat(selected["forecast_max_date"])
            if selected.get("forecast_max_date")
            else actual_max_date
        )
        max_date = max(actual_max_date, forecast_max_date)
        default_start = max(min_date, max_date - timedelta(days=365))
        selected_range = st.date_input(
            "Khoảng dữ liệu",
            value=(default_start, max_date),
            min_value=min_date,
            max_value=max_date,
        )
        show_forecast = st.toggle("Hiển thị dự báo AI", value=True)
        auto_refresh = st.toggle("Tự làm mới", value=False)
        interval_minutes = st.selectbox(
            "Chu kỳ tự làm mới",
            options=[1, 5, 15, 30],
            index=1,
            disabled=not auto_refresh,
            format_func=lambda value: f"{value} phút",
        )
        if st.button("Làm mới dữ liệu", icon=":material/refresh:", width="stretch"):
            st.session_state.api_refresh_nonce += 1
            api_get.clear()
            st.rerun()
        st.caption(f"Nguồn: {api_url}")

    if not isinstance(selected_range, (tuple, list)) or len(selected_range) != 2:
        st.warning("Hãy chọn đủ ngày bắt đầu và ngày kết thúc.")
        st.stop()
    start_date, end_date = selected_range
    interval_seconds = int(interval_minutes) * 60
    run_every = interval_seconds if auto_refresh else None

    @st.fragment(run_every=run_every)
    def live_data_fragment() -> None:
        render_dashboard(
            api_url,
            api_key,
            basin_id,
            start_date,
            end_date,
            show_forecast,
            auto_refresh,
            interval_seconds,
        )

    live_data_fragment()


__all__ = ["main"]
