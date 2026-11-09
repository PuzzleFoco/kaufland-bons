#!/usr/bin/env bash
# End-to-end check of the image without Kaufland credentials:
# pushes fixture receipts into a Paperless API imitation and verifies metadata,
# the ledger (no duplicate uploads), the import folder and the error path.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${1:-kaufland-bons:ci}"
ENGINE="${DOCKER:-}"
if [ -z "$ENGINE" ]; then
  if command -v docker >/dev/null 2>&1; then ENGINE=docker; else ENGINE=podman; fi
fi
PORT="$(( 18100 + RANDOM % 500 ))"
WORK="$(mktemp -d)"
MOCK_PID=""

cleanup() {
  # Exit-Status des Tests bewahren: der Trap darf kein Ergebnis umdrehen.
  status=$?
  if [ -n "$MOCK_PID" ]; then kill "$MOCK_PID" 2>/dev/null || true; fi
  rm -rf "$WORK" 2>/dev/null || true
  if [ -d "$WORK" ]; then
    # Die Container laufen als root und legen z.B. import/done/ mit root-Rechten
    # an; der Runner-User darf darin nicht loeschen. Deshalb im Container putzen.
    $ENGINE run --rm -v "$WORK":/w --entrypoint sh "$IMAGE" \
      -c 'rm -rf /w/* /w/.[!.]* 2>/dev/null || true' >/dev/null 2>&1 || true
    rm -rf "$WORK" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$WORK"/{config,receipts/2026-11/2026-11-03,import,mock,config-error}

python3 tests/mock_paperless.py "$PORT" "$WORK/mock" &
MOCK_PID=$!
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$PORT/api/tags/?name__iexact=kaufland&page_size=1" >/dev/null && break
  sleep 0.3
done
curl -sf "http://127.0.0.1:$PORT/api/tags/?name__iexact=kaufland&page_size=1" >/dev/null \
  || fail "Mock-Paperless startet nicht"

echo "== Fixtures erzeugen (Chromium im Image) =="
if ! $ENGINE run --rm -i --shm-size=512m -v "$WORK":/w --entrypoint python "$IMAGE" - >"$WORK/fixture.log" 2>&1 <<'PY'
from playwright.sync_api import sync_playwright
html = """<html><body style="font-family:sans-serif"><h2>Kaufland</h2>
<p>Karlsruhe, Musterstr. 1</p><p>03.11.2026 17:42</p><hr>
<p>Milch 1,29<br>Brot 2,49<br>Kaffee 8,99</p><hr><b>SUMME 42,17 EUR</b></body></html>"""
targets = [
    ("/w/receipts/2026-11/2026-11-03/2026-11-03_7f3a2b.pdf", ""),
    ("/w/receipts/2026-11/2026-11-03/2026-11-03_7f3a2b-return-1.pdf", "RETOURE"),
    ("/w/import/kassenbon_alt_2024-03-11.pdf", ""),
]
with sync_playwright() as p:
    browser = p.chromium.launch()
    for path, note in targets:
        page = browser.new_page()
        page.set_content(html + (f"<p>{note}</p>" if note else ""))
        page.pdf(path=path, format="A4")
        page.close()
    browser.close()
PY
then
  echo "Fixture-Erzeugung fehlgeschlagen:" >&2
  tail -25 "$WORK/fixture.log" >&2
  fail "Chromium konnte im Container keine PDFs erzeugen"
fi
cat "$WORK/fixture.log"

python3 - "$WORK" <<'PY'
import json, pathlib, sys
work = pathlib.Path(sys.argv[1])
summary = work / "receipts/2026-11/2026-11-03/2026-11-03_7f3a2b_summary.json"
summary.write_text(json.dumps({
    "id": "7f3a2b", "date": "2026-11-03", "timestamp": "2026-11-03T17:42:00+01:00",
    "store": {"name": "Kaufland Karlsruhe", "city": "Karlsruhe"},
    "sum": 4217, "saving": 250, "currency": "EUR", "positions": [{}, {}],
}), encoding="utf-8")
PY

run_export() {
  $ENGINE run --rm --network host --shm-size=512m \
    -e PUID=0 -e PGID=0 -e KAUFLAND_EXPORT=false \
    -e PAPERLESS_URL="http://127.0.0.1:$PORT" -e PAPERLESS_TOKEN=dummy \
    -e PAPERLESS_TAGS=kaufland,bon,kassenbon \
    -v "$WORK/config":/config -v "$WORK/receipts":/receipts -v "$WORK/import":/import \
    "$IMAGE" once
}

echo "== Lauf 1 =="
run_export | tee "$WORK/run1.log"
grep -q "3 Beleg(e) an Paperless" "$WORK/run1.log" || fail "Lauf 1 hat nicht 3 Belege uebertragen"

python3 - "$WORK" <<'PY' || exit 1
import json, pathlib, sys
work = pathlib.Path(sys.argv[1])
uploads = json.loads((work / "mock/_uploads.json").read_text(encoding="utf-8"))
assert len(uploads) == 3, f"erwartet 3 Uploads, waren {len(uploads)}"
expected = {"Kaufland 2026-11-03 42,17 €", "Kaufland Bon 2026-11-03 (Retoure)",
            "Kaufland Bon 2024-03-11"}
titles = {u["fields"]["title"][0] for u in uploads}
assert titles == expected, f"Titel weichen ab: {titles}"
for upload in uploads:
    fields = upload["fields"]
    assert len(fields["tags"]) == 3, f"Tags fehlen: {fields['tags']}"
    assert fields["correspondent"], "Korrespondent fehlt"
    assert fields["document_type"], "Dokumenttyp fehlt"
    assert fields["created"], "Erstellungsdatum fehlt"
    assert upload["files"][0]["bytes"] > 1000, "PDF ist leer"
assert (work / "import/done/kassenbon_alt_2024-03-11.pdf").exists(), "Import-Datei nicht verschoben"
print("Metadaten, Tags, Import-Verschiebung: ok")
PY

echo "== Lauf 2 (Duplikate) =="
run_export | tee "$WORK/run2.log"
grep -q "0 Beleg(e) an Paperless" "$WORK/run2.log" || fail "Lauf 2 hat erneut hochgeladen"
python3 - "$WORK" <<'PY' || exit 1
import json, pathlib, sys
uploads = json.loads((pathlib.Path(sys.argv[1]) / "mock/_uploads.json").read_text(encoding="utf-8"))
assert len(uploads) == 3, f"Duplikate: {len(uploads)} Uploads"
print("Ledger verhindert Doppel-Upload: ok")
PY

echo "== Fehlerpfad (kein Login) =="
set +e
$ENGINE run --rm --network host \
  -e PUID=0 -e PGID=0 -e KAUFLAND_EXPORT=true -e PAPERLESS_PUSH=false \
  -e NOTIFY_URL="http://127.0.0.1:$PORT/notify" \
  -v "$WORK/config-error":/config -v "$WORK/receipts":/receipts -v "$WORK/import":/import \
  "$IMAGE" once > "$WORK/error.log" 2>&1
status=$?
set -e
[ "$status" -eq 1 ] || fail "Exit-Code ohne Login war $status, erwartet 1"
grep -q "Keine Anmeldung gefunden" "$WORK/error.log" || fail "Meldung ohne Login fehlt"
[ -s "$WORK/mock/_notifications.json" ] || fail "Webhook wurde nicht ausgeloest"
python3 - "$WORK" <<'PY' || exit 1
import json, pathlib, sys
status = json.loads((pathlib.Path(sys.argv[1]) / "config-error/status.json").read_text())
assert status["ok"] is False and status["export_ok"] is False, status
print("Fehlerpfad, Statusdatei, Webhook: ok")
PY

echo "ALLE TESTS GRUEN"
