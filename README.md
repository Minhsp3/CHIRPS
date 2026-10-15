# HydroAI v5

Pipeline du bao luu luong ngay cho cac luu vuc BTS, BK va SP3 tu CHIRPS, DEM, PMIS va AI.

## Cau truc

```text
HydroAI v5
|-- 01_download_CHIRPS.py
|-- 02_download_DEM.py
|-- 03_create_basin.py
|-- 04_import_PMIS.py
|-- 05_build_dataset.py
|-- 06_feature_engineering.py
|-- 07_train_AI.py
|-- 08_forecast.py
|-- 09_dashboard.py
|-- 10_api.py
|-- hydroai.py
|-- HydroAI.exe
`-- _internal/
```

## Cai dat

```powershell
cd D:\VScode\CHIRPS
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Chay pipeline

In danh sach buoc:

```powershell
python hydroai.py --list
```

Chay toan bo pipeline du lieu va AI tu buoc 1 den buoc 8:

```powershell
python hydroai.py --start-step 1 --end-step 8
```

Chay rieng mot buoc:

```powershell
python hydroai.py --only 4
python hydroai.py --only 6
python hydroai.py --only 8
```

Mo dashboard:

```powershell
streamlit run 09_dashboard.py
```

Mo dashboard ho chua EVN/HydroAI:

```powershell
python hochua.py
```

Dashboard ho chua mac dinh dung database `PMIS\evn_hochua_manual.db` va chay tai `http://127.0.0.1:10005`.

Mo API:

```powershell
python 10_api.py
```

API mac dinh chay tai `http://127.0.0.1:8000`.

## Build HydroAI.exe

```powershell
pyinstaller --onedir --name HydroAI hydroai.py
Copy-Item .\dist\HydroAI\HydroAI.exe .\HydroAI.exe -Force
Copy-Item .\dist\HydroAI\_internal .\_internal -Recurse -Force
```

## Luu y

- Copy file Excel/CSV PMIS vao thu muc `PMIS` truoc khi chay buoc 4.
- Neu co `PMIS\evn_hochua_manual.db`, buoc 4 se uu tien doc bang `reservoir_readings` de tao `hydrology\*_daily.csv`.
- Buoc 6 se luu `hydroai_dataset_daily` va `hydroai_features_daily` vao database ho chua.
- Buoc 8 se luu `hydroai_forecast_daily` vao database ho chua de dashboard `hochua.py` ve du bao AI.
- Sau buoc 3, nen mo shapefile trong `luu_vuc` bang QGIS de kiem tra ranh gioi.
- Buoc 7 can cot muc tieu `Q_in_m3s` trong du lieu hydrology/PMIS.
- P10/P90 hien la khoang so bo dua tren RMSE backtest, khong phai du bao xac suat da hieu chinh day du.
