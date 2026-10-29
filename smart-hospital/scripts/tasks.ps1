<#
.SYNOPSIS
  One-command local task runner for Smart Hospital Management System.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task bootstrap
  powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task test
  powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task run
#>

[CmdletBinding()]
param(
    [ValidateSet(
        "help",
        "doctor",
        "setup",
        "bootstrap",
        "seed",
        "test-core",
        "test-ui",
        "test",
        "eval",
        "run-api",
        "run-ui",
        "run",
        "stop",
        "docker-up",
        "docker-down"
    )]
    [string]$Task = "help",

    [switch]$UseSqlite,
    [switch]$NoVenv,
    [switch]$NoInstall,
    [switch]$WithModelEval
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$VenvActivate = Join-Path $Root ".venv\Scripts\Activate.ps1"
$PidsFile = Join-Path $Root ".run\pids.json"
$DotEnv = Join-Path $Root ".env"
$DotEnvExample = Join-Path $Root ".env.example"

function Write-Step([string]$Message) {
    Write-Host ("`n==> " + $Message) -ForegroundColor Cyan
}

function Ensure-RunDir {
    $runDir = Join-Path $Root ".run"
    if (-not (Test-Path $runDir)) {
        New-Item -Path $runDir -ItemType Directory | Out-Null
    }
}

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path $Path)) {
        return
    }
    Write-Step "Loading environment from $Path"
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) {
            return
        }
        $name = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Ensure-Python {
    Write-Step "Checking Python"
    $null = python --version
}

function Ensure-Venv {
    if ($NoVenv) {
        return
    }
    if (-not (Test-Path $VenvPython)) {
        Write-Step "Creating virtual environment"
        Push-Location $Root
        python -m venv .venv
        Pop-Location
    }
}

function Use-Venv {
    if ($NoVenv) {
        return
    }
    if (-not (Test-Path $VenvActivate)) {
        throw "Virtual environment activation script not found: $VenvActivate"
    }
    Write-Step "Activating .venv"
    . $VenvActivate
}

function Install-Dependencies {
    if ($NoInstall) {
        return
    }
    Write-Step "Installing dependencies"
    Push-Location $Root
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    Pop-Location
}

function Ensure-EnvFile {
    if (-not (Test-Path $DotEnv) -and (Test-Path $DotEnvExample)) {
        Write-Step "Creating .env from .env.example"
        Copy-Item -Path $DotEnvExample -Destination $DotEnv
    }
}

function Set-DatabaseEnv {
    if ($UseSqlite) {
        $env:DATABASE_URL = "sqlite:///./dev_hospital.db"
        Write-Step "Using SQLite DATABASE_URL=$($env:DATABASE_URL)"
    }
}

function Run-Seed {
    Write-Step "Seeding synthetic data"
    Push-Location $Root
    python -m data.seed.seed_data
    Pop-Location
}

function Run-TestsCore {
    Write-Step "Running core tests"
    Push-Location $Root
    python -m pytest tests -q -p no:warnings --ignore=tests/integration/test_streamlit_ui.py
    Pop-Location
}

function Run-TestsUi {
    Write-Step "Running Streamlit UI smoke tests"
    Push-Location $Root
    python -m pytest tests/integration/test_streamlit_ui.py -q -p no:warnings
    Pop-Location
}

function Run-Eval {
    Write-Step "Running evaluation harness"
    Push-Location $Root
    if ($WithModelEval) {
        python -m evaluation.run_eval --with-model
    }
    else {
        python -m evaluation.run_eval
    }
    Pop-Location
}

function Start-Api {
    Write-Step "Starting FastAPI on :8000"
    Ensure-RunDir
    $api = Start-Process -FilePath "python" -ArgumentList "-m uvicorn backend.main:app --host 0.0.0.0 --port 8000" -WorkingDirectory $Root -PassThru
    return $api.Id
}

function Start-Ui {
    Write-Step "Starting Streamlit on :8501"
    Ensure-RunDir
    $ui = Start-Process -FilePath "python" -ArgumentList "-m streamlit run streamlit_app/app.py --server.address 0.0.0.0 --server.port 8501" -WorkingDirectory $Root -PassThru
    return $ui.Id
}

function Save-Pids([int]$ApiPid, [int]$UiPid) {
    Ensure-RunDir
    @{ api = $ApiPid; ui = $UiPid } | ConvertTo-Json | Set-Content -Path $PidsFile -Encoding UTF8
}

function Stop-Run {
    if (-not (Test-Path $PidsFile)) {
        Write-Host "No PID file found: $PidsFile"
        return
    }
    $pids = Get-Content $PidsFile | ConvertFrom-Json
    foreach ($name in @("api", "ui")) {
        $pid = [int]$pids.$name
        if ($pid -gt 0) {
            try {
                Stop-Process -Id $pid -Force -ErrorAction Stop
                Write-Host "Stopped $name (PID $pid)"
            }
            catch {
                Write-Host "$name already stopped (PID $pid)"
            }
        }
    }
    Remove-Item $PidsFile -Force
}

function Show-Doctor {
    Write-Step "Environment doctor"
    Push-Location $Root
    python --version
    python -c "import sqlalchemy, pydantic; print('sqlalchemy', sqlalchemy.__version__, '| pydantic', pydantic.VERSION)"
    if (Test-Path $DotEnv) { Write-Host ".env: present" } else { Write-Host ".env: missing" }
    $vars = @("GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    foreach ($v in $vars) {
        $isSet = -not [string]::IsNullOrWhiteSpace((Get-Item "Env:$v" -ErrorAction SilentlyContinue).Value)
        Write-Host "${v}: " -NoNewline
        if ($isSet) { Write-Host "set" -ForegroundColor Green } else { Write-Host "not set" -ForegroundColor Yellow }
    }
    Pop-Location
}

function Show-Help {
@"
Smart Hospital task runner

Usage:
  powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task <name>

Tasks:
  help        Show this help
  doctor      Print environment checks and key-variable presence
  setup       Create .venv, install requirements, ensure .env
  bootstrap   setup + (optional SQLite) + seed
  seed        Seed synthetic data
  test-core   Run non-UI tests
  test-ui     Run Streamlit UI smoke tests
  test        Run test-core + test-ui
  eval        Run evaluation harness (use -WithModelEval to call providers)
  run-api     Start FastAPI (foreground)
  run-ui      Start Streamlit (foreground)
  run         Start API + UI in background and save PIDs
  stop        Stop background API + UI started by this script
  docker-up   docker compose up --build
  docker-down docker compose down

Useful switches:
  -UseSqlite  Set DATABASE_URL=sqlite:///./dev_hospital.db
  -NoVenv     Skip .venv creation/activation
  -NoInstall  Skip pip install

Examples:
  powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task bootstrap -UseSqlite
  powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task test
  powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task run -UseSqlite
"@ | Write-Host
}

# --- Main -----------------------------------------------------------------
Push-Location $Root
try {
    Import-DotEnv -Path $DotEnv
    Set-DatabaseEnv

    switch ($Task) {
        "help"       { Show-Help }
        "doctor"     { Ensure-Python; Show-Doctor }
        "setup"      { Ensure-Python; Ensure-Venv; Use-Venv; Install-Dependencies; Ensure-EnvFile }
        "bootstrap"  { Ensure-Python; Ensure-Venv; Use-Venv; Install-Dependencies; Ensure-EnvFile; Run-Seed }
        "seed"       { Ensure-Python; Ensure-Venv; Use-Venv; Run-Seed }
        "test-core"  { Ensure-Python; Ensure-Venv; Use-Venv; Run-TestsCore }
        "test-ui"    { Ensure-Python; Ensure-Venv; Use-Venv; Run-TestsUi }
        "test"       { Ensure-Python; Ensure-Venv; Use-Venv; Run-TestsCore; Run-TestsUi }
        "eval"       { Ensure-Python; Ensure-Venv; Use-Venv; Run-Eval }
        "run-api"    { Ensure-Python; Ensure-Venv; Use-Venv; Write-Step "Running FastAPI"; python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 }
        "run-ui"     { Ensure-Python; Ensure-Venv; Use-Venv; Write-Step "Running Streamlit"; python -m streamlit run streamlit_app/app.py --server.address 0.0.0.0 --server.port 8501 }
        "run"        {
            Ensure-Python; Ensure-Venv; Use-Venv
            $apiPid = Start-Api
            Start-Sleep -Seconds 1
            $uiPid = Start-Ui
            Save-Pids -ApiPid $apiPid -UiPid $uiPid
            Write-Host "API PID: $apiPid"
            Write-Host "UI  PID: $uiPid"
            Write-Host "Stop them with: powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task stop"
        }
        "stop"       { Stop-Run }
        "docker-up"  { docker compose up --build }
        "docker-down"{ docker compose down }
    }
}
finally {
    Pop-Location
}
