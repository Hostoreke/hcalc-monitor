# =============================================================================
# 一键启动 hCALC Monitor（含 Funnel 公网暴露）
#
# 用法：在该文件上右键 → 使用 PowerShell 运行
#       或： powershell -ExecutionPolicy Bypass -File start_monitor.ps1
#
# 会做三件事：
#   1. 若监控服务没在跑，就把它启动起来（8000 端口）
#   2. 等它就绪后，开启 Tailscale Funnel 公网访问
#   3. 打印访问地址
# =============================================================================

$ErrorActionPreference = 'Continue'

$vpy      = 'D:\AIFlow\.venv\Scripts\python.exe'
$script   = 'D:\Calculation\PVDF\web\monitor.py'
$ts       = 'C:\Program Files\Tailscale\tailscale.exe'
$logDir   = 'D:\AIFlow\logs'
$port     = 8000
$fqdn     = 'hostoreke.tailacb782.ts.net'

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

Write-Host "=== hCALC Monitor 启动 ===" -ForegroundColor Cyan

# ---------------------------------------------------------------- 1. 监控服务
$listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "[1/3] 监控服务已在运行（端口 $port）" -ForegroundColor Green
} else {
    Write-Host "[1/3] 启动监控服务..." -ForegroundColor Yellow
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    Start-Process -FilePath $vpy -ArgumentList $script `
        -RedirectStandardOutput "$logDir\monitor.log" `
        -RedirectStandardError  "$logDir\monitor.err" `
        -NoNewWindow -PassThru | Out-Null

    # 等端口起来（最多 20 秒）
    $ok = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
            $ok = $true; break
        }
    }
    if ($ok) { Write-Host "      服务已就绪" -ForegroundColor Green }
    else {
        Write-Host "      ❌ 服务启动失败，请查看 $logDir\monitor.err" -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------------- 2. Tailscale
Write-Host "[2/3] 检查 Tailscale..." -ForegroundColor Yellow
$status = & $ts status 2>&1 | Out-String
if ($status -match 'Logged out') {
    Write-Host "      ❌ Tailscale 未登录，请先运行: tailscale up --hostname=hostoreke" -ForegroundColor Red
    exit 1
}

$serve = & $ts serve status 2>&1 | Out-String
if ($serve -match 'Funnel on') {
    Write-Host "      Funnel 已开启，无需重复配置" -ForegroundColor Green
} else {
    Write-Host "      开启 Funnel..." -ForegroundColor Yellow
    & $ts funnel --bg $port 2>&1 | Out-Null
    Start-Sleep -Seconds 5
}

# ---------------------------------------------------------------- 3. 验证
Write-Host "[3/3] 验证访问..." -ForegroundColor Yellow
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
    Write-Host "      本地: HTTP $($r.StatusCode)" -ForegroundColor Green
} catch {
    # 401 说明服务在，只是要认证，属正常
    if ($_.Exception.Response.StatusCode.value__ -eq 401) {
        Write-Host "      本地: HTTP 401（服务正常，需认证）" -ForegroundColor Green
    } else {
        Write-Host "      本地访问异常: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "===============================================" -ForegroundColor Cyan
Write-Host "  公网地址: https://$fqdn/" -ForegroundColor White
Write-Host "  账号口令: hostoreke / hcalc-2026-pvdf" -ForegroundColor White
Write-Host "===============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "关闭公网访问: & '$ts' funnel --https=443 off" -ForegroundColor DarkGray

