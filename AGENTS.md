# Projektanweisungen für Codex

## Ziel-PC-Auftrag

Der Windows-Sidecar prüft ausschließlich diese drei historisch bot-betroffenen
Portale sichtbar und read-only:

- `fischer-vroni`
- `paulaner`
- `poschner`

Zusätzliche Bot-Fehler anderer Zelte erweitern diese Allowlist nicht automatisch.
Die historische Evidenz in den YAML-Notizen ist kein aktueller Test-Oracle.

## Unverhandelbare Grenzen

- Niemals reservieren, ein Reservierungsformular absenden oder bis zum finalen
  Buchungsschritt navigieren.
- CAPTCHA, Turnstile, Bot-Schutz oder Rate Limits niemals umgehen. Eine Challenge
  als `needs_manual_action` melden; nur der Benutzer darf sie legitim lösen.
- Keine Stealth-Plugins, Fingerprint-Manipulation, Proxyrotation, Cookie-Exporte
  oder kopierte Browserprofile verwenden.
- Keine Cookies, Tokens, vollständigen HTML-Seiten, Screenshots oder Geheimnisse
  im Repository, State oder in Logs speichern.
- Der lokale Sidecar darf `state/state.json`, die Git-Outbox und Git-Refs niemals
  schreiben. Der GitHub-Workflow bleibt deren einziger Writer.
- Keine echte Pushover-Nachricht aus Windows-Setup, Preflight, Dry-run oder Tests.
- Ein Browserprofil liegt ausschließlich unter
  `%LOCALAPPDATA%\WiesnMonitor\ChromeProfile`, niemals im Projekt.
- Die Windows-Aufgabe erst nach einem erfolgreichen attended Dry-run registrieren.
  Sie läuft nur als aktueller angemeldeter Benutzer und nie mit höchsten Rechten.

## Unterstützte Einrichtung

Zielplattform ist Windows 11 mit Windows PowerShell 5.1, Python 3.12, installiertem
Google Chrome und einer projektlokalen `.venv`. Verwende immer den Interpreter
`.venv\Scripts\python.exe`; verlasse dich nicht auf den Windows-Store-Alias
`python`.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows\Bootstrap-Windows.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows\Setup-BrowserProfile.ps1
```

Danach:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows\Run-LocalBotCheck.ps1 -DryRun
```

## Verifikation bei Änderungen

- Unit-Tests vollständig ausführen; Tests senden keine Nachrichten und buchen nie.
- PowerShell-Dateien mit dem Windows-PowerShell-Parser syntaktisch prüfen.
- Bei Änderungen am Sidecar beweisen: feste Allowlist, nur das Samstags-Zieldatum, sichtbarer
  `channel="chrome"`, dediziertes Profil, keine Safari-UA-Überschreibung, klare
  Exitcodes `0/10/20/30`, keine Produktion-State-Mutation.
- Live-Zugriffe nur mit niedriger Last/Jitter und ausschließlich auf die drei
  konfigurierten Buchungs-URLs. Bei Blockierung abbrechen statt aggressiv neu laden.
- Vor Abschluss `git diff`, `git status --short --ignored` und `git rev-parse
  HEAD` auf Profile, Logs, State-, Ref- oder Secret-Leaks prüfen.

Weitere Einzelheiten und die Ziel-PC-Abnahme stehen in `WINDOWS-11-UEBERGABE.md`.
