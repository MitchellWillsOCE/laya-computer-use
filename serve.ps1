$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:USE_TF = "0"
if (-not $env:LAYA_DEVICE) { $env:LAYA_DEVICE = "cuda" }
& .\.venv\Scripts\python.exe -m driver serve
