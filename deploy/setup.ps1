param([string]$EnvironmentPath = '.venv')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$environmentDir = [IO.Path]::GetFullPath((Join-Path $projectRoot $EnvironmentPath))
if (-not $environmentDir.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The Python environment must be inside this project.'
}
$pythonExe = Join-Path $environmentDir 'Scripts\python.exe'
$runtimeDir = Join-Path $projectRoot 'artifacts\runtime'
$env:UV_CACHE_DIR = Join-Path $runtimeDir 'cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $runtimeDir 'python'
$env:UV_PYTHON_BIN_DIR = Join-Path $runtimeDir 'bin'
$env:UV_UNMANAGED_INSTALL = Join-Path $runtimeDir 'uv'
$env:PYTHONUTF8 = '1'
# PowerShell 7 launchers can pass a module path missing Windows PowerShell modules.
$windowsModules = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules'
$env:PSModulePath = $windowsModules + ';' + $env:PSModulePath

if (-not (Test-Path -LiteralPath $pythonExe)) {
    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    $uvExe = Join-Path $env:UV_UNMANAGED_INSTALL 'uv.exe'
    if (-not (Test-Path -LiteralPath $uvExe)) {
        Write-Output 'Downloading the official uv installer to the project...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $installer = Join-Path $runtimeDir 'uv-install.ps1'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $installer
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $uvExe)) { throw 'uv installation failed.' }
    }
    # Downloads an isolated Python 3.12 runtime without changing the global PATH.
    & $uvExe --no-config venv --python 3.12 --managed-python --seed $environmentDir
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}
& $pythonExe -m pip install -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Output "Setup complete: $environmentDir"
Write-Output 'Run: .\run.cmd serve'
