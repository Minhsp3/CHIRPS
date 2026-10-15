import argparse
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

import requests
try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError:
    BeautifulSoup = None


DEFAULT_SOURCE_URL = "https://hochuathuydien.evn.com.vn/PageHoChuaThuyDienEmbedEVN.aspx"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

# VSCode manual run:
# 1. Update start/end/db/sql below.
# 2. Press Run in VSCode.
# 3. If you want CLI args again, set "enabled" to False.
MANUAL_RUN_CONFIG = {
    "enabled": True,
    "source_url": DEFAULT_SOURCE_URL,
    "start": "2026-05-19 00:00",
    "end": "2026-05-28 16:00",
    "step_hours": 1,
    "sleep_seconds": 0,
    "batch_size": 20,
    "limit": None,
    "timeout": 30,
    "resume": True,
    "force_start": True,
    "skip_existing": False,
    "overwrite_existing": True,
    "db_path": "output/evn_hochua_manual.db",
    "sql_path": "output/evn_hochua_manual.sql",
}


@dataclass
class Reading:
    requested_at: datetime
    observed_at: datetime
    region: str | None
    reservoir_name: str
    reservoir_name_raw: str
    raw_time_label: str
    sync_hint: str | None
    h_tl: float | None
    h_dbt: float | None
    h_c: float | None
    q_ve: float | None
    sum_qx: float | None
    q_xt: float | None
    q_xm: float | None
    n_cxs: int | None
    n_cxm: int | None
    source_url: str
    fetched_at: datetime


class SimpleTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_table = False
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.current_cell = []
        self.current_row = []
        self.rows = []
        self._table_found = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table" and not self._table_found:
            self.in_table = True
            self._table_found = True
            self.table_depth = 1
            return
        if self.in_table and tag == "table":
            self.table_depth += 1
        if not self.in_table:
            return
        if tag == "tr":
            self.in_row = True
            self.current_row = []
        elif tag in ("td", "th") and self.in_row:
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if not self.in_table:
            return
        if tag in ("td", "th") and self.in_cell:
            self.current_row.append("".join(self.current_cell))
            self.current_cell = []
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            self.rows.append(self.current_row)
            self.current_row = []
            self.in_row = False
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_table = False

    def handle_data(self, data):
        if self.in_table and self.in_row and self.in_cell:
            self.current_cell.append(data)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill ho chua EVN tu 2020 den hien tai va luu vao SQL database."
    )
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL, help="Nguon EVN can crawl.")
    parser.add_argument("--start", default="2026-04-06 00:00", help="YYYY-MM-DD HH:MM")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD HH:MM")
    parser.add_argument("--step-hours", type=int, default=1, help="Buoc nhay theo gio.")
    parser.add_argument("--sleep-seconds", type=float, default=0.1, help="Thoi gian nghi giua cac request.")
    parser.add_argument("--batch-size", type=int, default=200, help="So request commit mot lan.")
    parser.add_argument("--limit", type=int, default=None, help="Gioi han so moc de test nhanh.")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout moi request.")
    parser.add_argument("--resume", action="store_true", help="Tiep tuc tu moc da crawl cuoi cung.")
    parser.add_argument("--force-start", action="store_true", help="Luon dung moc start ban nhap, khong nhay toi moc resume.")
    parser.add_argument("--skip-existing", action="store_true", help="Bo qua cac moc da ton tai trong DB de backfill nhanh hon.")
    parser.add_argument("--overwrite-existing", action="store_true", help="Neu moc da ton tai thi xoa snapshot cu va ghi de bang du lieu moi.")
    parser.add_argument("--db-path", default="output/evn_hochua.db", help="File DB dau ra.")
    parser.add_argument("--sql-path", default="output/evn_hochua.sql", help="File dump SQL dau ra.")
    return parser.parse_args()


def should_use_manual_config():
    if not MANUAL_RUN_CONFIG.get("enabled"):
        return False
    import sys

    return len(sys.argv) == 1


def build_manual_args():
    config = MANUAL_RUN_CONFIG
    print("Running with MANUAL_RUN_CONFIG")
    print(f"  start   : {config['start']}")
    print(f"  end     : {config['end']}")
    print(f"  db_path : {config['db_path']}")
    print(f"  sql_path: {config['sql_path']}")
    return argparse.Namespace(
        source_url=config["source_url"],
        start=config["start"],
        end=config["end"],
        step_hours=config["step_hours"],
        sleep_seconds=config["sleep_seconds"],
        batch_size=config["batch_size"],
        limit=config["limit"],
        timeout=config["timeout"],
        resume=config["resume"],
        force_start=config.get("force_start", False),
        skip_existing=config.get("skip_existing", False),
        overwrite_existing=config.get("overwrite_existing", False),
        db_path=config["db_path"],
        sql_path=config["sql_path"],
    )


def parse_cli_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


def format_source_datetime(value: datetime) -> str:
    return value.strftime("%d/%m/%Y %H:%M")


def to_db_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M:%S")


def normalize_spaces(value: str) -> str:
    return " ".join((value or "").split())


def parse_name_and_sync(raw_name: str) -> tuple[str, str | None]:
    normalized = normalize_spaces(raw_name)
    marker = "Dong bo luc:"
    normalized_ascii = normalized.replace("Đ", "D").replace("đ", "d")
    if marker not in normalized_ascii:
        return normalized, None
    index = normalized_ascii.index(marker)
    name = normalized[:index].strip()
    sync_hint = normalized[index + len(marker):].strip()
    return name, sync_hint or None


def parse_float(value: str) -> float | None:
    text = normalize_spaces(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: str) -> int | None:
    number = parse_float(value)
    if number is None:
        return None
    return int(number)


def infer_observed_at(requested_at: datetime, raw_value: str) -> datetime:
    day_part, time_part = raw_value.split()
    day_text, month_text = day_part.split("/")
    hour_text, minute_text = time_part.split(":")
    day = int(day_text)
    month = int(month_text)
    hour = int(hour_text)
    minute = int(minute_text)

    candidates = []
    for year in (requested_at.year - 1, requested_at.year, requested_at.year + 1):
        try:
            candidates.append(datetime(year, month, day, hour, minute))
        except ValueError:
            continue

    if not candidates:
        raise ValueError(f"Cannot infer observed_at from {raw_value}")

    return min(
        candidates,
        key=lambda item: (
            1 if item > requested_at else 0,
            abs((requested_at - item).total_seconds()),
        ),
    )


def iter_hours(start: datetime, end: datetime, step_hours: int) -> Iterable[datetime]:
    current = start
    step = timedelta(hours=step_hours)
    while current <= end:
        yield current
        current += step


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def build_request_url(source_url: str, requested_at: datetime) -> str:
    params = {
        "td": format_source_datetime(requested_at),
        "vm": "",
        "lv": "",
        "hc": "",
    }
    return f"{source_url}?{urlencode(params)}"


def extract_table_rows(html_text: str) -> list[list[str]]:
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_text, "lxml")
        table = soup.find("table")
        if table is None:
            return []
        return [
            [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            for row in table.find_all("tr")
        ]

    parser = SimpleTableParser()
    parser.feed(html_text)
    return parser.rows


def fetch_snapshot(session: requests.Session, source_url: str, requested_at: datetime, timeout: int) -> list[Reading]:
    params = {
        "td": format_source_datetime(requested_at),
        "vm": "",
        "lv": "",
        "hc": "",
    }
    response = session.get(source_url, params=params, timeout=timeout)
    response.raise_for_status()

    html_text = response.content.decode("utf-8", errors="replace")
    rows = extract_table_rows(html_text)
    if not rows:
        raise ValueError("Khong tim thay bang du lieu trong HTML tra ve.")
    if len(rows) < 3:
        raise ValueError("Bang du lieu khong du so dong de parse.")

    fetched_at = datetime.now()
    readings: list[Reading] = []
    current_region: str | None = None

    for row in rows[2:]:
        columns = [normalize_spaces(cell) for cell in row]
        non_empty = [item for item in columns if item]
        if not non_empty:
            continue
        if len(non_empty) == 1 and "/" not in non_empty[0]:
            current_region = non_empty[0]
            continue
        if len(columns) < 11 or "/" not in columns[1]:
            continue

        reservoir_name, sync_hint = parse_name_and_sync(columns[0])
        raw_time_label = columns[1]
        observed_at = infer_observed_at(requested_at, raw_time_label)

        readings.append(
            Reading(
                requested_at=requested_at,
                observed_at=observed_at,
                region=current_region,
                reservoir_name=reservoir_name,
                reservoir_name_raw=columns[0],
                raw_time_label=raw_time_label,
                sync_hint=sync_hint,
                h_tl=parse_float(columns[2]),
                h_dbt=parse_float(columns[3]),
                h_c=parse_float(columns[4]),
                q_ve=parse_float(columns[5]),
                sum_qx=parse_float(columns[6]),
                q_xt=parse_float(columns[7]),
                q_xm=parse_float(columns[8]),
                n_cxs=parse_int(columns[9]),
                n_cxm=parse_int(columns[10]),
                source_url=response.url,
                fetched_at=fetched_at,
            )
        )

    return readings


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS crawl_requests (
            requested_at TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            reading_count INTEGER NOT NULL,
            observed_min_at TEXT,
            observed_max_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS reservoir_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            requested_at_first_seen TEXT NOT NULL,
            requested_at_last_seen TEXT NOT NULL,
            region TEXT,
            reservoir_name TEXT NOT NULL,
            reservoir_name_raw TEXT NOT NULL,
            raw_time_label TEXT NOT NULL,
            sync_hint TEXT,
            h_tl REAL,
            h_dbt REAL,
            h_c REAL,
            q_ve REAL,
            sum_qx REAL,
            q_xt REAL,
            q_xm REAL,
            n_cxs INTEGER,
            n_cxm INTEGER,
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (observed_at, reservoir_name)
        );

        CREATE INDEX IF NOT EXISTS idx_reservoir_readings_reservoir
        ON reservoir_readings (reservoir_name, observed_at);

        CREATE INDEX IF NOT EXISTS idx_reservoir_readings_requested_last_seen
        ON reservoir_readings (requested_at_last_seen);
        """
    )


def get_resume_start(conn: sqlite3.Connection, fallback_start: datetime, step_hours: int) -> datetime:
    row = conn.execute("SELECT MAX(requested_at) FROM crawl_requests").fetchone()
    if not row or not row[0]:
        return fallback_start
    return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S") + timedelta(hours=step_hours)


def get_existing_requested_at(conn: sqlite3.Connection, start: datetime, end: datetime) -> set[str]:
    rows = conn.execute(
        """
        SELECT requested_at
        FROM crawl_requests
        WHERE requested_at BETWEEN ? AND ?
        """,
        (to_db_datetime(start), to_db_datetime(end)),
    ).fetchall()
    return {row[0] for row in rows if row and row[0]}


def save_snapshot(conn: sqlite3.Connection, requested_at: datetime, readings: list[Reading], source_url: str):
    requested_at_db = to_db_datetime(requested_at)
    observed_values = [to_db_datetime(item.observed_at) for item in readings]
    fetched_at_db = to_db_datetime(readings[0].fetched_at if readings else datetime.now())

    conn.execute(
        """
        INSERT INTO crawl_requests (
            requested_at, source_url, fetched_at, reading_count, observed_min_at, observed_max_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(requested_at) DO UPDATE SET
            source_url = excluded.source_url,
            fetched_at = excluded.fetched_at,
            reading_count = excluded.reading_count,
            observed_min_at = excluded.observed_min_at,
            observed_max_at = excluded.observed_max_at
        """,
        (
            requested_at_db,
            readings[0].source_url if readings else build_request_url(source_url, requested_at),
            fetched_at_db,
            len(readings),
            min(observed_values) if observed_values else None,
            max(observed_values) if observed_values else None,
        ),
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
        ON CONFLICT(observed_at, reservoir_name) DO UPDATE SET
            requested_at_last_seen = excluded.requested_at_last_seen,
            region = excluded.region,
            reservoir_name_raw = excluded.reservoir_name_raw,
            raw_time_label = excluded.raw_time_label,
            sync_hint = excluded.sync_hint,
            h_tl = excluded.h_tl,
            h_dbt = excluded.h_dbt,
            h_c = excluded.h_c,
            q_ve = excluded.q_ve,
            sum_qx = excluded.sum_qx,
            q_xt = excluded.q_xt,
            q_xm = excluded.q_xm,
            n_cxs = excluded.n_cxs,
            n_cxm = excluded.n_cxm,
            source_url = excluded.source_url,
            fetched_at = excluded.fetched_at,
            updated_at = CURRENT_TIMESTAMP
        """,
        [
            (
                to_db_datetime(item.observed_at),
                requested_at_db,
                requested_at_db,
                item.region,
                item.reservoir_name,
                item.reservoir_name_raw,
                item.raw_time_label,
                item.sync_hint,
                item.h_tl,
                item.h_dbt,
                item.h_c,
                item.q_ve,
                item.sum_qx,
                item.q_xt,
                item.q_xm,
                item.n_cxs,
                item.n_cxm,
                item.source_url,
                to_db_datetime(item.fetched_at),
            )
            for item in readings
        ],
    )


def export_sql_dump(conn: sqlite3.Connection, sql_path: Path):
    sql_path.parent.mkdir(parents=True, exist_ok=True)
    with sql_path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in conn.iterdump():
            handle.write(f"{line}\n")


def main():
    args = build_manual_args() if should_use_manual_config() else parse_args()
    start = parse_cli_datetime(args.start)
    end = parse_cli_datetime(args.end) if args.end else datetime.now().replace(second=0, microsecond=0)
    if args.step_hours <= 0:
        raise SystemExit("--step-hours phai lon hon 0.")
    if args.batch_size <= 0:
        raise SystemExit("--batch-size phai lon hon 0.")
    if start > end:
        raise SystemExit("Thoi gian bat dau phai nho hon hoac bang thoi gian ket thuc.")

    db_path = Path(args.db_path)
    sql_path = Path(args.sql_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)
        if args.resume and not args.force_start:
            start = get_resume_start(conn, start, args.step_hours)
            print(f"Resume tu: {start:%Y-%m-%d %H:%M}")
        elif args.resume and args.force_start:
            print(f"Force start tai: {start:%Y-%m-%d %H:%M}")

        if args.skip_existing and args.overwrite_existing:
            raise SystemExit("Khong the bat ca skip_existing va overwrite_existing cung luc.")

        existing_requested_at = set()
        if args.skip_existing or args.overwrite_existing:
            existing_requested_at = get_existing_requested_at(conn, start, end)
            if args.skip_existing:
                print(f"Co san {len(existing_requested_at)} moc trong DB se duoc bo qua.")
            elif args.overwrite_existing:
                print(f"Co san {len(existing_requested_at)} moc trong DB co the bi ghi de.")

        session = create_session()
        total_points = 0
        total_readings = 0
        skipped_points = 0
        overwritten_points = 0
        failed_points: list[tuple[datetime, str]] = []

        try:
            for index, requested_at in enumerate(iter_hours(start, end, args.step_hours), start=1):
                if args.limit and index > args.limit:
                    break
                requested_at_db = to_db_datetime(requested_at)
                if args.skip_existing and requested_at_db in existing_requested_at:
                    skipped_points += 1
                    if skipped_points <= 20 or skipped_points % 500 == 0:
                        print(f"[SKIP] {requested_at:%Y-%m-%d %H:%M} da co trong DB")
                    continue
                try:
                    readings = fetch_snapshot(session, args.source_url, requested_at, args.timeout)
                    if args.overwrite_existing and requested_at_db in existing_requested_at:
                        overwritten_points += 1
                        if overwritten_points <= 20 or overwritten_points % 500 == 0:
                            print(f"[OVERWRITE] {requested_at:%Y-%m-%d %H:%M} ghi de moc da co")
                    save_snapshot(conn, requested_at, readings, args.source_url)
                    total_points += 1
                    total_readings += len(readings)
                    if total_points % args.batch_size == 0:
                        conn.commit()
                    print(f"[OK] {requested_at:%Y-%m-%d %H:%M} -> {len(readings)} rows")
                except Exception as exc:
                    failed_points.append((requested_at, str(exc)))
                    print(f"[ERR] {requested_at:%Y-%m-%d %H:%M} -> {exc}")
                time.sleep(args.sleep_seconds)
        finally:
            session.close()

        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        export_sql_dump(conn, sql_path)

    print()
    print(f"DB file : {db_path.resolve()}")
    print(f"SQL file: {sql_path.resolve()}")
    print(f"Da crawl: {total_points} moc thoi gian")
    print(f"Da bo qua: {skipped_points} moc da ton tai")
    print(f"Da ghi de: {overwritten_points} moc da ton tai")
    print(f"Tong dong: {total_readings}")
    print(f"Loi: {len(failed_points)}")
    if failed_points:
        print("Mot vai loi dau tien:")
        for requested_at, message in failed_points[:20]:
            print(f"  - {requested_at:%Y-%m-%d %H:%M}: {message}")


if __name__ == "__main__":
    main()
