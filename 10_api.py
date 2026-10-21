from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config import API_HOST, API_PORT, BASIN_NAMES, HOCHUA_DB_PATH, MODEL_DIR


API_KEY_ENV = "HYDROAI_API_KEY"
API_KEY_HEADER = "X-API-Key"
DEFAULT_LIMIT = 1_000
MAX_LIMIT = 10_000
MAX_RANGE_DAYS = int(os.getenv("HYDROAI_API_MAX_RANGE_DAYS", "4000"))
RESERVOIR_BY_BASIN = {
    "BTS": "Buôn Tua Srah",
    "BK": "Buôn Kuốp",
    "SP3": "Srêpốk 3",
}
CLEAN_NAME_SQL = (
    "TRIM(CASE "
    "WHEN INSTR(reservoir_name, 'Đồng bộ lúc:') > 0 "
    "THEN SUBSTR(reservoir_name, 1, INSTR(reservoir_name, 'Đồng bộ lúc:') - 1) "
    "ELSE reservoir_name END)"
)


def database_path() -> Path:
    return Path(os.getenv("HYDROAI_DB_PATH", str(HOCHUA_DB_PATH))).expanduser().resolve()


def open_database() -> sqlite3.Connection:
    path = database_path()
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy database tại {path}")
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=10,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def rows_as_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def validate_basin_id(basin_id: str | None) -> str | None:
    if basin_id is None:
        return None
    normalized = basin_id.strip().upper()
    if normalized not in RESERVOIR_BY_BASIN:
        raise ValueError(f"Lưu vực không hợp lệ: {basin_id}")
    return normalized


def normalize_range(
    start_date: date | None,
    end_date: date | None,
    default_end: date,
    default_days: int = 30,
) -> tuple[date, date]:
    end = end_date or default_end
    start = start_date or (end - timedelta(days=default_days - 1))
    if start > end:
        raise ValueError("start_date phải nhỏ hơn hoặc bằng end_date")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ValueError(f"Khoảng ngày không được vượt quá {MAX_RANGE_DAYS} ngày")
    return start, end


def limited_result(rows: list[sqlite3.Row], limit: int) -> tuple[list[dict[str, Any]], bool]:
    truncated = len(rows) > limit
    return rows_as_dicts(rows[:limit]), truncated


def reservoir_filter(basin_id: str | None) -> tuple[str, list[str]]:
    selected = (
        [RESERVOIR_BY_BASIN[basin_id]]
        if basin_id
        else list(RESERVOIR_BY_BASIN.values())
    )
    clauses: list[str] = []
    params: list[str] = []
    for name in selected:
        clauses.append("(reservoir_name = ? OR reservoir_name LIKE ? OR reservoir_name LIKE ?)")
        params.extend([name, f"{name}Đồng bộ%", f"{name} Đồng bộ%"])
    return "(" + " OR ".join(clauses) + ")", params


def basin_case_sql() -> str:
    cases = " ".join(
        f"WHEN '{name.replace(chr(39), chr(39) * 2)}' THEN '{basin_id}'"
        for basin_id, name in RESERVOIR_BY_BASIN.items()
    )
    return f"CASE {CLEAN_NAME_SQL} {cases} END"


def latest_history_date(connection: sqlite3.Connection) -> date:
    row = connection.execute("SELECT MAX(observed_at) FROM reservoir_readings").fetchone()
    if not row or not row[0]:
        return date.today()
    return date.fromisoformat(str(row[0])[:10])


def query_history(
    connection: sqlite3.Connection,
    basin_id: str | None,
    start_date: date | None,
    end_date: date | None,
    limit: int,
) -> dict[str, Any]:
    basin_id = validate_basin_id(basin_id)
    start, end = normalize_range(start_date, end_date, latest_history_date(connection))
    filter_sql, filter_params = reservoir_filter(basin_id)
    end_exclusive = end + timedelta(days=1)
    sql = f"""
        WITH ranked AS (
            SELECT
                id,
                observed_at,
                requested_at_last_seen,
                {CLEAN_NAME_SQL} AS Reservoir_Name,
                {basin_case_sql()} AS Basin_ID,
                h_tl,
                h_dbt,
                h_c,
                q_ve,
                sum_qx,
                q_xt,
                q_xm,
                ROW_NUMBER() OVER (
                    PARTITION BY observed_at, {CLEAN_NAME_SQL}
                    ORDER BY requested_at_last_seen DESC, id DESC
                ) AS rn
            FROM reservoir_readings
            WHERE observed_at >= ?
              AND observed_at < ?
              AND {filter_sql}
        ),
        daily AS (
            SELECT
                date(observed_at) AS Date,
                Basin_ID,
                Reservoir_Name,
                AVG(h_tl) AS WaterLevel_m,
                AVG(h_dbt) AS MNDBT_m,
                AVG(h_c) AS MNC_m,
                AVG(q_ve) AS Q_in_m3s,
                AVG(sum_qx) AS Q_release_m3s,
                AVG(q_xt) AS Q_spill_m3s,
                AVG(q_xm) AS Q_turbine_m3s,
                COUNT(*) AS Sample_Count
            FROM ranked
            WHERE rn = 1 AND Basin_ID IS NOT NULL
            GROUP BY date(observed_at), Basin_ID, Reservoir_Name
        )
        SELECT *
        FROM (
            SELECT * FROM daily
            ORDER BY Date DESC, Basin_ID
            LIMIT ?
        )
        ORDER BY Date, Basin_ID
    """
    params: list[Any] = [start.isoformat(), end_exclusive.isoformat()]
    params.extend(filter_params)
    params.append(limit + 1)
    rows = connection.execute(sql, params).fetchall()
    payload, truncated = limited_result(rows, limit)
    return {
        "rows": payload,
        "basin_id": basin_id,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "limit": limit,
        "truncated": truncated,
    }


def table_date_bounds(
    connection: sqlite3.Connection,
    table_name: str,
    date_column: str,
    basin_id: str | None = None,
) -> tuple[date, date] | None:
    if not table_exists(connection, table_name):
        return None
    sql = f'SELECT MIN("{date_column}"), MAX("{date_column}") FROM "{table_name}"'
    params: tuple[Any, ...] = ()
    if basin_id:
        sql += ' WHERE "Basin_ID" = ?'
        params = (basin_id,)
    row = connection.execute(sql, params).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    return date.fromisoformat(str(row[0])[:10]), date.fromisoformat(str(row[1])[:10])


def query_forecast(
    connection: sqlite3.Connection,
    basin_id: str | None,
    start_date: date | None,
    end_date: date | None,
    limit: int,
) -> dict[str, Any]:
    basin_id = validate_basin_id(basin_id)
    bounds = table_date_bounds(connection, "hydroai_forecast_daily", "Date", basin_id)
    if bounds is None:
        return {"rows": [], "limit": limit, "truncated": False}
    start, end = normalize_range(start_date, end_date, bounds[1], default_days=366)
    where = ['"Date" >= ?', '"Date" <= ?']
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if basin_id:
        where.append('"Basin_ID" = ?')
        params.append(basin_id)
    params.append(limit + 1)
    rows = connection.execute(
        f"""
        SELECT
            Date, Basin_ID, Forecast_Type, ENSO_Q_Anomaly_pct,
            ENSO_Scenario_Source, Rain_Basin_mm,
            Q_P10_m3s, Q_P50_m3s, Q_P90_m3s,
            WaterLevel_P10_m, WaterLevel_P50_m, WaterLevel_P90_m
        FROM hydroai_forecast_daily
        WHERE {' AND '.join(where)}
        ORDER BY Date, Basin_ID
        LIMIT ?
        """,
        params,
    ).fetchall()
    payload, truncated = limited_result(rows, limit)
    return {
        "rows": payload,
        "basin_id": basin_id,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "limit": limit,
        "truncated": truncated,
    }


def query_generation(
    connection: sqlite3.Connection,
    basin_id: str | None,
    start_date: date | None,
    end_date: date | None,
    limit: int,
    include_forecast: bool,
) -> dict[str, Any]:
    basin_id = validate_basin_id(basin_id)
    bounds = table_date_bounds(connection, "pmis_generation_daily", "Date", basin_id)
    if bounds is None:
        return {"actual_rows": [], "forecast_rows": [], "limit": limit, "truncated": False}
    start, end = normalize_range(start_date, end_date, bounds[1], default_days=366)
    where = ['"Date" >= ?', '"Date" <= ?']
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if basin_id:
        where.append('"Basin_ID" = ?')
        params.append(basin_id)
    actual = connection.execute(
        f"""
        SELECT
            Date, Basin_ID, Reservoir_Name, Display_Name,
            Generation_MWh, Generation_Net_MWh, Self_Use_MWh,
            Running_Hours, Power_MW, Turbine_Water_Mm3,
            Q_turbine_m3s, Specific_Water_m3_per_kWh
        FROM pmis_generation_daily
        WHERE {' AND '.join(where)}
        ORDER BY Date, Basin_ID
        LIMIT ?
        """,
        [*params, limit + 1],
    ).fetchall()
    actual_payload, actual_truncated = limited_result(actual, limit)

    forecast_payload: list[dict[str, Any]] = []
    forecast_truncated = False
    if include_forecast and table_exists(connection, "hydroai_generation_forecast_daily"):
        forecast = connection.execute(
            f"""
            SELECT
                Date, Basin_ID, Reservoir_Name, Generation_MWh,
                Generation_Forecast_MWh, Generation_P10_MWh,
                Generation_P90_MWh, Forecast_Power_MW,
                Value_Type, Blend_Source, Actual_Cutoff
            FROM hydroai_generation_forecast_daily
            WHERE {' AND '.join(where)}
            ORDER BY Date, Basin_ID
            LIMIT ?
            """,
            [*params, limit + 1],
        ).fetchall()
        forecast_payload, forecast_truncated = limited_result(forecast, limit)

    return {
        "actual_rows": actual_payload,
        "forecast_rows": forecast_payload,
        "basin_id": basin_id,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "limit": limit,
        "truncated": actual_truncated or forecast_truncated,
    }


def create_app():
    try:
        from fastapi import Depends, FastAPI, HTTPException, Query, status
        from fastapi.security import APIKeyHeader
    except ImportError as exc:
        raise ImportError("Chưa có FastAPI. Hãy cài requirements.txt") from exc

    app = FastAPI(title="HydroAI data API", version="6.0")
    api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)

    def require_api_key(api_key: str | None = Depends(api_key_header)) -> None:
        expected = os.getenv(API_KEY_ENV, "")
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Máy chủ chưa cấu hình {API_KEY_ENV}",
            )
        if not api_key or not secrets.compare_digest(api_key, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key không hợp lệ",
            )

    def checked_basin(basin_id: str | None) -> str | None:
        try:
            return validate_basin_id(basin_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def run_query(callback, *args):
        try:
            with closing(open_database()) as connection:
                return callback(connection, *args)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="Không truy vấn được database") from exc

    @app.get("/health")
    def health() -> dict[str, Any]:
        path = database_path()
        return {
            "status": "ok" if path.is_file() else "degraded",
            "name": "HydroAI data API",
            "database_available": path.is_file(),
        }

    @app.get("/basins", dependencies=[Depends(require_api_key)])
    def basins() -> dict[str, Any]:
        def load(connection: sqlite3.Connection) -> dict[str, Any]:
            items = []
            for basin_id, reservoir_name in RESERVOIR_BY_BASIN.items():
                row = connection.execute(
                    """
                    SELECT MIN(observed_at), MAX(observed_at)
                    FROM reservoir_readings
                    WHERE reservoir_name = ?
                       OR reservoir_name LIKE ?
                       OR reservoir_name LIKE ?
                    """,
                    (
                        reservoir_name,
                        f"{reservoir_name}Đồng bộ%",
                        f"{reservoir_name} Đồng bộ%",
                    ),
                ).fetchone()
                forecast_bounds = table_date_bounds(
                    connection,
                    "hydroai_forecast_daily",
                    "Date",
                    basin_id,
                )
                items.append(
                    {
                        "id": basin_id,
                        "name": BASIN_NAMES.get(basin_id, basin_id),
                        "reservoir_name": reservoir_name,
                        "min_date": str(row[0])[:10] if row and row[0] else None,
                        "max_date": str(row[1])[:10] if row and row[1] else None,
                        "forecast_min_date": forecast_bounds[0].isoformat() if forecast_bounds else None,
                        "forecast_max_date": forecast_bounds[1].isoformat() if forecast_bounds else None,
                    }
                )
            return {"basins": items}

        return run_query(load)

    @app.get("/history", dependencies=[Depends(require_api_key)])
    def history(
        basin_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        return run_query(
            query_history,
            checked_basin(basin_id),
            start_date,
            end_date,
            limit,
        )

    @app.get("/forecast", dependencies=[Depends(require_api_key)])
    def forecast(
        basin_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        return run_query(
            query_forecast,
            checked_basin(basin_id),
            start_date,
            end_date,
            limit,
        )

    @app.get("/generation", dependencies=[Depends(require_api_key)])
    def generation(
        basin_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        include_forecast: bool = True,
    ) -> dict[str, Any]:
        return run_query(
            query_generation,
            checked_basin(basin_id),
            start_date,
            end_date,
            limit,
            include_forecast,
        )

    @app.get("/metrics/{basin_id}", dependencies=[Depends(require_api_key)])
    def metrics(basin_id: str) -> dict[str, Any]:
        normalized = checked_basin(basin_id)
        file = MODEL_DIR / f"metrics_{normalized}.json"
        if not file.exists():
            raise HTTPException(status_code=404, detail="Không tìm thấy model metrics")
        with open(file, encoding="utf-8") as handle:
            return json.load(handle)

    return app


app = create_app()


if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError as exc:
        raise ImportError("Chưa có Uvicorn. Hãy cài requirements.txt") from exc

    uvicorn.run(app, host=API_HOST, port=API_PORT, reload=False)
