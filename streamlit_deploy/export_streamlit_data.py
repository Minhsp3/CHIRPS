from __future__ import annotations

import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "PMIS" / "evn_hochua_manual.db"
OUTPUT_DB = Path(__file__).resolve().parent / "hydroai_streamlit.db"


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def main() -> None:
    if not SOURCE_DB.exists():
        raise FileNotFoundError(f"Không tìm thấy database nguồn: {SOURCE_DB}")
    OUTPUT_DB.unlink(missing_ok=True)

    with sqlite3.connect(OUTPUT_DB) as target:
        target.execute("ATTACH DATABASE ? AS source", (str(SOURCE_DB),))
        target.executescript(
            """
            CREATE TABLE daily_readings AS
            WITH normalized AS (
                SELECT
                    date(observed_at) AS Date,
                    trim(CASE
                        WHEN instr(reservoir_name, 'Đồng bộ') > 0
                            THEN substr(reservoir_name, 1, instr(reservoir_name, 'Đồng bộ') - 1)
                        ELSE reservoir_name
                    END) AS Reservoir_Name,
                    h_tl, h_dbt, h_c, q_ve, q_xt, q_xm
                FROM source.reservoir_readings
                WHERE observed_at IS NOT NULL
            )
            SELECT
                Date,
                Reservoir_Name,
                AVG(h_tl) AS WaterLevel_m,
                AVG(h_dbt) AS MNDBT_m,
                AVG(h_c) AS MNC_m,
                AVG(q_ve) AS Q_in_m3s,
                AVG(q_xt) AS Q_spill_m3s,
                AVG(q_xm) AS Q_turbine_m3s
            FROM normalized
            WHERE Date IS NOT NULL AND Reservoir_Name <> ''
            GROUP BY Date, Reservoir_Name;

            CREATE UNIQUE INDEX idx_daily_readings_name_date
                ON daily_readings(Reservoir_Name, Date);
            CREATE INDEX idx_daily_readings_date ON daily_readings(Date);
            """
        )

        copy_tables = [
            "hydroai_forecast_daily",
            "hydroai_generation_forecast_daily",
            "enso_hydrology_scenario_monthly",
            "pmis_generation_daily",
        ]
        for table_name in copy_tables:
            if not table_exists(target, f"source.{table_name}"):
                # sqlite_master does not resolve attached schema through the helper.
                exists = target.execute(
                    "SELECT 1 FROM source.sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                ).fetchone()
                if not exists:
                    continue
            target.execute(f'CREATE TABLE "{table_name}" AS SELECT * FROM source."{table_name}"')

        target.execute(
            "CREATE INDEX IF NOT EXISTS idx_forecast_basin_date "
            "ON hydroai_forecast_daily(Basin_ID, Date)"
        )
        target.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_date "
            "ON pmis_generation_daily(Date, Basin_ID)"
        )
        target.execute("DETACH DATABASE source")
        target.execute("VACUUM")

    size_mb = OUTPUT_DB.stat().st_size / 1024 / 1024
    print(f"Đã tạo {OUTPUT_DB} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
