param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$apiScript = Join-Path $projectRoot "10_api.py"
$databasePath = Join-Path $projectRoot "PMIS\evn_hochua_manual.db"
$keyPath = Join-Path $projectRoot ".hydroai_api_key"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Không tìm thấy Python tại $pythonPath"
}
if (-not (Test-Path -LiteralPath $databasePath)) {
    throw "Không tìm thấy database tại $databasePath"
}

if (-not (Test-Path -LiteralPath $keyPath)) {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    [Convert]::ToBase64String($bytes) | Set-Content -LiteralPath $keyPath -NoNewline
}

$apiKey = (Get-Content -LiteralPath $keyPath -Raw).Trim()
if ($apiKey.Length -lt 32) {
    throw "Khóa API trong $keyPath không hợp lệ."
}

$env:HYDROAI_API_KEY = $apiKey
$env:HYDROAI_DB_PATH = $databasePath
$env:HYDROAI_API_HOST = "0.0.0.0"
$env:HYDROAI_API_PORT = [string]$Port

$apiKey | Set-Clipboard
Write-Host "Khóa API đã được chép vào clipboard và lưu cục bộ tại $keyPath" -ForegroundColor Green
Write-Host "Giữ cửa sổ này mở khi Streamlit cần truy cập API." -ForegroundColor Yellow

& $pythonPath $apiScript
