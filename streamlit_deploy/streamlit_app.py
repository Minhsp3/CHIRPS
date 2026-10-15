from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("HYDROAI_STREAMLIT_DB", APP_DIR / "hydroai_streamlit.db"))
BASIN_BY_RESERVOIR = {
    "Buôn Tua Srah": "BTS",
    "Buôn Kuốp": "BK",
    "Srêpốk 3": "SP3",
}
COLORS = {
    "actual": "#16a34a",
    "compare": "#2563eb",
    "forecast": "#dc2626",
    "mndbt": "#f5c842",
    "mnc": "#7d8590",
}

st.set_page_config(page_title="HydroAI", layout="wide")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


@st.cache_data(ttl="10m")
def metadata() -> tuple[list[str], date, date]:
    with connect() as conn:
        names = [row[0] for row in conn.execute(
            "SELECT DISTINCT Reservoir_Name FROM daily_readings ORDER BY Reservoir_Name"
        )]
        start, end = conn.execute("SELECT MIN(Date), MAX(Date) FROM daily_readings").fetchone()
    return names, pd.Timestamp(start).date(), pd.Timestamp(end).date()


@st.cache_data(ttl="10m")
def load_readings(names: tuple[str, ...], start: date, end: date) -> pd.DataFrame:
    placeholders = ",".join("?" for _ in names)
    query = f"""
        SELECT Date, Reservoir_Name, WaterLevel_m, MNDBT_m, MNC_m,
               Q_in_m3s, Q_spill_m3s, Q_turbine_m3s
        FROM daily_readings
        WHERE Reservoir_Name IN ({placeholders}) AND Date BETWEEN ? AND ?
        ORDER BY Date, Reservoir_Name
    """
    with connect() as conn:
        frame = pd.read_sql_query(query, conn, params=(*names, start.isoformat(), end.isoformat()))
    frame["Date"] = pd.to_datetime(frame["Date"])
    return frame


@st.cache_data(ttl="10m")
def load_forecast(names: tuple[str, ...], start: date, end: date) -> pd.DataFrame:
    basin_ids = [BASIN_BY_RESERVOIR[name] for name in names if name in BASIN_BY_RESERVOIR]
    if not basin_ids:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in basin_ids)
    query = f"""
        SELECT Date, Basin_ID, Q_P50_m3s, WaterLevel_P50_m, Forecast_Type
        FROM hydroai_forecast_daily
        WHERE Basin_ID IN ({placeholders}) AND Date BETWEEN ? AND ?
        ORDER BY Date
    """
    with connect() as conn:
        frame = pd.read_sql_query(query, conn, params=(*basin_ids, start.isoformat(), end.isoformat()))
    if frame.empty:
        return frame
    name_by_basin = {value: key for key, value in BASIN_BY_RESERVOIR.items()}
    frame["Reservoir_Name"] = frame["Basin_ID"].map(name_by_basin)
    frame["Date"] = pd.to_datetime(frame["Date"])
    return frame


@st.cache_data(ttl="10m")
def load_generation(start: date, end: date, include_forecast: bool) -> pd.DataFrame:
    with connect() as conn:
        actual = pd.read_sql_query(
            """
            SELECT Date, SUM(Generation_MWh) AS Generation_MWh
            FROM pmis_generation_daily
            WHERE Basin_ID IN ('BTS','BK','SP3') AND Date BETWEEN ? AND ?
            GROUP BY Date ORDER BY Date
            """,
            conn,
            params=(start.isoformat(), end.isoformat()),
        )
        forecast = pd.DataFrame()
        if include_forecast:
            forecast = pd.read_sql_query(
                """
                SELECT Date, SUM(Generation_Forecast_MWh) AS Generation_MWh
                FROM hydroai_generation_forecast_daily
                WHERE Basin_ID IN ('BTS','BK','SP3') AND Date BETWEEN ? AND ?
                  AND Value_Type <> 'Actual'
                GROUP BY Date ORDER BY Date
                """,
                conn,
                params=(start.isoformat(), end.isoformat()),
            )
    actual["Series"] = "Tổng sản lượng CTTĐ Buôn Kuốp"
    if not forecast.empty:
        forecast["Series"] = "Dự báo AI sản lượng"
    result = pd.concat([actual, forecast], ignore_index=True)
    result["Date"] = pd.to_datetime(result["Date"])
    return result


@st.cache_data(ttl="1h")
def load_enso() -> pd.DataFrame:
    with connect() as conn:
        frame = pd.read_sql_query(
            "SELECT * FROM enso_hydrology_scenario_monthly ORDER BY YearMonth, Basin_ID",
            conn,
        )
    return frame


def period_start(values: pd.Series, granularity: str) -> pd.Series:
    if granularity == "Tuần":
        return values.dt.to_period("W-SAT").dt.start_time
    if granularity == "Tháng":
        return values.dt.to_period("M").dt.start_time
    if granularity == "Năm":
        return values.dt.to_period("Y").dt.start_time
    return values.dt.normalize()


def aggregate(frame: pd.DataFrame, granularity: str, value_columns: list[str]) -> pd.DataFrame:
    if frame.empty or granularity == "Ngày":
        return frame.copy()
    data = frame.copy()
    data["Date"] = period_start(data["Date"], granularity)
    keys = [column for column in ["Date", "Reservoir_Name", "Series"] if column in data.columns]
    aggregations = {
        column: ("last" if "WaterLevel" in column or column in {"MNDBT_m", "MNC_m"} else "mean")
        for column in value_columns
        if column in data.columns
    }
    if "Generation_MWh" in aggregations:
        aggregations["Generation_MWh"] = "sum"
    return data.groupby(keys, as_index=False).agg(aggregations)


def remap_comparison_year(frame: pd.DataFrame, target_year: int) -> pd.DataFrame:
    data = frame.copy()
    def replace_year(value: pd.Timestamp) -> pd.Timestamp:
        try:
            return value.replace(year=target_year)
        except ValueError:
            return value.replace(year=target_year, day=28)
    data["Date"] = data["Date"].map(replace_year)
    return data


def line_chart(
    frame: pd.DataFrame,
    value: str,
    title: str,
    unit: str,
    domain: list[float] | None = None,
    height: int = 390,
) -> None:
    if frame.empty or frame[value].dropna().empty:
        st.info(f"Chưa có dữ liệu {title.lower()} trong khoảng đã chọn.")
        return
    y_scale = alt.Scale(zero=False, domain=domain) if domain else alt.Scale(zero=False)
    series_names = frame["Series"].dropna().drop_duplicates().tolist()
    series_colors = []
    for series_name in series_names:
        normalized = str(series_name).lower()
        if series_name == "MNDBT":
            series_colors.append(COLORS["mndbt"])
        elif series_name == "MNC":
            series_colors.append(COLORS["mnc"])
        elif "dự báo" in normalized:
            series_colors.append(COLORS["forecast"])
        elif "so sánh" in normalized:
            series_colors.append(COLORS["compare"])
        else:
            series_colors.append(COLORS["actual"])
    chart = (
        alt.Chart(frame)
        .mark_line(strokeWidth=2.5)
        .encode(
            x=alt.X("Date:T", title=None, axis=alt.Axis(format="%d/%m", labelOverlap=True)),
            y=alt.Y(f"{value}:Q", title=unit, scale=y_scale),
            color=alt.Color(
                "Series:N",
                title=None,
                scale=alt.Scale(domain=series_names, range=series_colors),
            ),
            strokeDash=alt.StrokeDash("Dash:N", title=None),
            tooltip=[
                alt.Tooltip("Date:T", title="Ngày", format="%d/%m/%Y"),
                alt.Tooltip("Series:N", title="Chuỗi"),
                alt.Tooltip(f"{value}:Q", title=unit, format=",.2f"),
            ],
        )
        .properties(height=height, title=title)
        .interactive(bind_y=False)
    )
    st.altair_chart(chart)


def prepare_chart_series(
    actual: pd.DataFrame,
    comparison: pd.DataFrame,
    forecast: pd.DataFrame,
    value: str,
    forecast_value: str,
    selected_names: list[str],
) -> pd.DataFrame:
    frames = []
    for name in selected_names:
        current = actual[actual["Reservoir_Name"].eq(name)][["Date", value]].copy()
        current["Series"] = name
        current["Dash"] = "Liền"
        frames.append(current)
        if not comparison.empty:
            previous = comparison[comparison["Reservoir_Name"].eq(name)][["Date", value]].copy()
            previous["Series"] = f"{name} - năm so sánh"
            previous["Dash"] = "Nét đứt"
            frames.append(previous)
        if not forecast.empty:
            future = forecast[forecast["Reservoir_Name"].eq(name)][["Date", forecast_value]].copy()
            future = future.rename(columns={forecast_value: value})
            future["Series"] = f"Dự báo AI - {name}"
            future["Dash"] = "Nét đứt"
            frames.append(future)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


if not DB_PATH.exists():
    st.error(f"Không tìm thấy database triển khai: {DB_PATH}")
    st.code("python streamlit_deploy/export_streamlit_data.py")
    st.stop()

reservoir_names, min_date, max_date = metadata()
default_start = date(max_date.year, 1, 1)

st.title("HydroAI - Dashboard hồ chứa")
st.caption("Mực nước, lưu lượng về, sản lượng CTTĐ Buôn Kuốp và dự báo AI.")

with st.sidebar:
    st.header("Bộ lọc")
    selected_names = st.multiselect(
        "Hồ chứa",
        reservoir_names,
        default=["Buôn Tua Srah"] if "Buôn Tua Srah" in reservoir_names else reservoir_names[:1],
        max_selections=3,
    )
    selected_range = st.date_input(
        "Khoảng thời gian",
        value=(default_start, max_date),
        min_value=min_date,
        max_value=max(date(max_date.year, 12, 31), max_date),
    )
    granularity = st.segmented_control(
        "Độ phân giải",
        ["Ngày", "Tuần", "Tháng", "Năm"],
        default="Ngày",
    )
    compare_enabled = st.toggle("So sánh năm khác", value=True)
    available_years = list(range(min_date.year, max_date.year + 1))
    compare_year = st.selectbox(
        "Năm so sánh",
        [year for year in reversed(available_years) if year != max_date.year],
        index=0,
        disabled=not compare_enabled,
    )
    show_forecast = st.toggle("Hiển thị dự báo AI", value=True)
    st.caption(f"Dữ liệu: {min_date:%d/%m/%Y} - {max_date:%d/%m/%Y}")

if not selected_names:
    st.warning("Hãy chọn ít nhất một hồ chứa.")
    st.stop()
if not isinstance(selected_range, tuple) or len(selected_range) != 2:
    st.warning("Hãy chọn đủ ngày bắt đầu và kết thúc.")
    st.stop()

start_date, end_date = selected_range
actual = load_readings(tuple(selected_names), start_date, min(end_date, max_date))
comparison = pd.DataFrame()
if compare_enabled:
    try:
        compare_start = pd.Timestamp(start_date).replace(year=compare_year)
    except ValueError:
        compare_start = pd.Timestamp(start_date).replace(year=compare_year, day=28)
    try:
        compare_end = pd.Timestamp(end_date).replace(year=compare_year)
    except ValueError:
        compare_end = pd.Timestamp(end_date).replace(year=compare_year, day=28)
    comparison = remap_comparison_year(
        load_readings(tuple(selected_names), compare_start.date(), compare_end.date()),
        start_date.year,
    )
forecast = load_forecast(tuple(selected_names), start_date, end_date) if show_forecast else pd.DataFrame()

actual = aggregate(actual, granularity, ["WaterLevel_m", "MNDBT_m", "MNC_m", "Q_in_m3s"])
comparison = aggregate(comparison, granularity, ["WaterLevel_m", "MNDBT_m", "MNC_m", "Q_in_m3s"])
forecast = aggregate(forecast, granularity, ["WaterLevel_P50_m", "Q_P50_m3s"])

latest = actual.sort_values("Date").groupby("Reservoir_Name", as_index=False).tail(1)
metric_columns = st.columns(min(3, max(1, len(latest))))
for column, (_, row) in zip(metric_columns, latest.iterrows()):
    column.metric(
        row["Reservoir_Name"],
        f"{row['WaterLevel_m']:.2f} m" if pd.notna(row["WaterLevel_m"]) else "Chưa có MN",
        f"Q về {row['Q_in_m3s']:.2f} m³/s" if pd.notna(row["Q_in_m3s"]) else None,
        border=True,
    )

dashboard_tab, enso_tab, data_tab = st.tabs(["Vận hành hồ", "Xu hướng El Niño", "Dữ liệu"])
with dashboard_tab:
    water_series = prepare_chart_series(
        actual, comparison, forecast, "WaterLevel_m", "WaterLevel_P50_m", selected_names
    )
    water_domain = None
    if len(selected_names) == 1 and not actual.empty:
        mnc = actual["MNC_m"].dropna()
        mndbt = actual["MNDBT_m"].dropna()
        if not mnc.empty:
            lower = float(mnc.median())
            upper_candidates = [water_series["WaterLevel_m"].max()]
            if not mndbt.empty:
                upper_candidates.append(float(mndbt.median()))
            upper = max(value for value in upper_candidates if pd.notna(value))
            span = max(upper - lower, 0.5)
            water_domain = [lower - max(span * 0.06, 0.04), upper + max(span * 0.08, 0.04)]
            references = pd.DataFrame(
                {
                    "Date": [water_series["Date"].min(), water_series["Date"].max()] * 2,
                    "WaterLevel_m": [float(mndbt.median()) if not mndbt.empty else np.nan] * 2 + [lower] * 2,
                    "Series": ["MNDBT"] * 2 + ["MNC"] * 2,
                    "Dash": ["Liền"] * 4,
                }
            ).dropna(subset=["WaterLevel_m"])
            water_series = pd.concat([water_series, references], ignore_index=True)
    line_chart(water_series, "WaterLevel_m", "Mực nước thượng lưu", "m", water_domain)

    flow_series = prepare_chart_series(
        actual, comparison, forecast, "Q_in_m3s", "Q_P50_m3s", selected_names
    )
    line_chart(flow_series, "Q_in_m3s", "Lưu lượng về", "m³/s")

    generation = aggregate(
        load_generation(start_date, end_date, show_forecast),
        granularity,
        ["Generation_MWh"],
    )
    if not generation.empty:
        generation["Dash"] = np.where(generation["Series"].str.contains("Dự báo"), "Nét đứt", "Liền")
    line_chart(generation, "Generation_MWh", "Sản lượng phát điện", "MWh")

with enso_tab:
    st.subheader("Kịch bản ENSO/KTTV theo tháng")
    enso = load_enso()
    enso["Q_Anomaly_pct"] = pd.to_numeric(enso["Q_Anomaly_pct"], errors="coerce")
    enso_chart = (
        alt.Chart(enso)
        .mark_line(point=True, strokeWidth=2.5)
        .encode(
            x=alt.X("YearMonth:O", title="Tháng"),
            y=alt.Y("Q_Anomaly_pct:Q", title="Sai lệch lưu lượng so với TBNN (%)"),
            color=alt.Color("Basin_ID:N", title="Lưu vực"),
            tooltip=["YearMonth", "Basin_ID", alt.Tooltip("Q_Anomaly_pct:Q", format=".1f"), "Method"],
        )
        .properties(height=380)
    )
    st.altair_chart(enso_chart)
    st.dataframe(
        enso,
        hide_index=True,
        column_config={
            "Q_Anomaly_pct": st.column_config.NumberColumn("Sai lệch (%)", format="%.1f"),
            "YearMonth": "Tháng",
            "Basin_ID": "Lưu vực",
            "ENSO_Phase": "Pha ENSO",
            "Source": "Nguồn",
            "Method": "Phương pháp",
        },
    )

with data_tab:
    st.subheader("Số liệu đã lọc")
    st.dataframe(
        actual.sort_values(["Date", "Reservoir_Name"], ascending=[False, True]),
        hide_index=True,
        column_config={
            "Date": st.column_config.DateColumn("Ngày", format="DD/MM/YYYY"),
            "Reservoir_Name": "Hồ chứa",
            "WaterLevel_m": st.column_config.NumberColumn("Mực nước (m)", format="%.2f"),
            "Q_in_m3s": st.column_config.NumberColumn("Lưu lượng về (m³/s)", format="%.2f"),
            "MNDBT_m": st.column_config.NumberColumn("MNDBT (m)", format="%.2f"),
            "MNC_m": st.column_config.NumberColumn("MNC (m)", format="%.2f"),
        },
    )

st.caption("HydroAI | Database triển khai là bản tổng hợp theo ngày từ hệ thống PMIS/EVN.")
