"""Localhost OpenAI proxy with durable, content-free accounting."""

from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import time

from manager import ManagerError


class UsageReader:
    def __init__(self):
        self.pending = b""
        self.usage = None
        self.done = False
        self.error = False

    def feed(self, chunk):
        self.pending += chunk
        if len(self.pending) > 4 * 1024 * 1024:
            raise ManagerError("Inference stream event exceeds 4 MiB.", 502)
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                self.done = True
                continue
            try:
                value = json.loads(payload)
            except ValueError as exc:
                raise ManagerError("Invalid JSON in inference stream.", 502) from exc
            if not isinstance(value, dict):
                raise ManagerError("Unexpected inference stream payload.", 502)
            if isinstance(value.get("usage"), dict):
                self.usage = value["usage"]
            if value.get("error"):
                self.error = True


class TrackingServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self, port, manager):
        self.manager = manager
        super().__init__(("127.0.0.1", port), TrackingHandler)
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}


class TrackingHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(600)

    def log_message(self, fmt, *args):
        # Do not log client-controlled paths or request content.
        return

    def reply(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.server.manager._log("Tracked API client disconnected.")

    def trusted(self):
        if (len(self.headers.get_all("Host", [])) != 1
                or self.headers["Host"].lower() not in self.server.hosts):
            raise ManagerError("Use the tracked API localhost URL.", 403)
        if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ManagerError("Use a server-side OpenAI client, not cross-origin browser requests.", 403)

    def do_GET(self):
        try:
            self.trusted()
            manager = self.server.manager
            if self.path != "/v1/models":
                raise ManagerError("Route not found.", 404)
            with manager._lock:
                names = (list(manager.catalog.value["enabled"]) if manager.workspace["mode"] == "router"
                         else [manager.config.model] if manager.config.model else [])
            self.reply(200, {"object": "list", "data": [
                {"id": name, "object": "model", "owned_by": "local"} for name in names]})
        except ManagerError as exc:
            self.reply(exc.status, {"error": str(exc)})

    def do_POST(self):
        manager = self.server.manager
        request_id = None
        headers_sent = False
        connection = None
        started = time.monotonic()
        usage = None
        status = "failed"
        http_status = 502
        error = None
        try:
            self.trusted()
            if self.path not in {"/v1/chat/completions", "/v1/completions"}:
                raise ManagerError("Only chat/completions and completions are tracked.", 404)
            if self.headers.get("Transfer-Encoding"):
                raise ManagerError("Transfer-Encoding is not supported.")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise ManagerError("One numeric Content-Length is required.")
            length = int(lengths[0])
            if not 1 <= length <= 16 * 1024 * 1024:
                raise ManagerError("Request must be between 1 byte and 16 MiB.", 413)
            if self.headers.get_content_type() != "application/json":
                raise ManagerError("Content-Type must be application/json.", 415)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ManagerError("Incomplete request.")
            try:
                value = json.loads(raw)
            except ValueError as exc:
                raise ManagerError("Invalid JSON request.") from exc
            if not isinstance(value, dict) or not isinstance(value.get("model"), str):
                raise ManagerError("A string model ID is required.")
            if "stream" in value and type(value["stream"]) is not bool:
                raise ManagerError("stream must be a boolean.")
            streaming = value.get("stream", False)
            if streaming:
                options = value.get("stream_options", {})
                if not isinstance(options, dict):
                    raise ManagerError("stream_options must be an object.")
                value["stream_options"] = {**options, "include_usage": True}
            request_id, port, token = manager.begin_generation(value["model"])
            connection = HTTPConnection("127.0.0.1", port, timeout=600)
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            connection.request("POST", self.path, json.dumps(value).encode(), headers)
            response = connection.getresponse()
            http_status = response.status
            if streaming and http_status == 200:
                if "text/event-stream" not in response.getheader("Content-Type", ""):
                    raise ManagerError("Server did not return an SSE stream.", 502)
                self.send_response(http_status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
                reader = UsageReader()
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    reader.feed(chunk)
                    usage = reader.usage
                    self.wfile.write(chunk)
                    self.wfile.flush()
                if not reader.done or reader.error:
                    raise ManagerError("Inference stream failed or ended without [DONE].", 502)
                status = "complete"
            else:
                body = response.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise ManagerError("Inference response exceeds 16 MiB.", 502)
                try:
                    result = json.loads(body)
                except ValueError as exc:
                    raise ManagerError("Invalid JSON inference response.", 502) from exc
                if not isinstance(result, dict):
                    raise ManagerError("Unexpected inference response.", 502)
                usage = result.get("usage")
                status = "complete" if 200 <= http_status < 300 and not result.get("error") else "failed"
                if status == "failed":
                    error = f"Inference returned HTTP {http_status} or an error payload."
                self.send_response(http_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                headers_sent = True
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            status = "disconnected"
            error = "Client disconnected; usage may be incomplete."
            manager._log(error)
        except (ManagerError, OSError, HTTPException, sqlite3.Error) as exc:
            error = str(exc) if isinstance(exc, ManagerError) else "Tracked inference transport or storage failed."
            http_status = exc.status if isinstance(exc, ManagerError) else 502
            manager._log(f"Tracked request failed: {error}")
            if not headers_sent:
                self.reply(http_status, {"error": error})
        finally:
            if connection is not None:
                connection.close()
            if request_id is not None:
                try:
                    manager.ledger.finish_request(request_id, status, http_status,
                                                  time.monotonic() - started, usage, error)
                except sqlite3.Error as exc:
                    with manager._lock:
                        manager._error = f"Cannot persist generation statistics: {exc}"
                        manager._log(manager._error)
                finally:
                    manager.end_generation()
