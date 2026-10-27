# run.ps1 —— 项目脚本启动器
# =========================
# 干三件事:
#   1. 控制台切 UTF-8 (否则中文输出乱码, 你的 Windows 默认是 GBK)
#   2. 设 PYTHONUTF8=1 (Python 3.11 在 Windows 上默认不是 UTF-8 模式)
#   3. 用项目自己的 venv 跑脚本
#
# 用法:
#   .\run.ps1                          # 默认跑 mock_opcua_server.py
#   .\run.ps1 check_client.py
#   .\run.ps1 some_script.py --port 4841

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Error "找不到 venv 里的 python: $python`n先建环境: uv venv .venv --python 3.11"
}

# UTF-8: 控制台代码页 + Python 运行时
chcp 65001 > $null
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$scriptName = if ($args.Count -gt 0) { $args[0] } else { 'mock_opcua_server.py' }
$scriptPath = Join-Path $projectRoot $scriptName

if (-not (Test-Path $scriptPath)) {
    Write-Error "脚本不存在: $scriptPath"
}

$rest = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }

& $python $scriptPath @rest
exit $LASTEXITCODE
