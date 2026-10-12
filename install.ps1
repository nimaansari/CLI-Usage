# cli-usage one-line installer for Windows. In PowerShell:
#
#   irm https://raw.githubusercontent.com/nimaansari/CLI-Usage/main/install.ps1 | iex
#
# Installs Python if needed (via winget), downloads (or updates) cli-usage into
# %LOCALAPPDATA%\cli-usage\app, then runs setup_windows.ps1. Run it again to update.
# Overrides: $env:CLI_USAGE_DIR, $env:CLI_USAGE_REF.

& {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest much faster on PS 5.1
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $Ref = if ($env:CLI_USAGE_REF) { $env:CLI_USAGE_REF } else { "main" }
    $Dir = if ($env:CLI_USAGE_DIR) { $env:CLI_USAGE_DIR } else { Join-Path $env:LOCALAPPDATA "cli-usage\app" }

    function Say($msg) { Write-Host "==> $msg" -ForegroundColor Magenta }

    function Find-Python {
        $launcher = Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"
        foreach ($c in @(@("py", "-3"), @($launcher, "-3"), @("python"), @("python3"))) {
            $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
            if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
            try {
                & $exe @pre -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) { return $true }
            } catch { }
        }
        return $false
    }

    if (-not (Find-Python)) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            Write-Host "Python 3.9+ is needed. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." -ForegroundColor Red
            return
        }
        Say "Installing Python 3.13 with winget..."
        winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
        # Pick up the new PATH without opening a new window.
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
        if (-not (Find-Python)) {
            Write-Host "Python was installed but isn't visible yet. Open a new PowerShell window and run this command again." -ForegroundColor Yellow
            return
        }
    }

    Say "Downloading cli-usage into $Dir"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("cli-usage-" + [Guid]::NewGuid())
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp "src.zip"
        Invoke-WebRequest -UseBasicParsing "https://github.com/nimaansari/CLI-Usage/archive/refs/heads/$Ref.zip" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        # Copy over the old version; the existing .venv is kept.
        Copy-Item -Path (Join-Path $src.FullName "*") -Destination $Dir -Recurse -Force
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }

    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir "setup_windows.ps1")
    if ($LASTEXITCODE -eq 0) {
        Say "Installed in $Dir"
        Say "Update any time by running the same command again."
    }
}
