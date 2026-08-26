# Portal-Export-Worker — Interperform

Überträgt Immobilien aus Twenty CRM auf Immobilienportale (OpenImmo 1.2.7 per FTPS).
Läuft als eigener Docker-Container im Compose-Projekt `twenty-vlsc` auf dem
Kunden-VPS (`srv1747052.hstgr.cloud`), gepollt gegen Twenty, ausgehend zum
Portal — kein exponierter Port, kein Webhook.

Fachlicher Hintergrund: `BRAIN-IPR/spec-portal-export.md` (final),
`BRAIN-IPR/plan-portal-export.md` (reviewed, inkl. Spike-Ergebnisse und
Deployment-Status), `BRAIN-IPR/tasks-portal-export.md` (Umsetzungsstand).

## Architektur in Kürze

```
Twenty (Record-Actions RA-1/RA-2, WF-3, WF-4)
  → PortalExport-Datensatz (Warteschlange + Historie)
    → dieser Worker pollt alle 60 s
      → validate.py (Kern blockierend, GEG/Provision warnend)
      → openimmo.py (XML + ZIP, umfang="TEIL" immer)
      → portals.py (FTPS-Upload)
      → Status zurück nach Twenty
```

Module: `worker.py` (Poll-Loop, Dedup/FIFO, Retry), `twenty_client.py`
(GraphQL + Attachment-Download), `openimmo.py` (XML-Generator), `validate.py`
(Validierung), `portals.py` (Portal-Registry + FTPS).

**Kanal `portal: WEBSITE` (ab 26.08.) ist ein Sonderfall:** kein FTPS+XML,
sondern JSON-Payload + signierter HTTPS-POST an die Companion-WordPress-
Plugin-Route (`interperform-website-export`, Gegenstück zum früheren
PropStack-Plugin). Eigenes Modul `website.py` statt eines `portals.py`-
Eintrags (die FTPS-Registry passt strukturell nicht), `worker.process_order`
zweigt für diesen Kanal komplett vor dem FTPS-Pfad ab. Details, Feld-Mapping
und offene Punkte: `BRAIN-IPR/2026-08-26-konzept-website-export.md`.
`website.AKTIV = False`, bis Secret+Endpoint produktiv getestet sind (s. dort
Schritt 6) — bis dahin bricht jeder `WEBSITE`-Auftrag mit einem klaren
`RuntimeError` ab, statt fälschlich als „übermittelt" zu gelten.

## Betrieb

### Logs lesen

```bash
ssh root@srv1747052.hstgr.cloud
docker logs -f twenty-vlsc-portal-export-worker-1
```

Eine Log-Zeile pro verarbeitetem Auftrag (`order_id, portal, aktion, ergebnis,
dauer`). Logs enthalten nie Secrets (Passwörter/Token) — das wurde per `grep`
verifiziert und sollte bei jeder Code-Änderung erneut geprüft werden.

### Neustart

```bash
cd /docker/twenty-vlsc
docker compose restart portal-export-worker
```

Der Container hat `restart: unless-stopped` — er übersteht VPS-Reboots
automatisch. **Wichtig:** Ein manuelles `docker kill`/`docker stop` wird von
Docker als gewollter Stopp gewertet und startet den Container NICHT von
selbst neu (das ist die korrekte, beabsichtigte Semantik von
`unless-stopped` — nicht mit einem Absturz verwechseln). Nach einem
manuellen Stopp: `docker compose up -d portal-export-worker` oder
`docker start twenty-vlsc-portal-export-worker-1`.

Laufende Aufträge gehen bei einem Stopp nicht verloren — sie bleiben
`AUSSTEHEND` in Twenty und werden beim nächsten Poll-Zyklus abgearbeitet.

### Einzellauf / Dry-Run (Debug)

```bash
docker compose run --rm portal-export-worker python worker.py --once
docker compose run --rm portal-export-worker python worker.py --once --dry-run
```

`--dry-run` baut XML/ZIP (inkl. Validierung, ohne Bilder-Download), lädt aber
NICHTS hoch und schreibt KEINEN Status in Twenty — sicher gegen die
Produktivinstanz laufen lassen.

### Warn-View nutzen (FR-015)

Twenty-View **„⚠ Ausstehende Aufträge"** am Objekt `Portal-Export`: Filter
`status = AUSSTEHEND`, Sortierung nach `createdAt` aufsteigend (älteste
oben), Zeitstempel-Spalte sichtbar. Twenty kann keinen Minuten-genauen
„älter als 15 Minuten"-Filter — das ist ein bewusster Kompromiss (s.
plan.md, TASK-005): ein hängender Auftrag steht ganz oben mit sichtbarem
Alter. Regelmäßiger Blick in diese View ersetzt Server-Log-Monitoring für
den Betreiber.

## Neues Portal anbinden (FR-011)

Ausschließlich Config-Änderung, kein Code-Umbau:

1. In `portals.py` einen neuen Eintrag in `PORTALS` ergänzen:
   ```python
   "gloim": {
       "host": "<echter FTP-Host>",
       "port": 21,
       "user": "<echter User>",
       "password_env": "GLOIM_FTP_PASSWORD",
       "encoding": "utf-8",  # vor Aktivierung mit dem Portal klären (ADR-005)
       "anbieternr": "<echte Anbieternummer>",
       "aktiv": True,
   }
   ```
2. Passwort in `/docker/twenty-vlsc/.env` als `GLOIM_FTP_PASSWORD` hinterlegen
   (Rechte 600, nie committen).
3. In Twenty zwei neue Record-Actions nach dem Muster von RA-1/RA-2 anlegen
   (`portal = GLOIM` statt `MEINESTADT`), Portal-SELECT-Option `GLOIM`
   existiert am `PortalExport`-Objekt bereits.
4. Encoding **vor** dem ersten Test mit dem Portal-Ansprechpartner klären
   (viele ältere OpenImmo-Importer erwarten ISO-8859-1) — s. ADR-005.
5. Code auf den VPS spiegeln (`rsync`, s. u.) und Container neu bauen:
   `docker compose up -d --build portal-export-worker`.

Kein Redeploy der Twenty-Container nötig, keine Änderung an `worker.py`/
`openimmo.py` erforderlich.

## Verhalten bei Twenty-Update

Diese Lösung verlässt sich bewusst auf zwei **validierte Bugs in Twenty
v2.11.2**, die bei einem Twenty-Update repariert (oder anders kaputt) sein
könnten:

1. FIND-Step-Filter mit Trigger-Variablen werden nicht aufgelöst — die
   Workflows RA-1/RA-2/WF-3/WF-4 filtern deshalb bewusst im CODE-Step statt
   im FIND-Filter.
2. Logic-Function-Fetches gegen die eigene Twenty-API scheitern
   („Invalid auth context") — die Workflows machen deshalb keine
   Self-API-Calls.

**Nach jedem Twenty-Versions-Update:** RA-1, RA-2, WF-3, WF-4 einmal mit
einem `[TEST]`-Datensatz durchspielen (siehe Testfälle in
`tasks-portal-export.md` TASK-006/007/008). Wenn Twenty die Bugs behebt,
funktioniert das bestehende Muster trotzdem weiter (es ist ein Workaround,
keine Ausnutzung eines Fehlers) — es kann dann nur vereinfacht werden.

## Key-/Passwort-Rotation

- **Twenty-API-Key** (`TWENTY_API_TOKEN`, aktuell „API Token Portal-Sync"):
  neuen Key in Twenty → Settings → APIs & Webhooks anlegen (erfordert
  eingeloggten User — geht NICHT per API-Key-Automatisierung), Wert in
  `/docker/twenty-vlsc/.env` ersetzen, danach
  `docker compose restart portal-export-worker`. Alten Key in Twenty
  widerrufen (`revokeApiKey`), sobald der neue läuft.
- **Portal-FTP-Passwort** (z. B. `MEINESTADT_FTP_PASSWORD`): neues Passwort
  vom Portal-Betreiber holen, in `.env` ersetzen, Container neu starten.
  **Cutover-Regel beachten:** PropStack und Twenty dürfen nie gleichzeitig
  denselben Portal-Account bespielen (s. spec.md Domain Context).
- Vor jeder Änderung an `.env`/`docker-compose.yml`: Backup anlegen
  (Konvention dieses Projekts: `<datei>.bak-YYYY-MM-DD-<kurzbeschreibung>`).

## DELETE-Historie-Doppelungen (WF-3 / RA-2 / WF-4)

Drei Wege können einen DELETE-Auftrag für dasselbe Objekt×Portal erzeugen:
RA-2 (manuell), WF-4 (automatisch bei Soft-Delete der Immobilie). WF-3
selbst erzeugt **keinen** DELETE-Auftrag, nur eine Erinnerungs-Task für den
Makler (bewusste Never-Boundary, s. plan.md §7). Löst ein Makler RA-2 aus
und löscht die Immobilie danach trotzdem, entstehen zwei portalExport-
Datensätze mit `aktion=DELETE` — das ist **gewollt und harmlos**: das Portal
verarbeitet ein wiederholtes DELETE derselben Objektnummer idempotent, die
Twenty-Historie zeigt dann eben zwei Einträge statt einem. Kein Fehlerfall,
keine Aktion nötig.

## GEG-Validierung: warnend → blockierend umstellen

Aktuell (Entscheidung 14.07., ADR-004) lösen fehlende GEG-§87-Angaben
(Energieausweis-Art, Endenergie-Kennwert, Effizienzklasse, Energieträger,
Baujahr) und die Provisionsangabe nur eine **Warnung** aus, blockieren aber
nicht. Sobald der Bestand gepflegt ist (Paul), Umstellung auf blockierend:

```python
# validate.py
GEG_BLOCKIEREND = True  # war: False
```

Danach Container neu bauen und starten. **⚠️ Ask First** (plan.md §7) —
diese Umstellung nicht ohne Rücksprache mit Paul/AR vornehmen, sie kann
laufende Übertragungen blockieren, deren Pflichtfelder noch fehlen.

## Code-Stand / Deployment

- Lokales Arbeitsverzeichnis (kanonische Quelle mit Git-Historie):
  `/Users/ar/KIOS/WS-DH/interperform/portal-export-worker/`
- Deploy auf den VPS: Code-Verzeichnis nach
  `/docker/twenty-vlsc/portal-export-worker/` spiegeln, dann
  ```bash
  rsync -a --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
    portal-export-worker/ root@srv1747052.hstgr.cloud:/docker/twenty-vlsc/portal-export-worker/
  ssh root@srv1747052.hstgr.cloud \
    "cd /docker/twenty-vlsc && docker compose up -d --build portal-export-worker"
  ```
- Der Compose-Service-Eintrag liegt in `/docker/twenty-vlsc/docker-compose.yml`
  auf dem VPS (Backup vor jeder Änderung: `docker-compose.yml.bak-YYYY-MM-DD-*`).
- **Offener Punkt:** Eine Spiegelung dieses Repos in ein zentrales
  digitaleheimat-Git-Hosting war als Vorgabe genannt, aber in dieser Session
  war kein Remote dafür bekannt/konfiguriert. Das lokale Repo unter obigem
  Pfad ist bis dahin die vollständige, versionierte Quelle (jeder Task ist
  ein eigener Commit).

## Tests

```bash
cd /Users/ar/KIOS/WS-DH/interperform/portal-export-worker
.venv/bin/pytest tests -q
```

Stand zuletzt: alle Tests grün (siehe Commit-Historie für Testzahlen je Modul).

## Bekannte offene Punkte (aus TASK-013/014, vor Produktivbetrieb prüfen)

- Mietobjekte teilen sich aktuell den `kaufpreis`-Knoten der OpenImmo-XML-
  Vorlage mit Kaufobjekten (kein eigener Mietpreis-Knoten) — bei Bedarf
  `FELD_MAPPING` in `openimmo.py` erweitern.
- `bundesland`, `objektbeschreibung`, `wohnungtyp` sind mangels
  entsprechender Twenty-Felder hart auf leer/`"ETAGE"` gesetzt.
- Retry/Backoff (FR-016) greift nur bei Fehlern während des Uploads, nicht
  bei transienten Twenty-API-Fehlern während `get_immobilie`/`lade_bilder`.
- Bildformat-Regel: nur JPEG/PNG werden akzeptiert (Magic-Byte-Prüfung, nicht
  nur `fileCategory`) — im Bestand existiert mindestens ein PDF mit
  `fileCategory: IMAGE`, das beim Export zu einem Fehler ohne Retry führt,
  bis es in Twenty korrigiert wird.
