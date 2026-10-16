[CmdletBinding()]
param([switch]$Purge, [switch]$Yes)
$ErrorActionPreference = 'Stop'
$AppRoot = Join-Path $env:LOCALAPPDATA 'Reticulum-Bolt-On-Admin'
$StateRoot = Join-Path $env:LOCALAPPDATA 'Reticulum-Bolt-On-Admin-State'
$TaskName = 'Reticulum-Bolt-On-Admin'
if (-not $IsWindows) { Write-Error 'This uninstaller is for native Windows only.'; exit 1 }
Write-Host "Remove current-user Bolt-On startup task and application: $AppRoot"
if ($Purge) { Write-Host "Also remove Bolt-On state: $StateRoot" } else { Write-Host "Preserve login, labels, history, database, and secret: $StateRoot" }
Write-Host 'Reticulum config, storage, identities, rnsd, and unrelated port-8080 processes are preserved.'
if (-not $Yes) { $answer = Read-Host 'Proceed? [y/N]'; if ($answer -notmatch '^[Yy]$') { Write-Host 'Cancelled. Nothing was changed.'; exit 0 } }
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
$processes = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$AppRoot*" }
foreach ($process in $processes) { Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue }
if (Test-Path -LiteralPath $AppRoot) { Remove-Item -LiteralPath $AppRoot -Recurse -Force }
if ($Purge -and (Test-Path -LiteralPath $StateRoot)) { Remove-Item -LiteralPath $StateRoot -Recurse -Force }
$listener = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
if ($listener) { Write-Host "Port 8080 remains in use by PID(s) $($listener.OwningProcess -join ', '); it was not killed unless confirmed as Bolt-On." }
if ($Purge) { Write-Host 'Bolt-On application and state removed.' } else { Write-Host 'Bolt-On application removed; state preserved.' }
