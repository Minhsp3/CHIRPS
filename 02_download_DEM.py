from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import DEM_BOUNDS, DEM_FILE, ensure_project_dirs


def download_dem(output_file: Path = DEM_FILE, force: bool = False) -> Path:
    ensure_project_dirs()
    if output_file.exists() and output_file.stat().st_size > 0 and not force:
        print(f"DEM da ton tai: {output_file}")
        return output_file

    try:
        import elevation
    except ImportError as exc:
        raise ImportError("Chua co thu vien elevation. Hay chay: pip install elevation") from exc

    print(f"Dang tai SRTM theo bounds {DEM_BOUNDS}...")
    elevation.clip(bounds=DEM_BOUNDS, output=str(output_file))
    elevation.clean()

    if not output_file.exists() or output_file.stat().st_size == 0:
        raise RuntimeError("Khong tai duoc DEM.")

    print(f"Da luu DEM: {output_file}")
    return output_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Tai DEM SRTM cho HydroAI v5.")
    parser.add_argument("--force", action="store_true", help="Tai lai ke ca khi DEM da ton tai.")
    args = parser.parse_args()
    download_dem(force=args.force)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\nCHUONG TRINH DUNG DO LOI:")
        print(exc)
        sys.exit(1)
