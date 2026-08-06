[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]{1,128}$')]
    [string]$TaskName = 'WiesnMonitor-VisibleBotCheck',
    [switch]$WaitForCompletion,
    [ValidateRange(1, 420)][int]$TimeoutSeconds = 390,
    [datetime]$StartedAfter
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($WaitForCompletion -and $task) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        $newEnough = -not $PSBoundParameters.ContainsKey('StartedAfter') -or $info.LastRunTime -ge $StartedAfter
        if ($task.State -ne 'Running' -and $newEnough) {
            break
        }
        if ((Get-Date) -ge $deadline) {
            throw "Zeitlimit beim Warten auf einen neuen abgeschlossenen Task-Lauf: $TaskName"
        }
        Start-Sleep -Seconds 2
    } while ($true)
}
if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    [pscustomobject]@{
        TaskName = $TaskName
        State = $task.State
        LastRunTime = $info.LastRunTime
        LastTaskResult = $info.LastTaskResult
        NextRunTime = $info.NextRunTime
    } | Format-List
} else {
    Write-Host "Aufgabe '$TaskName' ist nicht registriert."
}

$reportPath = Join-Path $env:LOCALAPPDATA 'WiesnMonitor\Reports\latest.json'
if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
    Write-Host "Letzter privacy-sicherer Bericht: $reportPath"
    $reportText = Get-Content -LiteralPath $reportPath -Raw
    $reportText
    if ($task) {
        try {
            $report = $reportText | ConvertFrom-Json
            $finishedAt = [DateTimeOffset]::Parse([string]$report.finished_at).UtcDateTime
            if ($finishedAt -lt $info.LastRunTime.ToUniversalTime()) {
                Write-Warning 'Der Bericht ist älter als der letzte Task-Start und kann veraltet sein.'
            }
        } catch {
            Write-Warning 'Berichtszeitpunkt konnte nicht mit dem Task-Lauf korreliert werden.'
        }
    }
} else {
    Write-Host 'Noch kein dauerhafter Bericht vorhanden.'
}

Write-Host 'Exitcodes: 0=schlüssig, 10=manuelle Aufmerksamkeit, 20=unschlüssig, 30=Setupfehler.'
