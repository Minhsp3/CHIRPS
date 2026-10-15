from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from config import API_HOST, API_PORT, BASIN_NAMES, FORECAST_FILE, MODEL_DIR, OUTPUT_DIR


def load_forecast() -> pd.DataFrame:
    if FORECAST_FILE.exists():
        return pd.read_excel(FORECAST_FILE, parse_dates=["Date"])
    frames = []
    for basin_id in BASIN_NAMES:
        file = OUTPUT_DIR / f"Forecast_{basin_id}.csv"
        if file.exists():
            frames.append(pd.read_csv(file, parse_dates=["Date"]))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def create_app():
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:
        raise ImportError("Chua co FastAPI. Hay chay: pip install fastapi uvicorn") from exc

    app = FastAPI(title="HydroAI v5 API", version="5.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "name": "HydroAI v5"}

    @app.get("/basins")
    def basins() -> dict[str, object]:
        return {"basins": [{"id": basin_id, "name": name} for basin_id, name in BASIN_NAMES.items()]}

    @app.get("/metrics/{basin_id}")
    def metrics(basin_id: str) -> dict[str, object]:
        basin_id = basin_id.upper()
        if basin_id not in BASIN_NAMES:
            raise HTTPException(status_code=404, detail="Unknown basin")
        file = MODEL_DIR / f"metrics_{basin_id}.json"
        if not file.exists():
            raise HTTPException(status_code=404, detail="Metrics not found")
        with open(file, encoding="utf-8") as handle:
            return json.load(handle)

    @app.get("/forecast")
    def forecast(basin_id: str | None = None, limit: int = 30) -> dict[str, object]:
        df = load_forecast()
        if df.empty:
            raise HTTPException(status_code=404, detail="Forecast not found")
        if basin_id:
            basin_id = basin_id.upper()
            df = df[df["Basin_ID"] == basin_id]
        df = df.sort_values("Date").tail(max(1, min(limit, 500))).copy()
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        return {"rows": df.to_dict(orient="records")}

    return app


app = create_app()


if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError as exc:
        raise ImportError("Chua co Uvicorn. Hay chay: pip install uvicorn") from exc

    uvicorn.run(app, host=API_HOST, port=API_PORT, reload=False)
