#!/usr/bin/env python3
"""AI news timeline — single-port HTTP service (stdlib only).

Routes
  GET  /                       timeline page
  GET  /health                 health probe
  GET  /api/entries            lightweight index, newest first
  GET  /api/day?date=...       one structured day
  GET  /api/feed?limit=30      recent N days, fully structured
  GET  /api/export             full backup (days + raw html), for local sync
  GET  /d/<date>.html          the original, unmodified daily HTML
  POST /api/publish            publish one daily HTML (token protected)

Storage is plain JSON on disk. The project's data/ directory is the local
authoritative copy; if the deploy sandbox is read-only, a writable directory
is used and seeded from data/ at startup.

Publishing drops cards already carried by an earlier issue (see dedupe.py);
the original HTML stays untouched as the archival "original layout" page.
"""

import json
import os
import re
import shutil
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import dedupe
from daily_parser import parse_daily, valid_date

ROOT = os.path.dirname(os.path.abspath(__file__))
SEED_DATA = os.path.join(ROOT, "data")
PUBLIC_DIR = os.path.join(ROOT, "public")
CONFIG_PATH = os.path.join(ROOT, "config.json")
RAW_DATE_RE = re.compile(r"^/d/(\d{4}-\d{2}-\d{2})\.html$")
MAX_BODY = 4 * 1024 * 1024
LOCK = threading.RLock()


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


CONFIG = load_config()
# Environment variables take precedence so a clone can run without config.json.
TOKEN = os.environ.get("AINEWS_PUBLISH_TOKEN") or CONFIG.get("publish_token", "")


def pick_data_dir():
    env_dir = os.environ.get("AINEWS_DATA_DIR")
    candidates = [env_dir] if env_dir else []
    candidates.append(SEED_DATA)
    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write-test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            chosen = candidate
            break
        except OSError:
            continue
    else:
        chosen = os.path.join("/tmp", "ainews-data")
        os.makedirs(chosen, exist_ok=True)

    # Seed a fresh writable sandbox with the histories shipped in the project.
    if os.path.abspath(chosen) != os.path.abspath(SEED_DATA):
        if not os.path.exists(os.path.join(chosen, "entries.json")):
            for sub in ("days", "raw"):
                src = os.path.join(SEED_DATA, sub)
                if os.path.isdir(src):
                    shutil.copytree(src, os.path.join(chosen, sub), dirs_exist_ok=True)
            seed_index = os.path.join(SEED_DATA, "entries.json")
            if os.path.exists(seed_index):
                shutil.copy2(seed_index, os.path.join(chosen, "entries.json"))
    return chosen


DATA_DIR = pick_data_dir()
DAYS_DIR = os.path.join(DATA_DIR, "days")
RAW_DIR = os.path.join(DATA_DIR, "raw")
INDEX_PATH = os.path.join(DATA_DIR, "entries.json")
for directory in (DAYS_DIR, RAW_DIR):
    os.makedirs(directory, exist_ok=True)


def atomic_write(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def read_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_index():
    data = read_json(INDEX_PATH, {"entries": [], "updated_at": None})
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        data = {"entries": [], "updated_at": None}
    return data


def day_path(date):
    return os.path.join(DAYS_DIR, date + ".json")


def raw_path(date):
    return os.path.join(RAW_DIR, date + ".html")


def save_day(date, source, document):
    """Persist one day and refresh the index. Returns (created, index)."""
    with LOCK:
        created = not os.path.exists(day_path(date))
        atomic_write(raw_path(date), source)
        atomic_write(day_path(date), json.dumps(document, ensure_ascii=False, indent=1))

        index = load_index()
        entries = [e for e in index["entries"] if e.get("date") != date]
        entries.append({
            "date": date,
            "title": document["title"],
            "date_label": document["date_label"],
            "weekday": document["weekday"],
            "total": document["total"],
            "cat_count": document["cat_count"],
            "updated_at": document["updated_at"],
        })
        entries.sort(key=lambda e: e["date"], reverse=True)
        index = {"entries": entries, "updated_at": now_iso()}
        atomic_write(INDEX_PATH, json.dumps(index, ensure_ascii=False, indent=1))
        return created, index


def history_documents(date):
    """Structured days published before `date`, oldest first, for dedupe."""
    with LOCK:
        docs = []
        for entry in load_index()["entries"]:
            earlier = entry.get("date", "")
            if earlier and earlier < date and valid_date(earlier):
                doc = read_json(day_path(earlier), None)
                if doc:
                    docs.append(doc)
    docs.sort(key=lambda doc: doc.get("date", ""))
    return docs


def read_page():
    path = os.path.join(PUBLIC_DIR, "index.html")
    with open(path, "rb") as fh:
        return fh.read()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AINews/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload, extra=None):
        self._send(status, json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8", extra)

    def _error(self, status, message):
        self._json(status, {"ok": False, "error": message})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        cors = {"Access-Control-Allow-Origin": "*"}

        if path in ("/", "/index.html"):
            try:
                return self._send(200, read_page(), "text/html; charset=utf-8")
            except OSError:
                return self._error(500, "timeline page missing")

        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")

        if path == "/health":
            return self._json(200, {
                "ok": True,
                "data_dir": DATA_DIR,
                "days": len(load_index()["entries"]),
                "time": now_iso(),
            })

        if path == "/api/entries":
            return self._json(200, load_index(), cors)

        if path == "/api/day":
            date = (query.get("date") or [""])[0]
            if not valid_date(date):
                return self._error(400, "date must be YYYY-MM-DD")
            doc = read_json(day_path(date), None)
            if doc is None:
                return self._error(404, "no entry for " + date)
            return self._json(200, doc, cors)

        if path == "/api/feed":
            try:
                limit = max(1, min(365, int((query.get("limit") or ["30"])[0])))
            except ValueError:
                limit = 30
            with LOCK:
                docs = []
                for entry in load_index()["entries"][:limit]:
                    doc = read_json(day_path(entry["date"]), None)
                    if doc:
                        docs.append(doc)
            return self._json(200, {"days": docs, "total_days": len(load_index()["entries"])}, cors)

        if path == "/api/export":
            with LOCK:
                index = load_index()
                days, raw = {}, {}
                for entry in index["entries"]:
                    date = entry["date"]
                    doc = read_json(day_path(date), None)
                    if doc:
                        days[date] = doc
                    try:
                        with open(raw_path(date), encoding="utf-8") as fh:
                            raw[date] = fh.read()
                    except OSError:
                        pass
            return self._json(200, {
                "entries": index["entries"],
                "updated_at": index.get("updated_at"),
                "days": days,
                "raw": raw,
            }, cors)

        match = RAW_DATE_RE.match(path)
        if match:
            try:
                with open(raw_path(match.group(1)), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                return self._error(404, "original HTML not found")

        return self._error(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/publish":
            return self._error(404, "not found")

        if not TOKEN or self.headers.get("X-Publish-Token") != TOKEN:
            return self._error(401, "invalid publish token")

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error(400, "invalid content length")
        if length <= 0 or length > MAX_BODY:
            return self._error(400, "body size out of range")

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._error(400, "body must be JSON")

        date = payload.get("date", "") if isinstance(payload, dict) else ""
        source = payload.get("html", "") if isinstance(payload, dict) else ""
        if not valid_date(date):
            return self._error(400, "date must be YYYY-MM-DD")
        if not isinstance(source, str) or len(source) < 200:
            return self._error(400, "html is empty or too short")
        if 'class="card"' not in source and "class='card'" not in source:
            return self._error(400, "html does not look like a daily news page")

        try:
            document = parse_daily(source, date, updated_at=now_iso())
        except Exception as exc:  # parser failures must never corrupt the store
            return self._error(422, "parse failed: %s" % exc)

        if document["total"] == 0:
            return self._error(422, "parsed 0 cards; refusing to overwrite an existing day")

        document = dedupe.filter_document(document, history_documents(date))
        removed = document["dedupe"]["removed"]
        if document["total"] == 0:
            # A day with no fresh news would show up as an empty timeline entry.
            return self._error(422,
                "all %d cards already published in earlier issues; refusing an empty day" % removed)

        try:
            created, index = save_day(date, source, document)
        except OSError as exc:
            return self._error(500, "write failed: %s" % exc)

        print("[publish] %s %s cards=%d cats=%d removed=%d created=%s"
              % (date, document["title"], document["total"],
                 document["cat_count"], removed, created), flush=True)
        return self._json(200, {
            "ok": True,
            "date": date,
            "created": created,
            "title": document["title"],
            "total": document["total"],
            "removed": removed,
            "cat_count": document["cat_count"],
            "total_days": len(index["entries"]),
            "url": "/#%s" % date,
        })


def main():
    port = int(os.environ.get("PORT", "3000"))
    print("[ainews] data_dir=%s days=%d port=%d"
          % (DATA_DIR, len(load_index()["entries"]), port), flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
