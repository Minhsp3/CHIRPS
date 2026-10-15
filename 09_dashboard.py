from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from config import BASIN_NAMES, FORECAST_FILE, MODEL_DIR, OUTPUT_DIR


def load_forecast() -> pd.DataFrame:
    if FORECAST_FILE.exists():
        return pd.read_excel(FORECAST_FILE, parse_dates=["Date"])
    frames = []
    for basin_id in BASIN_NAMES:
        file = OUTPUT_DIR / f"Forecast_{basin_id}.csv"
        if file.exists():
            frames.append(pd.read_csv(file, parse_dates=["Date"]))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_metrics() -> pd.DataFrame:
    rows = []
    for basin_id in BASIN_NAMES:
        file = MODEL_DIR / f"metrics_{basin_id}.json"
        if file.exists():
            with open(file, encoding="utf-8") as handle:
                metrics = json.load(handle)
            rows.append({key: value for key, value in metrics.items() if key != "Features"})
    return pd.DataFrame(rows)


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise ImportError("Chua co Streamlit. Hay chay: pip install streamlit") from exc

    st.set_page_config(page_title="HydroAI v5", layout="wide")
    st.title("HydroAI v5")

    forecast = load_forecast()
    metrics = load_metrics()

    if not metrics.empty:
        st.subheader("Model metrics")
        st.dataframe(metrics, use_container_width=True)

    if forecast.empty:
        st.warning("Chua co du lieu du bao. Hay chay den buoc 08_forecast.py.")
        return

    basin_options = sorted(forecast["Basin_ID"].dropna().unique())
    basin_id = st.sidebar.selectbox("Luu vuc", basin_options)
    basin_forecast = forecast[forecast["Basin_ID"] == basin_id].sort_values("Date")

    latest = basin_forecast.tail(1)
    if not latest.empty:
        row = latest.iloc[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Q P10 (m3/s)", f"{row['Q_P10_m3s']:.2f}")
        c2.metric("Q P50 (m3/s)", f"{row['Q_P50_m3s']:.2f}")
        c3.metric("Q P90 (m3/s)", f"{row['Q_P90_m3s']:.2f}")

    chart = basin_forecast.set_index("Date")[["Q_P10_m3s", "Q_P50_m3s", "Q_P90_m3s"]]
    st.line_chart(chart)
    st.subheader("Du lieu du bao")
    st.dataframe(basin_forecast, use_container_width=True)


if __name__ == "__main__":
    main()
