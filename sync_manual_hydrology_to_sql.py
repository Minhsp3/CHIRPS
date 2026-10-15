from __future__ import annotations

import math
import sqlite3
import argparse
from datetime import datetime, timedelta

import pandas as pd

from config import HOCHUA_DB_PATH, HYDROLOGY_FILES


REGION = "Tay Nguyen"

BASIN_TO_RESERVOIR = {
    "BTS": "Buôn Tua Srah",
    "BK": "Buôn Kuốp",
    "SP3": "Srêpốk 3",
}


def to_float(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def to_db_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def iter_hours(day: datetime):
    current = day.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(24):
        yield current
        current += timedelta(hours=1)


def load_manual_rows(start_date: str, end_date: str, source_url: str):
    start_day = pd.Timestamp(start_date)
    end_day = pd.Timestamp(end_date)
    rows = []

    for basin_id, path in HYDROLOGY_FILES.items():
        if basin_id not in BASIN_TO_RESERVOIR:
            continue
        candidates = sorted(
            [path, *path.parent.glob(f"{path.stem}_new*{path.suffix}")],
            key=lambda item: item.stat().st_mtime if item.exists() else 0,
            reverse=True,
        )
        path = next((candidate for candidate in candidates if candidate.exists()), path)
        if not path.exists():
            continue

        df = pd.read_csv(path, parse_dates=["Date"])
        df = df[(df["Date"] >= start_day) & (df["Date"] <= end_day)].copy()
        df = df.sort_values("Date")

        reservoir_name = BASIN_TO_RESERVOIR[basin_id]
        for item in df.itertuples(index=False):
            day = pd.Timestamp(item.Date).to_pydatetime()
            h_tl = to_float(getattr(item, "WaterLevel_m", None))
            q_ve = to_float(getattr(item, "Q_in_m3s", None))
            q_xt = to_float(getattr(item, "Q_spill_m3s", None))
            q_xm = to_float(getattr(item, "Q_turbine_m3s", None))
            q_env = to_float(getattr(item, "Q_env_m3s", None))

            outflow_parts = [value for value in (q_xt, q_xm, q_env) if value is not None]
            sum_qx = round(sum(outflow_parts), 4) if outflow_parts else None

            for observed_at in iter_hours(day):
                label = f"{observed_at.hour:02d}h00 ngày {observed_at:%d/%m/%Y}"
                timestamp = to_db_datetime(observed_at)
                rows.append(
                    (
                        timestamp,
                        timestamp,
                        timestamp,
                        REGION,
                        reservoir_name,
                        reservoir_name,
                        label,
                        label,
                        h_tl,
                        None,
                        None,
                        q_ve,
                        sum_qx,
                        q_xt,
                        q_xm,
                        None,
                        None,
                        source_url,
                        timestamp,
                    )
                )

    return rows


def sync_to_sql(start_date: str, end_date: str, source_url: str):
    rows = load_manual_rows(start_date, end_date, source_url)
    if not rows:
        raise RuntimeError("No manual hydrology rows were found to sync.")

    start_at = f"{start_date} 00:00:00"
    end_at = f"{end_date} 23:00:00"
    reservoir_names = tuple(BASIN_TO_RESERVOIR.values())
    crawl_rows = sorted(
        {
            (row[0], source_url, row[0], len(BASIN_TO_RESERVOIR), row[0], row[0])
            for row in rows
        }
    )

    conn = sqlite3.connect(HOCHUA_DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("BEGIN")
        conn.execute(
            f"""
            DELETE FROM reservoir_readings
            WHERE source_url = ?
              AND observed_at BETWEEN ? AND ?
              AND reservoir_name IN ({','.join('?' for _ in reservoir_names)})
            """,
            (source_url, start_at, end_at, *reservoir_names),
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO crawl_requests (
                requested_at,
                source_url,
                fetched_at,
                reading_count,
                observed_min_at,
                observed_max_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            crawl_rows,
        )
        conn.executemany(
            """
            INSERT INTO reservoir_readings (
                observed_at,
                requested_at_first_seen,
                requested_at_last_seen,
                region,
                reservoir_name,
                reservoir_name_raw,
                raw_time_label,
                sync_hint,
                h_tl,
                h_dbt,
                h_c,
                q_ve,
                sum_qx,
                q_xt,
                q_xm,
                n_cxs,
                n_cxm,
                source_url,
                fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return len(rows), len(crawl_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync HydroAI daily hydrology files into the dashboard SQLite database.")
    parser.add_argument("--start", default="2021-08-18", help="Start date, yyyy-mm-dd.")
    parser.add_argument("--end", default="2022-07-10", help="End date, yyyy-mm-dd.")
    parser.add_argument("--source-tag", default="PMIS_MANUAL_CSV", help="Source marker saved into reservoir_readings.source_url.")
    args = parser.parse_args()

    reading_count, crawl_count = sync_to_sql(args.start, args.end, args.source_tag)
    print(f"Synced {reading_count:,} reservoir readings and {crawl_count:,} crawl hours.")


if __name__ == "__main__":
    main()
