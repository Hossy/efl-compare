#!/usr/bin/env python3
"""
Self-contained web server for the EFL Electricity Plan Comparator.

This folder is self-contained: its own `efl_compare.py` runs the pipeline and
writes `plans_latest.json` here, and this server renders it.

Serving is pure standard library. The one exception is POST /api/parse-bill,
which reads a user's bill PDF and needs PyMuPDF + openai (see bill_parser.py);
that import is guarded, so without them the rest of the site still runs and
only that endpoint returns 503.

Usage
-----
    python3 web/serve.py                  # serve web/plans_latest.json
    python3 web/serve.py --reload         # restart automatically on .py edits
    python3 web/serve.py --port 8000 --json-path ../plans_latest.json

Data resolution order (first hit wins):
    1. --json-path CLI argument
    2. EFL_JSON environment variable
    3. web/plans_latest.json             (written by this folder's efl_compare.py)
    4. <parent repo>/plans_latest.json   (the upstream tool's output)

If none of those exist, the pages render a friendly "run the CLI first" state
rather than failing.

The data file is re-read from disk on every /api/plans request, so re-running
`efl_compare.py --json` refreshes the site with no server restart.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import bill_parser
    import llm_backend
    # Real environment variables win over .env, so a container can point
    # EFL_LLM_BASE_URL at the host without editing the file.
    llm_backend.load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:          # PyMuPDF/openai absent — /api/parse-bill will 503
    bill_parser = None

# Abuse controls for the endpoints that spend GPU time. Imported after the
# .env load above so the limits can be configured there.
import ratelimit

RATE = ratelimit.RateLimiter()
BUSY = ratelimit.ConcurrencyGuard()

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
# This folder is self-contained: its own efl_compare.py writes plans_latest.json
# right here. The parent copy is still accepted as a fallback.
LOCAL_JSON  = HERE / "plans_latest.json"
PARENT_JSON = HERE.parent / "plans_latest.json"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
}


class Config:
    """Runtime configuration, resolved once at startup."""

    def __init__(self, json_path: Path | None):
        self.explicit_json_path = json_path

    def resolve_data_path(self) -> Path | None:
        """Return the path we should try to read, or None if nothing configured."""
        if self.explicit_json_path is not None:
            return self.explicit_json_path
        env = os.environ.get("EFL_JSON")
        if env:
            return Path(env).expanduser()
        if LOCAL_JSON.exists():
            return LOCAL_JSON
        if PARENT_JSON.exists():
            return PARENT_JSON
        return None


CONFIG: Config  # set in main()


def _load_data() -> dict:
    """Read and return the plan JSON plus a _source metadata block.

    Never raises to the request handler; returns a structured error payload so
    the front-end can render a helpful "no data yet" state.
    """
    path = CONFIG.resolve_data_path()
    if path is None:
        return {
            "_source": {
                "ok": False,
                "reason": "no_data_file",
                "message": (
                    "No plans_latest.json found. Run the parent CLI first, e.g.\n"
                    "    python3 efl_compare.py --zip YOUR_ZIP --json plans_latest.json\n"
                    "then reload this page (no server restart needed)."
                ),
                "looked_at": [str(LOCAL_JSON), str(PARENT_JSON)],
            },
            "plans": [],
        }

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            "_source": {
                "ok": False,
                "reason": "file_missing",
                "message": f"Configured data file does not exist: {path}",
                "path": str(path),
            },
            "plans": [],
        }
    except OSError as exc:
        return {
            "_source": {
                "ok": False,
                "reason": "read_error",
                "message": f"Could not read {path}: {exc}",
                "path": str(path),
            },
            "plans": [],
        }

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "_source": {
                "ok": False,
                "reason": "invalid_json",
                "message": f"{path} is not valid JSON: {exc}",
                "path": str(path),
            },
            "plans": [],
        }

    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        mtime_iso = mtime.isoformat(timespec="seconds")
    except OSError:
        mtime_iso = None

    data["_source"] = {
        "ok": True,
        "path": str(path),
        "file_mtime": mtime_iso,
        "plan_count": len(data.get("plans", [])),
    }
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "EFLCompareWeb/1.0"

    # ---- helpers ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This tool serves purely local, read-only data — no caching so a
        # re-run of the CLI shows up on the next reload.
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, obj, extra_headers: dict | None = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra_headers)

    def _serve_static(self, rel_path: str) -> None:
        # Normalise and confine to STATIC_DIR (no path traversal).
        rel_path = rel_path.lstrip("/")
        if rel_path == "":
            rel_path = "index.html"
        target = (STATIC_DIR / rel_path).resolve()
        if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


    # ---- routing ---------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/health":
            data = _load_data()
            self._send_json(200, {
                "status": "ok",
                "data_available": data["_source"].get("ok", False),
                "source": data["_source"],
            })
            return

        if path == "/api/plans":
            self._send_json(200, _load_data())
            return

        # The wizard is the front door: it answers "what should I switch to?",
        # which is what people arrive asking. The full table is a tool for
        # people who already know what they're looking at, so it lives at
        # /table with a link from the wizard's footer.
        if path in ("/", "/wizard", "/wizard.html"):
            self._serve_static("wizard.html")
            return

        if path in ("/table", "/index.html"):
            self._serve_static("index.html")
            return


        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        # Allow bare asset names too (e.g. /app.js) for convenience.
        self._serve_static(path)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/parse-bill", "/api/parse-contract"):
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        if bill_parser is None:
            self._send_json(503, {"ok": False, "error":
                "Bill reading needs PyMuPDF and openai: pip install -r requirements.txt"})
            return

        # Cheapest rejections first: a wrong key or an exhausted bucket should
        # cost nothing, so both are settled before the body is read off the wire.
        if not ratelimit.token_ok(self):
            self._send_json(401, {"ok": False, "error": "A valid API key is required."})
            return

        caller = ratelimit.client_key(self)
        allowed, retry_after = RATE.check(caller)
        if not allowed:
            mins = max(1, round(retry_after / 60))
            self._send_json(429, {"ok": False, "error":
                f"That's a lot of bills. Try again in about {mins} minute"
                f"{'s' if mins != 1 else ''}, or type the numbers in instead."},
                {"Retry-After": str(retry_after)})
            return

        # The body is the raw PDF (the wizard POSTs the File object directly),
        # so there is no multipart parsing to do. Refuse on the declared length
        # before reading, so an oversized upload costs nothing.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "No file was received."})
            return
        if length > bill_parser.MAX_PDF_BYTES:
            self._send_json(413, {"ok": False, "error":
                f"That PDF is larger than {bill_parser.MAX_PDF_BYTES // (1024*1024)} MB."})
            return

        try:
            body = self.rfile.read(length)
        except OSError:
            self._send_json(400, {"ok": False, "error": "Upload was interrupted."})
            return

        # A bill says what you used; a contract says what leaving costs.
        reader = (bill_parser.parse_bill if path == "/api/parse-bill"
                  else bill_parser.parse_contract)

        # One GPU, and a vision pass on a scan is not quick. Past the cap, say so
        # and let them type instead of piling onto a queue nobody is draining.
        with BUSY as got_slot:
            if not got_slot:
                self._send_json(503, {"ok": False, "error":
                    "The bill reader is busy right now. Try again in a moment, "
                    "or type the numbers in instead."}, {"Retry-After": "20"})
                return
            try:
                fields = reader(body)
            except bill_parser.BillParseError as exc:
                # Expected, user-fixable: say what went wrong.
                self._send_json(422, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                # Unexpected: log it, but don't leak internals to the page.
                sys.stderr.write(f"  {path} failed: {exc!r}\n")
                self._send_json(502, {"ok": False, "error":
                    "The bill reader is unavailable. Check the LLM endpoint in .env."})
                return

        self._send_json(200, {"ok": True, "fields": fields})

    # Quieter, single-line logging.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Serve the EFL plan comparison as an interactive web page.")
    p.add_argument("--host", default="127.0.0.1",
                   help="Interface to bind (default: 127.0.0.1). Use 0.0.0.0 to expose on LAN.")
    p.add_argument("--port", type=int, default=8090,
                   help="Port to listen on (default: 8090; 8000/8080/4000 are used by "
                        "other containers on this host).")
    p.add_argument("--json-path", default=None,
                   help="Path to the plans JSON produced by efl_compare.py --json. "
                        "Overrides EFL_JSON and the default ../plans_latest.json.")
    p.add_argument("--reload", action="store_true",
                   help="Restart the server automatically when a .py file in this "
                        "folder changes. Static assets and the plans JSON are always "
                        "re-read per request and need no restart.")
    return p.parse_args(argv)


def _watch_and_restart(interval: float = 1.0) -> None:
    """Re-exec the process when any .py file in this folder changes.

    Only Python code needs this: _serve_static() and _load_data() both read from
    disk on every request, so edits to static/ or plans_latest.json are already
    live. Without it, a long-running server keeps serving the code it started
    with, which looks exactly like a change that "didn't work".
    """
    watched = sorted(HERE.rglob("*.py"))
    stamps = {f: f.stat().st_mtime for f in watched if f.exists()}
    while True:
        time.sleep(interval)
        try:
            current = sorted(HERE.rglob("*.py"))
            changed = (set(current) != set(stamps)) or any(
                f.stat().st_mtime != stamps.get(f) for f in current if f.exists()
            )
        except OSError:
            continue           # file mid-write; try again next tick
        if changed:
            print("\n  Change detected — restarting.", flush=True)
            os.execv(sys.executable, [sys.executable] + sys.argv)


def main(argv=None) -> int:
    global CONFIG
    args = parse_args(argv)
    json_path = Path(args.json_path).expanduser().resolve() if args.json_path else None
    CONFIG = Config(json_path=json_path)

    resolved = CONFIG.resolve_data_path()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    url = f"http://{args.host}:{args.port}/"
    print("EFL Compare — web server")
    print(f"  Serving:  {url}")
    if resolved is None:
        print("  Data:     (none found yet — the page will show setup instructions)")
        print(f"            Expected: {LOCAL_JSON}")
    else:
        print(f"  Data:     {resolved}")
    print(f"  Limits:   {ratelimit.describe()}")
    if args.reload:
        print("  Reload:   on (watching *.py)")
        threading.Thread(target=_watch_and_restart, daemon=True).start()
    print("  Stop:     Ctrl-C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
