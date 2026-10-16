param([int]$Port = 8765, [int]$WechatPid = 0, [switch]$Background,
      [int]$WechatConcurrency = 4, [int]$WetypeConcurrency = 1, [int]$DoubaoConcurrency = 3)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot 'env\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '请先运行 .\setup.ps1 创建本项目的 conda 环境。' }
if ($WechatConcurrency -lt 1 -or $WetypeConcurrency -lt 1 -or $DoubaoConcurrency -lt 1) { throw '并发数必须为正整数。' }
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port 必须介于 1 和 65535。' }
$taskListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($taskListener) { throw "端口 $Port 已占用。可用 -Port 指定其他端口，或先停止已有服务。" }
$taskRuntime = Join-Path $taskRoot '.runtime'
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$taskSignal = Join-Path $taskRuntime ("shutdown-$Port-" + [guid]::NewGuid().ToString('N'))
$taskArgs = @((Join-Path $taskRoot 'server.py'), '--port', [string]$Port, '--shutdown-file', $taskSignal)
$taskArgs += @('--wechat-concurrency', [string]$WechatConcurrency, '--wetype-concurrency', [string]$WetypeConcurrency, '--doubao-concurrency', [string]$DoubaoConcurrency)
if ($WechatPid -gt 0) { $taskArgs += @('--pid', [string]$WechatPid) }
if (-not $Background) {
    & $taskPython @taskArgs
    if ($LASTEXITCODE -ne 0) { throw "服务异常退出，退出码 $LASTEXITCODE。" }
    return
}
$taskQuotedArgs = @($taskArgs | ForEach-Object { '"' + $_ + '"' })
# Start-Process truncates redirected files. Preserve the previous session's
# evidence before creating new logs, including abnormal service termination.
$taskLogStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fffffff'
foreach ($taskLogStream in @('stdout', 'stderr')) {
    $taskPreviousLog = Join-Path $taskRuntime "server-$Port.$taskLogStream.log"
    if (Test-Path -LiteralPath $taskPreviousLog) {
        Copy-Item -LiteralPath $taskPreviousLog -Destination (Join-Path $taskRuntime "server-$Port-$taskLogStamp.$taskLogStream.log")
    }
}
$taskService = Start-Process -FilePath $taskPython -ArgumentList $taskQuotedArgs -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRuntime "server-$Port.stdout.log") -RedirectStandardError (Join-Path $taskRuntime "server-$Port.stderr.log")
[ordered]@{ pid=$taskService.Id; port=$Port; shutdown_file=$taskSignal; script=(Join-Path $taskRoot 'server.py') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskRuntime "server-$Port.json") -Encoding UTF8
for ($taskAttempt=0; $taskAttempt -lt 50; $taskAttempt++) {
    if ($taskService.HasExited) { throw "服务启动失败，请查看 .runtime\server-$Port.stderr.log。" }
    try {
        $taskHealth=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1
        if ($taskHealth.backend -eq 'asr-bridge') {
            Write-Output "API 已启动：http://127.0.0.1:$Port/v1（PID=$($taskService.Id)）"
            Write-Output "停止命令：.\stop.ps1 -Port $Port"
            return
        }
    } catch { }
    Start-Sleep -Milliseconds 200
}
throw "服务未在时限内就绪，请查看 .runtime\server-$Port.stderr.log。"
