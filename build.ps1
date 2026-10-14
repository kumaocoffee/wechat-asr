param([string]$Python = '')
$ErrorActionPreference = 'Stop'
if (-not $Python) { $Python = Join-Path $PSScriptRoot 'env\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw '请先运行 setup.ps1 创建 conda 环境，或通过 -Python 指定 Python 3.11。' }
& $Python -m pip install -r (Join-Path $PSScriptRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw '打包依赖安装失败。' }
& $Python (Join-Path $PSScriptRoot 'packaging\build_portable.py')
if ($LASTEXITCODE -ne 0) { throw '打包失败。' }
