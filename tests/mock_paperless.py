#!/usr/bin/env python3
"""Minimal Paperless-ngx API imitation for testing the exporter container.

Implements just enough of /api/{correspondents,tags,document_types}/ and
/api/documents/post_document/ to check metadata and duplicate handling.
"""
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INBOX = Path(sys.argv[2])
INBOX.mkdir(parents=True, exist_ok=True)
KINDS = {"correspondents": 1, "tags": 100, "document_types": 200}
STATE = {kind: {} for kind in KINDS}
UPLOADS = []


def parse_multipart(body: bytes, boundary: bytes):
    fields, files = [], []
    for raw in body.split(b"--" + boundary):
        raw = raw.strip(b"\r\n")
        if not raw or raw == b"--":
            continue
        head, _, data = raw.partition(b"\r\n\r\n")
        header = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]+)"', header)
        filename = re.search(r'filename="([^"]+)"', header)
        if not name:
            continue
        if filename:
            files.append((name.group(1), filename.group(1), data))
        else:
            fields.append((name.group(1), data.decode("utf-8", "replace")))
    return fields, files


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].strip("/").split("/")
        if len(path) == 2 and path[0] == "api" and path[1] in KINDS:
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            wanted = re.search(r"name__iexact=([^&]+)", query)
            name = wanted.group(1) if wanted else ""
            from urllib.parse import unquote_plus
            name = unquote_plus(name)
            hits = [{"id": i, "name": n} for n, i in STATE[path[1]].items()
                    if n.lower() == name.lower()]
            return self._json(200, {"count": len(hits), "results": hits})
        if self.path.startswith("/api/documents/tasks/"):
            return self._json(200, {"status": "SUCCESS"})
        self._json(404, {"detail": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        path = self.path.split("?")[0].strip("/").split("/")
        if len(path) == 2 and path[0] == "api" and path[1] in KINDS:
            payload = json.loads(body or b"{}")
            kind = path[1]
            identifier = KINDS[kind] + len(STATE[kind]) + 1
            STATE[kind][payload["name"]] = identifier
            return self._json(201, {"id": identifier, "name": payload["name"]})
        if self.path.startswith("/api/documents/post_document/"):
            boundary = re.search(r"boundary=(\S+)", self.headers.get("Content-Type", ""))
            if not boundary:
                return self._json(400, {"detail": "no boundary"})
            fields, files = parse_multipart(body, boundary.group(1).encode())
            grouped: dict[str, list[str]] = {}
            for name, value in fields:
                grouped.setdefault(name, []).append(value)
            record = {"fields": grouped, "files": []}
            for _, filename, data in files:
                target = INBOX / filename
                target.write_bytes(data)
                record["files"].append({"name": filename, "bytes": len(data)})
            UPLOADS.append(record)
            (INBOX / "_uploads.json").write_text(json.dumps(UPLOADS, indent=2, ensure_ascii=False))
            return self._json(200, "task-uuid-1234")
        if self.path == "/notify":
            with open(INBOX / "_notifications.json", "a") as stream:
                stream.write(body.decode("utf-8", "replace") + "\n")
            return self._json(200, {"ok": True})
        self._json(404, {"detail": "not found"})


if __name__ == "__main__":
    port = int(sys.argv[1])
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
