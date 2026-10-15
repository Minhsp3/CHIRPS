import html
import json
import os
import socket
import sqlite3
import sys
import calendar
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from evn_hochua_to_sql import (
    DEFAULT_SOURCE_URL,
    create_session,
    ensure_schema,
    fetch_snapshot,
    save_snapshot,
)
from openpyxl import load_workbook

from analysis_2027 import render_analysis_page


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "PMIS" / "evn_hochua_manual.db"
FORECAST_DB_PATH = DB_PATH
NSMO_ROOT = Path(r"D:\VScode\Project\LayDuLieu_NMSO\NSMO_1")
HOST = "0.0.0.0"
DEFAULT_PORT = 10001
SAVE_BATCH_SIZE = 20
SAVE_TIMEOUT = 30
PYTHON_EXE = BASE_DIR / ".venv" / "Scripts" / "python.exe"
ENERGY_LOOKUP_PATH = BASE_DIR / "Xuat_hao_nuoc_BTS.xlsx"
ENERGY_RESERVOIR_NAME = "Buôn Tua Srah"
ENERGY_SHN_M3_PER_KWH = 8.58
DEFAULT_ENERGY_LOOKUP = [
    (465.0 + step * (487.5 - 465.0) / 90.0, 60.0 * ((step / 90.0) ** 1.15))
    for step in range(91)
]
FLOOD_THRESHOLD_BY_RESERVOIR = {
    "Buôn Tua Srah": 650,
    "Srêpốk 3": 1200,
}
HYDROAI_RESERVOIR_TO_BASIN = {
    "Buôn Tua Srah": "BTS",
    "Buôn Kuốp": "BK",
    "Srêpốk 3": "SP3",
}
GENERATION_RESERVOIRS = list(HYDROAI_RESERVOIR_TO_BASIN.keys())
_ENERGY_LOOKUP_CACHE = None
_WEATHER_SERVICE_CACHE = None

CLEAN_NAME_SQL = (
    "TRIM(CASE "
    "WHEN INSTR(reservoir_name, 'Đồng bộ lúc:') > 0 "
    "THEN SUBSTR(reservoir_name, 1, INSTR(reservoir_name, 'Đồng bộ lúc:') - 1) "
    "ELSE reservoir_name END)"
)

COLORS = [
    "#16a34a",
    "#d33f49",
    "#198754",
    "#f08c00",
    "#7c3aed",
    "#0891b2",
    "#334155",
    "#db2777",
    "#65a30d",
    "#c2410c",
    "#0f766e",
    "#4f46e5",
]

YEAR_COMPARE_COLORS = [
    "#16a34a",
    "#2563eb",
    "#f97316",
    "#7c3aed",
    "#d33f49",
]


@dataclass
class ReservoirRow:
    name: str
    sync_hint: str
    h_tl: float | None
    q_ve: float | None
    q_xt: float | None
    q_xm: float | None


def open_db():
    conn = sqlite3.connect(f"file:{DB_PATH.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -80000")
    conn.execute("PRAGMA mmap_size = 268435456")
    return conn


def parse_db_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def to_db_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def parse_input_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return floor_hour(datetime.strptime(value, fmt))
        except ValueError:
            continue
    raise ValueError("Thời gian phải theo định dạng yyyy-mm-dd HH:MM hoặc dd/mm/yyyy HH:MM.")


def format_input(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%dT%H:%M") if value else ""


def format_date_input(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else ""


def display_hour(value: datetime | None) -> int:
    return value.hour if value else 0


def load_nsmo_weather_service():
    global _WEATHER_SERVICE_CACHE
    if _WEATHER_SERVICE_CACHE is not None:
        return _WEATHER_SERVICE_CACHE
    if not NSMO_ROOT.exists():
        _WEATHER_SERVICE_CACHE = False
        return None
    nsmo_path = str(NSMO_ROOT)
    if nsmo_path not in sys.path:
        sys.path.insert(0, nsmo_path)
    try:
        from modules.weather.service import (  # type: ignore
            get_region_weather_bundle,
            load_region_weather_bundle_from_sqlite,
            merge_weather_summary,
            save_region_temperatures_to_sqlite,
            weather_summary_needs_live_refresh,
        )

        _WEATHER_SERVICE_CACHE = {
            "get_region_weather_bundle": get_region_weather_bundle,
            "load_region_weather_bundle_from_sqlite": load_region_weather_bundle_from_sqlite,
            "merge_weather_summary": merge_weather_summary,
            "save_region_temperatures_to_sqlite": save_region_temperatures_to_sqlite,
            "weather_summary_needs_live_refresh": weather_summary_needs_live_refresh,
        }
    except Exception:
        _WEATHER_SERVICE_CACHE = False
        return None
    return _WEATHER_SERVICE_CACHE


def fallback_weather_summary() -> dict:
    return {
        "mb": {"label": "Miền Bắc", "city": "Hà Nội", "value": "--", "humidity": "--", "condition": "--", "source": "unavailable"},
        "mt": {"label": "Miền Trung", "city": "Đà Nẵng", "value": "--", "humidity": "--", "condition": "--", "source": "unavailable"},
        "mn": {"label": "Miền Nam", "city": "TP.HCM", "value": "--", "humidity": "--", "condition": "--", "source": "unavailable"},
    }


def weather_summary_has_values(summary: dict) -> bool:
    if not isinstance(summary, dict):
        return False
    for item in summary.values():
        if not isinstance(item, dict):
            continue
        if item.get("temp_c") is not None or item.get("value") not in (None, "", "--"):
            return True
    return False


def latest_nsmo_weather_date() -> str | None:
    weather_db = NSMO_ROOT / "nsmo_data.db"
    if not weather_db.exists():
        return None
    try:
        with sqlite3.connect(weather_db) as conn:
            row = conn.execute("SELECT MAX(report_date) FROM nsmo_weather_region").fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def load_dashboard_weather_summary(target_date: datetime | None) -> dict:
    date_text = (target_date or datetime.now()).strftime("%Y-%m-%d")
    service = load_nsmo_weather_service()
    if not service:
        summary = fallback_weather_summary()
        summary["_source_date"] = date_text
        return summary
    try:
        cached_bundle = service["load_region_weather_bundle_from_sqlite"](date_text)
        cached_summary = cached_bundle.get("summary", {}) if isinstance(cached_bundle, dict) else {}
        if service["weather_summary_needs_live_refresh"](cached_summary):
            live_bundle = service["get_region_weather_bundle"](date_text)
            live_summary = live_bundle.get("summary", {}) if isinstance(live_bundle, dict) else {}
            if weather_summary_has_values(live_summary):
                try:
                    service["save_region_temperatures_to_sqlite"](date_text, live_summary)
                except Exception:
                    pass
                summary = service["merge_weather_summary"](live_summary, cached_summary)
                summary["_source_date"] = date_text
                return summary
        if not weather_summary_has_values(cached_summary):
            latest_date = latest_nsmo_weather_date()
            if latest_date and latest_date != date_text:
                latest_bundle = service["load_region_weather_bundle_from_sqlite"](latest_date)
                latest_summary = latest_bundle.get("summary", {}) if isinstance(latest_bundle, dict) else {}
                if weather_summary_has_values(latest_summary):
                    summary = service["merge_weather_summary"](latest_summary, fallback_weather_summary())
                    summary["_source_date"] = latest_date
                    return summary
        summary = service["merge_weather_summary"](cached_summary, fallback_weather_summary())
        summary["_source_date"] = date_text
        return summary
    except Exception:
        summary = fallback_weather_summary()
        summary["_source_date"] = date_text
        return summary


def format_display(value: datetime | None) -> str:
    return value.strftime("%d/%m/%Y %H:%M") if value else "-"


def format_num(value):
    if value is None:
        return ""
    text = f"{float(value):,.2f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def parse_flag(value) -> bool:
    if isinstance(value, list):
        value = value[-1] if value else ""
    return str(value).strip().lower() in {"1", "on", "true", "yes"}


def shift_one_year_back(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def shift_years_back(value: datetime, years: int) -> datetime:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def replace_year(value: datetime, year: int) -> datetime:
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year, day=28)


def iter_hours(start_at: datetime, end_at: datetime):
    current = start_at
    while current <= end_at:
        yield current
        current += timedelta(hours=1)


def is_daily_range(start_at: datetime, end_at: datetime) -> bool:
    return int((end_at - start_at).total_seconds() // 3600) + 1 > 24


def build_hourly_labels(hours):
    return [hour.strftime("%H:%M") for hour in hours]


def parse_granularity(value) -> str:
    if isinstance(value, list):
        value = value[-1] if value else ""
    value = str(value or "").strip().lower()
    aliases = {"day": "daily", "ngay": "daily", "week": "weekly", "tuan": "weekly", "month": "monthly", "thang": "monthly", "year": "yearly", "nam": "yearly"}
    value = aliases.get(value, value)
    return value if value in {"daily", "weekly", "monthly", "yearly"} else "auto"


def clean_name(value: str | None) -> str:
    text = (value or "").strip()
    marker = "Đồng bộ lúc:"
    index = text.find(marker)
    if index >= 0:
        text = text[:index].strip()
    return " ".join(text.split())


def get_latest_requested_at(conn) -> datetime | None:
    row = conn.execute("SELECT MAX(requested_at) AS requested_at FROM crawl_requests").fetchone()
    return parse_db_datetime(row["requested_at"] if row else None)


def get_requested_at_before(conn, end_at: datetime) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(requested_at) AS requested_at FROM crawl_requests WHERE requested_at <= ?",
        (to_db_datetime(end_at),),
    ).fetchone()
    return parse_db_datetime(row["requested_at"] if row else None)


def get_latest_data_at_before(conn, end_at: datetime) -> datetime | None:
    row = conn.execute(
        """
        SELECT MAX(requested_at_last_seen) AS requested_at
        FROM reservoir_readings
        WHERE requested_at_last_seen <= ?
        """,
        (to_db_datetime(end_at),),
    ).fetchone()
    return parse_db_datetime(row["requested_at"] if row else None)


def load_available_years(conn) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT CAST(strftime('%Y', observed_at) AS INTEGER) AS year
        FROM reservoir_readings
        WHERE observed_at IS NOT NULL
          AND (h_tl IS NOT NULL OR q_ve IS NOT NULL)
        ORDER BY year DESC
        """
    ).fetchall()
    return [int(row["year"]) for row in rows if row["year"]]


def count_db_hours(conn, start_at: datetime, end_at: datetime) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM crawl_requests WHERE requested_at BETWEEN ? AND ?",
        (to_db_datetime(start_at), to_db_datetime(end_at)),
    ).fetchone()
    return int(row["total"] or 0)


def load_snapshot_rows(conn, snapshot_at: datetime) -> list[ReservoirRow]:
    snapshot_text = to_db_datetime(snapshot_at)
    rows = conn.execute(
        f"""
        SELECT
            {CLEAN_NAME_SQL} AS clean_name,
            sync_hint,
            observed_at,
            h_tl,
            q_ve,
            q_xt,
            q_xm,
            requested_at_first_seen,
            requested_at_last_seen,
            id
        FROM reservoir_readings
        WHERE requested_at_first_seen <= ?
          AND requested_at_last_seen >= ?
        ORDER BY clean_name, requested_at_first_seen DESC, observed_at DESC, id DESC
        """,
        (snapshot_text, snapshot_text),
    ).fetchall()

    deduped = OrderedDict()
    for row in rows:
        name = clean_name(row["clean_name"])
        if not name or name in deduped:
            continue
        sync_hint = row["sync_hint"] or ""
        if not sync_hint and row["observed_at"]:
            observed = parse_db_datetime(row["observed_at"])
            sync_hint = observed.strftime("%Hh%M ngày %d/%m/%Y") if observed else ""
        deduped[name] = ReservoirRow(
            name=name,
            sync_hint=sync_hint,
            h_tl=row["h_tl"],
            q_ve=row["q_ve"],
            q_xt=row["q_xt"],
            q_xm=row["q_xm"],
        )
    return list(deduped.values())


def get_selected_reservoirs(params, available_names: list[str]) -> list[str]:
    raw = params.get("reservoir", [])
    if isinstance(raw, str):
        requested = [raw] if raw else []
    else:
        requested = [item for item in raw if item]
    available = set(available_names)
    return [name for name in requested if name in available]


def load_series(conn, start_at: datetime, end_at: datetime, names: list[str]):
    hours = list(iter_hours(start_at, end_at))
    hour_index = {hour: index for index, hour in enumerate(hours)}
    water = {name: [None] * len(hours) for name in names}
    flow = {name: [None] * len(hours) for name in names}
    db_hours = set()

    sql = f"""
        SELECT
            {CLEAN_NAME_SQL} AS clean_name,
            h_tl,
            q_ve,
            requested_at_first_seen,
            requested_at_last_seen,
            observed_at,
            id
        FROM reservoir_readings
        WHERE {CLEAN_NAME_SQL} = ?
          AND requested_at_last_seen >= ?
          AND requested_at_first_seen <= ?
        ORDER BY requested_at_first_seen DESC, observed_at DESC, id DESC
    """
    start_text = to_db_datetime(start_at)
    end_text = to_db_datetime(end_at)

    for name in names:
        rows = conn.execute(sql, (name, start_text, end_text)).fetchall()
        for row in rows:
            first_seen = parse_db_datetime(row["requested_at_first_seen"])
            last_seen = parse_db_datetime(row["requested_at_last_seen"])
            if not first_seen or not last_seen:
                continue
            row_start = max(start_at, first_seen)
            row_end = min(end_at, last_seen)
            if row_start > row_end:
                continue
            for snapshot_at in iter_hours(row_start, row_end):
                idx = hour_index[snapshot_at]
                if water[name][idx] is not None or flow[name][idx] is not None:
                    continue
                water[name][idx] = row["h_tl"]
                flow[name][idx] = row["q_ve"]
                db_hours.add(snapshot_at)

    return hours, water, flow, db_hours


def add_series_value(values, index: int, value):
    if value is None:
        return
    values[index] = value if values[index] is None else round(values[index] + value, 4)


def sum_present_values(*values):
    present = [value for value in values if value is not None]
    return round(sum(present), 4) if present else None


def load_flow_compare_series(conn, start_at: datetime, end_at: datetime, names: list[str]):
    hours = list(iter_hours(start_at, end_at))
    hour_index = {hour: index for index, hour in enumerate(hours)}
    inflow_total = [None] * len(hours)
    outflow_total = [None] * len(hours)
    db_hours = set()

    sql = f"""
        SELECT
            {CLEAN_NAME_SQL} AS clean_name,
            q_ve,
            q_xt,
            q_xm,
            requested_at_first_seen,
            requested_at_last_seen,
            observed_at,
            id
        FROM reservoir_readings
        WHERE {CLEAN_NAME_SQL} = ?
          AND requested_at_last_seen >= ?
          AND requested_at_first_seen <= ?
        ORDER BY requested_at_first_seen DESC, observed_at DESC, id DESC
    """
    start_text = to_db_datetime(start_at)
    end_text = to_db_datetime(end_at)

    for name in names:
        inflow = [None] * len(hours)
        outflow = [None] * len(hours)
        rows = conn.execute(sql, (name, start_text, end_text)).fetchall()
        for row in rows:
            first_seen = parse_db_datetime(row["requested_at_first_seen"])
            last_seen = parse_db_datetime(row["requested_at_last_seen"])
            if not first_seen or not last_seen:
                continue
            row_start = max(start_at, first_seen)
            row_end = min(end_at, last_seen)
            if row_start > row_end:
                continue
            for snapshot_at in iter_hours(row_start, row_end):
                idx = hour_index[snapshot_at]
                if inflow[idx] is None:
                    inflow[idx] = row["q_ve"]
                if outflow[idx] is None:
                    outflow[idx] = sum_present_values(row["q_xt"], row["q_xm"])
                db_hours.add(snapshot_at)
        for idx, value in enumerate(inflow):
            add_series_value(inflow_total, idx, value)
        for idx, value in enumerate(outflow):
            add_series_value(outflow_total, idx, value)

    return hours, {"Lưu lượng về": inflow_total, "Lưu lượng xả": outflow_total}, db_hours


def load_reference_levels(conn, end_at: datetime, names: list[str]):
    levels = {}
    sql = f"""
        SELECT h_dbt, h_c
        FROM reservoir_readings
        WHERE {CLEAN_NAME_SQL} = ?
          AND requested_at_first_seen <= ?
          AND (h_dbt IS NOT NULL OR h_c IS NOT NULL)
        ORDER BY requested_at_first_seen DESC, observed_at DESC, id DESC
        LIMIT 1
    """
    end_text = to_db_datetime(end_at)
    for name in names:
        row = conn.execute(sql, (name, end_text)).fetchone()
        levels[name] = {
            "mndbt": row["h_dbt"] if row else None,
            "mnc": row["h_c"] if row else None,
        }
    return levels


def smooth_outliers(values, metric: str):
    if len(values) < 3:
        return list(values)
    result = list(values)
    for index in range(1, len(values) - 1):
        previous_value = result[index - 1]
        current_value = result[index]
        next_value = values[index + 1]
        if previous_value is None or current_value is None or next_value is None:
            continue
        midpoint = (previous_value + next_value) / 2
        neighbor_gap = abs(previous_value - next_value)
        deviation = abs(current_value - midpoint)
        baseline = max(abs(previous_value), abs(next_value), 1.0)
        if metric == "water":
            is_outlier = (
                current_value > max(previous_value, next_value)
                and neighbor_gap <= max(0.25, baseline * 0.002)
                and deviation >= max(0.35, neighbor_gap * 5, baseline * 0.0006)
            )
        else:
            is_outlier = (
                current_value > max(previous_value, next_value)
                and neighbor_gap <= max(8.0, baseline * 0.20)
                and deviation >= max(25.0, neighbor_gap * 5, baseline * 3.0)
            )
        if is_outlier:
            result[index] = round(midpoint, 4)
    return result


def rollup_daily(hours, series_map, aggregate="average"):
    day_order = []
    labels = []
    for hour in hours:
        if hour.date() not in day_order:
            day_order.append(hour.date())
            labels.append(hour.strftime("%d/%m"))

    rolled = {}
    for name, values in series_map.items():
        daily_values = []
        for day in day_order:
            samples = [
                (hour, value)
                for hour, value in zip(hours, values)
                if hour.date() == day and value is not None
            ]
            if not samples:
                daily_values.append(None)
            elif aggregate == "last":
                daily_values.append(samples[-1][1])
            else:
                daily_values.append(round(sum(value for _, value in samples) / len(samples), 4))
        rolled[name] = daily_values
    return labels, rolled


def sunday_week_start(value: datetime) -> datetime:
    days_since_sunday = (value.weekday() + 1) % 7
    return value - timedelta(days=days_since_sunday)


def sunday_week_number(value: datetime) -> int:
    return min(52, int(value.strftime("%U")) + 1)


def week_label(value: datetime) -> str:
    return f"Tuần {sunday_week_number(value):02d}"


def rollup_weekly(hours, series_map):
    week_order = []
    labels = []
    for hour in hours:
        week_start = sunday_week_start(hour)
        week_key = week_start.date()
        if week_key not in week_order:
            week_order.append(week_key)
            labels.append(week_label(hour))

    rolled = {}
    for name, values in series_map.items():
        weekly_values = []
        for week_key in week_order:
            samples = [
                value
                for hour, value in zip(hours, values)
                if sunday_week_start(hour).date() == week_key and value is not None
            ]
            weekly_values.append(round(sum(samples) / len(samples), 4) if samples else None)
        rolled[name] = weekly_values
    return labels, rolled


def rollup_period(hours, series_map, period: str, aggregate="average"):
    period_order = []
    labels = []
    for hour in hours:
        if period == "weekly":
            key = sunday_week_start(hour).date()
        elif period == "monthly":
            key = (hour.year, hour.month)
        else:
            key = (hour.year,)
        if key not in period_order:
            period_order.append(key)
            if period == "weekly":
                labels.append(week_label(hour))
            elif period == "monthly":
                labels.append(hour.strftime("%m/%Y"))
            else:
                labels.append(str(hour.year))

    rolled = {}
    for name, values in series_map.items():
        period_values = []
        for key in period_order:
            samples = [
                (hour, value)
                for hour, value in zip(hours, values)
                if (
                    sunday_week_start(hour).date()
                    if period == "weekly"
                    else ((hour.year, hour.month) if period == "monthly" else (hour.year,))
                ) == key and value is not None
            ]
            if not samples:
                period_values.append(None)
            elif aggregate == "last":
                period_values.append(round(samples[-1][1], 4))
            elif aggregate == "sum":
                period_values.append(round(sum(value for _, value in samples), 4))
            else:
                period_values.append(round(sum(value for _, value in samples) / len(samples), 4))
        rolled[name] = period_values
    return labels, rolled


def align_length(values, length: int):
    if len(values) == length:
        return values
    if len(values) > length:
        return values[:length]
    return values + [None] * (length - len(values))


def build_datasets(series_map, selected_names, suffix="", dashed=False, color_override=None):
    datasets = []
    for index, name in enumerate(selected_names):
        color = color_override or COLORS[index % len(COLORS)]
        label = f"{name} - {suffix}" if suffix and len(selected_names) > 1 else (suffix or name)
        datasets.append(
            {
                "label": label,
                "data": series_map.get(name, []),
                "borderColor": color,
                "borderDash": [8, 6] if dashed else [],
                "borderWidth": 2 if dashed else 3,
            }
        )
    return datasets


def build_flow_compare_datasets(series_map):
    specs = [
        ("Lưu lượng về", "#16a34a"),
        ("Lưu lượng xả", "#f97316"),
    ]
    return [
        {
            "label": label,
            "data": series_map.get(label, []),
            "borderColor": color,
            "borderDash": [],
            "borderWidth": 3,
        }
        for label, color in specs
    ]


def load_generation_daily_series(conn, axis_dates, selected_names):
    generation_map = {name: [None] * len(axis_dates) for name in selected_names}
    reservoirs = [name for name in selected_names if name in HYDROAI_RESERVOIR_TO_BASIN]
    if not axis_dates or not reservoirs:
        return generation_map
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pmis_generation_daily'"
    ).fetchone()
    if not table_exists:
        return generation_map

    basin_to_name = {HYDROAI_RESERVOIR_TO_BASIN[name]: name for name in reservoirs}
    placeholders = ",".join("?" for _ in basin_to_name)
    start_date = axis_dates[0].strftime("%Y-%m-%d")
    end_date = axis_dates[-1].strftime("%Y-%m-%d")
    rows = conn.execute(
        f"""
        SELECT Date, Basin_ID, Generation_MWh
        FROM pmis_generation_daily
        WHERE Basin_ID IN ({placeholders})
          AND Date BETWEEN ? AND ?
        ORDER BY Date, Basin_ID
        """,
        [*basin_to_name.keys(), start_date, end_date],
    ).fetchall()
    date_index = {value.strftime("%Y-%m-%d"): index for index, value in enumerate(axis_dates)}
    for row in rows:
        name = basin_to_name.get(row["Basin_ID"])
        index = date_index.get(row["Date"])
        if name is not None and index is not None:
            generation_map[name][index] = row["Generation_MWh"]
    return generation_map


def build_generation_datasets(
    series_map,
    selected_names,
    suffix="",
    dashed=False,
    color_override=None,
    label="Tổng sản lượng CTTĐ Buôn Kuốp",
):
    length = max((len(series_map.get(name, [])) for name in selected_names), default=0)
    total_values = []
    for index in range(length):
        samples = []
        for name in selected_names:
            values = series_map.get(name, [])
            if index < len(values) and values[index] is not None:
                samples.append(values[index])
        total_values.append(round(sum(samples), 4) if samples else None)
    if not any(value is not None for value in total_values):
        return []

    display_label = f"{label} - {suffix}" if suffix else label
    return [
        {
            "label": display_label,
            "data": total_values,
            "borderColor": color_override or "#16a34a",
            "borderDash": [8, 5] if dashed else [],
            "borderWidth": 3,
            "unit": "MWh",
        }
    ]


def build_year_compare_datasets(year_maps, selected_names):
    datasets = []
    show_name = len(selected_names) > 1
    for index, item in enumerate(year_maps):
        year = item["year"]
        series_map = item["series"]
        color = YEAR_COMPARE_COLORS[index % len(YEAR_COMPARE_COLORS)]
        for name in selected_names:
            label = f"{name} - {year}" if show_name else str(year)
            datasets.append(
                {
                    "label": label,
                    "data": series_map.get(name, []),
                    "borderColor": color,
                    "borderDash": [8, 6] if index else [],
                    "borderWidth": 2 if index else 3,
                }
            )
    return datasets


def build_five_year_compare_datasets(
    conn,
    start_at,
    chart_end_at,
    selected_names,
    weekly,
    daily,
    labels,
    current_water_map,
    current_flow_map,
):
    water_year_maps = [{"year": start_at.year, "series": {name: list(current_water_map.get(name, [])) for name in selected_names}}]
    flow_year_maps = [{"year": start_at.year, "series": {name: list(current_flow_map.get(name, [])) for name in selected_names}}]

    for offset in range(1, 5):
        period_start = shift_years_back(start_at, offset)
        period_end = shift_years_back(chart_end_at, offset)
        period_hours, water_map, flow_map, _ = load_series(conn, period_start, period_end, selected_names)
        for name in selected_names:
            water_map[name] = smooth_outliers(water_map[name], "water")
            flow_map[name] = smooth_outliers(flow_map[name], "flow")
        if weekly:
            _, water_map = rollup_weekly(period_hours, water_map)
            _, flow_map = rollup_weekly(period_hours, flow_map)
        elif daily:
            _, water_map = rollup_daily(period_hours, water_map, aggregate="last")
            _, flow_map = rollup_daily(period_hours, flow_map, aggregate="average")
        for name in selected_names:
            water_map[name] = align_length(water_map[name], len(labels))
            flow_map[name] = align_length(flow_map[name], len(labels))
        water_year_maps.append({"year": period_start.year, "series": water_map})
        flow_year_maps.append({"year": period_start.year, "series": flow_map})

    return build_year_compare_datasets(water_year_maps, selected_names), build_year_compare_datasets(
        flow_year_maps, selected_names
    )


def build_reference_datasets(levels, selected_names, label_count: int, water_map=None):
    datasets = []
    show_name = len(selected_names) > 1
    water_values = [
        value
        for name in selected_names
        for value in (water_map or {}).get(name, [])
        if value is not None
    ]
    level_values = [
        value
        for name in selected_names
        for value in (
            levels.get(name, {}).get("mndbt"),
            levels.get(name, {}).get("mnc"),
        )
        if value is not None
    ]
    for name in selected_names:
        level = levels.get(name, {})
        mndbt = level.get("mndbt")
        mnc = level.get("mnc")
        if mndbt is not None:
            datasets.append(
                {
                    "label": f"{name} - MNDBT" if show_name else "MNDBT",
                    "data": [mndbt] * label_count,
                    "borderColor": "#fde68a",
                    "borderDash": [],
                    "borderWidth": 2,
                    "actualValue": mndbt,
                }
            )
        if mnc is not None:
            datasets.append(
                {
                    "label": f"{name} - MNC" if show_name else "MNC",
                    "data": [mnc] * label_count,
                    "borderColor": "#8a8f98",
                    "borderDash": [],
                    "borderWidth": 2,
                    "actualValue": mnc,
                }
            )
    return datasets


def load_energy_lookup():
    global _ENERGY_LOOKUP_CACHE
    if _ENERGY_LOOKUP_CACHE is not None:
        return _ENERGY_LOOKUP_CACHE
    if not ENERGY_LOOKUP_PATH.exists():
        _ENERGY_LOOKUP_CACHE = DEFAULT_ENERGY_LOOKUP
        return _ENERGY_LOOKUP_CACHE

    workbook = load_workbook(ENERGY_LOOKUP_PATH, data_only=True, read_only=True)
    try:
        worksheet = workbook[ENERGY_RESERVOIR_NAME] if ENERGY_RESERVOIR_NAME in workbook.sheetnames else workbook.worksheets[0]
        rows = []
        for row in worksheet.iter_rows(min_row=1, values_only=True):
            if len(row) < 5:
                continue
            level, storage_million_m3 = row[3], row[4]
            if not isinstance(level, (int, float)) or not isinstance(storage_million_m3, (int, float)):
                continue
            level = float(level)
            storage_million_m3 = float(storage_million_m3)
            if not 450.0 <= level <= 500.0 or storage_million_m3 < 0:
                continue
            energy_million_kwh = storage_million_m3 / ENERGY_SHN_M3_PER_KWH
            rows.append((level, energy_million_kwh))
        _ENERGY_LOOKUP_CACHE = sorted(set(rows))
        return _ENERGY_LOOKUP_CACHE
    finally:
        workbook.close()


def lookup_energy_by_water_level(level):
    if level is None:
        return None
    lookup = load_energy_lookup()
    if not lookup:
        return None

    level = float(level)
    if level <= lookup[0][0]:
        return round(lookup[0][1], 4)
    if level >= lookup[-1][0]:
        return round(lookup[-1][1], 4)

    left = 0
    right = len(lookup) - 1
    while left <= right:
        mid = (left + right) // 2
        mid_level = lookup[mid][0]
        if mid_level == level:
            return round(lookup[mid][1], 4)
        if mid_level < level:
            left = mid + 1
        else:
            right = mid - 1

    lower_level, lower_energy = lookup[right]
    upper_level, upper_energy = lookup[left]
    ratio = (level - lower_level) / (upper_level - lower_level)
    return round(lower_energy + ratio * (upper_energy - lower_energy), 4)


def build_energy_datasets(water_map, selected_names, reference_levels):
    if ENERGY_RESERVOIR_NAME not in selected_names:
        return []
    values = [lookup_energy_by_water_level(level) for level in water_map.get(ENERGY_RESERVOIR_NAME, [])]
    if not any(value is not None for value in values):
        return []
    levels = reference_levels.get(ENERGY_RESERVOIR_NAME, {})
    axis_min = lookup_energy_by_water_level(levels.get("mnc"))
    axis_max = lookup_energy_by_water_level(levels.get("mndbt"))
    return [
        {
            "label": "Sản lượng còn lại",
            "data": values,
            "borderColor": "#f97316",
            "borderDash": [5, 4],
            "borderWidth": 2,
            "yAxisID": "energy",
            "unit": "tr. kWh",
            "axisMin": axis_min,
            "axisMax": axis_max,
            "alignWaterMin": levels.get("mnc"),
            "alignWaterMax": levels.get("mndbt"),
        }
    ]


def refresh_energy_dataset_from_water_datasets(water_datasets):
    energy_dataset = next((dataset for dataset in water_datasets if dataset.get("label") == "Sản lượng còn lại"), None)
    if not energy_dataset:
        return

    water_values = []
    for dataset in water_datasets:
        label = str(dataset.get("label", ""))
        if label == ENERGY_RESERVOIR_NAME or label.startswith(f"Dự báo AI MN {ENERGY_RESERVOIR_NAME}"):
            values = list(dataset.get("data", []))
            if len(values) > len(water_values):
                water_values.extend([None] * (len(values) - len(water_values)))
            for index, value in enumerate(values):
                if value is not None:
                    water_values[index] = value

    if not water_values:
        return
    energy_dataset["data"] = [lookup_energy_by_water_level(level) for level in water_values]


def build_flood_threshold_datasets(selected_names, label_count: int):
    datasets = []
    matched_names = [name for name in selected_names if name in FLOOD_THRESHOLD_BY_RESERVOIR]
    show_name = len(matched_names) > 1
    for name in matched_names:
        threshold = FLOOD_THRESHOLD_BY_RESERVOIR[name]
        datasets.append(
            {
                "label": f"ngưỡng tạo lũ - {name}" if show_name else "ngưỡng tạo lũ",
                "data": [threshold] * label_count,
                "borderColor": "#fde68a",
                "borderDash": [],
                "borderWidth": 2,
                "actualValue": threshold,
            }
        )
    return datasets


def max_flow_point(hours, flow_map, selected_names):
    best = None
    for name in selected_names:
        for hour, value in zip(hours, flow_map.get(name, [])):
            if value is None:
                continue
            if best is None or value > best["value"]:
                best = {"name": name, "hour": hour, "value": value}
    return best


def format_flow_max_note(current_max, previous_max, current_year: int, previous_year: int | None = None):
    parts = []
    if current_max:
        parts.append(
            f"Max năm {current_year}: {format_num(current_max['value'])} m3/s "
            f"lúc {current_max['hour']:%H}h {current_max['hour']:%d/%m}"
        )
    if previous_max:
        year_label = previous_year if previous_year is not None else current_year - 1
        parts.append(
            f"Max năm {year_label}: {format_num(previous_max['value'])} m3/s "
            f"lúc {previous_max['hour']:%H}h {previous_max['hour']:%d/%m}"
        )
    return " và ".join(parts)


def parse_params(query: str):
    raw_params = parse_qs(query, keep_blank_values=True)
    params = {}
    for key, values in raw_params.items():
        if key == "reservoir":
            params[key] = values
        else:
            params[key] = values[-1] if values else ""
    return params


def params_to_query(params):
    query_items = []
    for key, value in params.items():
        if isinstance(value, list):
            for item in value:
                if item:
                    query_items.append((key, item))
        elif value:
            query_items.append((key, value))
    return urlencode(query_items)


def week_start_sunday(value: datetime) -> datetime:
    days_since_sunday = (value.weekday() + 1) % 7
    return value - timedelta(days=days_since_sunday)


def forecast_week_query(payload: dict) -> str:
    start_at = parse_input_datetime(payload.get("start_input"))
    end_at = parse_input_datetime(payload.get("end_input"))
    if start_at and end_at and payload.get("show_forecast") and (end_at.date() - start_at.date()).days >= 6:
        next_start = week_start_sunday(start_at) + timedelta(days=7)
    else:
        latest = None
        try:
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute(
                    """
                    SELECT MAX(Date)
                    FROM hydroai_features_daily
                    WHERE Q_in_m3s IS NOT NULL OR WaterLevel_m IS NOT NULL
                    """
                ).fetchone()
                latest = datetime.strptime(row[0], "%Y-%m-%d") if row and row[0] else None
        except Exception:
            latest = None
        next_start = week_start_sunday((latest or datetime.now()) + timedelta(days=1))
        if next_start <= (latest or datetime.now()):
            next_start += timedelta(days=7)
    next_end = next_start + timedelta(days=6, hours=23)
    params = {
        "start": format_input(next_start),
        "end": format_input(next_end),
        "compare_prev_year": "1" if payload.get("compare_prev_year") else "",
        "compare_flow_io": "1" if payload.get("compare_flow_io") else "",
        "compare_5_years": "",
        "show_forecast": "1",
        "granularity": "auto",
        "compare_year": str(payload.get("previous_year") or ""),
        "save_notice": f"Dự báo tuần {next_start:%d/%m} - {next_end:%d/%m/%Y}",
    }
    selected_names = payload.get("selected_names") or []
    if selected_names:
        params["reservoir"] = selected_names
    return params_to_query(params)


def run_ai_training_pipeline() -> str:
    python_exe = PYTHON_EXE if PYTHON_EXE.exists() else Path(sys.executable)
    commands = [
        [str(python_exe), "07_train_AI.py"],
        [str(python_exe), "08_forecast.py"],
    ]
    outputs = []
    for command in commands:
        result = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        outputs.append(result.stdout.strip())
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "Lỗi không rõ").strip())
    return " | ".join(line for output in outputs for line in output.splitlines() if line.startswith("Da ") or "MAE=" in line)[-500:]


def save_data_range(start_at: datetime, end_at: datetime):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    session = create_session()
    session.trust_env = False
    saved_points = 0
    saved_readings = 0
    errors = []

    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)
        for requested_at in iter_hours(start_at, end_at):
            try:
                readings = fetch_snapshot(session, DEFAULT_SOURCE_URL, requested_at, SAVE_TIMEOUT)
                save_snapshot(conn, requested_at, readings, DEFAULT_SOURCE_URL)
                saved_points += 1
                saved_readings += len(readings)
                if saved_points % SAVE_BATCH_SIZE == 0:
                    conn.commit()
            except Exception as exc:
                errors.append(f"{requested_at:%d/%m/%Y %H:%M}: {exc}")
        conn.commit()

    return {
        "saved_points": saved_points,
        "saved_readings": saved_readings,
        "errors": errors,
    }


def build_payload(params):
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Không tìm thấy database: {DB_PATH}")

    now_hour = floor_hour(datetime.now())
    with open_db() as conn:
        latest_db = get_latest_requested_at(conn)
        default_end = min(now_hour, latest_db) if latest_db else now_hour
        end_at = parse_input_datetime(params.get("end")) or default_end
        start_at = parse_input_datetime(params.get("start")) or (end_at - timedelta(hours=23))
        if start_at > end_at:
            raise ValueError("Từ ngày giờ phải nhỏ hơn hoặc bằng đến ngày giờ.")

        latest_data_at = get_latest_data_at_before(conn, end_at) or get_requested_at_before(conn, end_at) or latest_db
        data_end_at = min(end_at, latest_data_at) if latest_data_at else end_at

        db_window_hours = count_db_hours(conn, start_at, data_end_at)
        table_snapshot = get_latest_data_at_before(conn, data_end_at) or get_requested_at_before(conn, data_end_at) or latest_db
        table_rows = load_snapshot_rows(conn, table_snapshot) if table_snapshot else []

        available_names = [row.name for row in table_rows]
        selected_names = get_selected_reservoirs(params, available_names)
        compare_prev_year = parse_flag(params.get("compare_prev_year"))
        compare_flow_io = parse_flag(params.get("compare_flow_io"))
        compare_5_years = parse_flag(params.get("compare_5_years"))
        show_forecast = parse_flag(params.get("show_forecast"))
        granularity_mode = parse_granularity(params.get("granularity"))
        available_years = load_available_years(conn)
        try:
            compare_year = int(params.get("compare_year") or start_at.year - 1)
        except (TypeError, ValueError):
            compare_year = start_at.year - 1
        if compare_year == start_at.year:
            compare_year = start_at.year - 1
        if available_years and compare_year not in available_years:
            fallback_years = [year for year in available_years if year != start_at.year]
            compare_year = fallback_years[0] if fallback_years else start_at.year - 1
        chart_end_at = end_at if (compare_prev_year or compare_5_years or show_forecast) else data_end_at

        hours = list(iter_hours(start_at, chart_end_at))
        requested_daily = granularity_mode == "daily"
        weekly = granularity_mode == "weekly"
        monthly = granularity_mode == "monthly"
        yearly = granularity_mode == "yearly"
        daily = requested_daily or ((granularity_mode == "auto") and is_daily_range(start_at, chart_end_at))
        flow_note = ""
        if selected_names:
            hours, water_map, flow_map, db_hours = load_series(conn, start_at, chart_end_at, selected_names)
            raw_flow_hours = list(hours)
            raw_flow_map = {name: list(values) for name, values in flow_map.items()}
            current_flow_max = max_flow_point(raw_flow_hours, raw_flow_map, selected_names)
            for name in selected_names:
                water_map[name] = smooth_outliers(water_map[name], "water")
                flow_map[name] = smooth_outliers(flow_map[name], "flow")
            if yearly:
                labels, water_map = rollup_period(hours, water_map, "yearly", aggregate="last")
                _, flow_map = rollup_period(hours, flow_map, "yearly", aggregate="average")
            elif monthly:
                labels, water_map = rollup_period(hours, water_map, "monthly", aggregate="last")
                _, flow_map = rollup_period(hours, flow_map, "monthly", aggregate="average")
            elif weekly:
                labels, water_map = rollup_weekly(hours, water_map)
                _, flow_map = rollup_weekly(hours, flow_map)
            elif daily:
                labels, water_map = rollup_daily(hours, water_map, aggregate="last")
                _, flow_map = rollup_daily(hours, flow_map, aggregate="average")
            else:
                labels = build_hourly_labels(hours)
            water_datasets = build_datasets(water_map, selected_names)
            flow_datasets = build_datasets(flow_map, selected_names)
            generation_datasets = []
            if daily or weekly or monthly or yearly:
                generation_axis_dates = daily_axis_dates_from_payload(
                    {
                        "start_input": format_input(start_at),
                        "end_input": format_input(chart_end_at),
                    }
                )
                generation_map = load_generation_daily_series(conn, generation_axis_dates, GENERATION_RESERVOIRS)
                if yearly:
                    generation_hours = [datetime.combine(value, datetime.min.time()) for value in generation_axis_dates]
                    _, generation_map = rollup_period(generation_hours, generation_map, "yearly", aggregate="sum")
                elif monthly:
                    generation_hours = [datetime.combine(value, datetime.min.time()) for value in generation_axis_dates]
                    _, generation_map = rollup_period(generation_hours, generation_map, "monthly", aggregate="sum")
                elif weekly:
                    generation_hours = [datetime.combine(value, datetime.min.time()) for value in generation_axis_dates]
                    _, generation_map = rollup_period(generation_hours, generation_map, "weekly", aggregate="sum")
                generation_datasets = build_generation_datasets(generation_map, GENERATION_RESERVOIRS)
            five_year_water_datasets = []
            five_year_flow_datasets = []
            if compare_5_years:
                five_year_water_datasets, five_year_flow_datasets = build_five_year_compare_datasets(
                    conn,
                    start_at,
                    chart_end_at,
                    selected_names,
                    weekly,
                    daily,
                    labels,
                    water_map,
                    flow_map,
                )
            flow_compare_datasets = []
            if compare_flow_io:
                _, flow_compare_map, _ = load_flow_compare_series(conn, start_at, chart_end_at, selected_names)
                for name in flow_compare_map:
                    flow_compare_map[name] = smooth_outliers(flow_compare_map[name], "flow")
                if yearly:
                    _, flow_compare_map = rollup_period(hours, flow_compare_map, "yearly", aggregate="average")
                elif monthly:
                    _, flow_compare_map = rollup_period(hours, flow_compare_map, "monthly", aggregate="average")
                elif weekly:
                    _, flow_compare_map = rollup_weekly(hours, flow_compare_map)
                elif daily:
                    _, flow_compare_map = rollup_daily(hours, flow_compare_map, aggregate="average")
                flow_compare_datasets = build_flow_compare_datasets(flow_compare_map)
            reference_levels = load_reference_levels(conn, data_end_at, selected_names)
            reference_datasets = build_reference_datasets(reference_levels, selected_names, len(labels), water_map)
            energy_datasets = build_energy_datasets(water_map, selected_names, reference_levels)
            flood_threshold_datasets = build_flood_threshold_datasets(selected_names, len(labels))

            previous_water_datasets = []
            previous_flow_datasets = []
            previous_generation_datasets = []
            previous_flow_max = None
            if compare_prev_year:
                previous_start = replace_year(start_at, compare_year)
                previous_end = replace_year(chart_end_at, compare_year)
                previous_hours, previous_water, previous_flow, _ = load_series(
                    conn, previous_start, previous_end, selected_names
                )
                previous_flow_max = max_flow_point(previous_hours, previous_flow, selected_names)
                for name in selected_names:
                    previous_water[name] = smooth_outliers(previous_water[name], "water")
                    previous_flow[name] = smooth_outliers(previous_flow[name], "flow")
                if yearly:
                    _, previous_water = rollup_period(previous_hours, previous_water, "yearly", aggregate="last")
                    _, previous_flow = rollup_period(previous_hours, previous_flow, "yearly", aggregate="average")
                elif monthly:
                    _, previous_water = rollup_period(previous_hours, previous_water, "monthly", aggregate="last")
                    _, previous_flow = rollup_period(previous_hours, previous_flow, "monthly", aggregate="average")
                elif weekly:
                    _, previous_water = rollup_weekly(previous_hours, previous_water)
                    _, previous_flow = rollup_weekly(previous_hours, previous_flow)
                elif daily:
                    _, previous_water = rollup_daily(previous_hours, previous_water, aggregate="last")
                    _, previous_flow = rollup_daily(previous_hours, previous_flow, aggregate="average")
                for name in selected_names:
                    previous_water[name] = align_length(previous_water[name], len(labels))
                    previous_flow[name] = align_length(previous_flow[name], len(labels))
                previous_water_datasets = build_datasets(
                    previous_water,
                    selected_names,
                    suffix=str(previous_start.year),
                    dashed=True,
                    color_override="#2563eb",
                )
                previous_flow_datasets = build_datasets(
                    previous_flow,
                    selected_names,
                    suffix=str(previous_start.year),
                    dashed=True,
                    color_override="#2563eb",
                )
                if daily or weekly or monthly or yearly:
                    previous_generation_axis_dates = daily_axis_dates_from_payload(
                        {
                            "start_input": format_input(previous_start),
                            "end_input": format_input(previous_end),
                        }
                    )
                    previous_generation_map = load_generation_daily_series(
                        conn, previous_generation_axis_dates, GENERATION_RESERVOIRS
                    )
                    if yearly:
                        generation_hours = [
                            datetime.combine(value, datetime.min.time()) for value in previous_generation_axis_dates
                        ]
                        _, previous_generation_map = rollup_period(
                            generation_hours, previous_generation_map, "yearly", aggregate="sum"
                        )
                    elif monthly:
                        generation_hours = [
                            datetime.combine(value, datetime.min.time()) for value in previous_generation_axis_dates
                        ]
                        _, previous_generation_map = rollup_period(
                            generation_hours, previous_generation_map, "monthly", aggregate="sum"
                        )
                    elif weekly:
                        generation_hours = [
                            datetime.combine(value, datetime.min.time()) for value in previous_generation_axis_dates
                        ]
                        _, previous_generation_map = rollup_period(
                            generation_hours, previous_generation_map, "weekly", aggregate="sum"
                        )
                    for name in GENERATION_RESERVOIRS:
                        previous_generation_map[name] = align_length(previous_generation_map.get(name, []), len(labels))
                    previous_generation_datasets = build_generation_datasets(
                        previous_generation_map,
                        GENERATION_RESERVOIRS,
                        suffix=str(previous_start.year),
                        dashed=True,
                        color_override="#2563eb",
                    )
            flow_note = format_flow_max_note(
                current_flow_max,
                previous_flow_max,
                start_at.year,
                compare_year,
            )
        else:
            db_hours = set()
            if yearly:
                labels = []
                for hour in hours:
                    label = str(hour.year)
                    if not labels or labels[-1] != label:
                        labels.append(label)
            elif monthly:
                labels = []
                for hour in hours:
                    label = hour.strftime("%m/%Y")
                    if not labels or labels[-1] != label:
                        labels.append(label)
            elif weekly:
                labels = []
                for hour in hours:
                    label = week_label(hour)
                    if not labels or labels[-1] != label:
                        labels.append(label)
            elif daily:
                labels = []
                for hour in hours:
                    label = hour.strftime("%d/%m")
                    if not labels or labels[-1] != label:
                        labels.append(label)
            else:
                labels = build_hourly_labels(hours)
            water_datasets = []
            flow_datasets = []
            flow_compare_datasets = []
            five_year_water_datasets = []
            five_year_flow_datasets = []
            reference_datasets = []
            energy_datasets = []
            flood_threshold_datasets = []
            previous_water_datasets = []
            previous_flow_datasets = []
            generation_datasets = []
            previous_generation_datasets = []

    return {
        "window_label": f"{format_display(start_at)} - {format_display(end_at)}",
        "start_input": format_input(start_at),
        "end_input": format_input(end_at),
        "start_date": format_date_input(start_at),
        "end_date": format_date_input(end_at),
        "start_hour": display_hour(start_at),
        "end_hour": display_hour(end_at),
        "display_end": format_display(table_snapshot),
        "latest_db": format_display(latest_db),
        "table_cycle": format_display(table_snapshot),
        "table_rows": [
            {
                "name": row.name,
                "sync": f"Đồng bộ lúc: {row.sync_hint}" if row.sync_hint else "",
                "h_tl": format_num(row.h_tl),
                "q_ve": format_num(row.q_ve),
                "q_xt": format_num(row.q_xt),
                "q_xm": format_num(row.q_xm),
                "checked": row.name in selected_names,
            }
            for row in table_rows
        ],
        "labels": labels,
        "water_datasets": water_datasets,
        "flow_datasets": flow_datasets,
        "flow_compare_datasets": flow_compare_datasets,
        "five_year_water_datasets": five_year_water_datasets,
        "five_year_flow_datasets": five_year_flow_datasets,
        "reference_datasets": reference_datasets,
        "energy_datasets": energy_datasets,
        "flood_threshold_datasets": flood_threshold_datasets,
        "previous_water_datasets": previous_water_datasets,
        "previous_flow_datasets": previous_flow_datasets,
        "generation_datasets": generation_datasets,
        "previous_generation_datasets": previous_generation_datasets,
        "generation_names": GENERATION_RESERVOIRS,
        "selected_names": selected_names,
        "compare_prev_year": compare_prev_year,
        "compare_flow_io": compare_flow_io,
        "compare_5_years": compare_5_years,
        "show_forecast": show_forecast,
        "previous_year": compare_year,
        "available_years": available_years,
        "granularity_mode": granularity_mode,
        "chart_granularity": (
            "yearly" if yearly else ("monthly" if monthly else ("weekly" if weekly else ("daily" if daily else "hourly")))
        ),
        "source_note": f"DB: {max(len(db_hours), db_window_hours)} giờ | Live EVN: 0 giờ",
        "save_notice": params.get("save_notice", ""),
        "flow_note": flow_note,
        "weather_summary": (weather_summary := load_dashboard_weather_summary(datetime.now())),
        "weather_date": weather_summary.get("_source_date", datetime.now().strftime("%Y-%m-%d")),
    }


def checkbox(value: bool):
    return " checked" if value else ""


def selected_hidden_inputs(selected_names):
    return "".join(
        f'<input type="hidden" name="reservoir" value="{html.escape(name, quote=True)}">'
        for name in selected_names
    )


def granularity_query(payload: dict, granularity: str) -> str:
    params = {
        "start": payload["start_input"],
        "end": payload["end_input"],
        "compare_prev_year": "1" if payload["compare_prev_year"] else "",
        "compare_flow_io": "1" if payload["compare_flow_io"] else "",
        "compare_5_years": "1" if payload["compare_5_years"] else "",
        "show_forecast": "1" if payload["show_forecast"] else "",
        "granularity": granularity,
        "compare_year": str(payload.get("previous_year") or ""),
    }
    selected_names = payload.get("selected_names") or []
    if selected_names:
        params["reservoir"] = selected_names
    return params_to_query(params)


def render_chart_modes(payload: dict) -> str:
    modes = [("daily", "Ngày"), ("weekly", "Tuần"), ("monthly", "Tháng"), ("yearly", "Năm")]
    current = payload.get("granularity_mode") or "auto"
    if current == "auto":
        current = payload.get("chart_granularity") or "daily"
    links = []
    for mode, label in modes:
        active = " active" if current == mode else ""
        links.append(f'<a class="chart-mode{active}" href="/?{granularity_query(payload, mode)}">{label}</a>')
    return f'<div class="chart-modes">{"".join(links)}</div>'


def render_weather_strip(summary: dict, weather_date: str) -> str:
    region_order = [("mb", "Hà Nội"), ("mt", "Đà Nẵng"), ("mn", "TP Hồ Chí Minh")]
    cards = []
    for key, fallback_city in region_order:
        item = summary.get(key, {}) if isinstance(summary, dict) else {}
        city = item.get("city") or fallback_city
        temp_c = item.get("temp_c")
        value = f"{float(temp_c):.1f}°C".replace(".", ",") if temp_c is not None else (item.get("value") or "--")
        humidity = item.get("humidity") or "--"
        condition = item.get("condition") or "--"
        if len(str(condition)) > 26:
            condition = f"{str(condition)[:23]}..."
        cards.append(
            '<div class="weather-card">'
            f'<span class="weather-region">{html.escape(str(city))}</span>'
            f'<strong>{html.escape(str(value))}</strong>'
            f'<span>{html.escape(str(humidity))} | {html.escape(str(condition))}</span>'
            '</div>'
        )
    return (
        '<div class="weather-strip" title="Thời tiết hiện tại Bắc/Trung/Nam">'
        f'<div class="weather-title">Thời tiết {html.escape(weather_date or "")}</div>'
        f'{"".join(cards)}'
        '</div>'
    )


def axis_dates_for_daily_payload(payload, labels):
    if payload.get("chart_granularity") != "daily":
        return []
    start_at = parse_input_datetime(payload.get("start_input"))
    end_at = parse_input_datetime(payload.get("end_input"))
    if not start_at or not end_at:
        return []
    dates = []
    current = start_at.date()
    end_date = end_at.date()
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=1)
    return dates if len(dates) == len(labels) else []


def daily_axis_dates_from_payload(payload):
    start_at = parse_input_datetime(payload.get("start_input"))
    end_at = parse_input_datetime(payload.get("end_input"))
    if not start_at or not end_at:
        return []
    dates = []
    current = start_at.date()
    end_date = end_at.date()
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def last_actual_point(datasets, reservoir):
    for dataset in datasets:
        if dataset.get("label") != reservoir:
            continue
        data = list(dataset.get("data", []))
        for index in range(len(data) - 1, -1, -1):
            if data[index] is not None:
                return index, data[index], dataset.get("borderColor", "#16a34a")
    return None, None, "#16a34a"


def month_end_date(year, month):
    return datetime(year, month, calendar.monthrange(year, month)[1]).date()


def smoothstep(ratio):
    ratio = max(0.0, min(1.0, ratio))
    return ratio * ratio * (3 - 2 * ratio)


def shaped_ratio(shape_values, start_index, end_index, index):
    if not shape_values:
        return None
    if start_index >= len(shape_values) or end_index >= len(shape_values) or index >= len(shape_values):
        return None
    start_value = shape_values[start_index]
    end_value = shape_values[end_index]
    current_value = shape_values[index]
    if start_value is None or end_value is None or current_value is None:
        return None
    delta = float(end_value) - float(start_value)
    if abs(delta) < 1e-9:
        return None
    return max(0.0, min(1.0, (float(current_value) - float(start_value)) / delta))


def smooth_forecast_values(values, anchor_indices, passes=2):
    result = list(values)
    anchor_set = set(anchor_indices)
    for _ in range(passes):
        next_values = list(result)
        for index in range(1, len(result) - 1):
            if index in anchor_set or result[index] is None:
                continue
            previous_value = result[index - 1]
            next_value = result[index + 1]
            if previous_value is None or next_value is None:
                continue
            next_values[index] = 0.25 * previous_value + 0.50 * result[index] + 0.25 * next_value
        result = next_values
    return result


def interpolate_forecast_on_axis(
    axis_dates,
    last_index,
    last_value,
    monthly_values,
    shape_values=None,
    shape_weight=0.60,
    smooth_passes=0,
):
    values = [None] * len(axis_dates)
    if last_index is None or last_value is None:
        return values

    anchors = [(last_index, float(last_value))]
    for (year, month), value in sorted(monthly_values.items()):
        if value is None:
            continue
        target_date = month_end_date(year, month)
        if target_date not in axis_dates:
            continue
        target_index = axis_dates.index(target_date)
        if target_index > last_index:
            anchors.append((target_index, float(value)))

    if len(anchors) <= 1:
        return values

    anchor_indices = [index for index, _ in anchors]
    values[last_index] = float(last_value)
    for (start_index, start_value), (end_index, end_value) in zip(anchors, anchors[1:]):
        span = end_index - start_index
        if span <= 0:
            continue
        for index in range(start_index + 1, end_index + 1):
            linear_ratio = (index - start_index) / span
            ratio = smoothstep(linear_ratio)
            local_shape_ratio = shaped_ratio(shape_values, start_index, end_index, index)
            if local_shape_ratio is not None:
                ratio = (1 - shape_weight) * ratio + shape_weight * local_shape_ratio
            values[index] = start_value + (end_value - start_value) * ratio
    if smooth_passes:
        values = smooth_forecast_values(values, anchor_indices, passes=smooth_passes)
    return values


def forecast_shape_values(base_datasets, analog_year):
    preferred_labels = [str(analog_year), "2025"]
    for label in preferred_labels:
        for dataset in base_datasets:
            if dataset.get("label") == label:
                return list(dataset.get("data", []))
    for dataset in base_datasets:
        label = str(dataset.get("label", ""))
        if label.isdigit() and len(label) == 4:
            return list(dataset.get("data", []))
    return None


def load_hydroai_daily_forecast_series(selected_names, labels, payload, flow_base_datasets):
    granularity = payload.get("chart_granularity")
    if granularity == "hourly":
        return [], []
    axis_dates = daily_axis_dates_from_payload(payload)
    if not axis_dates or not DB_PATH.exists():
        return [], []

    reservoirs = [name for name in selected_names if name in HYDROAI_RESERVOIR_TO_BASIN]
    if not reservoirs:
        return [], []

    start_date = axis_dates[0].strftime("%Y-%m-%d")
    end_date = axis_dates[-1].strftime("%Y-%m-%d")
    water_datasets = []
    flow_datasets = []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hydroai_forecast_daily'"
        ).fetchone()
        if not table_exists:
            return [], []

        for reservoir in reservoirs:
            basin_id = HYDROAI_RESERVOIR_TO_BASIN[reservoir]
            rows = conn.execute(
                """
                SELECT Date, Q_P10_m3s, Q_P50_m3s, Q_P90_m3s,
                       WaterLevel_P10_m, WaterLevel_P50_m, WaterLevel_P90_m
                FROM hydroai_forecast_daily
                WHERE Basin_ID = ?
                  AND Date BETWEEN ? AND ?
                ORDER BY Date
                """,
                (basin_id, start_date, end_date),
            ).fetchall()
            if not rows:
                continue

            flow_values = [None] * len(axis_dates)
            water_values = [None] * len(axis_dates)
            date_index = {value.strftime("%Y-%m-%d"): index for index, value in enumerate(axis_dates)}
            for row in rows:
                index = date_index.get(row["Date"])
                if index is None:
                    continue
                flow_values[index] = row["Q_P50_m3s"]
                water_values[index] = row["WaterLevel_P50_m"]
            if granularity in {"weekly", "monthly", "yearly"}:
                forecast_hours = [datetime.combine(value, datetime.min.time()) for value in axis_dates]
                _, water_rolled = rollup_period(
                    forecast_hours,
                    {reservoir: water_values},
                    granularity,
                    aggregate="last",
                )
                _, flow_rolled = rollup_period(
                    forecast_hours,
                    {reservoir: flow_values},
                    granularity,
                    aggregate="average",
                )
                water_values = align_length(water_rolled.get(reservoir, []), len(labels))
                flow_values = align_length(flow_rolled.get(reservoir, []), len(labels))
            else:
                water_values = align_length(water_values, len(labels))
                flow_values = align_length(flow_values, len(labels))

            if any(value is not None for value in water_values):
                water_datasets.append(
                    {
                        "label": f"Dự báo AI MN {reservoir}",
                        "data": water_values,
                        "borderColor": "#dc2626",
                        "borderDash": [8, 5],
                        "borderWidth": 3,
                        "pointRadius": 0,
                        "pointHoverRadius": 5,
                        "tension": 0.24,
                    }
                )

            if any(value is not None for value in flow_values):
                flow_datasets.append(
                    {
                        "label": f"Dự báo AI Q về {reservoir}",
                        "data": flow_values,
                        "borderColor": "#dc2626",
                        "borderDash": [8, 5],
                        "borderWidth": 3,
                        "pointRadius": 0,
                        "pointHoverRadius": 5,
                        "tension": 0.24,
                    }
                )
    finally:
        conn.close()

    return water_datasets, flow_datasets


def load_generation_forecast_series(selected_names, labels, payload):
    granularity = payload.get("chart_granularity")
    if granularity == "hourly":
        return []
    axis_dates = daily_axis_dates_from_payload(payload)
    if not axis_dates or not DB_PATH.exists():
        return []

    reservoirs = [name for name in selected_names if name in HYDROAI_RESERVOIR_TO_BASIN]
    if not reservoirs:
        return []

    start_date = axis_dates[0].strftime("%Y-%m-%d")
    end_date = axis_dates[-1].strftime("%Y-%m-%d")
    forecast_map = {reservoir: [None] * len(axis_dates) for reservoir in reservoirs}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hydroai_generation_forecast_daily'"
        ).fetchone()
        if not table_exists:
            return []

        date_index = {value.strftime("%Y-%m-%d"): index for index, value in enumerate(axis_dates)}
        for reservoir in reservoirs:
            basin_id = HYDROAI_RESERVOIR_TO_BASIN[reservoir]
            rows = conn.execute(
                """
                SELECT Date, Generation_Forecast_MWh, Value_Type
                FROM hydroai_generation_forecast_daily
                WHERE Basin_ID = ?
                  AND Date BETWEEN ? AND ?
                ORDER BY Date
                """,
                (basin_id, start_date, end_date),
            ).fetchall()
            if not rows:
                continue
            values = [None] * len(axis_dates)
            for row in rows:
                if row["Value_Type"] != "Forecast":
                    continue
                index = date_index.get(row["Date"])
                if index is not None:
                    values[index] = row["Generation_Forecast_MWh"]
            if granularity in {"weekly", "monthly", "yearly"}:
                generation_hours = [datetime.combine(value, datetime.min.time()) for value in axis_dates]
                _, rolled = rollup_period(generation_hours, {reservoir: values}, granularity, aggregate="sum")
                values = align_length(rolled.get(reservoir, []), len(labels))
            forecast_map[reservoir] = values
    finally:
        conn.close()
    return build_generation_datasets(
        forecast_map,
        reservoirs,
        dashed=True,
        color_override="#dc2626",
        label="Dự báo AI tổng sản lượng CTTĐ Buôn Kuốp",
    )


def load_forecast_series(selected_names, labels, payload, water_base_datasets, flow_base_datasets):
    if not FORECAST_DB_PATH.exists():
        return load_hydroai_daily_forecast_series(selected_names, labels, payload, flow_base_datasets)
    if payload.get("chart_granularity") != "daily":
        return load_hydroai_daily_forecast_series(selected_names, labels, payload, flow_base_datasets)

    forecast_reservoirs = [name for name in selected_names if name in {"Buôn Tua Srah", "Buôn Kuốp", "Srêpốk 3"}]
    if not forecast_reservoirs:
        return [], []

    axis_dates = axis_dates_for_daily_payload(payload, labels)
    if not axis_dates:
        return [], []

    conn = sqlite3.connect(FORECAST_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in forecast_reservoirs)
        try:
            rows = conn.execute(
                f"""
                SELECT reservoir, forecast_year, month, q_inflow_forecast, water_level_forecast_m,
                       analog_year, model_name
                FROM hydro_forecast_monthly
                WHERE reservoir IN ({placeholders})
                  AND is_future = 1
                  AND model_name = 'ensemble_analog_water_balance'
                ORDER BY reservoir, forecast_year, month
                """,
                forecast_reservoirs,
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    finally:
        conn.close()

    if not rows:
        return load_hydroai_daily_forecast_series(selected_names, labels, payload, flow_base_datasets)

    water_datasets = []
    flow_datasets = []
    for reservoir in forecast_reservoirs:
        by_month = {
            (row["forecast_year"], row["month"]): row
            for row in rows
            if row["reservoir"] == reservoir
        }
        first_row = next(iter(by_month.values()), None)
        suffix = f" (năm đồng dạng {first_row['analog_year']})" if first_row else ""
        water_monthly = {
            key: row["water_level_forecast_m"]
            for key, row in by_month.items()
        }
        flow_monthly = {
            key: row["q_inflow_forecast"]
            for key, row in by_month.items()
        }
        water_index, water_value, water_color = last_actual_point(water_base_datasets, reservoir)
        flow_index, flow_value, flow_color = last_actual_point(flow_base_datasets, reservoir)
        water_shape = forecast_shape_values(water_base_datasets, first_row["analog_year"] if first_row else None)
        flow_shape = forecast_shape_values(flow_base_datasets, first_row["analog_year"] if first_row else None)
        water_datasets.append(
            {
                "label": f"Dự báo MN {reservoir}{suffix}",
                "data": interpolate_forecast_on_axis(
                    axis_dates,
                    water_index,
                    water_value,
                    water_monthly,
                    shape_values=water_shape,
                    shape_weight=0.35,
                ),
                "borderColor": water_color,
                "borderDash": [7, 5],
                "borderWidth": 2,
                "tension": 0.36,
            }
        )
        flow_datasets.append(
            {
                "label": f"Dự báo Q về {reservoir}{suffix}",
                "data": interpolate_forecast_on_axis(
                    axis_dates,
                    flow_index,
                    flow_value,
                    flow_monthly,
                    shape_values=flow_shape,
                    shape_weight=0.18,
                    smooth_passes=4,
                ),
                "borderColor": flow_color,
                "borderDash": [7, 5],
                "borderWidth": 2,
                "tension": 0.46,
            }
        )
    return water_datasets, flow_datasets


def hour_options(selected_hour: int):
    return "".join(
        f'<option value="{hour}"{" selected" if hour == selected_hour else ""}>{hour}:00</option>'
        for hour in range(0, 24)
    )


def render_page(payload):
    labels = list(payload["labels"])
    chart_modes = render_chart_modes(payload)
    if payload["compare_5_years"]:
        water_chart_datasets = payload["five_year_water_datasets"]
        flow_chart_datasets = payload["five_year_flow_datasets"]
        generation_chart_datasets = []
    else:
        water_chart_datasets = (
            payload["water_datasets"]
            + payload["reference_datasets"]
            + payload["previous_water_datasets"]
            + payload["energy_datasets"]
        )
        flow_chart_datasets = (
            payload["flow_compare_datasets"]
            if payload["compare_flow_io"]
            else payload["flow_datasets"] + payload["previous_flow_datasets"] + payload["flood_threshold_datasets"]
        )
        generation_chart_datasets = list(payload["generation_datasets"]) + payload["previous_generation_datasets"]
        if payload["show_forecast"]:
            forecast_water_datasets, forecast_flow_datasets = load_forecast_series(
                payload["selected_names"],
                labels,
                payload,
                water_chart_datasets,
                flow_chart_datasets,
            )
            if forecast_water_datasets or forecast_flow_datasets:
                water_chart_datasets += forecast_water_datasets
                flow_chart_datasets += forecast_flow_datasets
                refresh_energy_dataset_from_water_datasets(water_chart_datasets)
            generation_chart_datasets += load_generation_forecast_series(
                payload["generation_names"],
                labels,
                payload,
            )
    chart_payload = {
        "labels": labels,
        "water": water_chart_datasets,
        "flow": flow_chart_datasets,
        "generation": generation_chart_datasets,
        "granularity": payload["chart_granularity"],
        "flowNote": payload["flow_note"],
    }
    payload_json = json.dumps(chart_payload, ensure_ascii=False)
    rows_html = []
    for row in payload["table_rows"]:
        rows_html.append(
            "<tr>"
            f'<td class="select-col"><input type="checkbox" name="reservoir" value="{html.escape(row["name"], quote=True)}"{checkbox(row["checked"])}></td>'
            f'<td class="name-cell sticky-name"><strong>{html.escape(row["name"])}</strong><small>{html.escape(row["sync"])}</small></td>'
            f'<td class="num">{html.escape(row["h_tl"])}</td>'
            f'<td class="num">{html.escape(row["q_ve"])}</td>'
            f'<td class="num">{html.escape(row["q_xt"])}</td>'
            f'<td class="num">{html.escape(row["q_xm"])}</td>'
            "</tr>"
        )
    if not rows_html:
        rows_html.append('<tr><td colspan="6" class="empty">Không có dữ liệu hồ tại mốc đang chọn.</td></tr>')

    clear_query = urlencode(
        {
            "start": payload["start_input"],
            "end": payload["end_input"],
            "compare_prev_year": "1" if payload["compare_prev_year"] else "",
            "compare_flow_io": "1" if payload["compare_flow_io"] else "",
            "compare_5_years": "1" if payload["compare_5_years"] else "",
            "show_forecast": "1" if payload["show_forecast"] else "",
            "granularity": payload["granularity_mode"],
            "compare_year": str(payload.get("previous_year") or ""),
        }
    )

    top_selected_inputs = selected_hidden_inputs(payload["selected_names"])
    compare_checked = checkbox(payload["compare_prev_year"])
    compare_hidden = "1" if payload["compare_prev_year"] else ""
    compare_year_options = "".join(
        f'<option value="{year}"{" selected" if year == payload["previous_year"] else ""}>{year}</option>'
        for year in payload.get("available_years", [])
        if year != (parse_input_datetime(payload.get("start_input")) or datetime.now()).year
    )
    if not compare_year_options:
        compare_year_options = f'<option value="{payload["previous_year"]}" selected>{payload["previous_year"]}</option>'
    compare_flow_checked = checkbox(payload["compare_flow_io"])
    compare_flow_hidden = "1" if payload["compare_flow_io"] else ""
    compare_5_years_checked = checkbox(payload["compare_5_years"])
    compare_5_years_hidden = "1" if payload["compare_5_years"] else ""
    show_forecast_checked = checkbox(payload["show_forecast"])
    show_forecast_hidden = "1" if payload["show_forecast"] else ""
    weekly_checked = checkbox(payload["granularity_mode"] == "weekly")
    start_hour_options = hour_options(payload["start_hour"])
    end_hour_options = hour_options(payload["end_hour"])
    weather_strip_html = render_weather_strip(payload.get("weather_summary", {}), payload.get("weather_date", ""))
    next_forecast_query = forecast_week_query(payload)
    save_notice_html = (
        f'<div class="save-notice">{html.escape(payload["save_notice"])}</div>'
        if payload.get("save_notice")
        else ""
    )

    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dashboard Chọn Hồ</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg: #f3f7fc;
      --panel: #ffffff;
      --panel-soft: #f8fbff;
      --line: #d5e3f5;
      --line-soft: #edf3fb;
      --text: #082747;
      --muted: #58708a;
      --blue: #1665d8;
      --blue-soft: #eaf3ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, Helvetica, sans-serif;
      font-size: 14px;
    }}
    .page {{ padding: 8px 10px 12px; }}
    .topbar {{
      min-height: 150px;
      padding: 16px 12px 14px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: linear-gradient(135deg, #ffffff 0%, #eef6ff 100%);
      box-shadow: 0 12px 28px rgba(19, 59, 103, 0.06);
    }}
    .topbar-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 8px;
    }}
    .brand-row {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .analysis-link {{
      display: inline-flex;
      align-items: center;
      height: 34px;
      margin-top: 7px;
      padding: 0 11px;
      border: 1px solid #c6dbff;
      border-radius: 7px;
      background: #eaf3ff;
      color: #0b55b7;
      font-size: 11px;
      font-weight: 800;
      text-decoration: none;
      white-space: nowrap;
    }}
    h1 {{
      flex: 0 0 auto;
      margin: 8px 0 0;
      font-size: 23px;
      line-height: 1.2;
    }}
    .filters {{
      display: flex;
      align-items: flex-end;
      gap: 10px;
      overflow-x: auto;
      padding: 2px 0;
    }}
    .control-row {{
      align-items: center;
    }}
    .weather-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 8px 0 2px;
      overflow-x: auto;
      padding-bottom: 2px;
    }}
    .info, .field, .source {{
      flex: 0 0 auto;
      min-width: 178px;
      min-height: 52px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: rgba(255, 255, 255, 0.86);
    }}
    .info.wide {{ min-width: 236px; }}
    .source {{ min-width: 208px; }}
    .label {{
      display: block;
      margin-bottom: 5px;
      color: var(--muted);
      font-size: 10px;
      text-transform: uppercase;
    }}
    .value {{ font-weight: 700; white-space: nowrap; }}
    .time-pair {{
      display: grid;
      grid-template-columns: 126px 80px;
      gap: 6px;
      align-items: center;
    }}
    input[type="date"], .hour-select {{
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 8px;
      color: var(--text);
      background: #fff;
      font: inherit;
      font-weight: 700;
    }}
    .hour-select {{
      cursor: pointer;
    }}
    .field {{ padding: 8px 10px; }}
    .field .label {{ margin-bottom: 4px; }}
    .compare {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      height: 44px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: #fff;
      color: #244a70;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .compare input {{ margin: 0; }}
    .compare-select {{
      height: 28px;
      min-width: 72px;
      border: 1px solid #c7dcf5;
      border-radius: 6px;
      background: #fff;
      color: #0f2742;
      font: inherit;
      font-size: 11px;
      font-weight: 800;
    }}
    .weather-strip {{
      flex: 0 1 620px;
      max-width: 620px;
      display: grid;
      grid-template-columns: 120px repeat(3, minmax(135px, 1fr));
      align-items: stretch;
      gap: 6px;
      min-height: 58px;
      padding: 7px 10px;
      border: 1px solid #c7dcf5;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.9);
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.65);
    }}
    .weather-title {{
      display: flex;
      align-items: center;
      max-width: 120px;
      color: #315a82;
      font-size: 10px;
      font-weight: 800;
      line-height: 1.15;
      text-transform: uppercase;
    }}
    .weather-card {{
      display: grid;
      grid-template-columns: auto;
      align-content: center;
      min-width: 135px;
      padding: 3px 8px;
      border-left: 1px solid #e0ebf8;
      color: #244a70;
      line-height: 1.1;
    }}
    .weather-card strong {{
      color: #0b55b7;
      font-size: 15px;
      line-height: 1.1;
    }}
    .weather-card span {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 9px;
      font-weight: 700;
    }}
    .weather-region {{
      color: #082747;
      font-size: 9px !important;
      font-weight: 800 !important;
    }}
    .btn {{
      height: 36px;
      border: 1px solid transparent;
      border-radius: 8px;
      padding: 0 13px;
      background: var(--blue);
      color: #fff;
      cursor: pointer;
      font-weight: 700;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      white-space: nowrap;
    }}
    .btn.secondary {{
      background: var(--blue-soft);
      border-color: #c6dbff;
      color: #0b55b7;
    }}
    .hint {{
      margin-top: 8px;
      color: #3f5b76;
      font-size: 11px;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(540px, 46%) 1fr;
      gap: 12px;
      margin-top: 12px;
      align-items: start;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: 13px;
      background: linear-gradient(180deg, #ffffff 0%, #f9fcff 100%);
      box-shadow: 0 14px 30px rgba(22, 53, 86, 0.08);
    }}
    .table-panel {{ padding: 12px 18px 12px 6px; }}
    .chart-panel {{ padding: 12px 12px 8px; }}
    .panel-title {{
      margin: 0 0 8px;
      color: #082747;
      font-size: 16px;
      line-height: 1.25;
    }}
    .chart-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 6px;
    }}
    .chart-head .panel-title {{ margin: 0; }}
    .chart-modes {{
      display: inline-flex;
      gap: 4px;
      padding: 3px;
      border: 1px solid #c7dcf5;
      border-radius: 8px;
      background: #f8fbff;
      flex: 0 0 auto;
    }}
    .chart-mode {{
      min-width: 42px;
      height: 26px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      color: #244a70;
      text-decoration: none;
      font-size: 10px;
      font-weight: 800;
      white-space: nowrap;
    }}
    .chart-mode.active {{
      background: #1769d8;
      color: #fff;
    }}
    .toolbar {{
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
    }}
    .table-wrap {{
      height: calc(100vh - 286px);
      min-height: 430px;
      overflow: auto;
      border-top: 1px solid var(--line);
    }}
    table {{
      width: 100%;
      min-width: 835px;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 12px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      background: #fff;
      padding: 7px 12px;
      vertical-align: middle;
    }}
    th {{
      position: sticky;
      top: 0;
      z-index: 3;
      background: #f8fbff;
      color: #082747;
      font-size: 9px;
      font-weight: 700;
      text-align: left;
    }}
    .select-col {{
      position: sticky;
      left: 0;
      z-index: 4;
      width: 58px;
      background: #fff;
      text-align: center;
    }}
    th.select-col {{ background: #f8fbff; }}
    .sticky-name {{
      position: sticky;
      left: 58px;
      z-index: 3;
      width: 240px;
      background: #fff;
      box-shadow: 8px 0 13px rgba(19, 59, 103, 0.04);
    }}
    th.sticky-name {{ z-index: 4; background: #f8fbff; }}
    .name-cell strong {{
      display: block;
      color: #10375f;
      font-size: 13px;
    }}
    .name-cell small {{
      display: block;
      margin-top: 3px;
      color: #0d56b3;
      font-size: 9px;
      font-weight: 700;
    }}
    .num {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .metric-col {{ text-align: right; white-space: nowrap; }}
    .empty {{
      padding: 24px;
      text-align: center;
      color: var(--muted);
    }}
    .charts {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }}
    .chart-panel canvas {{
      width: 100%;
      height: calc((100vh - 295px) / 3) !important;
      min-height: 210px;
      max-height: 280px;
    }}
    .custom-legend {{
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 12px;
      min-height: 20px;
      margin-top: 4px;
      color: #244a70;
      font-size: 10px;
      font-weight: 700;
    }}
    .legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      white-space: nowrap;
    }}
    .legend-line {{
      width: 34px;
      height: 0;
      border-top: 3px solid currentColor;
      border-radius: 999px;
    }}
    .legend-line.dashed {{
      border-top-style: dashed;
    }}
    .chart-note {{
      min-height: 16px;
      margin: -3px 0 2px;
      color: var(--muted);
      font-size: 11px;
    }}
    .save-notice {{
      margin-top: 8px;
      padding: 8px 10px;
      border: 1px solid #b8dec2;
      border-radius: 8px;
      background: #eefaf1;
      color: #1b6b31;
      font-size: 12px;
      font-weight: 700;
    }}
    @media (max-width: 1180px) {{
      .layout {{ grid-template-columns: 1fr; }}
      .table-wrap {{ height: 520px; }}
      .chart-panel canvas {{ height: 300px !important; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="topbar">
      <div class="topbar-head">
        <div class="brand-row"><h1>Dashboard Chọn Hồ</h1><a class="analysis-link" href="/analysis">Phân tích El Niño 2027</a></div>
        {weather_strip_html}
      </div>
      <form method="GET">
        <div class="filters control-row">
          <label class="field"><span class="label">Từ ngày giờ</span><span class="time-pair"><input type="date" data-date-for="start" value="{html.escape(payload["start_date"], quote=True)}"><select class="hour-select" data-hour-for="start">{start_hour_options}</select><input type="hidden" name="start" value="{html.escape(payload["start_input"], quote=True)}"></span></label>
          <label class="field"><span class="label">Đến ngày giờ</span><span class="time-pair"><input type="date" data-date-for="end" value="{html.escape(payload["end_date"], quote=True)}"><select class="hour-select" data-hour-for="end">{end_hour_options}</select><input type="hidden" name="end" value="{html.escape(payload["end_input"], quote=True)}"></span></label>
          {top_selected_inputs}
          <label class="compare"><input type="checkbox" name="compare_prev_year" value="1"{compare_checked}>SS 2 năm <select class="compare-select" name="compare_year" title="Năm so sánh MNH và lưu lượng về">{compare_year_options}</select></label>
          <label class="compare"><input type="checkbox" name="compare_flow_io" value="1"{compare_flow_checked}>SS lưu lượng về/xả</label>
          <label class="compare"><input type="checkbox" name="compare_5_years" value="1"{compare_5_years_checked}>SS mực nước/lưu lượng 5 năm</label>
          <label class="compare"><input type="checkbox" name="show_forecast" value="1"{show_forecast_checked}>Dự báo</label>
          <button class="btn secondary" type="submit" formaction="/train-ai" formmethod="POST">Training AI</button>
          <a class="btn secondary" href="/?{next_forecast_query}">Dự báo 7 ngày tới</a>
          <button class="btn" type="submit">Xem dữ liệu</button>
          <button class="btn secondary" type="submit" formaction="/save-data" formmethod="POST">Lưu dữ liệu</button>
        </div>
      </form>
      {save_notice_html}
      <div class="hint">Bảng bên trái lấy theo chu kỳ dữ liệu hiện tại. Mỗi biểu đồ có nút Ngày/Tuần/Tháng/Năm để gom trục thời gian.</div>
    </section>

    <div class="layout">
      <section class="panel table-panel">
        <h2 class="panel-title">Danh sách {len(payload["table_rows"])} hồ theo chu kỳ {html.escape(payload["table_cycle"])}</h2>
        <form method="GET">
          <input type="hidden" name="start" value="{html.escape(payload["start_input"], quote=True)}">
          <input type="hidden" name="end" value="{html.escape(payload["end_input"], quote=True)}">
          <input type="hidden" name="compare_prev_year" value="{compare_hidden}">
          <input type="hidden" name="compare_year" value="{html.escape(str(payload["previous_year"]), quote=True)}">
          <input type="hidden" name="compare_flow_io" value="{compare_flow_hidden}">
          <input type="hidden" name="compare_5_years" value="{compare_5_years_hidden}">
          <input type="hidden" name="show_forecast" value="{show_forecast_hidden}">
          <input type="hidden" name="granularity" value="{html.escape(payload["granularity_mode"], quote=True)}">
          <div class="toolbar">
            <button class="btn" type="submit">Xem dữ liệu</button>
            <a class="btn secondary" href="/?{clear_query}">Bỏ chọn</a>
          </div>
          <div class="table-wrap">
            <table>
              <colgroup>
                <col style="width:58px">
                <col style="width:240px">
                <col style="width:175px">
                <col style="width:130px">
                <col style="width:110px">
                <col style="width:145px">
              </colgroup>
              <thead>
                <tr>
                  <th class="select-col">Chọn</th>
                  <th class="sticky-name">Tên hồ</th>
                  <th class="metric-col">Mực nước thượng lưu</th>
                  <th class="metric-col">Lưu lượng về</th>
                  <th class="metric-col">Xả tràn</th>
                  <th class="metric-col">Xả qua nhà máy</th>
                </tr>
              </thead>
              <tbody>{''.join(rows_html)}</tbody>
            </table>
          </div>
        </form>
      </section>

      <section class="charts">
        <section class="panel chart-panel">
          <div class="chart-head"><h2 class="panel-title">Mực nước thượng lưu</h2>{chart_modes}</div>
          <div class="chart-note" id="water-note"></div>
          <canvas id="waterChart"></canvas>
        </section>
        <section class="panel chart-panel">
          <div class="chart-head"><h2 class="panel-title">Lưu lượng về</h2>{chart_modes}</div>
          <div class="chart-note" id="flow-note"></div>
          <canvas id="flowChart"></canvas>
        </section>
        <section class="panel chart-panel">
          <div class="chart-head"><h2 class="panel-title">Sản lượng phát điện</h2>{chart_modes}</div>
          <div class="chart-note" id="generation-note"></div>
          <canvas id="generationChart"></canvas>
        </section>
      </section>
    </div>
  </main>

  <script>
    const payload = {payload_json};

    function wireHourSelectors() {{
      document.querySelectorAll('form').forEach((form) => {{
        form.addEventListener('submit', () => {{
          ['start', 'end'].forEach((name) => {{
            const dateInput = form.querySelector(`[data-date-for="${{name}}"]`);
            const hourSelect = form.querySelector(`[data-hour-for="${{name}}"]`);
            const hiddenInput = form.querySelector(`input[type="hidden"][name="${{name}}"]`);
            if (!dateInput || !hourSelect || !hiddenInput || !dateInput.value) return;
            const internalHour = Math.max(0, Math.min(23, Number(hourSelect.value || 0)));
            hiddenInput.value = `${{dateInput.value}}T${{String(internalHour).padStart(2, '0')}}:00`;
          }});
        }});
      }});
    }}

    wireHourSelectors();

    function alpha(hex, opacity) {{
      const normalized = hex.replace('#', '');
      const value = parseInt(normalized.length === 3 ? normalized.split('').map((c) => c + c).join('') : normalized, 16);
      const r = (value >> 16) & 255;
      const g = (value >> 8) & 255;
      const b = value & 255;
      return `rgba(${{r}}, ${{g}}, ${{b}}, ${{opacity}})`;
    }}

    function createLineChart(canvasId, datasets, yLabel) {{
      const context = document.getElementById(canvasId);
      const hasEnergyAxis = datasets.some((item) => item.yAxisID === 'energy');
      const scaleBounds = getScaleBounds(datasets, yLabel, 'y');
      const energyScaleBounds = hasEnergyAxis ? getAlignedEnergyScaleBounds(datasets, scaleBounds) : {{ min: undefined, max: undefined }};
      new Chart(context, {{
        type: 'line',
        data: {{
          labels: payload.labels,
          datasets: datasets.map((item) => ({{
            label: item.label,
            data: item.data,
            borderColor: item.borderColor,
            backgroundColor: alpha(item.borderColor, 0.12),
            borderDash: item.borderDash || [],
            borderWidth: item.borderWidth || 3,
            actualValue: item.actualValue,
            yAxisID: item.yAxisID || 'y',
            unit: item.unit || '',
            axisMin: item.axisMin,
            axisMax: item.axisMax,
            alignWaterMin: item.alignWaterMin,
            alignWaterMax: item.alignWaterMax,
            pointRadius: item.pointRadius || 0,
            pointHoverRadius: item.pointHoverRadius || 3,
            pointHitRadius: 12,
            cubicInterpolationMode: 'monotone',
            tension: item.tension || 0.28,
            fill: false,
            spanGaps: true
          }}))
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          interaction: {{ mode: 'index', intersect: false }},
          plugins: {{
            legend: {{
              display: false
            }},
            tooltip: {{
              backgroundColor: 'rgba(255, 255, 255, 0.96)',
              titleColor: '#0f2742',
              bodyColor: '#173a5e',
              borderColor: 'rgba(117, 139, 163, 0.35)',
              borderWidth: 1,
              cornerRadius: 6,
              padding: 10,
              callbacks: {{
                label: (ctx) => {{
                  const unitLabel = ctx.dataset.unit || (ctx.dataset.yAxisID === 'energy' ? 'tr. kWh' : yLabel);
                  const unit = unitLabel ? ` ${{unitLabel}}` : '';
                  if (ctx.dataset.actualValue !== undefined && ctx.dataset.actualValue !== null) {{
                    return `${{ctx.dataset.label}}: ${{formatChartNumber(ctx.dataset.actualValue, unitLabel)}}${{unit}}`;
                  }}
                  return `${{ctx.dataset.label}}: ${{formatChartNumber(ctx.parsed.y, unitLabel)}}${{unit}}`;
                }}
              }}
            }}
          }},
          scales: {{
            x: {{
              ticks: {{
                maxRotation: 0,
                autoSkip: payload.granularity !== 'hourly',
                maxTicksLimit: payload.granularity === 'yearly' ? 16 : (payload.granularity === 'monthly' ? 24 : (payload.granularity === 'weekly' ? 24 : (payload.granularity === 'daily' ? 18 : 24))),
                color: '#5b718a',
                font: {{ size: 10 }}
              }},
              grid: {{ color: 'rgba(138, 171, 207, 0.16)' }}
            }},
            y: {{
              beginAtZero: false,
              min: scaleBounds.min,
              max: scaleBounds.max,
              title: {{ display: true, text: yLabel, color: '#35526e', font: {{ size: 11, weight: '700' }} }},
              ticks: {{
                color: '#5b718a',
                font: {{ size: 10 }},
                stepSize: yLabel === 'm' ? scaleBounds.step : undefined,
                callback: (value) => {{
                  return formatAxisTick(value, yLabel);
                }}
              }},
              grid: {{ color: 'rgba(138, 171, 207, 0.22)' }}
            }},
            energy: {{
              display: hasEnergyAxis,
              position: 'right',
              min: energyScaleBounds.min,
              max: energyScaleBounds.max,
              title: {{ display: hasEnergyAxis, text: 'tr. kWh', color: '#f97316', font: {{ size: 11, weight: '700' }} }},
              ticks: {{
                color: '#f97316',
                font: {{ size: 10 }},
                callback: (value) => formatAxisTick(value, 'tr. kWh')
              }},
              grid: {{ drawOnChartArea: false }}
            }}
          }}
        }},
        plugins: [makeWaterAxisTicksPlugin(yLabel, scaleBounds)]
      }});
      renderCustomLegend(context, datasets);
    }}

    function formatAxisTick(value, yLabel) {{
      const number = Number(value);
      if (!Number.isFinite(number)) return value;
      return number.toLocaleString('vi-VN', {{
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
      }});
    }}

    function formatChartNumber(value, unitLabel) {{
      const number = Number(value);
      if (!Number.isFinite(number)) return value;
      return number.toLocaleString('vi-VN', {{
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
      }});
    }}

    function makeWaterAxisTicksPlugin(yLabel, scaleBounds) {{
      return {{
        id: `water-axis-ticks-${{Math.random().toString(36).slice(2)}}`,
        afterBuildTicks: (chart, args) => {{
          const scale = args.scale;
          if (yLabel !== 'm' || scale.id !== 'y' || scaleBounds.firstTick === undefined) return;
          const ticks = [];
          for (let value = scaleBounds.firstTick; value <= scaleBounds.lastTick + 0.001; value += scaleBounds.step) {{
            ticks.push({{ value }});
          }}
          scale.ticks = ticks;
        }}
      }};
    }}

    function getScaleBounds(datasets, yLabel, axisId) {{
      const values = [];
      datasets.forEach((dataset) => {{
        const datasetAxis = dataset.yAxisID || 'y';
        if (datasetAxis !== axisId) return;
        (dataset.data || []).forEach((value) => {{
          if (value !== null && value !== undefined && Number.isFinite(Number(value))) {{
            values.push(Number(value));
          }}
        }});
      }});
      if (!values.length) return {{ min: undefined, max: undefined }};
      const minValue = Math.min(...values);
      const maxValue = Math.max(...values);
      const axisMins = datasets
        .filter((dataset) => (dataset.yAxisID || 'y') === axisId && dataset.axisMin !== undefined && dataset.axisMin !== null)
        .map((dataset) => Number(dataset.axisMin))
        .filter((value) => Number.isFinite(value));
      const axisMaxs = datasets
        .filter((dataset) => (dataset.yAxisID || 'y') === axisId && dataset.axisMax !== undefined && dataset.axisMax !== null)
        .map((dataset) => Number(dataset.axisMax))
        .filter((value) => Number.isFinite(value));
      if (axisId === 'energy' && axisMins.length && axisMaxs.length) {{
        const axisMin = Math.min(...axisMins);
        const axisMax = Math.max(...axisMaxs);
        const padding = Math.max((axisMax - axisMin) * 0.04, 0.1);
        return {{ min: axisMin - padding, max: axisMax + padding }};
      }}
      if (yLabel === 'm') {{
        const deadLevelValues = datasets
          .filter((dataset) => (dataset.yAxisID || 'y') === axisId && /(^| - )MNC$/.test(dataset.label || ''))
          .flatMap((dataset) => dataset.data || [])
          .map((value) => Number(value))
          .filter((value) => Number.isFinite(value));
        if (deadLevelValues.length) {{
          const deadLevel = Math.min(...deadLevelValues);
          const visualMax = Math.max(maxValue, deadLevel + 0.5);
          const visualRange = Math.max(visualMax - deadLevel, 0.5);
          const step = visualRange <= 2 ? 0.5 : (visualRange <= 8 ? 1 : (visualRange <= 20 ? 2 : 5));
          const bottomPadding = Math.max(visualRange * 0.06, 0.04);
          const topPadding = Math.max(visualRange * 0.08, 0.04);
          const lastTick = deadLevel + Math.ceil(visualRange / step) * step;
          return {{
            min: deadLevel - bottomPadding,
            max: Math.max(visualMax + topPadding, lastTick + topPadding * 0.35),
            firstTick: deadLevel,
            lastTick,
            step
          }};
        }}
        const dataRange = Math.max(maxValue - minValue, 1);
        const step = dataRange <= 8 ? 1 : (dataRange <= 20 ? 2 : 5);
        const minTick = Math.floor(minValue / step) * step;
        const maxTick = Math.ceil(maxValue / step) * step;
        return {{
          min: minTick - step / 2,
          max: maxTick + step / 2,
          firstTick: minTick,
          lastTick: maxTick,
          step
        }};
      }}
      const range = Math.max(maxValue - minValue, Math.abs(maxValue) * 0.01, 1);
      const padding = range * 0.08;
      return {{ min: minValue - padding, max: maxValue + padding }};
    }}

    function getAlignedEnergyScaleBounds(datasets, waterBounds) {{
      const energyDataset = datasets.find((dataset) =>
        dataset.yAxisID === 'energy'
        && dataset.axisMin !== undefined
        && dataset.axisMax !== undefined
        && dataset.alignWaterMin !== undefined
        && dataset.alignWaterMax !== undefined
      );
      if (!energyDataset || waterBounds.min === undefined || waterBounds.max === undefined) {{
        return getScaleBounds(datasets, 'tr. kWh', 'energy');
      }}

      const energyMinRef = Number(energyDataset.axisMin);
      const energyMaxRef = Number(energyDataset.axisMax);
      const waterMinRef = Number(energyDataset.alignWaterMin);
      const waterMaxRef = Number(energyDataset.alignWaterMax);
      const waterRange = waterBounds.max - waterBounds.min;
      const waterRefRange = waterMaxRef - waterMinRef;
      const energyRefRange = energyMaxRef - energyMinRef;
      if (!Number.isFinite(waterRange) || !Number.isFinite(waterRefRange) || !Number.isFinite(energyRefRange)
          || waterRange <= 0 || waterRefRange === 0 || energyRefRange === 0) {{
        return getScaleBounds(datasets, 'tr. kWh', 'energy');
      }}

      const energyUnitsPerWaterUnit = energyRefRange / waterRefRange;
      return {{
        min: energyMinRef - (waterMinRef - waterBounds.min) * energyUnitsPerWaterUnit,
        max: energyMinRef + (waterBounds.max - waterMinRef) * energyUnitsPerWaterUnit
      }};
    }}

    function renderCustomLegend(canvas, datasets) {{
      const oldLegend = canvas.parentElement.querySelector('.custom-legend');
      if (oldLegend) oldLegend.remove();
      const legend = document.createElement('div');
      legend.className = 'custom-legend';
      datasets.forEach((item) => {{
        const entry = document.createElement('div');
        entry.className = 'legend-item';
        const line = document.createElement('span');
        line.className = `legend-line ${{item.borderDash && item.borderDash.length ? 'dashed' : ''}}`;
        line.style.color = item.borderColor;
        const label = document.createElement('span');
        label.textContent = item.label;
        entry.appendChild(line);
        entry.appendChild(label);
        legend.appendChild(entry);
      }});
      canvas.insertAdjacentElement('afterend', legend);
    }}

    const noteText = payload.granularity === 'yearly'
      ? 'Trục năm, mực nước lấy cuối kỳ; lưu lượng là trung bình năm.'
      : (payload.granularity === 'monthly'
        ? 'Trục tháng, mực nước lấy cuối kỳ; lưu lượng là trung bình tháng.'
        : (payload.granularity === 'weekly'
          ? 'Trục tuần, giá trị là trung bình các mốc giờ trong tuần.'
          : (payload.granularity === 'daily'
            ? 'Trục ngày, giá trị là trung bình các mốc giờ trong ngày.'
            : 'Trục 24h theo giờ trong ngày được chọn.')));
    document.getElementById('water-note').textContent = noteText;
    document.getElementById('flow-note').textContent = payload.flowNote || noteText;
    document.getElementById('generation-note').textContent = payload.granularity === 'yearly'
      ? 'Trục năm, đơn vị MWh/năm; đường đỏ là dự báo AI phần còn lại.'
      : (payload.granularity === 'monthly'
        ? 'Trục tháng, đơn vị MWh/tháng; đường đỏ là dự báo AI phần còn lại.'
        : (payload.granularity === 'weekly'
          ? 'Trục tuần, đơn vị MWh/tuần; đường đỏ là dự báo AI phần còn lại.'
          : 'Trục ngày, đơn vị MWh/ngày; đường đỏ là dự báo AI phần còn lại.'));

    createLineChart('waterChart', payload.water, 'm');
    createLineChart('flowChart', payload.flow, 'm3/s');
    createLineChart('generationChart', payload.generation, 'MWh');
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path == "/analysis":
            content = render_analysis_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path != "/":
            self.send_error(404, "Not Found")
            return
        try:
            payload = build_payload(parse_params(parsed.query))
            content = render_page(payload).encode("utf-8")
            self.send_response(200)
        except Exception as exc:
            content = (
                "<!doctype html><meta charset='utf-8'>"
                f"<h1>Lỗi dashboard</h1><pre>{html.escape(str(exc))}</pre>"
            ).encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in {"/save-data", "/train-ai"}:
            self.send_error(404, "Not Found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length).decode("utf-8")
            params = parse_params(body)
            if parsed.path == "/train-ai":
                summary = run_ai_training_pipeline()
                params["show_forecast"] = "1"
                params["save_notice"] = f"Đã training AI và cập nhật dự báo. {summary}"
                query = params_to_query(params)
                location = f"/?{query}" if query else "/"
                self.send_response(303)
                self.send_header("Location", location)
                self.end_headers()
                return

            start_at = parse_input_datetime(params.get("start"))
            end_at = parse_input_datetime(params.get("end"))
            if not start_at or not end_at:
                raise ValueError("Vui lòng chọn Từ ngày giờ và Đến ngày giờ trước khi lưu.")
            if start_at > end_at:
                raise ValueError("Từ ngày giờ phải nhỏ hơn hoặc bằng Đến ngày giờ.")

            result = save_data_range(start_at, end_at)
            notice = (
                f"Đã lưu {result['saved_points']} mốc, "
                f"{result['saved_readings']} dòng vào {DB_PATH.name}."
            )
            if result["errors"]:
                notice += f" Có {len(result['errors'])} mốc lỗi, xem terminal/log để biết chi tiết."
                for error in result["errors"][:10]:
                    print(f"[SAVE ERROR] {error}")
            params["save_notice"] = notice
        except Exception as exc:
            params = parse_params(body) if "body" in locals() else {}
            if parsed.path == "/train-ai":
                params["save_notice"] = f"Training AI lỗi: {exc}"
            else:
                params["save_notice"] = f"Lưu dữ liệu lỗi: {exc}"

        query = params_to_query(params)
        location = f"/?{query}" if query else "/"
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def get_port() -> int:
    raw_port = str(os.environ.get("PORT", DEFAULT_PORT)).strip().strip('"').strip("'")
    return int(raw_port or DEFAULT_PORT)


def main():
    port = get_port()
    lan_ip = get_lan_ip()
    print(f"Dashboard local: http://127.0.0.1:{port}")
    print(f"LAN access: http://{lan_ip}:{port}")
    print(f"Database: {DB_PATH}")
    server = ThreadingHTTPServer((HOST, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
