# kaufland-bons — Kaufland-Kassenbons automatisch nach Paperless

Container, der die digitalen Kassenbons der Kaufland Card exportiert (JSON, HTML,
JPG, A4-PDF) und jedes neue PDF mit Metadaten an Paperless-ngx übergibt.

- **Erster Lauf:** komplette Historie (Kaufland hält Belege nur ~3 Jahre vor).
- **Danach:** täglich inkrementell ab dem 1. des Vormonats, idempotent.
- **Kein Doppel-Upload:** Ledger (`pushed.json`) mit SHA-256 pro Datei.
- **Metadaten:** Korrespondent `Kaufland`, Dokumenttyp `Kassenbon`, Tags
  `kaufland,bon`, Titel `Kaufland 2026-11-03 42,17 €`, Erstellungsdatum = Einkaufsdatum.
  Fehlende Korrespondenten/Tags/Typen werden in Paperless angelegt.
- **Fehlerfall:** `status.json` + optionale Webhook-Benachrichtigung (HA, ntfy),
  z. B. wenn der Kaufland-Login abgelaufen ist.

Grundlage ist der inoffizielle Client
[BlackDark/kaufland-api](https://github.com/BlackDark/kaufland-api), gepinnt auf
Commit `66e3946` (`ARG KAUFLAND_REF` in der Dockerfile). Es gibt **keine**
offizielle API — siehe Grenzen unten.

## Image beziehen

```sh
docker pull ghcr.io/<owner>/kaufland-bons:latest
```

Tags: `latest` (Default-Branch), `v1.2.3` (Release), `sha-<kurz>` und
`YYYYMMDD` (jeder Build, gut für Rollback: alten Tag setzen, Container neu
starten, Ledger bleibt unberührt).

Unraid: Am Release hängen **zwei** fertig gerenderte Templates —
`kaufland-bons-login.xml` (einmalig für den Login) und `kaufland-bons.xml`
(Dauerbetrieb). Beide über *Docker → Template hinzufügen* einlesen.

Reihenfolge: erst **login** starten, im WebUI-Fenster anmelden, Container
beendet sich → Login-Template wieder entfernen. Dann **kaufland-bons** starten
und dort `PAPERLESS_URL` und `PAPERLESS_TOKEN` eintragen.

Ohne Template, per Compose: `docker-compose.yml` zieht bereits
`ghcr.io/puzzlefoco/kaufland-bons:latest` — nur `PAPERLESS_URL` und
`PAPERLESS_TOKEN` setzen, dann `docker compose up -d`.

Das Paket ist öffentlich (anonamer Pull getestet). Falls du es auf privat
stellst: in Unraid einmal `docker login ghcr.io` mit einem Personal Access
Token (`read:packages`) ausführen.

## Voraussetzungen

- Kaufland Card (digitaler Kassenbon in der App aktiviert), Konto mit E-Mail/Passwort.
- Paperless-ngx — entweder API-Token (Einstellungen → „API-Token") oder der
  Consume-Ordner als gemounteter Pfad (`PAPERLESS_MODE=consume`).
- Unraid/Docker, x86_64.

## 1. Einmalig anmelden (auth.json)

Der Login braucht einen **Browser** (E-Mail, Passwort, ggf. 2FA) — dafür gibt es
drei Wege. Gespeichert wird immer nur ein Refresh-Token (`/config/auth.json`,
Modus 0600); das Passwort liegt nie im Container.

**Variante A — Login-Fenster im Container (kein zweiter Rechner nötig):**

Das Image bringt Xvfb, x11vnc und noVNC mit. Ein separater Einmal-Container
öffnet den Login als Webseite; der Dauerbetrieb hat keinen VNC-Port.

Unraid: Template `kaufland-bons-login.xml` importieren, starten, das WebUI-Symbol
öffnet `http://<nas>:6080/vnc.html?autoconnect=1&resize=scale`. Passwort ist
`VNC_PASSWORD` oder steht in `/mnt/user/appdata/kaufland-bons/vnc-password.txt`
(Modus 0600, absichtlich nicht im Log). Nach Login + 2FA schreibt der Container
`auth.json` und beendet sich — Template/Container danach wieder entfernen.

Docker/Compose:

```sh
VNC_PASSWORD='<frei waehlen>' docker compose --profile login up login
```

Sicherheit: noVNC läuft **unverschlüsselt** (ws) — nur im LAN, kein
Port-Forwarding ins Internet, Container nach dem Login löschen. Wer auf die
Passwortabfrage verzichten will (Heimnetz, Login-Fenster lebt nur Minuten):
`VNC_PASSWORD=none` startet x11vnc mit `-nopw`, dann sieht und bedient jeder im
LAN das Fenster. Der
Login-Container und der Dauer-Container teilen `/config`; der Dauer-Container
liest `auth.json` beim nächsten Lauf.

**Variante B — auf einem Rechner mit Browser:**

```sh
git clone https://github.com/BlackDark/kaufland-api /tmp/kaufland-api
cd /tmp/kaufland-api
git checkout 66e39463a4c7bf0d0adc1d2b4474548fbd6f7461
sed -i 's/requires-python = ">=3.15"/requires-python = ">=3.14"/' pyproject.toml
rm -f uv.lock .python-version
uv sync
uv run kaufland login --country DE --language de --auth-file ./auth.json
```

`auth.json` nach `/mnt/user/appdata/kaufland-bons/auth.json` kopieren.

**Variante C — aus dem Image mit X11 (Linux-Desktop, ohne noVNC):**

```sh
xhost +local:
docker run --rm -it --network host \
  -e DISPLAY -e PUID=$(id -u) -e PGID=$(id -g) \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v /mnt/user/appdata/kaufland-bons:/config \
  ghcr.io/<owner>/kaufland-bons:latest login
xhost -local:
```

## 2. Starten

Wichtige Variablen:

| Variable | Default | Bedeutung |
|---|---|---|
| `PAPERLESS_MODE` | `api` wenn URL+Token gesetzt, sonst `off` | `api` = REST mit Metadaten, `consume` = PDFs in einen Ordner, `off` = nur exportieren |
| `PAPERLESS_URL` | – | nur `api`: z. B. `http://192.168.1.10:8000` |
| `PAPERLESS_TOKEN` | – | nur `api`: API-Token aus Paperless |
| `CONSUME_DIR` | `/consume` | nur `consume`: Zielordner (Paperless-Consume) |
| `EXPORT_FORMATS` | `pdf,json` | welche Dateien im Archiv bleiben: `pdf`, `jpg`, `html`, `json` |
| `PAPERLESS_CORRESPONDENT` | `Kaufland` | wird bei Bedarf angelegt |
| `PAPERLESS_DOCUMENT_TYPE` | `Kassenbon` | leer = kein Dokumenttyp |
| `PAPERLESS_TAGS` | `kaufland,bon` | kommasepariert |
| `PAPERLESS_PUSH` | `true` | `false` = nur exportieren |
| `RUN_AT` | `04:30` | tägliche Laufzeit, Zeitzone `TZ` |
| `RUN_ON_START` | `true` | sofort ein Lauf beim Start |
| `KAUFLAND_EXPORT` | `true` | `false` = nur Dateien aus `/receipts` und `/import` pushen |
| `KAUFLAND_EXPORT_ARGS` | – | z. B. `--period this-year` oder `--since 2024-07-01` |
| `NOTIFY_URL` | – | Webhook bei Fehlern (HA-Webhook oder ntfy) |
| `PUID` / `PGID` | `99` / `100` | Dateirechte auf dem Array |
| `VNC_PASSWORD` | – | nur Login-Container (`login-web`): leer = Zufallspasswort in `/config/vnc-password.txt`, `none` = ohne Abfrage (nur vertrauenswürdiges LAN) |
| `LOGIN_PORT` | `6080` | nur Login-Container: Port der Weboberfläche |

## Verzeichnisse

| Mount | Inhalt |
|---|---|
| `/config` | `auth.json` (Login), `pushed.json` (Ledger), `status.json`, `initial_done` |
| `/receipts` | Archiv: `2026-11/2026-11-03/2026-11-03_<id>.{json,html,jpg,pdf}` |
| `/import` | optional: alte PDFs hier ablegen → werden gepusht und nach `done/` verschoben |

`/import` ist der Weg für Belege vor Juli 2024 (die App exportierte damals reine
Bild-PDFs) oder für PDFs, die du von Hand gespeichert hast.

## Ohne Paperless-API (Consume-Ordner)

`PAPERLESS_MODE=consume` — dann braucht der Container **weder URL noch Token**,
nur einen Pfad: das Verzeichnis, das dein Paperless als Consume-Ordner nutzt.

```yaml
environment:
  PAPERLESS_MODE: consume
volumes:
  - /mnt/user/paperless/consume:/consume
```

Die PDFs werden mit lesbarem Namen kopiert (`Kaufland 2026-11-03 42-17EUR.pdf`),
das Archiv in `/receipts` bleibt unangetastet. Kopiert wird über einen
Staging-Ordner (`.incoming`), damit Paperless keine halben Dateien einsammelt;
das ledger verhindert, dass derselbe Beleg erneut landet.

**Nicht** `/receipts` auf den Consume-Ordner zeigen lassen: Paperless verschiebt
eingelesene Dateien aus dem Ordner, dein Archiv wäre nach dem ersten Lauf leer.
Genau deshalb kopiert der `consume`-Modus, statt zu verschieben.

Was du dabei verlierst: Titel, Datum, Tags und Korrespondent kommen nicht mehr
aus dem Container. Paperless nutzt den Dateinamen als Titel (daher die lesbaren
Namen) und muss Korrespondent `Kaufland`, Tags und Dokumenttyp über eigene
Regeln zuordnen; das Datum zieht es meist aus dem PDF-Text.

## Formate

Standard ist `pdf,json`. Was du behältst, hängt am Ziel:

| Format | Nutzen | Für Paperless |
|---|---|---|
| `pdf` | A4-Rendering mit Textebene, wird immer behalten sobald Paperless aktiv ist | **ja** |
| `json` | Rohdaten (Positionen, Preise), Basis für `convert` und Auswertungen | nein, aber winzig |
| `jpg` | Vorschau/Weitergeben per Handy | nein — Paperless würde es als **zweites** Dokument OCRn |
| `html` | nur zum Anschauen im Browser | nein — Paperless lehnt unbekannte Endungen im Consume-Ordner ab |

Paperless-ngx kann laut FAQ PDF, PNG/JPEG/TIFF/GIF/WebP (OCR), Plaintext und
— nur mit Tika — Office-Formate. **HTML gehört nicht dazu.** Deshalb ist
`pdf,json` die sinnvolle Vorgabe; `jpg` und `html` erzeugen nur Ballast.

Der Upstream rendert immer alle vier Formate; `EXPORT_FORMATS` entfernt die
unerwünschten nach jedem Lauf (Kommando `prune`). Das spart Plattenplatz, nicht
Renderzeit — die Chromium-Läufe finden trotzdem statt. Für ein bestehendes
Archiv einmalig:

```sh
docker exec kaufland-bons python /app/runner.py prune
```

## Betrieb

```sh
docker logs -f kaufland-bons          # Verlauf, ein Block pro Lauf
docker exec kaufland-bons python /app/runner.py once    # manuell auslösen
docker exec kaufland-bons python /app/runner.py push    # nur Paperless-Abgleich
docker exec kaufland-bons python /app/runner.py health  # Healthcheck
```

`status.json` enthält `last_run`, `ok`, `pushed`, `errors`.

## Aktualisierung

Drei getrennte Dinge, die man nicht verwechseln sollte:

1. **Python-Pakete** (`requirements.txt`) und **GitHub Actions** und das
   **Basis-Image**: Dependabot, wöchentlich montags (`dependabot.yml`), PRs
   laufen durch die CI.
2. **Upstream-Client** (der gepinnte Commit): Dependabot kann Dockerfile-ARGs
   nicht verfolgen. `upstream-check.yml` prüft montags den Upstream-HEAD und
   öffnet einen PR mit dem neuen `KAUFLAND_REF` — baut und testet das Image
   vorher im selben Job (PRs mit dem Standard-Token lösen keine weiteren
   Workflows aus). Merge erst nach einem echten Lauf: die App-API ist inoffiziell.
3. **Dein Container:** Image neu ziehen und starten.

```sh
docker compose pull && docker compose up -d
# Unraid: Docker-Tab → „Check for Updates" → apply
```

Bewusst **kein Watchtower-Autoupdate**: bricht ein Upstream-Update die API,
möchtest du das an einem Lauf sehen und nicht stillschweigend über Wochen.
Wer trotzdem automatisieren will: Watchtower nur mit `monitor-only` und
Benachrichtigung betreiben.

## Selbst bauen (Fallback)

```sh
git clone <dieses-repo> && cd kaufland-bon-exporter
docker build -t kaufland-bons:latest .
```

Das Image ist ~1,8 GB (Chromium für PDF/JPG-Rendering, plus Xvfb/noVNC für den
Login im Browser). Bauzeit auf einem Unraid-Server: einige Minuten.

## Grenzen (ehrlich)

- **Inoffiziell.** Die App-API kann jederzeit brechen (Client-ID, `app-version`,
  Endpunkt). Deshalb der wöchentliche Upstream-Check statt „läuft für immer".
- **Refresh-Token läuft ab** oder wird widerrufen → Re-Login nötig, interaktiv
  (2FA). Der Container meldet das per `NOTIFY_URL`, nicht per Wunder.
- **Nur Bons mit gescannter Kaufland Card.** Ohne Scan existiert kein digitaler
  Beleg; Papierbons weiter über Paperless scannen.
- **Historie ~3 Jahre.** Was Kaufland löscht, ist danach weg — daher der
  Voll-Export beim ersten Lauf.
- **ToS-Graubereich:** eigene Daten, privates Umfeld, Rate 1×/Tag. Keine
  Garantie, dass Kaufland das dauerhaft duldet.
- **Ein Haushalt, ein Konto.** Kein Multi-User, keine Retry-Queue für Paperless-
  Ausfälle (der nächste Lauf holt es nach, weil das Ledger erst nach Erfolg schreibt).

## Tests

`tests/container_test.sh` läuft in der CI (`test.yml`) bei jedem Push und Pull
Request: baut das Image, erzeugt Test-PDFs mit dem Chromium im Image, schiebt sie
gegen `tests/mock_paperless.py` und prüft Metadaten, Tags, Ledger, die
`/import`-Verschiebung und den Fehlerpfad ohne Login.

Lokal:

```sh
docker build -t kaufland-bons:ci .
./tests/container_test.sh kaufland-bons:ci
```

Zusätzlich manuell verifiziert (nicht in der CI, weil Port/Bildschirm nötig):

- `login-web` liefert `http://…:6080/vnc.html` mit HTTP 200; Xvfb, openbox,
  x11vnc, websockify und der Client laufen, Chromium startet **sichtbar** auf
  `:99`, das echte Kaufland-Formular rendert (Titel „Kaufland - Einloggen").
- Ohne `VNC_PASSWORD` entsteht `/config/vnc-password.txt` mit Modus 0600 —
  das Passwort erscheint nicht im Log.

Nicht automatisiert testbar (braucht echte Zugangsdaten): der eigentliche Login
und ein echter Transaktionsabruf.

## Lizenz

GPL-3.0-only (`LICENSE`), wie der Upstream-Client. Der Quellcode des im Image
enthaltenen Clients ist unter
`https://github.com/BlackDark/kaufland-api/tree/66e39463a4c7bf0d0adc1d2b4474548fbd6f7461`
abrufbar; diese Dockerfile baut ihn aus genau diesem Stand.
