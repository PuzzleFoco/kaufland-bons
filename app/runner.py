#!/usr/bin/env python3
"""Kaufland Card receipts -> Paperless-ngx.

Exports receipts with the (unofficial) Kaufland Card API client, then uploads
every new PDF to Paperless with metadata. Keeps a ledger so nothing is pushed
twice, and can also drain a plain import folder (old PDFs pulled by hand).

Only one secret is used: the Kaufland refresh token in /config/auth.json and
the Paperless API token from the environment. Neither is ever logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

# Konfiguration aus /config/kaufland.env (KEY=VALUE) ergaenzen, bevor die
# Konstanten gelesen werden. So ueberleben Einstellungen ein Template-Update
# (Unraid speichert Env-Werte in der Template-XML auf dem Flash) und der
# Paperless-Token liegt nicht im Flash-Template.
CONFIG_ENV_FILE = Path(os.getenv("CONFIG_ENV_FILE", "/config/kaufland.env"))


def _load_config_file() -> None:
    try:
        lines = CONFIG_ENV_FILE.read_text("utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_config_file()

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
RECEIPTS_DIR = Path(os.getenv("RECEIPTS_DIR", "/receipts"))
IMPORT_DIR = Path(os.getenv("IMPORT_DIR", "/import"))
AUTH_FILE = Path(os.getenv("KAUFLAND_AUTH_FILE", str(CONFIG_DIR / "auth.json")))
LEDGER_FILE = CONFIG_DIR / "pushed.json"
STATUS_FILE = CONFIG_DIR / "status.json"

PAPERLESS_URL = os.getenv("PAPERLESS_URL", "").rstrip("/")
PAPERLESS_TOKEN = os.getenv("PAPERLESS_TOKEN", "")
# api = REST-Upload mit Metadaten, consume = PDFs in den Consume-Ordner legen,
# off = nur exportieren. Default: api, wenn URL+Token gesetzt sind, sonst off.
PAPERLESS_MODE = os.getenv(
    "PAPERLESS_MODE",
    "api" if (os.getenv("PAPERLESS_URL") and os.getenv("PAPERLESS_TOKEN")) else "off",
).strip().lower()
CONSUME_DIR = Path(os.getenv("CONSUME_DIR", "/consume"))
# Welche Dateien im Archiv bleiben. JSON immer (Titel/Preise, convert braucht es).
# pdf ist Pflicht, sobald etwas an Paperless gehen soll.
EXPORT_FORMATS = {
    f.strip().lower().lstrip(".")
    for f in os.getenv("EXPORT_FORMATS", "pdf,json").split(",")
    if f.strip()
}
CORRESPONDENT = os.getenv("PAPERLESS_CORRESPONDENT", "Kaufland")
DOCUMENT_TYPE = os.getenv("PAPERLESS_DOCUMENT_TYPE", "Kassenbon")
TAGS = [t.strip() for t in os.getenv("PAPERLESS_TAGS", "kaufland,bon").split(",") if t.strip()]

RUN_AT = os.getenv("RUN_AT", "04:30")
RUN_ON_START = os.getenv("RUN_ON_START", "true").lower() in {"1", "true", "yes"}
DO_EXPORT = os.getenv("KAUFLAND_EXPORT", "true").lower() in {"1", "true", "yes"}
DO_PUSH = os.getenv("PAPERLESS_PUSH", "true").lower() in {"1", "true", "yes"}
EXPORT_ARGS = os.getenv("KAUFLAND_EXPORT_ARGS", "").strip()
NOTIFY_URL = os.getenv("NOTIFY_URL", "").strip()

TIMEOUT = 120


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# Paperless
# --------------------------------------------------------------------------- #


def _request(method: str, path: str, *, body: bytes | None = None,
             content_type: str | None = None) -> tuple[int, object]:
    url = f"{PAPERLESS_URL}{path}"
    headers = {"Authorization": f"Token {PAPERLESS_TOKEN}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Paperless nicht erreichbar ({PAPERLESS_URL}): {exc.reason}") from exc
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, raw.decode("utf-8", "replace")


def _json_request(method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
    body = None if payload is None else json.dumps(payload).encode()
    return _request(method, path, body=body,
                    content_type=None if payload is None else "application/json")


def resolve_id(kind: str, name: str, cache: dict[str, int]) -> int | None:
    """Look up a correspondent/tag/document_type by name, create it if missing."""
    if not name:
        return None
    key = f"{kind}:{name.lower()}"
    if key in cache:
        return cache[key]
    query = urllib.parse.urlencode({"name__iexact": name, "page_size": 1})
    status, payload = _json_request("GET", f"/api/{kind}/?{query}")
    if status == 200 and isinstance(payload, dict) and payload.get("results"):
        identifier = payload["results"][0]["id"]
        cache[key] = identifier
        return identifier
    if status not in (200, 404):
        raise RuntimeError(f"Paperless {kind}-Abfrage fehlgeschlagen: HTTP {status}")
    status, payload = _json_request("POST", f"/api/{kind}/", {"name": name})
    if status in (200, 201) and isinstance(payload, dict) and payload.get("id"):
        log(f"Paperless: {kind} '{name}' angelegt")
        cache[key] = payload["id"]
        return payload["id"]
    raise RuntimeError(f"Paperless {kind} '{name}' konnte nicht angelegt werden: HTTP {status}")


def _multipart(fields: list[tuple[str, str]], file_path: Path) -> tuple[bytes, str]:
    boundary = f"----kaufland{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields:
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    chunks.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{file_path.name}\"\r\nContent-Type: application/pdf\r\n\r\n".encode()
    )
    chunks.append(file_path.read_bytes())
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def upload(pdf: Path, title: str, created: str, tags: list[int],
           correspondent: int | None, document_type: int | None) -> int:
    fields: list[tuple[str, str]] = [("title", title), ("created", created)]
    if correspondent:
        fields.append(("correspondent", str(correspondent)))
    if document_type:
        fields.append(("document_type", str(document_type)))
    fields.extend(("tags", str(tag)) for tag in tags)
    body, content_type = _multipart(fields, pdf)
    status, payload = _request("POST", "/api/documents/post_document/",
                              body=body, content_type=content_type)
    if status not in (200, 201, 202):
        raise RuntimeError(f"Upload {pdf.name} fehlgeschlagen: HTTP {status} {payload}")
    return status


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        try:
            return json.loads(LEDGER_FILE.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            log("Ledger unlesbar, starte mit leerem Ledger")
    return {}


def save_ledger(ledger: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = LEDGER_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), "utf-8")
    temporary.replace(LEDGER_FILE)


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #


def summary_for(pdf: Path) -> dict:
    candidate = pdf.with_name(f"{pdf.stem}_summary.json")
    if not candidate.exists():
        return {}
    try:
        return json.loads(candidate.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def describe(pdf: Path) -> tuple[str, str]:
    """Return (title, created-date) for a receipt PDF."""
    summary = summary_for(pdf)
    purchased = summary.get("date")
    if not purchased:
        if pdf.parent.name.count("-") == 2 and pdf.parent.name[:2] == "20":
            purchased = pdf.parent.name
        else:
            # imported files: trust a date in the filename, else the mtime
            match = re.search(r"(20\d{2}-\d{2}-\d{2})", pdf.stem)
            purchased = match.group(1) if match else date.fromtimestamp(
                pdf.stat().st_mtime).isoformat()
    total = summary.get("sum")
    if isinstance(total, (int, float)):
        amount = f"{total / 100:.2f}".replace(".", ",")
        title = f"Kaufland {purchased} {amount} €"
    else:
        title = f"Kaufland Bon {purchased}"
    if pdf.stem.endswith("-return-1"):
        title += " (Retoure)"
    return title, str(purchased)


def run_export() -> tuple[bool, str]:
    if not AUTH_FILE.exists():
        return False, (f"Keine Anmeldung gefunden ({AUTH_FILE}). Einmalig `kaufland login` "
                       "auf einem Rechner mit Browser ausführen und auth.json hierher kopieren.")
    command = ["kaufland", "--auth-file", str(AUTH_FILE), "download",
               "--output", str(RECEIPTS_DIR)]
    if EXPORT_ARGS:
        command.extend(EXPORT_ARGS.split())
    elif (CONFIG_DIR / "initial_done").exists():
        # incremental: cover last and current month, cheap and catches late postings
        today = date.today()
        first_of_last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        command.extend(["--since", first_of_last_month.isoformat()])
    log("Kaufland-Export: " + " ".join(command[1:]))
    process = subprocess.run(command, capture_output=True, text=True)
    for line in (process.stdout or "").splitlines():
        if line.strip():
            log(f"  kaufland: {line.strip()}")
    if process.returncode != 0:
        stderr = (process.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else f"Exit-Code {process.returncode}"
        for line in stderr:
            if line.strip():
                log(f"  kaufland: {line.strip()}")
        return False, detail
    (CONFIG_DIR / "initial_done").touch()
    return True, "ok"


def paths_collide() -> bool:
    """True when archive and consume folder are the same directory."""
    try:
        return RECEIPTS_DIR.resolve() == CONSUME_DIR.resolve()
    except OSError:
        return False


def prune_formats() -> int:
    """Delete rendered files the user does not want (html/jpg), keep json+pdf."""
    wanted = set(EXPORT_FORMATS) | {"json", "pdf"} if PAPERLESS_MODE != "off" else set(EXPORT_FORMATS) | {"json"}
    removed = 0
    for path in RECEIPTS_DIR.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lstrip(".").lower()
        if suffix in {"tmp", "part"} or suffix in wanted:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            log(f"Konnte {path.name} nicht entfernen: {exc}")
    return removed


def candidate_pdfs() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    if RECEIPTS_DIR.exists():
        found.extend((pdf, "archive") for pdf in sorted(RECEIPTS_DIR.rglob("*.pdf")))
    if IMPORT_DIR.exists():
        found.extend((pdf, "import") for pdf in sorted(IMPORT_DIR.glob("*.pdf")))
    return found


def push(ledger: dict) -> tuple[int, list[str]]:
    pushed, errors = 0, []
    cache: dict[str, int] = {}
    pending: list[tuple[Path, str, str, str]] = []
    for pdf, origin in candidate_pdfs():
        key = f"{origin}:" + str(pdf.relative_to(RECEIPTS_DIR if origin == "archive" else IMPORT_DIR))
        checksum = digest(pdf)
        if ledger.get(key, {}).get("sha256") == checksum:
            continue
        pending.append((pdf, origin, key, checksum))
    if not pending:
        return 0, []
    # only touch paperless metadata once there is something to upload
    correspondent = resolve_id("correspondents", CORRESPONDENT, cache)
    document_type = resolve_id("document_types", DOCUMENT_TYPE, cache)
    tags = [identifier for identifier in
            (resolve_id("tags", name, cache) for name in TAGS) if identifier]
    for pdf, origin, key, checksum in pending:
        title, created = describe(pdf)
        try:
            upload(pdf, title, created, tags, correspondent, document_type)
        except Exception as exc:  # keep going, report at the end
            errors.append(f"{pdf.name}: {exc}")
            log(f"FEHLER beim Upload von {pdf.name}: {exc}")
            continue
        ledger[key] = {"sha256": checksum, "title": title,
                       "pushed_at": datetime.now().isoformat(timespec="seconds")}
        save_ledger(ledger)
        pushed += 1
        log(f"-> Paperless: {title} ({pdf.name})")
        if origin == "import":
            target = IMPORT_DIR / "done"
            target.mkdir(exist_ok=True)
            pdf.replace(target / pdf.name)
    return pushed, errors


def consume_filename(pdf: Path) -> str:
    """Readable name for the consume folder (Paperless uses it as the title)."""
    title, _ = describe(pdf)
    name = title.replace(" €", "EUR").replace("€", "EUR").replace(",", "-")
    return f"{re.sub(r'\s+', ' ', name).strip()}.pdf"


def copy_to_consume(ledger: dict) -> tuple[int, list[str]]:
    """Hand receipts to Paperless' consume folder instead of the REST API."""
    if not CONSUME_DIR.exists():
        raise RuntimeError(f"Consume-Ordner {CONSUME_DIR} ist nicht gemountet")
    staging = CONSUME_DIR / ".incoming"
    staging.mkdir(exist_ok=True)
    copied, errors = 0, []
    for pdf, origin in candidate_pdfs():
        key = f"{origin}:" + str(
            pdf.relative_to(RECEIPTS_DIR if origin == "archive" else IMPORT_DIR))
        checksum = digest(pdf)
        if ledger.get(key, {}).get("sha256") == checksum:
            continue
        target = CONSUME_DIR / consume_filename(pdf)
        if target.exists():
            target = target.with_name(f"{target.stem}-{checksum[:6]}.pdf")
        temporary = staging / target.name
        try:
            shutil.copyfile(pdf, temporary)
            temporary.replace(target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            errors.append(f"{pdf.name}: {exc}")
            log(f"FEHLER beim Kopieren von {pdf.name}: {exc}")
            continue
        ledger[key] = {"sha256": checksum, "target": str(target),
                       "pushed_at": datetime.now().isoformat(timespec="seconds")}
        save_ledger(ledger)
        copied += 1
        log(f"-> Consume: {target.name}")
        if origin == "import":
            done = IMPORT_DIR / "done"
            done.mkdir(exist_ok=True)
            pdf.replace(done / pdf.name)
    return copied, errors


# --------------------------------------------------------------------------- #
# Notifications / status
# --------------------------------------------------------------------------- #


def notify(title: str, message: str) -> None:
    if not NOTIFY_URL:
        return
    payload = json.dumps({"title": title, "message": message}).encode()
    request = urllib.request.Request(NOTIFY_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
    except Exception as exc:  # notification must never break the run
        log(f"Benachrichtigung fehlgeschlagen: {exc}")


def write_status(ok: bool, exported: bool, pushed: int, errors: list[str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "ok": ok and not errors,
        "export_ok": exported,
        "pushed": pushed,
        "errors": errors[:20],
    }
    STATUS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #


def run_once() -> int:
    log("Lauf gestartet")
    if PAPERLESS_MODE != "off" and paths_collide():
        # Paperless verschiebt eingelesene Dateien aus dem Consume-Ordner.
        # Zeigen /receipts und /consume auf denselben Pfad, frisst es das Archiv.
        message = ("/receipts und /consume zeigen auf dasselbe Verzeichnis "
                   f"({RECEIPTS_DIR}). Paperless wuerde dein Archiv leeren - "
                   "bitte getrennte Pfade mounten (Archiv z.B. /mnt/user/documents/"
                   "kaufland-bons, Consume z.B. /mnt/user/paperless/consume).")
        log(f"FEHLER: {message}")
        write_status(False, False, 0, [message])
        notify("Kaufland-Bons: Pfade kollidieren", message)
        return 1
    exported, export_error = True, ""
    if DO_EXPORT:
        exported, export_error = run_export()
        if not exported:
            log(f"Export fehlgeschlagen: {export_error}")
    if exported and DO_EXPORT:
        removed = prune_formats()
        if removed:
            log(f"{removed} Datei(en) entfernt (EXPORT_FORMATS={','.join(sorted(EXPORT_FORMATS))})")
    pushed, errors = (0, [])
    mode = PAPERLESS_MODE if DO_PUSH else "off"
    if mode == "consume":
        try:
            pushed, errors = copy_to_consume(load_ledger())
        except Exception as exc:
            errors.append(str(exc))
            log(f"Consume-Modus abgebrochen: {exc}")
    elif mode == "api":
        if PAPERLESS_URL and PAPERLESS_TOKEN:
            try:
                pushed, errors = push(load_ledger())
            except Exception as exc:
                errors.append(str(exc))
                log(f"Paperless-Upload abgebrochen: {exc}")
        else:
            log("PAPERLESS_MODE=api, aber PAPERLESS_URL/PAPERLESS_TOKEN fehlen – nur Export")
    else:
        log("Paperless nicht aktiv (PAPERLESS_MODE=off) – nur Export")
    write_status(exported, exported, pushed, errors)
    problems = []
    if not exported:
        problems.append(export_error)
    problems.extend(errors)
    if problems:
        notify("Kaufland-Bons: Fehler", "\n".join(problems[:5]))
        log(f"Lauf beendet mit {len(problems)} Fehler(n)")
        return 1
    log(f"Lauf beendet: {pushed} Beleg(e) an Paperless")
    return 0


def next_run_at(now: datetime) -> datetime:
    try:
        hour, minute = (int(part) for part in RUN_AT.split(":", 1))
    except ValueError:
        hour, minute = 4, 30
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def daemon() -> int:
    stop = {"flag": False}

    def handler(signum, frame):
        stop["flag"] = True
        log("Beende nach diesem Lauf (Signal erhalten)")

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    if RUN_ON_START:
        run_once()
    while not stop["flag"]:
        target = next_run_at(datetime.now())
        log(f"Nächster Lauf: {target.strftime('%Y-%m-%d %H:%M')}")
        while not stop["flag"] and datetime.now() < target:
            # short tick so SIGTERM is honoured quickly
            time.sleep(min(1.0, max(0.2, (target - datetime.now()).total_seconds())))
        if stop["flag"]:
            break
        run_once()
    return 0


def health() -> int:
    if not STATUS_FILE.exists():
        print("noch kein Lauf")
        return 0
    try:
        payload = json.loads(STATUS_FILE.read_text("utf-8"))
        last = datetime.fromisoformat(payload["last_run"])
    except (OSError, ValueError, KeyError):
        print("Status unlesbar")
        return 1
    age = datetime.now() - last
    print(f"letzter Lauf {payload['last_run']} ({age.total_seconds() / 3600:.1f} h alt), "
          f"ok={payload.get('ok')}")
    return 0 if age < timedelta(hours=48) else 1


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "daemon"
    if command == "once":
        return run_once()
    if command == "daemon":
        return daemon()
    if command == "prune":
        print(f"{prune_formats()} Datei(en) entfernt")
        return 0
    if command == "push":
        pushed, errors = push(load_ledger())
        for error in errors:
            log(error)
        log(f"{pushed} Beleg(e) übertragen")
        return 1 if errors else 0
    if command == "health":
        return health()
    print(f"unbekanntes Kommando: {command} (once|daemon|push|prune|health)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
