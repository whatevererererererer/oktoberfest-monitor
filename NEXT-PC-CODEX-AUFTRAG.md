# Auftrag für Codex auf dem neuen Windows-11-PC

Lies zuerst vollständig `AGENTS.md`, `README.md` und
`WINDOWS-11-UEBERGABE.md`. Richte diesen frisch installierten Windows-11-PC für
die sichtbare, read-only Prüfung von `fischer-vroni`, `paulaner` und `poschner`
ein und führe die Übergabe bis zu einem belegten Ergebnis aus.

Arbeite in dieser Reihenfolge:

1. Prüfe `git status --short --ignored`, den aktuellen `git rev-parse HEAD`,
   Windows-Version, Architektur, S-Modus, WinGet, Python, Git und Chrome.
   Erhalte alle vorhandenen Projektänderungen und die komplette Git-/State-
   Historie. Bei aktivem S-Modus abbrechen; ihn niemals eigenmächtig verlassen.
   Führe keine Bereinigung oder Löschung ohne ausdrückliche Freigabe aus. Einzig
   eine eventuell mitkopierte projektlokale `.venv` darfst du für diesen Auftrag
   über die geprüfte Option `-RecreateVenv` exakt löschen und neu erzeugen; keine
   anderen Pfade oder ignorierten Dateien löschen.
2. Führe `scripts/windows/Bootstrap-Windows.ps1` mit
   `powershell.exe -NoProfile -ExecutionPolicy Bypass -File` aus; falls `.venv`
   vorhanden/kopiert ist, zusätzlich mit `-RecreateVenv`. Wenn Installation oder
   Schreibzugriff außerhalb des Projekts eine Codex-Freigabe benötigt, fordere
   sie eng begrenzt an. Verwende Python 3.12 und immer den `.venv`-Interpreter.
3. Verifiziere `pip check`, Playwright 1.59.0, das passende Chromium, den
   installierten Google-Chrome-Kanal und die komplette Unit-Test-Suite. Keine
   Buchungsseite ist für diesen Preflight nötig.
4. Bitte **mich**, `scripts/windows/Setup-BrowserProfile.ps1` in einer sichtbaren,
   interaktiven PowerShell mit `powershell.exe -NoProfile -ExecutionPolicy
   Bypass -File` zu starten; führe diesen dreimal auf `ENTER` wartenden Schritt
   nicht als nicht-interaktiven Tool-Aufruf aus. Begleite mich durch die
   Ersteinrichtung des dedizierten Profils. Ich allein löse eine
   legitime Challenge. Du umgehst weder CAPTCHA noch Turnstile/Bot-Schutz und
   klickst keinen finalen Reservierungs-/Submit-Schritt.
5. Führe danach `scripts/windows/Run-LocalBotCheck.ps1 -DryRun` aus und verifiziere:
   exakt drei Slugs, ausschließlich Samstag 26.09.2026, klare datenkorrelierte
   Resultate, Exitcode,
   unveränderten SHA-256 von `state/state.json`, keine Nachricht und keine
   sensitiven Artefakte im Projekt.
6. Wenn ein Resultat `needs_manual_action` oder `inconclusive` ist, diagnostiziere
   konservativ anhand der kleinen Diagnostik. Speichere keine HTML-Seite,
   Cookies, Tokens oder Screenshots im Projekt. Erkläre die verbleibende Grenze;
   erfinde keinen Stealth-/Bypass-Weg.
7. Registriere die Windows-Aufgabe erst, wenn der attended Dry-run die in
   `WINDOWS-11-UEBERGABE.md` genannten Kriterien erfüllt und ich das bestätige.
   Nutze den aktuellen Benutzer, `Interactive`/nur bei Anmeldung, eingeschränkte
   Rechte, keine parallele Instanz und das vorhandene Registrierskript mit dem
   dann ausdrücklich erlaubten Schalter `-AttendedDryRunPassed`.
8. Erfasse den Startzeitpunkt, starte die registrierte Aufgabe einmal und rufe
   `Get-LocalMonitorStatus.ps1 -WaitForCompletion -StartedAfter <Startzeitpunkt>`
   auf. Warte begrenzt auf genau diesen abgeschlossenen Lauf; prüfe erst dann
   frischen Report, Exitcode und nächsten Lauf.
   Codex darf danach geschlossen werden.
9. Prüfe abschließend `git status --short --ignored`, `git diff` und den HEAD-
   Wert vor/nach: keine Git-Ref-Änderung, `.venv`, `.env`, `.claude`, `work`,
   `__pycache__`, `*.egg-info`, Profile, Logs, Reports, Geheimnisse oder
   Produktions-State-Änderungen im Transferbestand. Ignorierte Dateien sind in
   normalem `git status` unsichtbar und müssen separat geprüft werden. Liefere
   konkrete Start-/Stop-/Statusbefehle und alle verbleibenden Grenzen.

Wichtige Architekturgrenzen:

- Der GitHub-Workflow bleibt alleiniger Writer von `state/state.json` und seiner
  Outbox. Der Windows-Sidecar schreibt ausschließlich unter
  `%LOCALAPPDATA%\WiesnMonitor`.
- Dieser Auftrag aktiviert keine lokale Pushover-Zustellung. Eine solche
  Übernahme benötigt eine eigene Dedupe-/Ownership-Entscheidung, damit GitHub
  und Windows nicht doppelt alarmieren.
- Historische Beobachtungen (`Fischer Mittag/Mittag`, `Paulaner
  Mittag+Nachmittag/Mittag`, `Poschner Mittag/Zieldatum fehlte`) sind nur
  Referenz und dürfen nicht als aktuelle Sollwerte hart codiert werden.
- Löwenbräu und Ochsenbraterei haben im aktuellen State ebenfalls Bot-Fehler,
  werden aber ohne neue positive Ziel-/Schicht-Evidenz nicht automatisch in die
  feste Dreier-Allowlist aufgenommen.

Am Ende brauche ich:

1. installierte/geprüfte Versionen,
2. Testergebnisse,
3. Zelt/Datum-Evidenztabelle des Dry-runs,
4. State-Hash vor/nach,
5. Task-Scheduler-Status und letzten Exitcode (nur wenn registriert),
6. Pfade zu lokalem Profil, Report und Log,
7. verbleibende Risiken und manuelle Schritte.
