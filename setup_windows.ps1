# cli-usage — Windows Setup
# Run from PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

python .\install.py --frontend xplat @args
