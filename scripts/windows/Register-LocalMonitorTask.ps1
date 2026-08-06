[CmdletBinding()]
param(
    [ValidateRange(5, 60)][int]$IntervalMinutes = 5,
    [ValidatePattern('^[A-Za-z0-9._-]{1,128}$')]
    [string]$TaskName = 'WiesnMonitor-VisibleBotCheck',
    [switch]$AttendedDryRunPassed
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

if (-not $AttendedDryRunPassed) {
    throw 'Nicht registriert: Erst den sichtbaren Dry-run abnehmen und -AttendedDryRunPassed ausdrücklich setzen.'
}

$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Runner = Join-Path $PSScriptRoot 'Run-LocalBotCheck.ps1'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Virtuelle Umgebung fehlt. Zuerst Bootstrap-Windows.ps1 ausführen.'
}

& (Join-Path $PSScriptRoot 'Test-WindowsPreflight.ps1')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$principalId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$argument = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal `
    -UserId $principalId `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 6)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Sichtbare read-only Prüfung von Fischer-Vroni, Paulaner und Poschner; kein Booking/CAPTCHA-Bypass.' `
    -Force | Out-Null

Write-Host "Aufgabe '$TaskName' registriert (alle $IntervalMinutes Minuten, nur bei angemeldetem Benutzer)." -ForegroundColor Green
Write-Host 'Der attended Dry-run wurde mit -AttendedDryRunPassed ausdrücklich bestätigt. Codex muss für den Betrieb nicht geöffnet bleiben.'
Write-Host "Status: powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\Get-LocalMonitorStatus.ps1`""
