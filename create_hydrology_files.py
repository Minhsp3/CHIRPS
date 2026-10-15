from pathlib import Path
import pandas as pd

output_dir = Path(r"D:\VScode\CHIRPS\hydrology")
output_dir.mkdir(parents=True, exist_ok=True)

columns = [
    "Date",
    "Q_in_m3s",
    "WaterLevel_m",
    "Storage_Mm3",
    "Generation_MWh",
    "Q_turbine_m3s",
    "Q_spill_m3s",
    "Q_env_m3s",
]

for file_name in [
    "BTS_daily.xlsx",
    "BK_daily.xlsx",
    "SP3_daily.xlsx",
]:
    output_file = output_dir / file_name
    pd.DataFrame(columns=columns).to_excel(output_file, index=False)
    print(f"Đã tạo: {output_file}")