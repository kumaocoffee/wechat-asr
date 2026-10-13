# Use from the project directory: powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = 'Stop'
if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw '没有找到 conda。请安装 Miniconda，并在 Miniconda/Anaconda Prompt 中运行此脚本。'
}
$taskEnv = Join-Path $PSScriptRoot 'env'
$taskPython = Join-Path $taskEnv 'python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    conda create --prefix $taskEnv python=3.11 pip -y
    if ($LASTEXITCODE -ne 0) { throw 'conda 环境创建失败。' }
}
& $taskPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。' }
Write-Output '安装完成。先登录微信，再运行 .\start.ps1。'
