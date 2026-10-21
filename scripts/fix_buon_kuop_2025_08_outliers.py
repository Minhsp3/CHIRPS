"""Correct two decimal-place water-level errors for Buon Kuop in August 2025."""

from __future__ import annotations

import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parents[1] / "PMIS" / "evn_hochua_manual.db"
CORRECTIONS = {
    1547336: (41175.0, 411.75),
    1941743: (41175.0, 411.75),
    1550222: (41033.0, 410.33),
    1944629: (41033.0, 410.33),
}
METHOD = "Decimal-place correction validated against adjacent hourly readings"


def main() -> None:
    connection = sqlite3.connect(DB_PATH)
    try:
        with connection:
            for record_id, (old_value, new_value) in CORRECTIONS.items():
                row = connection.execute(
                    "SELECT h_tl FROM reservoir_readings WHERE id = ?",
                    (record_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"Missing reservoir_readings id={record_id}")

                current_value = float(row[0])
                if current_value == new_value:
                    continue
                if current_value != old_value:
                    raise RuntimeError(
                        f"Unexpected h_tl for id={record_id}: {current_value}"
                    )

                connection.execute(
                    "UPDATE reservoir_readings SET h_tl = ? WHERE id = ?",
                    (new_value, record_id),
                )
                connection.execute(
                    """
                    INSERT INTO data_quality_corrections
                        (
                            table_name,
                            record_id,
                            field_name,
                            old_value,
                            new_value,
                            method,
                            corrected_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                    """,
                    (
                        "reservoir_readings",
                        record_id,
                        "h_tl",
                        old_value,
                        new_value,
                        METHOD,
                    ),
                )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
