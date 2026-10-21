# HydroAI Streamlit

Dashboard này đọc dữ liệu trực tiếp từ HydroAI API. Không cần export hoặc commit
database SQLite lên GitHub khi dữ liệu trên máy chủ thay đổi.

## Chạy tại máy

```powershell
cd D:\VScode\CHIRPS
$env:HYDROAI_API_URL = "http://127.0.0.1:8000"
$env:HYDROAI_API_KEY = "cung-khoa-voi-api-server"
.\.venv\Scripts\streamlit.exe run streamlit_deploy\streamlit_app.py
```

## Đưa lên GitHub và Streamlit Community Cloud

1. Commit thư mục `streamlit_deploy` lên GitHub.
2. Trên Streamlit Community Cloud, chọn repository và đặt **Main file path** là:
   `streamlit_deploy/streamlit_app.py`.
3. Streamlit sẽ cài các gói từ `streamlit_deploy/requirements.txt`.
4. Cấu hình `HYDROAI_API_URL` và `HYDROAI_API_KEY` trong App settings > Secrets.

Xem hướng dẫn đầy đủ tại `API_DEPLOYMENT.md` ở thư mục gốc dự án.
