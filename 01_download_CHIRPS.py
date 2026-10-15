from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

from config import CHIRPS_URL_TEMPLATE, DAILY_DIR, END_YEAR, START_YEAR, ensure_project_dirs


def chirps_file_for_year(year: int) -> Path:
    return DAILY_DIR / f"chirps-v2.0.{year}.days_p05.nc"


def download_one_year(year: int, force: bool = False) -> Path:
    output_file = chirps_file_for_year(year)
    if output_file.exists() and output_file.stat().st_size > 0 and not force:
        print(f"CHIRPS {year} da ton tai: {output_file}")
        return output_file

    url = CHIRPS_URL_TEMPLATE.format(year=year)
    temp_file = output_file.with_suffix(output_file.suffix + ".download")
    print(f"Dang tai CHIRPS {year}: {url}")
    urllib.request.urlretrieve(url, temp_file)
    temp_file.replace(output_file)
    print(f"Da luu: {output_file}")
    return output_file


def download_chirps(start_year: int = START_YEAR, end_year: int = END_YEAR, force: bool = False) -> list[Path]:
    ensure_project_dirs()
    if start_year > end_year:
        raise ValueError("start_year phai nho hon hoac bang end_year.")
    return [download_one_year(year, force=force) for year in range(start_year, end_year + 1)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Tai du lieu mua CHIRPS Daily NetCDF cho HydroAI v5.")
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--end-year", type=int, default=END_YEAR)
    parser.add_argument("--force", action="store_true", help="Tai lai ke ca khi file da ton tai.")
    args = parser.parse_args()
    download_chirps(args.start_year, args.end_year, args.force)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\nCHUONG TRINH DUNG DO LOI:")
        print(exc)
        sys.exit(1)
