$ErrorActionPreference = 'Stop'
$venv = Join-Path $PSScriptRoot '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    if (Test-Path -LiteralPath $venv) { throw 'Existing .venv is incomplete. It has been preserved; repair the environment before retrying.' }
    $selected = $null
    foreach ($candidate in @('python', 'python3', 'py')) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        $detected = & $candidate -c 'import sys; print(sys.executable) if sys.version_info >= (3,12) else sys.exit(1)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $detected -and (Test-Path -LiteralPath $detected -PathType Leaf)) {
            $selected = $detected
            break
        }
    }
    if (-not $selected) { throw 'Install Python 3.12 or newer from https://www.python.org/downloads/windows/ with Tcl/Tk and Add Python to PATH, then retry setup.bat.' }
    & $selected -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
}
& $python -c 'import sys, tkinter; assert sys.version_info >= (3,12)'
if ($LASTEXITCODE -ne 0) { throw 'The virtual environment needs Python 3.12+ with Tcl/Tk support.' }
& $python -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network/proxy and retry setup.bat.' }
& $python -c 'import fastapi, uvicorn, httpx, PIL, tomli_w, websockets'
if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed.' }
Write-Output 'Setup complete. Double-click start.bat. No QQ software or system service was installed.'
