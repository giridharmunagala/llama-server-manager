"""Local-only HTTP dashboard. No third-party dependencies."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import sys
import threading
import webbrowser
from urllib.parse import parse_qs, urlsplit

from manager import ManagerError, ServerManager
from file_picker import select_model_file


ROOT = Path(__file__).resolve().parent
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/options.js": ("options.js", "text/javascript; charset=utf-8"),
    "/static/decision.js": ("decision.js", "text/javascript; charset=utf-8"),
    "/static/observability.js": ("observability.js", "text/javascript; charset=utf-8"),
    "/static/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}


def open_dashboard(url):
    try:
        opened = webbrowser.open(url)
    except webbrowser.Error as exc:
        print(f"Cannot open the default browser: {exc}", file=sys.stderr)
        return False
    if not opened:
        print(f"Cannot open the default browser. Open {url} manually.", file=sys.stderr)
    return opened


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, port, manager):
        self.manager = manager
        self.csrf_token = secrets.token_urlsafe(32)
        self.file_picker_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        self.origins = {f"http://{host}" for host in self.hosts}


class DashboardHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, fmt, *args):
        # Routine polling is intentionally quiet; unexpected errors go to stderr.
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _reply(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.log_error("Client disconnected before the response could be delivered.")

    def _trusted_request(self, mutation=False, public_asset=False):
        if len(self.headers.get_all("Host", [])) != 1 or self.headers["Host"].lower() not in self.server.hosts:
            raise ManagerError("Use the dashboard's localhost URL; this Host is not allowed.", 403)
        # Opening a link from another site must work; assets contain no settings or token.
        if public_asset:
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.origins:
            raise ManagerError("Cross-origin requests are not allowed.", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ManagerError("Cross-site requests are not allowed.", 403)
        if mutation and not secrets.compare_digest(
                self.headers.get("X-CSRF-Token", "").encode("utf-8"),
                self.server.csrf_token.encode("utf-8")):
            raise ManagerError("Missing or invalid dashboard request token.", 403)

    def _json_body(self):
        if self.headers.get_content_type() != "application/json":
            raise ManagerError("Content-Type must be application/json.", 415)
        if self.headers.get("Transfer-Encoding"):
            raise ManagerError("Transfer-Encoding is not supported.")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise ManagerError("One Content-Length header is required.")
        try:
            length = int(lengths[0])
        except ValueError as exc:
            raise ManagerError("Content-Length must be an integer.") from exc
        limit = 1024 * 1024 if self.path.split("?", 1)[0] == "/api/catalog" else 16384
        if not 1 <= length <= limit:
            raise ManagerError(f"JSON request must be between 1 and {limit} bytes.", 413)
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ManagerError("Incomplete JSON request.")
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ManagerError("Request body is not valid UTF-8 JSON.") from exc

    def do_GET(self):
        try:
            path = self.path.split("?", 1)[0]
            self._trusted_request(public_asset=path in ASSETS)
            if path in ASSETS:
                filename, content_type = ASSETS[path]
                self._reply(200, (ROOT / "static" / filename).read_bytes(), content_type)
            elif path == "/api/config":
                self._reply(200, {"config": self.server.manager.get_config(),
                                  "csrf_token": self.server.csrf_token})
            elif path == "/api/status":
                self._reply(200, self.server.manager.status())
            elif path == "/api/logs":
                self._reply(200, {"lines": self.server.manager.logs()})
            elif path == "/api/workspace":
                workspace = self.server.manager.get_workspace()
                self._reply(200, {**workspace,
                                  "api_url": f"http://127.0.0.1:{workspace['settings']['port']}/v1"})
            elif path == "/api/detect":
                self._reply(200, self.server.manager.detect_executable())
            elif path == "/api/models":
                self._reply(200, self.server.manager.router_models())
            elif path == "/api/statistics":
                self._reply(200, self.server.manager.ledger.summary())
            elif path == "/api/observability":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if set(query) - {"model", "range"} or any(len(values) != 1 for values in query.values()):
                    raise ManagerError("Observability accepts one model and one range filter.")
                self._reply(200, self.server.manager.observability.summary(
                    query.get("model", [None])[0], query.get("range", ["24h"])[0]))
            else:
                self._reply(404, {"error": "Route not found."})
        except ManagerError as exc:
            self._reply(exc.status, {"error": str(exc)})
        except (OSError, sqlite3.Error) as exc:
            self.log_error("Cannot serve dashboard: %s", exc)
            self._reply(500, {"error": f"Cannot serve dashboard: {exc}"})

    def do_POST(self):
        try:
            self._trusted_request(mutation=True)
            path = self.path.split("?", 1)[0]
            if path not in {"/api/config", "/api/start", "/api/stop", "/api/workspace",
                            "/api/catalog", "/api/import", "/api/router", "/api/costs",
                            "/api/pick-model"}:
                raise ManagerError("Route not found.", 404)
            value = self._json_body()
            if path == "/api/pick-model":
                if value != {}:
                    raise ManagerError("File picker requests must contain an empty JSON object.")
                if not self.server.file_picker_lock.acquire(blocking=False):
                    raise ManagerError("A file picker is already open. Select a file or cancel it.", 409)
                try:
                    self._reply(200, {"path": select_model_file()})
                finally:
                    self.server.file_picker_lock.release()
            elif path == "/api/config":
                self._reply(200, {"config": self.server.manager.save_config(value)})
            elif path == "/api/workspace":
                self._reply(200, self.server.manager.save_workspace(value))
            elif path == "/api/catalog":
                self._reply(200, self.server.manager.save_catalog(value))
            elif path == "/api/import":
                if not isinstance(value, dict) or set(value) != {"source"}:
                    raise ManagerError("Import requires a source path.")
                self._reply(200, self.server.manager.import_catalog(value["source"]))
            elif path == "/api/router":
                if (not isinstance(value, dict) or set(value) != {"action", "model"}
                        or not isinstance(value["action"], str)
                        or (value["model"] is not None and not isinstance(value["model"], str))):
                    raise ManagerError("Router action requires action and model.")
                self._reply(200, self.server.manager.router_action(value["action"], value["model"]))
            elif path == "/api/costs":
                self._reply(200, self.server.manager.ledger.save_costs(value))
            else:
                if value != {}:
                    raise ManagerError("Start and stop requests must contain an empty JSON object.")
                action = self.server.manager.start if path == "/api/start" else self.server.manager.stop
                if path == "/api/start":
                    port = self.server.manager._effective_config().port
                    if port == self.server.server_port:
                        raise ManagerError("Router port is occupied by the dashboard; ports must differ.", 409)
                self._reply(200, action())
        except ManagerError as exc:
            self._reply(exc.status, {"error": str(exc)})
        except (OSError, sqlite3.Error) as exc:
            self.log_error("Dashboard operation failed: %s", exc)
            self._reply(500, {"error": f"Dashboard operation failed: {exc}"})


def main():
    parser = argparse.ArgumentParser(description="Manage an installed llama.cpp server on localhost.")
    parser.add_argument("--port", type=int, default=1101, help="Dashboard port (default: 1101)")
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local",
                        help="Local settings/log directory (default: project .local)")
    parser.add_argument("--open-browser", action="store_true",
                        help="Open the dashboard in the default browser after startup")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    manager = None
    server = None
    try:
        manager = ServerManager(args.data_dir, ROOT)
        try:
            server = DashboardServer(args.port, manager)
        except OSError as exc:
            raise ManagerError(
                f"Dashboard port {args.port} is occupied or unavailable. Try --port with another port: {exc}"
            ) from exc
        def interrupt(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        dashboard_url = f"http://127.0.0.1:{server.server_port}"
        print(f"Dashboard: {dashboard_url}", flush=True)
        print(f"llama.cpp router: http://127.0.0.1:{manager.get_workspace()['settings']['port']}/v1", flush=True)
        print("Ctrl+C stops this dashboard and its managed llama.cpp server.", flush=True)
        if args.open_browser:
            open_dashboard(dashboard_url)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nStopping dashboard and managed server...", flush=True)
    except (ManagerError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
        if manager is not None:
            try:
                manager.close()
            except (ManagerError, OSError, sqlite3.Error) as exc:
                print(f"Shutdown error: {exc}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
