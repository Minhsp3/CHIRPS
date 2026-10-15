from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from config import BASE_DIR, ensure_project_dirs

STEPS = [
    ("01_download_CHIRPS.py", "Tai du lieu mua CHIRPS Daily"),
    ("02_download_DEM.py", "Tai DEM SRTM"),
    ("03_create_basin.py", "Phan dinh luu vuc"),
    ("04_import_PMIS.py", "Import va chuan hoa PMIS"),
    ("05_build_dataset.py", "Xay dung dataset tho"),
    ("06_feature_engineering.py", "Tao dac trung AI"),
    ("07_train_AI.py", "Huan luyen mo hinh AI"),
    ("08_forecast.py", "Du bao luu luong"),
    ("09_dashboard.py", "Dashboard HydroAI"),
    ("10_api.py", "API HydroAI"),
]


def python_executable() -> str:
    if getattr(sys, "frozen", False):
        venv_python = BASE_DIR / ".venv" / "Scripts" / "python.exe"
        return str(venv_python) if venv_python.exists() else "python"
    return sys.executable


def run_script(script_name: str, extra_args: list[str] | None = None) -> None:
    script = BASE_DIR / script_name
    if not script.exists():
        raise FileNotFoundError(f"Khong tim thay script: {script}")
    command = [python_executable(), str(script)]
    if extra_args:
        command.extend(extra_args)
    subprocess.run(command, check=True, cwd=script.parent)


def run_step(step_number: int) -> None:
    script_name, description = STEPS[step_number - 1]
    print("\n" + "=" * 78, flush=True)
    print(f"BUOC {step_number:02d}: {description}", flush=True)
    print("=" * 78, flush=True)
    run_script(script_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="HydroAI v5 pipeline runner.")
    parser.add_argument("--only", type=int, choices=range(1, 11), help="Chi chay mot buoc.")
    parser.add_argument("--start-step", type=int, default=1, choices=range(1, 11))
    parser.add_argument("--end-step", type=int, default=8, choices=range(1, 11))
    parser.add_argument("--list", action="store_true", help="In danh sach cac buoc.")
    args = parser.parse_args()

    ensure_project_dirs()

    if args.list:
        for index, (script_name, description) in enumerate(STEPS, start=1):
            print(f"{index:02d}. {script_name} - {description}", flush=True)
        return

    if args.only:
        run_step(args.only)
        return

    if args.start_step > args.end_step:
        raise ValueError("start-step phai nho hon hoac bang end-step.")

    for step_number in range(args.start_step, args.end_step + 1):
        run_step(step_number)

    print("\nHOAN THANH HYDROAI v5", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\nCHUONG TRINH DUNG DO LOI:", flush=True)
        print(exc, flush=True)
        sys.exit(1)
