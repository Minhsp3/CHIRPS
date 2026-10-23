from __future__ import annotations

import json
import os
import secrets
import sqlite3
import base64
import re
import uuid
from contextlib import closing
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from config import API_HOST, API_PORT, BASIN_NAMES, HOCHUA_DB_PATH, MODEL_DIR


API_KEY_ENV = "HYDROAI_API_KEY"
API_KEY_HEADER = "X-API-Key"
API_KEY_FILE_ENV = "HYDROAI_API_KEY_FILE"
DEFAULT_LIMIT = 1_000
MAX_LIMIT = 10_000
MAX_RANGE_DAYS = int(os.getenv("HYDROAI_API_MAX_RANGE_DAYS", "4000"))
MAX_UPLOAD_FILE_BYTES = int(os.getenv("HYDROAI_MAX_UPLOAD_FILE_BYTES", str(25 * 1024 * 1024)))
MAX_UPLOAD_BATCH_BYTES = int(os.getenv("HYDROAI_MAX_UPLOAD_BATCH_BYTES", str(50 * 1024 * 1024)))
ALLOWED_UPLOAD_SUFFIXES = {".xls", ".xlsx", ".xlsm"}
RESERVOIR_BY_BASIN = {
    "BTS": "Buôn Tua Srah",
    "BK": "Buôn Kuốp",
    "SP3": "Srêpốk 3",
}
RESERVOIR_ID_PREFIX = "RESERVOIR:"
RESERVOIR_DISPLAY_ALIASES = {"KHE BỐ": "Khe Bố"}
CLEAN_NAME_SQL = (
    "TRIM(CASE "
    "WHEN INSTR(reservoir_name, 'Đồng bộ lúc:') > 0 "
    "THEN SUBSTR(reservoir_name, 1, INSTR(reservoir_name, 'Đồng bộ lúc:') - 1) "
    "ELSE reservoir_name END)"
)


def hydrology_upload_dir() -> Path:
    default = Path(__file__).resolve().parent / "Truyvan_thuyvan" / "saved_uploads"
    return Path(os.getenv("HYDROAI_UPLOAD_DIR", str(default))).expanduser().resolve()


def safe_upload_name(raw_name: str) -> str:
    name = Path(raw_name).name.strip()
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise ValueError("Chỉ chấp nhận tệp Excel .xls, .xlsx hoặc .xlsm")
    stem = re.sub(r"[^0-9A-Za-zÀ-ỹ _().-]+", "_", Path(name).stem).strip(" .")
    if not stem:
        stem = "du_lieu"
    return f"{stem[:160]}{suffix}"


def save_hydrology_uploads(payload: dict[str, Any]) -> dict[str, Any]:
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= 20:
        raise ValueError("Cần chọn từ 1 đến 20 tệp Excel")
    decoded: list[tuple[str, bytes]] = []
    total_bytes = 0
    used_names: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict):
            raise ValueError("Danh sách tệp tải lên không hợp lệ")
        raw_name = item.get("name")
        raw_content = item.get("content_base64")
        if not isinstance(raw_name, str) or not 1 <= len(raw_name) <= 180:
            raise ValueError("Tên tệp tải lên không hợp lệ")
        if not isinstance(raw_content, str) or not raw_content:
            raise ValueError(f"Nội dung tệp {raw_name} không hợp lệ")
        name = safe_upload_name(raw_name)
        if name.casefold() in used_names:
            raise ValueError(f"Tên tệp bị trùng trong lần tải lên: {name}")
        used_names.add(name.casefold())
        try:
            content = base64.b64decode(raw_content, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Nội dung tệp {name} không hợp lệ") from exc
        if not content:
            raise ValueError(f"Tệp {name} không có dữ liệu")
        if len(content) > MAX_UPLOAD_FILE_BYTES:
            raise ValueError(f"Tệp {name} vượt quá giới hạn dung lượng")
        total_bytes += len(content)
        if total_bytes > MAX_UPLOAD_BATCH_BYTES:
            raise ValueError("Tổng dung lượng các tệp vượt quá giới hạn cho phép")
        decoded.append((name, content))

    root = hydrology_upload_dir()
    root.mkdir(parents=True, exist_ok=True)
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    batch_dir = root / batch_id
    batch_dir.mkdir(parents=False, exist_ok=False)
    for name, content in decoded:
        (batch_dir / name).write_bytes(content)

    saved_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "batch_id": batch_id,
        "saved_at": saved_at,
        "files": [{"name": name, "size": len(content)} for name, content in decoded],
    }
    manifest_path = root / "current.json"
    temporary_manifest = root / f".{batch_id}.json.tmp"
    temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_manifest.replace(manifest_path)
    return manifest


def load_hydrology_uploads() -> dict[str, Any]:
    root = hydrology_upload_dir()
    manifest_path = root / "current.json"
    if not manifest_path.is_file():
        return {"batch_id": None, "saved_at": None, "files": []}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    batch_id = str(manifest.get("batch_id", ""))
    if not re.fullmatch(r"[0-9TZ-]+[0-9a-f]{8}", batch_id):
        raise ValueError("Manifest dữ liệu tải lên không hợp lệ")
    batch_dir = root / batch_id
    files = []
    for item in manifest.get("files", []):
        name = safe_upload_name(str(item.get("name", "")))
        path = batch_dir / name
        if not path.is_file():
            continue
        content = path.read_bytes()
        files.append(
            {
                "name": name,
                "size": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
    return {
        "batch_id": batch_id,
        "saved_at": manifest.get("saved_at"),
        "files": files,
    }


def database_path() -> Path:
    return Path(os.getenv("HYDROAI_DB_PATH", str(HOCHUA_DB_PATH))).expanduser().resolve()


def configured_api_key() -> str:
    value = os.getenv(API_KEY_ENV, "").strip()
    if value:
        return value
    default_file = Path(__file__).resolve().parent / ".hydroai_api_key"
    key_file = Path(os.getenv(API_KEY_FILE_ENV, str(default_file))).expanduser()
    try:
        return key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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


def resolve_basin_selector(basin_id: str | None) -> tuple[str | None, str | None]:
    if basin_id is None:
        return None, None
    selector = basin_id.strip()
    if not selector:
        raise ValueError("Lưu vực không được để trống")
    normalized = selector.upper()
    if normalized in RESERVOIR_BY_BASIN:
        return normalized, RESERVOIR_BY_BASIN[normalized]
    if selector.startswith(RESERVOIR_ID_PREFIX):
        reservoir_name = selector[len(RESERVOIR_ID_PREFIX) :].strip()
        if reservoir_name:
            return None, reservoir_name
    raise ValueError(f"Lưu vực không hợp lệ: {basin_id}")


def model_basin_for_reservoir(reservoir_name: str) -> str | None:
    folded = reservoir_name.casefold()
    for basin_id, known_name in RESERVOIR_BY_BASIN.items():
        if known_name.casefold() == folded:
            return basin_id
    return None


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


def reservoir_filter(reservoir_name: str | None) -> tuple[str, list[str]]:
    if reservoir_name is None:
        return "1 = 1", []
    return f"{CLEAN_NAME_SQL} = ? COLLATE NOCASE", [reservoir_name]


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
    model_basin_id, reservoir_name = resolve_basin_selector(basin_id)
    start, end = normalize_range(start_date, end_date, latest_history_date(connection))
    filter_sql, filter_params = reservoir_filter(reservoir_name)
    end_exclusive = end + timedelta(days=1)
    sql = f"""
        WITH ranked AS (
            SELECT
                id,
                observed_at,
                requested_at_last_seen,
                {CLEAN_NAME_SQL} AS Reservoir_Name,
                COALESCE({basin_case_sql()}, {CLEAN_NAME_SQL}) AS Basin_ID,
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
        "model_basin_id": model_basin_id,
        "reservoir_name": reservoir_name,
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
    requested_basin_id = basin_id
    model_basin_id, reservoir_name = resolve_basin_selector(basin_id)
    if basin_id is not None and model_basin_id is None:
        return {
            "rows": [],
            "basin_id": requested_basin_id,
            "model_basin_id": None,
            "reservoir_name": reservoir_name,
            "limit": limit,
            "truncated": False,
        }
    bounds = table_date_bounds(connection, "hydroai_forecast_daily", "Date", model_basin_id)
    if bounds is None:
        return {"rows": [], "limit": limit, "truncated": False}
    start, end = normalize_range(start_date, end_date, bounds[1], default_days=366)
    where = ['"Date" >= ?', '"Date" <= ?']
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if model_basin_id:
        where.append('"Basin_ID" = ?')
        params.append(model_basin_id)
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
        "basin_id": requested_basin_id,
        "model_basin_id": model_basin_id,
        "reservoir_name": reservoir_name,
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
    requested_basin_id = basin_id
    model_basin_id, reservoir_name = resolve_basin_selector(basin_id)
    if basin_id is not None and model_basin_id is None:
        return {
            "actual_rows": [],
            "forecast_rows": [],
            "basin_id": requested_basin_id,
            "model_basin_id": None,
            "reservoir_name": reservoir_name,
            "limit": limit,
            "truncated": False,
        }
    bounds = table_date_bounds(connection, "pmis_generation_daily", "Date", model_basin_id)
    if bounds is None:
        return {"actual_rows": [], "forecast_rows": [], "limit": limit, "truncated": False}
    start, end = normalize_range(start_date, end_date, bounds[1], default_days=366)
    where = ['"Date" >= ?', '"Date" <= ?']
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if model_basin_id:
        where.append('"Basin_ID" = ?')
        params.append(model_basin_id)
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
        "basin_id": requested_basin_id,
        "model_basin_id": model_basin_id,
        "reservoir_name": reservoir_name,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "limit": limit,
        "truncated": actual_truncated or forecast_truncated,
    }


def create_app():
    try:
        from fastapi import Body, Depends, FastAPI, HTTPException, Query, status
        from fastapi.security import APIKeyHeader
    except ImportError as exc:
        raise ImportError("Chưa có FastAPI. Hãy cài requirements.txt") from exc

    app = FastAPI(title="HydroAI data API", version="6.0")
    api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)

    def require_api_key(api_key: str | None = Depends(api_key_header)) -> None:
        expected = configured_api_key()
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Máy chủ chưa cấu hình {API_KEY_ENV} hoặc file .hydroai_api_key",
            )
        if not api_key or not secrets.compare_digest(api_key, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key không hợp lệ",
            )

    def checked_basin(basin_id: str | None) -> str | None:
        try:
            resolve_basin_selector(basin_id)
            return basin_id.strip() if basin_id is not None else None
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

    @app.get("/hydrology-uploads", dependencies=[Depends(require_api_key)])
    def hydrology_uploads() -> dict[str, Any]:
        try:
            return load_hydrology_uploads()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Không đọc được dữ liệu thủy văn đã lưu: {exc}",
            ) from exc

    @app.post("/hydrology-uploads", dependencies=[Depends(require_api_key)])
    def persist_hydrology_uploads(
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        try:
            return save_hydrology_uploads(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Không lưu được dữ liệu thủy văn: {exc}",
            ) from exc

    @app.get("/basins", dependencies=[Depends(require_api_key)])
    def basins() -> dict[str, Any]:
        def load(connection: sqlite3.Connection) -> dict[str, Any]:
            rows = connection.execute(
                f"""
                SELECT
                    MIN({CLEAN_NAME_SQL}) AS Reservoir_Name,
                    MIN(observed_at) AS Min_Date,
                    MAX(observed_at) AS Max_Date
                FROM reservoir_readings
                WHERE {CLEAN_NAME_SQL} <> ''
                GROUP BY {CLEAN_NAME_SQL} COLLATE NOCASE
                ORDER BY Reservoir_Name COLLATE NOCASE
                """
            ).fetchall()
            merged_rows: dict[str, dict[str, str]] = {}
            for row in rows:
                raw_name = str(row["Reservoir_Name"])
                display_name = RESERVOIR_DISPLAY_ALIASES.get(raw_name, raw_name)
                key = display_name.casefold()
                current = merged_rows.get(key)
                min_date = str(row["Min_Date"])
                max_date = str(row["Max_Date"])
                if current is None:
                    merged_rows[key] = {
                        "reservoir_name": display_name,
                        "min_date": min_date,
                        "max_date": max_date,
                    }
                else:
                    current["min_date"] = min(current["min_date"], min_date)
                    current["max_date"] = max(current["max_date"], max_date)
            items = []
            for merged in sorted(
                merged_rows.values(), key=lambda item: item["reservoir_name"].casefold()
            ):
                reservoir_name = merged["reservoir_name"]
                model_basin_id = model_basin_for_reservoir(reservoir_name)
                basin_id = model_basin_id or f"{RESERVOIR_ID_PREFIX}{reservoir_name}"
                forecast_bounds = table_date_bounds(
                    connection,
                    "hydroai_forecast_daily",
                    "Date",
                    model_basin_id,
                )
                items.append(
                    {
                        "id": basin_id,
                        "name": BASIN_NAMES.get(model_basin_id, reservoir_name),
                        "reservoir_name": reservoir_name,
                        "model_basin_id": model_basin_id,
                        "min_date": merged["min_date"][:10],
                        "max_date": merged["max_date"][:10],
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
