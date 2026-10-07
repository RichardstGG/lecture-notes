param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Native Windows is required (not WSL).' }
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
Write-Host "Project directory: $ProjectRoot"
Write-Host 'This prepares basic tests only. It does not install GPU engines or open the microphone.'
if ((Read-Host 'Type YES to use this directory') -cne 'YES') { throw 'Cancelled.' }

Get-Command git -ErrorAction Stop | Out-Null
Get-Command python -ErrorAction Stop | Out-Null
& python -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Install native Windows Python 3.11+ first.' }

if (-not (Test-Path $ProjectRoot)) {
    & git clone https://github.com/RichardstGG/lecture-notes.git $ProjectRoot
    if ($LASTEXITCODE -ne 0) { throw 'Clone failed.' }
} else {
    $origin = & git -C $ProjectRoot remote get-url origin
    if ($LASTEXITCODE -ne 0 -or $origin -ne 'https://github.com/RichardstGG/lecture-notes.git') {
        throw 'Existing path is not the expected repository; nothing overwritten.'
    }
}

$control = Join-Path $env:LOCALAPPDATA 'lecture-notes-test-runner'
New-Item -ItemType Directory -Path $control -Force | Out-Null
$venv = Join-Path $control 'venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    & python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed.' }
}
$python = Join-Path $venv 'Scripts\python.exe'
& $python -m pip install 'fastapi>=0.115,<1' 'uvicorn>=0.32,<1' 'httpx>=0.27,<1'
if ($LASTEXITCODE -ne 0) { throw 'Test dependency installation failed.' }

Write-Host "Basic test environment prepared: $control"
Write-Host 'Next: register a Windows x64 GitHub Actions runner in a SEPARATE directory.'
Write-Host 'Use repository Settings > Actions > Runners > New self-hosted runner.'
Write-Host 'URL: https://github.com/RichardstGG/lecture-notes/settings/actions/runners/new'
Write-Host 'Add custom label: lecture-notes-win11'
Write-Host 'Run interactively with run.cmd under THIS Windows account; do not install as a service.'
Write-Host 'Keep registration tokens private. Hardware tests remain disabled until a local window is opened.'
