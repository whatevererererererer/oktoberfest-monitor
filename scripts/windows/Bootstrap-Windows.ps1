[CmdletBinding()]
param(
    [switch]$SkipSystemPackages,
    [switch]$SkipPlaywrightBrowser,
    [switch]$SkipTests,
    [switch]$RecreateVenv,
    [switch]$AllowCompatiblePython
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$VenvPath = Join-Path $ProjectRoot '.venv'
$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'
$Constraints = Join-Path $ProjectRoot 'constraints-windows.txt'

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$Description
    )
    Write-Host "==> $Description"
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Refresh-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (($machine, $user) -join ';')
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

function Get-Python312Launcher {
    $attempts = @()
    if (Get-Command pymanager -ErrorAction SilentlyContinue) {
        # `pymanager exec` may automatically install a missing runtime. Listing
        # first keeps this detection function read-only, especially when
        # -SkipSystemPackages was requested.
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'SilentlyContinue'
        $managed = & pymanager list --one --format=exe 3.12 2>$null
        $managedRc = $LASTEXITCODE
        $ErrorActionPreference = $savedPreference
        $managedPath = $managed | Select-Object -Last 1
        if ($managedRc -eq 0 -and $managedPath -and (Test-Path -LiteralPath $managedPath -PathType Leaf)) {
            $attempts += @{ File = [string]$managedPath; Args = @() }
        }
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        # Legacy launcher does not auto-install runtimes.
        $attempts += @{ File = 'py'; Args = @('-V:3.12') }
    }
    foreach ($attempt in $attempts) {
        if (-not (Get-Command $attempt.File -ErrorAction SilentlyContinue)) {
            continue
        }
        $versionArgs = @($attempt.Args) + @('-c', 'import sys; print(sys.executable); raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)')
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'SilentlyContinue'
        $output = & $attempt.File @versionArgs 2>$null
        $ErrorActionPreference = $savedPreference
        if ($LASTEXITCODE -eq 0) {
            return @{ File = $attempt.File; Args = @($attempt.Args); Executable = ($output | Select-Object -Last 1) }
        }
    }
    return $null
}

function Get-CompatiblePythonLauncher {
    if (-not $AllowCompatiblePython) {
        return $null
    }
    $attempts = @()
    if (Get-Command pymanager -ErrorAction SilentlyContinue) {
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'SilentlyContinue'
        $managed = & pymanager list --one --format=exe '>=3.12' 2>$null
        $managedRc = $LASTEXITCODE
        $ErrorActionPreference = $savedPreference
        $managedPath = $managed | Select-Object -Last 1
        if ($managedRc -eq 0 -and $managedPath -and (Test-Path -LiteralPath $managedPath -PathType Leaf)) {
            $attempts += @{ File = [string]$managedPath; Args = @() }
        }
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $attempts += @{ File = 'py'; Args = @() }
    }
    foreach ($attempt in $attempts) {
        if (-not (Get-Command $attempt.File -ErrorAction SilentlyContinue)) {
            continue
        }
        $versionArgs = @($attempt.Args) + @('-c', 'import sys; print(sys.executable); raise SystemExit(0 if sys.version_info >= (3, 12) else 1)')
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'SilentlyContinue'
        $output = & $attempt.File @versionArgs 2>$null
        $ErrorActionPreference = $savedPreference
        if ($LASTEXITCODE -eq 0) {
            return @{ File = $attempt.File; Args = @($attempt.Args); Executable = ($output | Select-Object -Last 1) }
        }
    }
    return $null
}

function Ensure-WinGet {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        return
    }
    Write-Host 'WinGet ist noch nicht registriert; App Installer wird neu registriert.'
    Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe
    Start-Sleep -Seconds 2
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'WinGet fehlt. Windows Update/Microsoft Store App Installer aktualisieren und das Skript erneut starten.'
    }
}

function Install-WinGetPackage {
    param([string]$Id, [string]$Source = 'winget')
    Invoke-Native -FilePath 'winget' -Description "Installiere $Id" -ArgumentList @(
        'install', '--id', $Id, '-e', '--source', $Source,
        '--silent', '--disable-interactivity',
        '--accept-package-agreements', '--accept-source-agreements'
    )
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Dieses Bootstrap-Skript unterstützt ausschließlich Windows.'
}

Write-Host "Projekt: $ProjectRoot"
Set-Location -LiteralPath $ProjectRoot

if (-not $SkipSystemPackages) {
    Ensure-WinGet
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Install-WinGetPackage -Id 'Git.Git'
    }
    if (-not (Get-ChromePath)) {
        Install-WinGetPackage -Id 'Google.Chrome'
    }
    if (-not (Get-Python312Launcher)) {
        if (-not (Get-Command pymanager -ErrorAction SilentlyContinue)) {
            Install-WinGetPackage -Id '9NQ7512CXL7T' -Source 'msstore'
            Refresh-ProcessPath
        }
        if (-not (Get-Command pymanager -ErrorAction SilentlyContinue)) {
            throw 'Python Install Manager wurde installiert, ist in diesem Terminal aber noch nicht verfügbar. PowerShell neu öffnen und das Skript erneut starten.'
        }
        Invoke-Native -FilePath 'pymanager' -ArgumentList @('install', '3.12') -Description 'Installiere Python 3.12'
    }
    Refresh-ProcessPath
}

$Python = Get-Python312Launcher
if (-not $Python) {
    $Python = Get-CompatiblePythonLauncher
}
if (-not $Python) {
    throw 'Python 3.12 wurde nicht gefunden. Ohne Systeminstallation zuerst Bootstrap ohne -SkipSystemPackages ausführen.'
}

if ($RecreateVenv -and (Test-Path -LiteralPath $VenvPath)) {
    $resolvedVenv = [IO.Path]::GetFullPath($VenvPath)
    if (-not $resolvedVenv.StartsWith(($ProjectRoot + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsicheres venv-Ziel: $resolvedVenv"
    }
    Write-Host "==> Entferne ausdrücklich zur Neuerstellung freigegebene .venv: $resolvedVenv"
    Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
}

if (Test-Path -LiteralPath $VenvPath) {
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        if (-not $RecreateVenv) {
            throw "Vorhandene .venv ist unvollständig oder von einem anderen PC kopiert. Mit -RecreateVenv ausdrücklich neu erstellen: $VenvPath"
        }
        $resolvedVenv = [IO.Path]::GetFullPath($VenvPath)
        if (-not $resolvedVenv.StartsWith(($ProjectRoot + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase)) {
            throw "Unsicheres venv-Ziel: $resolvedVenv"
        }
        Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
    } else {
        & $VenvPython -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'
        $venvMatches = ($LASTEXITCODE -eq 0)
        if (-not $venvMatches -and $AllowCompatiblePython) {
            & $VenvPython -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
            $venvMatches = ($LASTEXITCODE -eq 0)
        }
        if (-not $venvMatches) {
            if (-not $RecreateVenv) {
                throw 'Vorhandene .venv hat nicht die erwartete Python-Version. Mit -RecreateVenv ausdrücklich neu erstellen.'
            }
            $resolvedVenv = [IO.Path]::GetFullPath($VenvPath)
            if (-not $resolvedVenv.StartsWith(($ProjectRoot + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase)) {
                throw "Unsicheres venv-Ziel: $resolvedVenv"
            }
            Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
        }
    }
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    $venvArgs = @($Python.Args) + @('-m', 'venv', $VenvPath)
    Invoke-Native -FilePath $Python.File -ArgumentList $venvArgs -Description 'Erstelle virtuelle Python-Umgebung'
}

Invoke-Native -FilePath $VenvPython -ArgumentList @('-m', 'pip', 'install', '--upgrade', 'pip') -Description 'Aktualisiere pip'
Invoke-Native -FilePath $VenvPython -ArgumentList @(
    '-m', 'pip', 'install', '-c', $Constraints, '-e', '.[headless]'
) -Description 'Installiere getestete Projektabhängigkeiten'
Invoke-Native -FilePath $VenvPython -ArgumentList @('-m', 'pip', 'check') -Description 'Prüfe Python-Abhängigkeiten'

if (-not $SkipPlaywrightBrowser) {
    Invoke-Native -FilePath $VenvPython -ArgumentList @('-m', 'playwright', 'install', 'chromium') -Description 'Installiere das zu Playwright passende Chromium'
}

if (-not $SkipTests) {
    Invoke-Native -FilePath $VenvPython -ArgumentList @('-m', 'unittest', 'discover', '-s', 'tests', '-v') -Description 'Führe synthetische Tests aus'
}

$Preflight = Join-Path $PSScriptRoot 'Test-WindowsPreflight.ps1'
$preflightArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Preflight)
if ($AllowCompatiblePython) {
    $preflightArgs += '-AllowCompatiblePython'
}
Invoke-Native -FilePath 'powershell.exe' -ArgumentList $preflightArgs -Description 'Führe Windows-Preflight aus'

Write-Host ''
Write-Host 'Bootstrap erfolgreich.' -ForegroundColor Green
Write-Host 'Nächster attended Schritt:'
Write-Host "  powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\Setup-BrowserProfile.ps1`""
