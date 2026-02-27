param(
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

Set-Location -LiteralPath $ProjectRoot

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Host "[start-local] Using venv python: $venvPython"
    & $venvPython "main.py"
    exit $LASTEXITCODE
}

$uvCmd = Get-Command "uv" -ErrorAction SilentlyContinue
if ($uvCmd) {
    Write-Host "[start-local] Using uv: $($uvCmd.Path)"
    & $uvCmd.Path "run" "main.py"
    exit $LASTEXITCODE
}

$pythonCmd = Get-Command "python" -ErrorAction SilentlyContinue
if ($pythonCmd) {
    Write-Warning "[start-local] .venv not found, fallback to system python: $($pythonCmd.Path)"
    & $pythonCmd.Path "main.py"
    exit $LASTEXITCODE
}

throw "[start-local] No Python runtime found. Please install Python or run 'uv sync' first."

