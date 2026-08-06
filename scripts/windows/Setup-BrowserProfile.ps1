[CmdletBinding()]
param()

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

try {
    $ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw 'Virtuelle Umgebung fehlt. Zuerst Bootstrap-Windows.ps1 ausführen.'
    }

    & (Join-Path $PSScriptRoot 'Test-WindowsPreflight.ps1')
    if ($LASTEXITCODE -ne 0) {
        throw 'Windows-Preflight fehlgeschlagen.'
    }

    Remove-Item Env:PUSHOVER_USER -ErrorAction SilentlyContinue
    Remove-Item Env:PUSHOVER_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:PUSHOVER_TOKEN_ERROR -ErrorAction SilentlyContinue

    Write-Host ''
    Write-Host 'Nun öffnet sich ausschließlich das dedizierte WiesnMonitor-Chrome-Profil.' -ForegroundColor Cyan
    Write-Host 'Eine Challenge darf nur von Ihnen selbst gelöst werden. Nichts reservieren oder absenden.' -ForegroundColor Yellow

    Set-Location -LiteralPath $ProjectRoot
    & $Python -B -m src.workstation_probe --warm-up --dry-run
    $rc = $LASTEXITCODE
    Write-Host ''
    switch ($rc) {
        0  { Write-Host 'Alle sechs Zielprüfungen waren schlüssig.' -ForegroundColor Green }
        10 { Write-Host 'Mindestens eine Seite benötigt weiterhin manuelle Aufmerksamkeit.' -ForegroundColor Yellow }
        20 { Write-Host 'Mindestens eine Prüfung blieb technisch/strukturell unschlüssig.' -ForegroundColor Yellow }
        30 { Write-Host 'Browser-Ersteinrichtung endete mit einem Setupfehler.' -ForegroundColor Red }
        default { throw "Browser-Ersteinrichtung lieferte unerwarteten Exitcode $rc." }
    }
    exit $rc
} catch {
    Write-Host ("Lokaler Setupfehler (Exitcode 30): " + $_.Exception.GetType().Name) -ForegroundColor Red
    exit 30
}
