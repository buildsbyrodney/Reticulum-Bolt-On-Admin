[CmdletBinding()]
param(
    [string]$Config,
    [switch]$Yes,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$AppRoot = Join-Path $env:LOCALAPPDATA 'Reticulum-Bolt-On-Admin'
$StateRoot = Join-Path $env:LOCALAPPDATA 'Reticulum-Bolt-On-Admin-State'
$TaskName = 'Reticulum-Bolt-On-Admin'
$ScriptRoot = $PSScriptRoot

function Stop-Safely([string]$Message) { Write-Error "$Message Nothing was changed."; exit 1 }
function Test-WindowsPlatform {
    # Windows PowerShell 5.1 uses the conventional OS environment marker; the
    # platform fallback keeps the
    # guard reliable if that environment variable has been cleared.
    if ($env:OS -eq 'Windows_NT') { return $true }
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
}
function Find-Utility([string]$Name) {
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $candidates = @()
    if ($env:APPDATA) { $candidates += Join-Path $env:APPDATA 'Python' }
    foreach ($base in $candidates) {
        foreach ($directory in @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue)) {
            $candidate = Join-Path $directory.FullName "Scripts\\$Name.exe"
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    return $null
}
function Find-Python {
    $candidates = @()
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { $candidates += $command.Source }
    if ($env:LOCALAPPDATA) {
        $root = Join-Path $env:LOCALAPPDATA 'Programs\\Python'
        foreach ($directory in @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue)) {
            $candidates += Join-Path $directory.FullName 'python.exe'
        }
    }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        & $candidate -c 'import sys' 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}
function Find-Config {
    if ($Config) { return (Resolve-Path -LiteralPath $Config -ErrorAction SilentlyContinue).Path }
    $paths = @()
    if ($env:RNS_CONFIG_DIR) { $paths += Join-Path $env:RNS_CONFIG_DIR 'config' }
    if ($env:RETICULUM_CONFIG) { $paths += $env:RETICULUM_CONFIG }
    $paths += Join-Path $HOME '.reticulum\\config'
    if ($env:APPDATA) { $paths += Join-Path $env:APPDATA 'Reticulum\\config' }
    if ($env:LOCALAPPDATA) { $paths += Join-Path $env:LOCALAPPDATA 'Reticulum\\config' }
    $found = @($paths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -Unique)
    if ($found.Count -eq 1) { return (Resolve-Path -LiteralPath $found[0]).Path }
    if ($found.Count -gt 1) { Stop-Safely 'Multiple Reticulum configs were detected. Re-run with -Config <path>.' }
    return $null
}
function Port-Free {
    try { return -not (Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) }
    catch { return -not (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) }
}

if (-not (Test-WindowsPlatform)) { Stop-Safely 'This installer is for native Windows only.' }
$python = Find-Python
if (-not $python) { Stop-Safely 'Python 3.10 or newer is required. Install it for this user, then retry.' }
$pyVersion = & $python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
if ($LASTEXITCODE -ne 0 -or [version]$pyVersion -lt [version]'3.10') { Stop-Safely 'Python 3.10 or newer is required.' }
& $python -c 'import venv' 2>$null
if ($LASTEXITCODE -ne 0) { Stop-Safely 'This Python installation does not provide virtual-environment support.' }

$rnstatus = Find-Utility 'rnstatus'; $rnpath = Find-Utility 'rnpath'
if (-not $rnstatus -or -not $rnpath) { Stop-Safely 'rnstatus and rnpath are required. Install rns==1.5.7 for this user, then retry.' }
$rnsVersion = & $python -c 'import RNS; print(RNS.__version__)' 2>$null
if ($LASTEXITCODE -ne 0 -or $rnsVersion.Trim() -ne '1.5.7') { Stop-Safely 'Exactly rns==1.5.7 is required. Do not change Reticulum automatically; install that exact version and retry.' }
$selectedConfig = Find-Config
if (-not $selectedConfig) { Stop-Safely 'No readable Reticulum config was detected. Start/configure Reticulum for this user, or pass -Config.' }
$configDir = Split-Path -Parent $selectedConfig
$shared = (& $rnstatus --config $configDir --json 2>$null) -and ($LASTEXITCODE -eq 0)
$freePort = Port-Free

Write-Host "Python             $pyVersion OK"
Write-Host 'Reticulum          1.5.7 OK'
Write-Host 'rnstatus           OK'
Write-Host 'rnpath             OK'
Write-Host "Reticulum config   Detected ($selectedConfig)"
if ($shared) { Write-Host 'Shared instance    Available' } else { Write-Host 'Shared instance    Unavailable (GUI will report Unavailable; rnsd will not be started)' }
if ($freePort) { Write-Host 'Port 8080          Free' } else { Write-Host 'Port 8080          In use (not modified)' }
Write-Host 'Administrator      Not required'
if (-not $freePort) { Stop-Safely 'Port 8080 is already in use.' }
Write-Host "Application        $AppRoot"
Write-Host "State              $StateRoot"
if (Test-Path -LiteralPath $AppRoot) { Write-Host 'Bolt-On application Existing installation will be upgraded' } else { Write-Host 'Bolt-On application Not installed' }
if (Test-Path -LiteralPath $StateRoot) { Write-Host 'Bolt-On state       Existing state will be reused' } else { Write-Host 'Bolt-On state       New state directory' }
if (-not $Yes) { $answer = Read-Host 'Ready to install only Bolt-On files and its current-user startup task? [y/N]'; if ($answer -notmatch '^[Yy]$') { Write-Host 'Cancelled. Nothing was changed.'; exit 0 } }

New-Item -ItemType Directory -Force -Path $AppRoot, $StateRoot | Out-Null
& $python -m venv (Join-Path $AppRoot 'venv')
if ($LASTEXITCODE -ne 0) { throw 'Bolt-On virtual environment creation failed.' }
$venvPython = Join-Path $AppRoot 'venv\\Scripts\\python.exe'
& $venvPython -m pip install $ScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'Bolt-On application dependency installation failed.' }
$secret = Join-Path $StateRoot 'secret'
if (-not (Test-Path -LiteralPath $secret)) { [System.IO.File]::WriteAllText($secret, ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))) }
$launcher = Join-Path $AppRoot 'run.ps1'
$escapedConfig = $selectedConfig.Replace("'", "''"); $escapedState = $StateRoot.Replace("'", "''"); $escapedTools = (Split-Path $rnstatus).Replace("'", "''")
@"
`$env:BOLT_ON_SECRET_KEY = [System.IO.File]::ReadAllText('$escapedState\\secret').Trim()
`$env:BOLT_ON_DATABASE = '$escapedState\\observer.db'
`$env:RETICULUM_CONFIG = '$escapedConfig'
`$env:BOLT_ON_RNS_PYTHON = '$($python.Replace("'", "''"))'
`$env:PATH = '$escapedTools;' + `$env:PATH
& '$($AppRoot.Replace("'", "''"))\\venv\\Scripts\\waitress-serve.exe' --listen=127.0.0.1:8080 'reticulum_bolt_on.app:create_app()'
"@ | Set-Content -LiteralPath $launcher -Encoding utf8

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Description 'Reticulum Bolt-On Admin (current user only)' -Force | Out-Null
if (-not $NoStart) { Start-ScheduledTask -TaskName $TaskName }
Write-Host "Installed. Start: powershell -NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
Write-Host "Stop: Stop-ScheduledTask -TaskName $TaskName"
Write-Host "Status: Get-ScheduledTask -TaskName $TaskName"
Write-Host 'URL: http://127.0.0.1:8080/'
Write-Host 'No Reticulum config, storage, identities, or rnsd process was modified.'
