"""A very small web toolkit built on the Python standard library.

Everything the app needs is here: URL routing, static files, JSON replies and
streaming (Server-Sent Events) responses. No Flask, no FastAPI, no install
step — `python start.py` is the entire setup, on any operating system.

The only trade-off is that routing is hand-rolled; it is deliberately tiny.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import socketserver
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("font/woff2", ".woff2")

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# What a half-closed connection looks like, on every platform. A browser that
# navigates away mid-request, or a client that stops reading a reply it no
# longer needs, aborts the socket — and Windows reports that as
# ConnectionAbortedError rather than the BrokenPipeError the other two systems
# raise. Catching all of them is about how the app feels to use: somebody's
# terminal window should not fill with red because they closed a tab.
_CLIENT_WENT_AWAY = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError)


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
class Json:
    """A JSON response."""

    def __init__(self, data, status: int = 200, headers: dict | None = None):
        self.data = json.dumps(data, default=str).encode("utf-8")
        self.status = status
        self.content_type = "application/json; charset=utf-8"
        self.headers = headers or {}


class Text:
    """A plain-text response."""

    def __init__(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8"):
        self.data = text.encode("utf-8")
        self.status = status
        self.content_type = content_type
        self.headers = {}


class Error(Json):
    """A JSON error response the front end knows how to display."""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__({"error": message, **extra}, status=status)


class Bytes:
    """A raw-bytes response, for images and other non-text assets."""

    def __init__(self, data: bytes, content_type: str, status: int = 200):
        self.data = data
        self.content_type = content_type
        self.status = status
        self.headers = {"Cache-Control": "no-cache"}


class Redirect:
    def __init__(self, location: str, status: int = 302):
        self.location = location
        self.status = status


class Stream:
    """A streaming response.

    ``chunks`` is any iterable of ``bytes``. Each item is flushed to the
    browser as it arrives, which is what makes text appear word-by-word
    instead of all at once.
    """

    def __init__(self, chunks, content_type: str = "text/event-stream; charset=utf-8", status: int = 200):
        self.chunks = chunks
        self.content_type = content_type
        self.status = status
        self.headers = {
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        }

    @classmethod
    def sse(cls, events):
        """Turn an iterable of dicts into a Server-Sent Events stream."""

        def generate():
            try:
                for event in events:
                    if event is None:
                        continue
                    payload = json.dumps(event, default=str)
                    yield ("data: " + payload + "\n\n").encode("utf-8")
            except Exception as exc:  # surface mid-stream failures in the UI
                payload = json.dumps({"type": "error", "message": str(exc)})
                yield ("data: " + payload + "\n\n").encode("utf-8")
            yield b"data: [DONE]\n\n"

        return cls(generate())


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
class Request:
    def __init__(self, method, path, query, headers, body: bytes, params: dict):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.params = params

    def q(self, key: str, default=None):
        values = self.query.get(key)
        return values[0] if values else default

    def q_int(self, key: str, default: int) -> int:
        try:
            return int(self.q(key, default))
        except (TypeError, ValueError):
            return default

    def json(self, default=None):
        if not self.body:
            return {} if default is None else default
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {} if default is None else default
        return value if isinstance(value, (dict, list)) else ({} if default is None else default)

    def header(self, name: str, default=None):
        return self.headers.get(name, default)


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------
class Router:
    """Maps "METHOD /path/with/{params}" to a function.

    A handler is always called with one argument, the ``Request``. Any
    ``{name}`` pieces of the path are on ``request.params["name"]`` rather than
    being passed as arguments — worth knowing, because a handler that expects
    them as arguments fails at request time rather than at import time.
    """

    def __init__(self):
        self._routes: list[tuple[str, re.Pattern, object]] = []

    def add(self, method: str, pattern: str, handler):
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method.upper(), regex, handler))

    def route(self, method: str, pattern: str):
        def decorator(handler):
            self.add(method, pattern, handler)
            return handler

        return decorator

    def get(self, pattern: str):
        return self.route("GET", pattern)

    def post(self, pattern: str):
        return self.route("POST", pattern)

    def delete(self, pattern: str):
        return self.route("DELETE", pattern)

    def match(self, method: str, path: str):
        allowed = set()
        for route_method, regex, handler in self._routes:
            found = regex.match(path)
            if not found:
                continue
            if route_method == method or (route_method == "GET" and method == "HEAD"):
                return handler, found.groupdict()
            allowed.add(route_method)
        if allowed:
            return None, {"_allow": ", ".join(sorted(allowed))}
        return None, None


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
def free_port(preferred: int, host: str = "127.0.0.1", tries: int = 25) -> int:
    """Return `preferred` if it is free, else the next free port after it."""
    for offset in range(tries):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "nanoDoc"

    # Silence the default stderr noise; the app prints its own banner.
    def log_message(self, fmt, *args):  # noqa: D102
        if os.environ.get("NANODOC_VERBOSE"):
            sys.stderr.write("[http] " + (fmt % args) + "\n")

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self):  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self):  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")

    # -- internals ---------------------------------------------------------
    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        app: "App" = self.server.app  # type: ignore[attr-defined]

        handler, params = app.router.match(method, path)
        if handler is None:
            if params is None:
                self._send(app.static_response(path, method))
            else:
                self._send(Error("Method not allowed on this address.", 405, allow=params["_allow"]))
            return

        request = Request(method, path, parse_qs(parsed.query), self.headers, self._read_body(), params)
        try:
            response = handler(request)
        except Exception:  # turn crashes into a readable message, not a dead tab
            traceback.print_exc()
            response = Error("Something went wrong inside the app. The details are in the terminal window.", 500)
        if response is None:
            response = Json({"ok": True})
        self._send(response, head_only=(method == "HEAD"))

    def _send(self, response, head_only: bool = False) -> None:
        if isinstance(response, Redirect):
            self.send_response(response.status)
            self.send_header("Location", response.location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if isinstance(response, Stream):
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for chunk in response.chunks:
                    if not chunk:
                        continue
                    data = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
                    self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except _CLIENT_WENT_AWAY:
                pass  # the user closed the tab mid-answer; nothing to do
            return

        data = response.data
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(data)
            except _CLIENT_WENT_AWAY:
                pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, _Handler)

    def server_bind(self):
        """Bind the port without the reverse-DNS lookup Python does by default.

        ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` — a name
        lookup for the address just bound. On a healthy machine that is free;
        on one whose DNS resolver is slow or unreachable it is tens of seconds
        of silence before the app says anything at all. Nothing here uses
        ``server_name``, so skipping the lookup is a straight win.
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class App:
    """The whole application: a router plus a folder of static files."""

    def __init__(self, name: str, web_dir, version: str = "0.0.0"):
        self.name = name
        self.version = version
        self.web_dir = Path(web_dir)
        self.router = Router()
        self.server: _Server | None = None
        self.url = ""
        self._stopping = threading.Event()

    # -- convenience route helpers ----------------------------------------
    def get(self, pattern):
        return self.router.get(pattern)

    def post(self, pattern):
        return self.router.post(pattern)

    def delete(self, pattern):
        return self.router.delete(pattern)

    def set(self, method, pattern, handler):
        self.router.add(method, pattern, handler)

    # -- static files ------------------------------------------------------
    def static_response(self, path: str, method: str = "GET"):
        """Serve a file from the web folder, refusing to escape it."""
        if method == "HEAD":
            method = "GET"
        relative = path.lstrip("/") or "index.html"
        target = (self.web_dir / relative).resolve()
        try:
            target.relative_to(self.web_dir.resolve())
        except ValueError:
            return Error("Not found.", 404)
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            # An unknown API address is a mistake, not a page — say so plainly.
            if relative.startswith("api/"):
                return Error("There is no such address: /" + relative, 404)
            # Anything else is treated as a page in the single-page app.
            if "." not in Path(relative).name:
                target = self.web_dir / "index.html"
                if not target.is_file():
                    return Error("Not found.", 404)
            else:
                return Error("Not found.", 404)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return Bytes(target.read_bytes(), content_type)

    # -- lifecycle ---------------------------------------------------------
    def serve(self, host: str = "127.0.0.1", port: int = 8760, open_browser: bool = True, quiet: bool = False):
        port = free_port(port, host)
        self.server = _Server((host, port), self)
        shown_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        self.url = "http://%s:%d" % (shown_host, port)

        if not quiet:
            print()
            print("  %s %s  ·  %s" % (self.name, self.version, self.url))
            print("  Keep this window open. Press Ctrl+C to stop.")
            print()
        if open_browser:
            threading.Thread(target=self._open_when_ready, args=(self.url,), daemon=True).start()
        try:
            self.server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            # serve_forever has already returned here, so closing is safe.
            self._close_socket()
        return self.url

    def _open_when_ready(self, url: str) -> None:
        for _ in range(50):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect((urlparse(url).hostname, urlparse(url).port))
                    break
                except OSError:
                    time.sleep(0.1)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def stop(self) -> None:
        """Ask the server to stop from any thread."""
        threading.Thread(target=self.shutdown, daemon=True).start()

    def shutdown(self) -> None:
        """Stop serving and release the port.

        ``serve_forever`` is stopped first and *then* the socket is closed —
        closing it underneath a live ``select()`` raises WinError 10038 on
        Windows, which looks alarming and means nothing.
        """
        if not self.server:
            return
        try:
            self.server.shutdown()
        except Exception:
            pass
        self._close_socket()

    def _close_socket(self) -> None:
        if self.server:
            try:
                self.server.server_close()
            except Exception:
                pass


def is_local_request(handler) -> bool:
    """True when the request came from this machine (the app is local-only)."""
    host = (handler.client_address[0] if handler.client_address else "") or ""
    return host in _LOCAL_HOSTS
