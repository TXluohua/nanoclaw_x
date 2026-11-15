# ============================================================
# NanoClaw 后台启动脚本
# 用法（在 PowerShell 里）：
#     cd D:\huaqing-work\python\nanoclaw\nanoclaw
#     .\start_nanoclaw.ps1
# 效果：脱离当前终端后台运行，关掉终端也不会退出；日志写到 nanoclaw_run.log
# ============================================================

$ErrorActionPreference = 'Stop'
$root    = $PSScriptRoot
$pidFile = Join-Path $root '.nanoclaw.pid'
$logFile = Join-Path $root 'nanoclaw_run.log'
$errFile = Join-Path $root 'nanoclaw_err.log'

# ---- 1. 已在运行？----
if (Test-Path $pidFile) {
    $oldPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Host "NanoClaw 已经在运行 (PID $oldPid)。" -ForegroundColor Yellow
        Write-Host "如需重启，先执行： .\stop_nanoclaw.ps1"
        exit 1
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# ---- 2. 端口占用检查（Web 渠道默认 8080）----
$port = 8080
$busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "★ 端口 $port 已被占用（PID $($busy.OwningProcess -join ',')）—— 可能是另一个 NanoClaw 实例。" -ForegroundColor Red
    Write-Host "  先确认并停掉它，或者改 config.json / .env 里的 web.port。"
    exit 1
}

# ---- 3. 启动（UTF-8 + 无缓冲，保证日志能实时写入）----
$env:PYTHONUTF8       = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

$proc = Start-Process -FilePath 'python' `
                      -ArgumentList '-u', 'main.py' `
                      -WorkingDirectory $root `
                      -RedirectStandardOutput $logFile `
                      -RedirectStandardError  $errFile `
                      -WindowStyle Hidden -PassThru

$proc.Id | Set-Content -Path $pidFile -Encoding ascii

Write-Host ""
Write-Host "✅ NanoClaw 已在后台启动" -ForegroundColor Green
Write-Host "   启动器 PID : $($proc.Id)   （真正进程是它的子进程，脚本停止时会整棵树一起杀）"
Write-Host "   日志       : $logFile"
Write-Host "   错误日志   : $errFile"
Write-Host "   Web 界面   : http://127.0.0.1:8080"
Write-Host "   飞书/QQ    : 直接在对应客户端给机器人发消息"
Write-Host ""
Write-Host "   查看日志   : Get-Content .\nanoclaw_run.log -Wait -Tail 30" -ForegroundColor Cyan
Write-Host "   停止       : .\stop_nanoclaw.ps1" -ForegroundColor Cyan
