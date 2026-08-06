[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NoJitter
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

try {
    if ($NoJitter -and -not $DryRun) {
        throw '-NoJitter ist ausschließlich zusammen mit -DryRun zulässig.'
    }

    $ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Virtuelle Umgebung fehlt. Zuerst Bootstrap-Windows.ps1 ausführen: $Python"
    }

    # Defense in depth: this sidecar has no notification code and must never inherit
    # production credentials from an interactive shell or a parent process.
    Remove-Item Env:PUSHOVER_USER -ErrorAction SilentlyContinue
    Remove-Item Env:PUSHOVER_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:PUSHOVER_TOKEN_ERROR -ErrorAction SilentlyContinue

    $arguments = @('-B', '-m', 'src.workstation_probe')
    if ($DryRun) {
        $arguments += '--dry-run'
    }
    if ($NoJitter) {
        $arguments += '--no-jitter'
    }

    Set-Location -LiteralPath $ProjectRoot
    & $Python @arguments
    $rc = $LASTEXITCODE
    if ($rc -notin @(0, 10, 20, 30)) {
        throw "Python-Sidecar lieferte unerwarteten Exitcode $rc."
    }
    exit $rc
} catch {
    Write-Host ("Lokaler Setupfehler (Exitcode 30): " + $_.Exception.GetType().Name) -ForegroundColor Red
    exit 30
}
