# cli-usage — Windows setup
# Run from PowerShell in this folder:
#   powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
#   powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1 --uninstall

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Find a real Python 3.9+. The "python" in WindowsApps is often just a
# Microsoft Store stub that opens the Store instead of running anything.
function Test-Python($exe, $pre) {
    try {
        $v = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2]); sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0 -and $v)
    } catch { return $false }
}

$Python = $null; $PyArgs = @()
if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python "py" @("-3"))) {
    $Python = "py"; $PyArgs = @("-3")
} elseif ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-Python "python" @())) {
    $Python = "python"
} elseif ((Get-Command python3 -ErrorAction SilentlyContinue) -and (Test-Python "python3" @())) {
    $Python = "python3"
}

if (-not $Python) {
    Write-Host "Python 3.9+ was not found." -ForegroundColor Red
    Write-Host "Install it with:  winget install -e --id Python.Python.3.13"
    Write-Host "or from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
    Write-Host "then open a new PowerShell window and run this script again."
    exit 1
}

& $Python @PyArgs .\install.py --frontend xplat @args
exit $LASTEXITCODE
