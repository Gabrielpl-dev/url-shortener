#!/usr/bin/env python3
"""A self-contained URL shortener with click analytics.

Implements the HTTP contract of SPEC v0.1.0 (frozen): create short links,
redirect visitors, record clicks with referrer/user-agent metadata, and expose
aggregated per-link statistics. Persistence is a single SQLite file. No external
services, no third-party runtime dependencies: only the Python standard library.
"""

import argparse
import json
import os
import random
import re
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit, urlunsplit

# --- Constants ---------------------------------------------------------------

ALIAS_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
ALIAS_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
ALIAS_GEN_LEN = 7
ALIAS_MAX_ATTEMPTS = 5

RESERVED_ALIASES = {
    "api",
    "health",
    "serve",
    "data",
    "static",
    "assets",
    "favicon.ico",
    "robots.txt",
}

MAX_URL_LEN = 2048
MAX_BODY_BYTES = 8192

BOT_MARKERS = (
    "bot",
    "crawler",
    "spider",
    "slurp",
    "archiver",
    "curl/",
    "wget/",
    "python-requests",
    "python-urllib",
    "go-http-client",
    "httpie",
    "postman",
    "insomnia",
    "okhttp",
    "java/",
    "libwww",
)

MOBILE_MARKERS = (
    "mobile",
    "android",
    "iphone",
    "ipod",
    "ipad",
    "windows phone",
    "opera mini",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    alias      TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clicks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    alias      TEXT NOT NULL REFERENCES links(alias) ON DELETE CASCADE,
    clicked_at TEXT NOT NULL,
    day        TEXT NOT NULL,
    referrer   TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    device     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clicks_alias ON clicks(alias);
"""

DEVICE_KEYS = ("desktop", "mobile", "bot")


# --- Helpers -----------------------------------------------------------------


def now_iso():
    """Current UTC instant as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_device(user_agent):
    """Classify a User-Agent string into desktop | mobile | bot (rule R-19)."""
    if not user_agent:
        return "bot"
    lowered = user_agent.lower()
    for marker in BOT_MARKERS:
        if marker in lowered:
            return "bot"
    for marker in MOBILE_MARKERS:
        if marker in lowered:
            return "mobile"
    return "desktop"


def validate_url(value):
    """Return True when ``value`` is an acceptable absolute http(s) URL."""
    if not isinstance(value, str):
        return False
    if len(value) > MAX_URL_LEN:
        return False
    for char in value:
        code = ord(char)
        if code < 0x21 or code == 0x7F:  # spaces and control characters
            return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if not parts.hostname:
        return False
    return True


def generate_alias():
    return "".join(random.choice(ALIAS_ALPHABET) for _ in range(ALIAS_GEN_LEN))


def encode_url(value):
    """Convert an international URL to an ASCII URI for storage and headers."""
    if value.isascii():
        return value
    parts = urlsplit(value)
    userinfo, separator, authority = parts.netloc.rpartition("@")
    if not authority.isascii():
        host, colon, port = authority.partition(":")
        authority = host.encode("idna").decode("ascii") + colon + port
    netloc = userinfo + separator + authority
    return quote(urlunsplit(parts._replace(netloc=netloc)), safe=":/?#[]@!$&'()*+,;=%")


# --- Storage -----------------------------------------------------------------


class Store:
    """Serialized access to the SQLite database (writes are serialized)."""

    def __init__(self, path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    def create_link(self, alias, url, created_at):
        """Insert a link. Raises sqlite3.IntegrityError when the alias is taken."""
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO links(alias, url, created_at) VALUES(?, ?, ?)",
                    (alias, url, created_at),
                )
                self.conn.commit()
            except sqlite3.IntegrityError:
                self.conn.rollback()
                raise

    def get_link(self, alias):
        with self.lock:
            cur = self.conn.execute(
                "SELECT alias, url, created_at FROM links WHERE alias = ?", (alias,)
            )
            return cur.fetchone()

    def list_links(self):
        with self.lock:
            cur = self.conn.execute(
                """
                SELECT l.alias, l.url, l.created_at,
                       (SELECT COUNT(*) FROM clicks c WHERE c.alias = l.alias) AS total
                  FROM links l
                 ORDER BY l.created_at DESC, l.alias ASC
                """
            )
            return cur.fetchall()

    def delete_link(self, alias):
        with self.lock:
            cur = self.conn.execute("DELETE FROM links WHERE alias = ?", (alias,))
            self.conn.commit()
            return cur.rowcount > 0

    def record_click(self, alias, referrer, user_agent, device):
        clicked_at = now_iso()
        day = clicked_at[:10]
        with self.lock:
            self.conn.execute(
                "INSERT INTO clicks(alias, clicked_at, day, referrer, user_agent, device)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (alias, clicked_at, day, referrer, user_agent, device),
            )
            self.conn.commit()

    def stats(self, alias):
        """Return (total, clicks_by_day, top_referrers, devices) for an alias."""
        with self.lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM clicks WHERE alias = ?", (alias,)
            ).fetchone()[0]
            by_day = self.conn.execute(
                "SELECT day AS date, COUNT(*) AS count FROM clicks WHERE alias = ?"
                " GROUP BY day ORDER BY day ASC",
                (alias,),
            ).fetchall()
            referrers = self.conn.execute(
                "SELECT referrer, COUNT(*) AS count FROM clicks WHERE alias = ?"
                " GROUP BY referrer ORDER BY count DESC, referrer ASC LIMIT 10",
                (alias,),
            ).fetchall()
            devices = {key: 0 for key in DEVICE_KEYS}
            for row in self.conn.execute(
                "SELECT device, COUNT(*) AS count FROM clicks WHERE alias = ?"
                " GROUP BY device",
                (alias,),
            ).fetchall():
                if row["device"] in devices:
                    devices[row["device"]] = row["count"]
        return total, by_day, referrers, devices


# --- HTTP handler ------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "url-shortener/0.1.0"
    sys_version = ""

    store = None
    base_url = ""

    def version_string(self):
        return self.server_version

    # -- logging ------------------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- method dispatch ----------------------------------------------------

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle()

    def __getattr__(self, name):
        # Any other HTTP verb dispatch to the generic router.
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    # -- response helpers ---------------------------------------------------

    def _send(self, status, body, content_type=None, extra_headers=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, status, obj, extra_headers=None):
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra_headers)

    def _error(self, status, code, extra_headers=None):
        self._json(status, {"error": code}, extra_headers)

    def _method_not_allowed(self, allow):
        self._error(405, "method_not_allowed", {"Allow": allow})

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _no_content(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- body reading -------------------------------------------------------

    def _read_body(self):
        """Return (body_bytes, too_large)."""
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            return self._read_chunked_body()
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b"", False
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            return b"", True
        return (self.rfile.read(length) if length else b""), False

    def _read_chunked_body(self):
        chunks = []
        total = 0
        while True:
            line = self.rfile.readline(65537)
            if not line:
                break
            size_token = line.split(b";", 1)[0].strip()
            try:
                size = int(size_token, 16)
            except ValueError:
                self.close_connection = True
                break
            if size == 0:
                while True:
                    trailer = self.rfile.readline(65537)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += size
            if total > MAX_BODY_BYTES:
                self.close_connection = True
                return b"", True
            chunks.append(self.rfile.read(size))
            self.rfile.read(2)  # trailing CRLF
        return b"".join(chunks), False

    # -- router -------------------------------------------------------------

    def _handle(self):
        try:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                return self._route_health()
            if path == "/api/links":
                return self._route_links_collection()
            if path.startswith("/api/links/"):
                return self._route_links_item(path[len("/api/links/"):])
            if path == "/api" or path.startswith("/api/"):
                return self._error(404, "route_not_found")
            if re.fullmatch(r"/[^/]+", path):
                return self._route_alias(path[1:])
            return self._error(404, "route_not_found")
        except Exception:  # pragma: no cover - defensive
            self._error(500, "internal_error")

    def _route_health(self):
        if self.command in ("GET", "HEAD"):
            return self._json(200, {"status": "ok"})
        return self._method_not_allowed("GET, HEAD")

    def _route_links_collection(self):
        if self.command == "POST":
            return self._create_link()
        if self.command == "GET":
            return self._list_links()
        return self._method_not_allowed("GET, POST")

    def _route_links_item(self, rest):
        parts = rest.split("/")
        if len(parts) == 2 and parts[1] == "stats" and parts[0]:
            if self.command == "GET":
                return self._stats(parts[0])
            return self._method_not_allowed("GET")
        if len(parts) == 1 and parts[0]:
            if self.command == "DELETE":
                return self._delete(parts[0])
            return self._method_not_allowed("DELETE")
        return self._error(404, "route_not_found")

    def _route_alias(self, alias):
        if self.command not in ("GET", "HEAD"):
            return self._method_not_allowed("GET, HEAD")
        row = self.store.get_link(alias)
        if row is None:
            return self._error(404, "not_found")
        if self.command == "GET":
            referrer = self.headers.get("Referer")
            if referrer is None or referrer == "":
                referrer = "(direct)"
            user_agent = self.headers.get("User-Agent")
            if user_agent is None:
                user_agent = ""
            self.store.record_click(
                alias, referrer, user_agent, classify_device(user_agent)
            )
        return self._redirect(row["url"])

    # -- link operations ----------------------------------------------------

    def _link_public(self, alias, url, created_at, total_clicks):
        return {
            "alias": alias,
            "url": url,
            "short_url": self.base_url + "/" + alias,
            "created_at": created_at,
            "total_clicks": total_clicks,
        }

    def _create_link(self):
        body, too_large = self._read_body()
        if too_large:
            return self._error(413, "payload_too_large")
        try:
            data = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            return self._error(400, "invalid_json")

        url = data.get("url")
        if not validate_url(url):
            return self._error(400, "invalid_url")
        try:
            url = encode_url(url)
        except UnicodeError:
            return self._error(400, "invalid_url")

        created_at = now_iso()
        alias = data.get("alias", _MISSING)
        if alias is _MISSING or alias == "":
            created = self._create_generated(url, created_at)
            if created is None:
                return self._error(500, "internal_error")
            alias = created
        else:
            if (
                not isinstance(alias, str)
                or not ALIAS_RE.fullmatch(alias)
                or alias in RESERVED_ALIASES
            ):
                return self._error(400, "invalid_alias")
            try:
                self.store.create_link(alias, url, created_at)
            except sqlite3.IntegrityError:
                return self._error(409, "alias_taken")

        self._json(201, self._link_public(alias, url, created_at, 0))

    def _create_generated(self, url, created_at):
        for _ in range(ALIAS_MAX_ATTEMPTS):
            candidate = generate_alias()
            try:
                self.store.create_link(candidate, url, created_at)
                return candidate
            except sqlite3.IntegrityError:
                continue
        return None

    def _list_links(self):
        links = [
            self._link_public(row["alias"], row["url"], row["created_at"], row["total"])
            for row in self.store.list_links()
        ]
        self._json(200, {"links": links})

    def _stats(self, alias):
        row = self.store.get_link(alias)
        if row is None:
            return self._error(404, "not_found")
        total, by_day, referrers, devices = self.store.stats(alias)
        payload = {
            "alias": row["alias"],
            "url": row["url"],
            "short_url": self.base_url + "/" + row["alias"],
            "created_at": row["created_at"],
            "total_clicks": total,
            "clicks_by_day": [
                {"date": item["date"], "count": item["count"]} for item in by_day
            ],
            "top_referrers": [
                {"referrer": item["referrer"], "count": item["count"]}
                for item in referrers
            ],
            "devices": devices,
        }
        self._json(200, payload)

    def _delete(self, alias):
        if not self.store.delete_link(alias):
            return self._error(404, "not_found")
        self._no_content()


_MISSING = object()


# --- Entry point -------------------------------------------------------------


def parse_args(argv):
    def port_type(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("port must be an integer")
        if not 1 <= number <= 65535:
            raise argparse.ArgumentTypeError("port must be between 1 and 65535")
        return number

    parser = argparse.ArgumentParser(prog="serve", description="URL shortener server")
    parser.add_argument("--port", type=port_type, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    data_dir = os.environ.get("DATA_DIR") or "./data"
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as exc:
        sys.stderr.write("error: cannot create DATA_DIR %r: %s\n" % (data_dir, exc))
        return 1

    db_path = os.path.join(data_dir, "shortener.db")
    try:
        store = Store(db_path)
    except sqlite3.Error as exc:
        sys.stderr.write("error: cannot open database %r: %s\n" % (db_path, exc))
        return 1

    base_url = os.environ.get("BASE_URL")
    if base_url:
        base_url = base_url.rstrip("/")
    else:
        base_url = "http://127.0.0.1:%d" % args.port

    Handler.store = store
    Handler.base_url = base_url

    host = os.environ.get("HOST") or "127.0.0.1"
    try:
        httpd = ThreadingHTTPServer((host, args.port), Handler)
    except OSError as exc:
        sys.stderr.write("error: cannot bind %s:%d: %s\n" % (host, args.port, exc))
        store.close()
        return 1

    httpd.daemon_threads = True

    def _shutdown(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
