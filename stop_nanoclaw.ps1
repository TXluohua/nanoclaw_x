# ============================================================
# NanoClaw 停止脚本
# 用法： .\stop_nanoclaw.ps1
# 说明：Windows 上杀进程【不会】连带杀掉子孙进程，
#       所以这里必须用 taskkill /T（连整棵进程树一起杀），
#       否则 MCP 子进程（poetry_server.py）会变成孤儿进程留在后台。
# ============================================================

$root    = $PSScriptRoot
$pidFile = Join-Path $root '.nanoclaw.pid'

if (-not (Test-Path $pidFile)) {
    Write-Host "找不到 $pidFile —— 可能不是用 start_nanoclaw.ps1 启动的。" -ForegroundColor Yellow
    Write-Host "手动查找： Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Select ProcessId,CommandLine"
    exit 1
}

$targetPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $targetPid) {
    Write-Host "PID 文件是空的，已清理。" -ForegroundColor Yellow
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

Write-Host "正在停止 NanoClaw (PID $targetPid 及其子进程) ..."
# /T = 结束整棵进程树（关键），/F = 强制
taskkill /PID $targetPid /T /F 2>&1 | ForEach-Object { "   $_" }

Start-Sleep -Seconds 2
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue

# ---- 复查端口是否释放 ----
$busy = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "★ 8080 仍被占用：PID $($busy.OwningProcess -join ',')（可能有另一个实例或孤儿进程）" -ForegroundColor Red
} else {
    Write-Host "✅ 已停止，8080 端口已释放" -ForegroundColor Green
}

# ---- 复查是否还有孤儿 python（MCP 子进程等）----
$leftover = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -match 'poetry_server|main\.py' }
if ($leftover) {
    Write-Host "⚠️ 仍有相关 python 进程残留（可能是孤儿 MCP 子进程）：" -ForegroundColor Yellow
    $leftover | Select-Object ProcessId, CommandLine | Format-Table -AutoSize -Wrap
    Write-Host "   如需清理： taskkill /PID <上面的PID> /T /F"
}
