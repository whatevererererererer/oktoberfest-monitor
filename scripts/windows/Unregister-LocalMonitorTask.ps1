[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [ValidatePattern('^[A-Za-z0-9._-]{1,128}$')]
    [string]$TaskName = 'WiesnMonitor-VisibleBotCheck'
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$tasks = @(Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
if ($tasks.Count -gt 1) {
    throw "Mehr als eine Aufgabe wurde unerwartet für den exakten Namen gefunden: $TaskName"
}
$task = $tasks | Select-Object -First 1
if ($task -and $task.TaskName -cne $TaskName) {
    throw "Aufgelöster Aufgabenname stimmt nicht exakt überein: $($task.TaskName)"
}
if (-not $task) {
    Write-Host "Aufgabe '$TaskName' ist nicht registriert."
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, 'Windows-Aufgabe stoppen und entfernen')) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Aufgabe '$TaskName' wurde entfernt. Lokales Chrome-Profil und Berichte bleiben erhalten."
}
