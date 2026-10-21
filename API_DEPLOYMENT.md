# Chạy HydroAI bằng API và Streamlit Community Cloud

Dashboard Streamlit chỉ đọc dữ liệu qua HTTPS. Database SQLite tiếp tục nằm trên
máy chủ cập nhật dữ liệu và không cần đưa lên GitHub.

## 1. Cấu hình máy chủ API

Mở PowerShell tại thư mục dự án và đặt các biến môi trường cho phiên chạy:

```powershell
cd D:\VScode\CHIRPS
$env:HYDROAI_DB_PATH = "D:\VScode\CHIRPS\PMIS\evn_hochua_manual.db"
$bytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$env:HYDROAI_API_KEY = [Convert]::ToBase64String($bytes)
$env:HYDROAI_API_HOST = "0.0.0.0"
$env:HYDROAI_API_PORT = "8000"
$env:HYDROAI_API_KEY | Set-Clipboard
.\.venv\Scripts\python.exe 10_api.py
```

Lệnh trên dùng `GetBytes`, tương thích với Windows PowerShell/.NET cũ không có
`RandomNumberGenerator.Fill`. Khóa được chép vào clipboard để nhập vào Streamlit
Secrets; không ghi khóa vào GitHub hoặc ảnh chụp màn hình. Giữ cửa sổ chạy API mở
và dùng một cửa sổ PowerShell khác để kiểm tra `/health`.

Để giữ nguyên khóa giữa các lần khởi động và tránh clipboard bị thay đổi, có thể
dùng trình khởi động đi kèm:

```powershell
cd D:\VScode\CHIRPS
.\start_hydroai_api.ps1
```

Trình khởi động lưu khóa trong `.hydroai_api_key` trên máy và tệp này đã được
`.gitignore` loại khỏi Git. Khi cần chép lại khóa để nhập Streamlit Secrets:

```powershell
Get-Content D:\VScode\CHIRPS\.hydroai_api_key | Set-Clipboard
```

API mở SQLite bằng `mode=ro` và `PRAGMA query_only=ON`. Tiến trình cập nhật dữ liệu
trên máy chủ vẫn là tiến trình duy nhất ghi vào database.

Kiểm tra tại máy chủ:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/basins `
  -Headers @{ "X-API-Key" = $env:HYDROAI_API_KEY }
```

Các endpoint dữ liệu:

- `GET /basins`
- `GET /history?basin_id=BTS&start_date=2026-01-01&end_date=2026-12-31&limit=1000`
- `GET /forecast` với cùng bộ lọc
- `GET /generation` với cùng bộ lọc và `include_forecast=true|false`
- `GET /metrics/{basin_id}`

`/health` không yêu cầu khóa. Các endpoint còn lại yêu cầu header `X-API-Key`.

## 2. Công bố API cho Streamlit Cloud

Streamlit Community Cloud không truy cập được `127.0.0.1` hoặc địa chỉ LAN của
máy chủ. API cần một URL HTTPS công khai, ví dụ thông qua reverse proxy Nginx,
Caddy hoặc một tunnel HTTPS có kiểm soát. Chỉ mở endpoint API cần thiết và giữ
khóa API bí mật.

Ví dụ URL sau khi cấu hình HTTPS:

```text
https://hydroai-api.example.com
```

Không đặt database trong thư mục web và không công bố trực tiếp cổng SQLite.

## 3. Cấu hình Streamlit Community Cloud

Main file path:

```text
streamlit_deploy/streamlit_app.py
```

Trong **App settings > Secrets**, nhập:

```toml
HYDROAI_API_URL = "https://hydroai-api.example.com"
HYDROAI_API_KEY = "cung-khoa-da-cau-hinh-tren-may-chu"
```

Mẫu nằm tại `streamlit_deploy/.streamlit/secrets.toml.example`. Không đổi tên mẫu
thành `secrets.toml` rồi commit lên GitHub.

Dashboard cache dữ liệu tối đa 5 phút. Nút **Làm mới dữ liệu** bỏ qua cache ngay.
Khi bật **Tự làm mới**, `st.fragment` chủ động chạy lại theo chu kỳ 1, 5, 15 hoặc
30 phút và tạo khóa cache mới theo chu kỳ, nên trang đang mở thực sự gọi lại API.

## 4. Chạy dashboard tại máy chủ hoặc máy cá nhân

```powershell
$env:HYDROAI_API_URL = "http://127.0.0.1:8000"
$env:HYDROAI_API_KEY = $env:HYDROAI_API_KEY
.\.venv\Scripts\streamlit.exe run 09_dashboard.py
```

## 5. Lưu ý Git

`.gitignore` chặn `*.db`, các tệp `-wal`, `-shm`, `.env` và Streamlit Secrets.
Nếu một database đã từng được Git theo dõi, cần bỏ theo dõi một lần nhưng vẫn giữ
tệp trên máy:

```powershell
git rm --cached -- streamlit_deploy/hydroai_streamlit.db
```

Sau lệnh này, kiểm tra lại bằng `git status`. Không xóa
`PMIS/evn_hochua_manual.db` trên máy chủ.
