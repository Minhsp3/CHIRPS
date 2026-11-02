param(
    [int]$Port = 8000,
    [int]$TimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

$projectRoot = $PSScriptRoot
$apiStarter = Join-Path $projectRoot "start_hydroai_api.ps1"
$runtimeDir = Join-Path $projectRoot "tmp_runtime"
$tunnelOutLog = Join-Path $runtimeDir "cloudflared-quick-out.log"
$tunnelErrLog = Join-Path $runtimeDir "cloudflared-quick-err.log"
$tunnelPidFile = Join-Path $runtimeDir "cloudflared-quick.pid"
$urlFile = Join-Path $projectRoot ".hydroai_api_url"
$healthUrl = "http://127.0.0.1:$Port/health"

New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

function Test-HydroAiApi {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        return $response.status -eq "ok"
    }
    catch {
        return $false
    }
}

function Test-PublicHydroAiApi {
    param([string]$BaseUrl)

    if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
        return $false
    }

    try {
        $publicHealthUrl = $BaseUrl.TrimEnd('/') + "/health"
        $response = Invoke-RestMethod -Uri $publicHealthUrl -TimeoutSec 8
        return $response.status -eq "ok"
    }
    catch {
        return $false
    }
}

function Get-SavedTunnelProcess {
    if (-not (Test-Path -LiteralPath $tunnelPidFile)) {
        return $null
    }

    $savedPid = (Get-Content -LiteralPath $tunnelPidFile -Raw).Trim()
    if ($savedPid -notmatch '^\d+$') {
        return $null
    }

    $process = Get-Process -Id ([int]$savedPid) -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.ProcessName -ne "cloudflared") {
        return $null
    }

    return $process
}

function Stop-SavedTunnel {
    $process = Get-SavedTunnelProcess
    if ($null -ne $process) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        try {
            Wait-Process -Id $process.Id -Timeout 5 -ErrorAction SilentlyContinue
        }
        catch {
            # The process may already have exited.
        }
    }

    Remove-Item -LiteralPath $tunnelPidFile -Force -ErrorAction SilentlyContinue
}

if (-not (Test-HydroAiApi)) {
    if (-not (Test-Path -LiteralPath $apiStarter)) {
        throw "Không tìm thấy $apiStarter"
    }

    Start-Process -FilePath "powershell.exe" `
        -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", ('"{0}"' -f $apiStarter),
            "-Port", [string]$Port
        ) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden | Out-Null

    $apiDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $apiDeadline -and -not (Test-HydroAiApi)) {
        Start-Sleep -Seconds 1
    }

    if (-not (Test-HydroAiApi)) {
        throw "API HydroAI chưa khởi động được tại $healthUrl"
    }
}

$savedTunnel = Get-SavedTunnelProcess
$savedUrl = if (Test-Path -LiteralPath $urlFile) {
    (Get-Content -LiteralPath $urlFile -Raw).Trim()
}
else {
    $null
}

if ($null -ne $savedTunnel -and -not [string]::IsNullOrWhiteSpace($savedUrl)) {
    $publicUrl = $savedUrl
    Write-Host "Tunnel hiện tại vẫn hoạt động, tiếp tục sử dụng URL đã lưu." -ForegroundColor Green
}
else {
    if ($null -ne $savedTunnel -or -not [string]::IsNullOrWhiteSpace($savedUrl)) {
        Write-Host "Tunnel cũ không còn truy cập được. Đang tạo tunnel mới..." -ForegroundColor Yellow
    }

    Stop-SavedTunnel
    Remove-Item -LiteralPath $urlFile -Force -ErrorAction SilentlyContinue

    $cloudflaredCandidates = @(
        (Get-Command "cloudflared.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        "C:\Program Files (x86)\cloudflared\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

    $cloudflared = $cloudflaredCandidates | Select-Object -First 1
    if (-not $cloudflared) {
        throw "Không tìm thấy cloudflared.exe trên máy."
    }

    $publicUrl = $null
    $lastTunnelError = $null

    for ($attempt = 1; $attempt -le 3 -and -not $publicUrl; $attempt++) {
        $lastTunnelError = $null
        Stop-SavedTunnel
        Remove-Item -LiteralPath $tunnelOutLog -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $tunnelErrLog -Force -ErrorAction SilentlyContinue

        Write-Host "Đang tạo Cloudflare Tunnel (lần $attempt/3)..." -ForegroundColor Cyan
        $tunnelProcess = Start-Process -FilePath $cloudflared `
            -ArgumentList @(
                "tunnel",
                "--no-autoupdate",
                "--protocol", "http2",
                "--url", "http://127.0.0.1:$Port"
            ) `
            -WorkingDirectory $projectRoot `
            -RedirectStandardOutput $tunnelOutLog `
            -RedirectStandardError $tunnelErrLog `
            -WindowStyle Hidden `
            -PassThru

        Set-Content -LiteralPath $tunnelPidFile -Value $tunnelProcess.Id -NoNewline

        $candidateUrl = $null
        $tunnelDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $tunnelDeadline) {
            Start-Sleep -Seconds 1
            $logText = @($tunnelOutLog, $tunnelErrLog) |
                Where-Object { Test-Path -LiteralPath $_ } |
                ForEach-Object { Get-Content -LiteralPath $_ -Raw -ErrorAction SilentlyContinue } |
                Out-String

            $match = [regex]::Match($logText, 'https://[a-z0-9-]+\.trycloudflare\.com')
            if ($match.Success) {
                $candidateUrl = $match.Value
            }

            if ($logText -match 'Unauthorized: Tunnel not found') {
                $lastTunnelError = "Cloudflare từ chối tunnel vừa tạo (Unauthorized: Tunnel not found)."
                break
            }

            if ($candidateUrl -and $logText -match 'Registered tunnel connection') {
                # Invoke-RestMethod may inherit a local proxy that cannot reach
                # trycloudflare.com. Registration is the authoritative tunnel
                # readiness signal; Streamlit performs the external health check.
                $publicUrl = $candidateUrl
                break
            }

            if ($tunnelProcess.HasExited) {
                $lastTunnelError = "Tiến trình cloudflared đã dừng trước khi tunnel sẵn sàng."
                break
            }
        }

        if (-not $publicUrl) {
            if (-not $lastTunnelError) {
                $lastTunnelError = "Tunnel chưa vượt qua kiểm tra /health trong $TimeoutSeconds giây."
            }
            Stop-SavedTunnel
            if ($attempt -lt 3) {
                Start-Sleep -Seconds 2
            }
        }
    }

    if (-not $publicUrl) {
        throw "$lastTunnelError Xem nhật ký trong $runtimeDir"
    }

    Set-Content -LiteralPath $urlFile -Value $publicUrl -NoNewline
}

$publicUrl | Set-Clipboard
Write-Host ""
Write-Host "HydroAI API đang chạy." -ForegroundColor Green
Write-Host "HYDROAI_API_URL = `"$publicUrl`"" -ForegroundColor Cyan
Write-Host "URL đã được chép vào clipboard và lưu tại $urlFile" -ForegroundColor Green
Write-Host ""
Write-Warning "Đây là Quick Tunnel tạm thời. Nếu tiến trình tunnel được tạo lại, URL sẽ đổi và Streamlit Secrets phải cập nhật lại."
