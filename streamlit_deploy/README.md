# HydroAI Streamlit

## Chạy tại máy

```powershell
cd D:\VScode\CHIRPS
.\.venv\Scripts\python.exe streamlit_deploy\export_streamlit_data.py
.\.venv\Scripts\streamlit.exe run streamlit_deploy\streamlit_app.py
```

## Đưa lên GitHub và Streamlit Community Cloud

1. Commit thư mục `streamlit_deploy` lên GitHub.
2. Trên Streamlit Community Cloud, chọn repository và đặt **Main file path** là:
   `streamlit_deploy/streamlit_app.py`.
3. Streamlit sẽ cài các gói từ `streamlit_deploy/requirements.txt`.

Database `hydroai_streamlit.db` là bản tổng hợp theo ngày để đủ nhỏ cho GitHub. Khi
database vận hành thay đổi, chạy lại `export_streamlit_data.py` rồi commit bản mới.
Không đưa `PMIS/evn_hochua_manual.db` lên GitHub vì tệp này lớn hơn giới hạn thông thường.
