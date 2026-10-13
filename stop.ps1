param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$taskRuntime = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '.runtime'))
$taskMetadataPath = Join-Path $taskRuntime "server-$Port.json"
if (-not (Test-Path -LiteralPath $taskMetadataPath)) { throw "没有本项目端口 $Port 的后台服务记录。前台启动的服务请按 Ctrl+C。" }
$taskMetadata = Get-Content -LiteralPath $taskMetadataPath -Raw | ConvertFrom-Json
$taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskMetadata.pid)"
if (-not $taskProcess) { Write-Output '服务已经停止。'; return }
$taskScript = Join-Path $PSScriptRoot 'server.py'
$taskSignal = [IO.Path]::GetFullPath([string]$taskMetadata.shutdown_file)
if (-not $taskSignal.StartsWith($taskRuntime + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or -not $taskProcess.CommandLine.Contains($taskScript) -or -not $taskProcess.CommandLine.Contains($taskSignal)) {
    throw '服务记录与当前进程不符，停止操作已取消。'
}
New-Item -ItemType File -Path $taskSignal -Force | Out-Null
Write-Output '正在正常停止服务；已开始的识别会先完成会话清理。'
for ($taskAttempt=0; $taskAttempt -lt 180; $taskAttempt++) {
    if (-not (Get-Process -Id $taskMetadata.pid -ErrorAction SilentlyContinue)) { Write-Output '服务已停止。'; return }
    Start-Sleep -Seconds 1
}
throw '服务仍未退出；没有强行结束进程，请查看服务日志。'
