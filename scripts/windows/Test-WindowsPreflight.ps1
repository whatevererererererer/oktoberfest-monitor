[CmdletBinding()]
param(
    [switch]$AllowCompatiblePython,
    [switch]$SkipBrowserLaunch
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ProductionState = Join-Path $ProjectRoot 'state\state.json'

function Invoke-Native {
    param([string]$FilePath, [string[]]$ArgumentList, [string]$Description)
    Write-Host "==> $Description"
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Get-ChromePath {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe')
    )
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Preflight unterstützt ausschließlich Windows.'
}
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw "Virtuelle Umgebung fehlt: $VenvPython"
}

& $VenvPython -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'
$versionMatches = ($LASTEXITCODE -eq 0)
if (-not $versionMatches -and $AllowCompatiblePython) {
    & $VenvPython -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
    $versionMatches = ($LASTEXITCODE -eq 0)
}
if (-not $versionMatches) {
    throw 'Die .venv verwendet nicht Python 3.12.'
}

$Chrome = Get-ChromePath
if (-not $Chrome) {
    throw 'Google Chrome wurde nicht gefunden.'
}
Write-Host "Chrome: $Chrome"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git wurde nicht gefunden. Codex-Projektfunktionen und einige Tests benötigen Git.'
}

$stateHashBefore = (Get-FileHash -LiteralPath $ProductionState -Algorithm SHA256).Hash

Invoke-Native -FilePath $VenvPython -ArgumentList @(
    '-c',
    "import importlib.metadata as m; expected=[('playwright','1.59.0'),('httpx','0.28.1'),('selectolax','0.4.7'),('PyYAML','6.0.3'),('pydantic','2.13.3'),('jsonpath-ng','1.8.0')]; bad={k:(m.version(k),v) for k,v in expected if m.version(k)!=v}; print(dict(expected)); raise SystemExit(1 if bad else 0)"
) -Description 'Prüfe getestete direkte Python-Versionen'
Invoke-Native -FilePath $VenvPython -ArgumentList @('-m', 'pip', 'check') -Description 'Prüfe installierte Python-Pakete'

if (-not $SkipBrowserLaunch) {
    Invoke-Native -FilePath $VenvPython -ArgumentList @(
        '-c',
        "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); page=b.new_page(); page.set_content('<title>wiesn-smoke</title><main>ok</main>'); assert page.title()=='wiesn-smoke' and page.locator('main').inner_text()=='ok'; b.close(); p.stop(); print('Playwright Chromium smoke: OK')"
    ) -Description 'Starte Playwright-Chromium gegen lokale Testseite'
    Invoke-Native -FilePath $VenvPython -ArgumentList @(
        '-c',
        "from pathlib import Path; import sys; from playwright.sync_api import sync_playwright; profile=Path(sys.argv[1]); profile.mkdir(parents=True,exist_ok=True); p=sync_playwright().start(); c=p.chromium.launch_persistent_context(user_data_dir=str(profile),channel='chrome',headless=True); page=c.new_page(); page.set_content('<title>chrome-smoke</title>'); assert page.title()=='chrome-smoke'; c.close(); p.stop(); print('Google Chrome persistent-context smoke: OK')",
        (Join-Path $env:LOCALAPPDATA 'WiesnMonitor\ChromeProfile')
    ) -Description 'Starte persistenten Google-Chrome-Kanal gegen lokale Testseite'
}

$DataDir = Join-Path $env:LOCALAPPDATA 'WiesnMonitor'
$AtomicDir = Join-Path $DataDir 'Preflight'
New-Item -ItemType Directory -Path $AtomicDir -Force | Out-Null
$source = Join-Path $AtomicDir 'atomic-source.tmp'
$target = Join-Path $AtomicDir 'atomic-target.tmp'
$backup = Join-Path $AtomicDir 'atomic-backup.tmp'
Remove-Item -LiteralPath $source, $target, $backup -Force -ErrorAction SilentlyContinue
[IO.File]::WriteAllText($source, 'ok', [Text.Encoding]::UTF8)
[IO.File]::WriteAllText($target, 'old', [Text.Encoding]::UTF8)
[IO.File]::Replace($source, $target, $backup)
if ((Get-Content -LiteralPath $target -Raw) -notmatch 'ok') {
    throw 'Atomarer Dateiersatz im lokalen Datenverzeichnis fehlgeschlagen.'
}
Remove-Item -LiteralPath $target -Force
Remove-Item -LiteralPath $backup -Force

$stateHashAfter = (Get-FileHash -LiteralPath $ProductionState -Algorithm SHA256).Hash
if ($stateHashBefore -ne $stateHashAfter) {
    throw 'Preflight hat unerwartet state/state.json verändert.'
}

Write-Host ''
Write-Host 'Windows-Preflight erfolgreich; keine Webseite aufgerufen und Produktions-State unverändert.' -ForegroundColor Green
