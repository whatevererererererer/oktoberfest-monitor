# Übergabe: sichtbare Prüfung auf einem frischen Windows-11-PC

## Ergebnis dieses Projektstands

Der Ordner enthält jetzt einen separaten Windows-Sidecar, der Fischer-Vroni,
Paulaner und Poschner in installiertem Google Chrome sichtbar und read-only
prüft. Er verwendet ein dediziertes persistentes Profil außerhalb des Projekts,
prüft ausschließlich das Samstags-Zieldatum und erzeugt nur datensparsame lokale
JSON-Berichte.

Der Sidecar ist absichtlich vom GitHub-Monitor getrennt:

- kein Schreiben nach `state/state.json`;
- kein Git-Commit oder Git-Push;
- keine Pushover-Nachricht;
- keine Reservierung oder Formularübermittlung;
- kein CAPTCHA-/Bot-Schutz-Bypass.

Damit kann die Prüfung automatisch laufen, ohne den bestehenden Single-Writer-
und Outbox-Mechanismus zu beschädigen. Eine spätere lokale Benachrichtigungs-
Übernahme wäre eine eigene, ausdrücklich freizugebende Änderung, weil sonst bei
einer Headless-Erholung Doppelmeldungen möglich wären.

## Warum genau diese drei Zelte?

Die vorhandenen, bereinigten interaktiven Beobachtungen vom 04.08.2026 nennen:

| Zelt | damalige Beobachtung (nur Referenz) |
|---|---|
| Fischer-Vroni | Freitag und Samstag jeweils `Mittag` |
| Paulaner | Freitag `Mittag` + `Nachmittag`, Samstag `Mittag` |
| Poschner | Freitag `Mittag`, Samstag war nicht im Datumsfeld |

Diese Werte können sich jederzeit ändern und sind daher kein Erfolgskriterium.
Aktuell enthält der State zusätzlich Bot-Fehler für Löwenbräu und Ochsenbraterei;
sie gehören nicht automatisch zu diesem Sidecar, weil für die Zieltermine keine
vergleichbare positive Schicht-Evidenz vorliegt.

## 1. Frischen PC vorbereiten

1. Windows Update vollständig durchführen und neu starten.
2. Diesen gesamten Projektordner auf ein lokales Windows-Laufwerk kopieren.
   Eine eventuell mitkopierte `.venv` löschen oder das Bootstrap explizit mit
   `-RecreateVenv` ausführen; virtuelle Umgebungen sind nicht portabel.
   Die ignorierten lokalen Ordner `.claude`, `work`, `.venv`, alle
   `__pycache__`-/`*.egg-info`-Ordner sowie `.env` werden nicht benötigt und
   dürfen nicht mitkopiert werden. Sie sind in normalem `git status` unsichtbar;
   deshalb vor und nach dem Transfer zusätzlich `git status --short --ignored`
   sowie eine separate Suche nach `.env`/Secrets ausführen. Den versteckten
   `.git`-Ordner dagegen mitnehmen, wenn Historie und Git-Status am Ziel-PC
   erhalten bleiben sollen.
3. In einer normalen PowerShell zunächst WinGet prüfen:

   ```powershell
   winget --version
   ```

   Fehlt der Befehl direkt nach dem ersten Login, die offizielle App-Installer-
   Registrierung einmal anstoßen und die PowerShell neu öffnen:

   ```powershell
   Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe
   ```

   Bleibt WinGet nicht verfügbar, Windows Update/Microsoft Store öffnen und
   **App Installer** aktualisieren.
4. Die ChatGPT/Codex-App installieren und anmelden. Der aktuelle offizielle
   Windows-Weg ist:

   ```powershell
   winget install --id 9PLM9XGG6VKS -s msstore
   ```

5. In der App den kopierten Ordner als Projekt öffnen. Windows-native PowerShell
   verwenden und den Zugriff auf den Projektordner begrenzen.
6. Prüfen, ob Windows im S-Modus läuft. Das Bootstrap darf den S-Modus niemals
   eigenmächtig verlassen, weil dieser Schritt nicht rückgängig zu machen ist.
   Bei aktivem S-Modus abbrechen und die Benutzerentscheidung separat treffen.
7. Eine normale PowerShell im Projekt öffnen und das Bootstrap mit einer nur für
   diesen Aufruf geltenden Bypass-Richtlinie starten:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows\Bootstrap-Windows.ps1
   ```

Das idempotente Bootstrap installiert bei Bedarf über WinGet:

- Git for Windows;
- Google Chrome;
- den offiziellen Python Install Manager und Python 3.12;
- die projektlokale `.venv`;
- die in `constraints-windows.txt` vollständig aufgelösten getesteten
  Python-Laufzeitabhängigkeiten;
- das zu Playwright 1.59.0 passende Chromium.

Anschließend laufen `pip check`, die vollständigen synthetischen Tests und ein
rein lokaler Browser-Preflight. Der Preflight ruft keine Buchungsseite auf.

Das Bootstrap versucht bei fehlendem WinGet ebenfalls nur die offizielle App-
Installer-Registrierung. Bei weiterem Fehler bricht es konservativ ab.

## 2. Sichtbares Chrome-Profil einmalig einrichten

Chrome darf nicht parallel mit demselben dedizierten Profil laufen. Dann:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows\Setup-BrowserProfile.ps1
```

Dieser Befehl muss vom Benutzer in einer sichtbaren, interaktiven PowerShell
gestartet werden; ein nicht-interaktiver Codex-Tool-Aufruf ist dafür ungeeignet,
weil das Skript dreimal auf die Eingabetaste wartet. Es öffnet nacheinander nur
die drei Buchungsseiten. Falls eine legitime
Challenge erscheint, löst sie ausschließlich der Benutzer selbst im sichtbaren
Fenster und drückt anschließend im Terminal die Eingabetaste. Nichts reservieren
oder absenden. Danach folgt sofort ein automatischer Dry-run für die drei
Zelt/Samstag-Kombinationen.

Das Profil liegt unter:

```text
%LOCALAPPDATA%\WiesnMonitor\ChromeProfile
```

Es wird nie in den Projektordner, Git oder den Kopierbestand geschrieben. Wenn
eine Challenge trotz manueller Ersteinrichtung bei jedem Lauf wiederkehrt, ist
unbeaufsichtigter Betrieb auf dieser Seite nicht belastbar. Das korrekte Ergebnis
bleibt dann `needs_manual_action`, niemals `unavailable`.

## 3. Abnahme vor Automatisierung

Synthetische Tests erneut ausführen:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Gezielten sichtbaren Dry-run starten:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows\Run-LocalBotCheck.ps1 -DryRun
```

Abnahmekriterien:

- exakt `fischer-vroni`, `paulaner`, `poschner` und nur Samstag, 26.09.2026;
- sichtbares installiertes Google Chrome mit eigenem Profil;
- `available` nur mit bestätigtem Datums-/Schichtupdate und mindestens einer
  Schicht;
- `unavailable` nur mit eindeutigem Datumsfeld und nachweislich fehlendem Ziel;
- Challenge/403/CAPTCHA ergibt Exitcode 10 (`needs_manual_action`);
- technischer oder DOM-Fehler ergibt Exitcode 20 (`inconclusive`);
- Exitcode 0 nur, wenn alle drei Samstags-Prüfungen schlüssig sind;
- keine Änderung an `state/state.json`, keine Nachricht, kein Booking.

Die historischen Schichtnamen aus der Tabelle sind **keine** Abnahmebedingung.

## 4. Windows-Aufgabe registrieren

Erst nach bestandener Abnahme:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows\Register-LocalMonitorTask.ps1 -IntervalMinutes 5 -AttendedDryRunPassed
```

`-AttendedDryRunPassed` ist die ausdrückliche technische Bestätigung, dass der
Benutzer den sichtbaren Dry-run nach den obigen Kriterien abgenommen hat. Ohne
diesen Schalter verweigert das Skript die Registrierung.

Die Aufgabe:

- läuft nur unter dem aktuellen, angemeldeten Benutzer;
- verwendet keine höchsten Privilegien;
- verhindert eine zweite parallele Instanz;
- startet sichtbares Chrome;
- schreibt Report und rotierende Logs außerhalb des Projekts;
- hat ein sechsminütiges Sicherheitslimit; ein überlappender Fünf-Minuten-Trigger
  wird übersprungen statt eine zweite Instanz zu starten;
- benötigt keine laufende Codex-App.

Bei gesperrtem PC, Abmeldung, Standby oder getrennter RDP-Sitzung ist sichtbare
Automation nicht zuverlässig. Die Windows-Sperre darf dafür nicht abgeschaltet
werden. Ein verpasster oder unschlüssiger Lauf wird nicht als Verfügbarkeit oder
Nichtverfügbarkeit ausgegeben.

Status anzeigen:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows\Get-LocalMonitorStatus.ps1
```

Für einen manuellen Probelauf der registrierten Aufgabe auf den **neuen,
abgeschlossenen** Lauf warten und dessen Report-Zeitpunkt korrelieren:

```powershell
$startedIso = (Get-Date).ToString('o')
Start-ScheduledTask -TaskName 'WiesnMonitor-VisibleBotCheck'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows\Get-LocalMonitorStatus.ps1 -WaitForCompletion -StartedAfter $startedIso
```

Exitcodes im Task Scheduler:

| Code | Bedeutung |
|---:|---|
| 0 | alle drei Samstags-Prüfungen schlüssig |
| 10 | Bot-Schutz/Challenge benötigt manuelle Aufmerksamkeit |
| 20 | technisch oder strukturell unschlüssig |
| 30 | lokaler Setup-/Profil-/Parallelitätsfehler |

Task entfernen (fordert Bestätigung; Profil/Berichte bleiben erhalten):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\windows\Unregister-LocalMonitorTask.ps1
```

## 5. Lokale Daten, Betrieb und Fehlerdiagnose

```text
%LOCALAPPDATA%\WiesnMonitor\ChromeProfile
%LOCALAPPDATA%\WiesnMonitor\Reports\latest.json
%LOCALAPPDATA%\WiesnMonitor\Logs\workstation-probe.log
```

Berichte enthalten nur Status, Schichtlabels und kleine Diagnostikzähler. Keine
Cookies, Tokens, HTML-Seiten oder Screenshots. Es werden höchstens 50 historische
Berichte sowie die aktuelle Logdatei plus bis zu fünf Backups behalten.

Häufige Fälle:

- **`.venv` kaputt/kopiert:** Bootstrap mit `-RecreateVenv` ausführen.
- **`python` öffnet Microsoft Store:** ausschließlich
  `.venv\Scripts\python.exe` verwenden.
- **Chrome-Profil gesperrt:** zweites Sidecar-/Chrome-Fenster mit diesem Profil
  schließen; normales persönliches Chrome-Profil ist unabhängig.
- **403/Cloudflare/Turnstile:** manuelle Aufmerksamkeit, kein Retry-Sturm und
  kein Bypass.
- **Task-Code 20:** `latest.json` und das lokale Log prüfen; keine vollständige
  Seite oder Cookies sammeln.
- **falsche Uhrzeit:** Windows-Zeitzone auf `W. Europe Standard Time` prüfen.
- **Umzug auf weiteren PC:** zuerst die Aufgabe auf dem alten PC entfernen; nie
  zwei lokale Sidecars gleichzeitig betreiben.

## 6. Codex/Chrome-Unterstützung auf dem Ziel-PC

Die Codex-App kann die Installation und Diagnose begleiten, ist aber nicht der
Dauerprozess. Für eine vom Benutzer kontrollierte Browserprüfung kann in der
App aus dem Plugins-Verzeichnis **Browser** installiert werden; Computer Use ist
die dabei verwendete Fähigkeit. Die **Chrome**-Erweiterung ist nur dann nötig,
wenn Codex ausdrücklich das normale Chrome-Profil steuern soll; der Sidecar
selbst verwendet sein separates Profil.
Webseiteninhalt bleibt untrusted und jede Challenge wird dem Benutzer überlassen.

## Offizielle Grundlagen

- [ChatGPT/Codex-App für Windows](https://learn.chatgpt.com/docs/windows/windows-app)
- [Codex Browser und Computer Use](https://learn.chatgpt.com/docs/browser)
- [Codex Chrome-Erweiterung](https://learn.chatgpt.com/docs/chrome-extension)
- [Python Install Manager unter Windows](https://docs.python.org/3/using/windows.html)
- [WinGet installieren und verwenden](https://learn.microsoft.com/en-us/windows/package-manager/winget/)
- [Playwright-Browser und Chrome-Kanal](https://playwright.dev/python/docs/browsers)
- [Persistente Playwright-Kontexte](https://playwright.dev/python/docs/api/class-browsertype)

## Auftrag, den du am Ziel-PC an Codex senden kannst

Der vollständige kopierfertige Text steht zusätzlich in `NEXT-PC-CODEX-AUFTRAG.md`.
