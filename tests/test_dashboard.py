from dataclasses import asdict
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import DashboardServer, ROOT, open_dashboard
from manager import LaunchConfig, ManagerError, ServerManager, parse_config


FAKE = Path(__file__).with_name("fake_server.py").resolve()


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for expected server state.")


class FakeManager(ServerManager):
    def command(self, config):
        return [sys.executable, str(FAKE), *super().command(config)[1:]]


class ManagerFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="llama dashboard tests ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.data = self.base / ".local"
        self.model = self.base / "test model.gguf"
        self.model.write_bytes(b"GGUF{}")
        self.manager = FakeManager(self.data, self.base)
        self.addCleanup(self.manager.close)
        self.config = asdict(LaunchConfig(executable=sys.executable, model=str(self.model),
                                         port=free_port()))

    def save(self, **changes):
        self.config.update(changes)
        return self.manager.save_config(self.config)

    def ready(self):
        return wait_for(lambda: self.manager.status()["state"] == "running")

    def options(self, **value):
        self.model.write_bytes(b"GGUF" + json.dumps(value).encode())


class LifecycleTests(ManagerFixture, unittest.TestCase):
    def test_defaults_and_atomic_persistence(self):
        self.assertEqual(self.manager.get_config(), asdict(LaunchConfig()))
        saved = self.save()
        self.assertEqual(saved["model"], str(self.model.resolve()))
        self.manager.close()
        reopened = FakeManager(self.data, self.base)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_config(), saved)
        self.assertEqual(list(self.data.glob("tmp*")), [])

    def test_relative_model_path(self):
        saved = self.save(model=self.model.name)
        self.assertEqual(saved["model"], str(self.model))

    def test_invalid_fields_ranges_and_types(self):
        for key, value in [("port", True), ("port", 0), ("port", 65536),
                           ("context_size", 0), ("threads", -1), ("gpu_layers", 1000),
                           ("threads", "4"), ("model", ""), ("executable", "a\0b")]:
            with self.subTest(key=key, value=value), self.assertRaises(ManagerError):
                parse_config({**self.config, key: value})
        for value in [[], None, {}, {**self.config, "host": "0.0.0.0"},
                      {**self.config, "extra_args": ["--host", "0.0.0.0"]}]:
            with self.subTest(value=value), self.assertRaises(ManagerError):
                parse_config(value)

    def test_missing_and_invalid_files(self):
        for key in ("executable", "model"):
            with self.subTest(key=key), self.assertRaisesRegex(ManagerError, "does not exist"):
                self.manager.save_config({**self.config, key: str(self.base / "missing.gguf")})
        self.model.write_bytes(b"not a model")
        with self.assertRaisesRegex(ManagerError, "GGUF header"):
            self.save()
        wrong = self.base / "wrong.txt"
        wrong.write_bytes(b"GGUF")
        with self.assertRaisesRegex(ManagerError, r"\.gguf"):
            self.save(model=str(wrong))

    @unittest.skipIf(os.name == "nt", "POSIX executable permission check")
    def test_executable_permission(self):
        executable = self.base / "llama-server"
        executable.write_text("not executable")
        executable.chmod(0o600)
        with self.assertRaisesRegex(ManagerError, "permissions"):
            self.save(executable=str(executable))

    @unittest.skipUnless(os.name == "nt", "Windows requires native .exe")
    def test_windows_rejects_batch_files(self):
        executable = self.base / "llama-server.cmd"
        executable.write_text("@echo fake")
        with self.assertRaisesRegex(ManagerError, "not a batch file"):
            self.save(executable=str(executable))

    def test_only_one_manager_per_data_directory(self):
        with self.assertRaisesRegex(ManagerError, "another dashboard"):
            FakeManager(self.data, self.base)
        self.manager.close()
        replacement = FakeManager(self.data, self.base)
        replacement.close()

    def test_corrupt_configuration_is_explicit(self):
        self.manager.close()
        (self.data / "config.json").write_text("{invalid")
        with self.assertRaises(json.JSONDecodeError):
            FakeManager(self.data, self.base)
        (self.data / "config.json").write_text("{}")
        with self.assertRaises(ManagerError):
            FakeManager(self.data, self.base)

    def test_command_is_local_and_shell_free(self):
        command = ServerManager.command(self.manager, LaunchConfig(**self.config))
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertEqual(command[command.index("--model") + 1], str(self.model))
        self.assertNotIn("--threads", command)
        command = ServerManager.command(self.manager, LaunchConfig(**{**self.config, "threads": 4}))
        self.assertEqual(command[-2:], ["--threads", "4"])

    def test_start_ready_logs_stop_and_restart(self):
        self.save()
        self.assertEqual(self.manager.start()["state"], "starting")
        self.ready()
        status = self.manager.status()
        self.assertIsNotNone(status["pid"])
        self.assertIn("reports ok", status["health_detail"])
        wait_for(lambda: "fake server stderr" in "\n".join(self.manager.logs()))
        self.assertIn("fake server stdout", "\n".join(self.manager.logs()))
        with self.assertRaisesRegex(ManagerError, "Stop the managed"):
            self.manager.start()
        with self.assertRaisesRegex(ManagerError, "Stop the managed"):
            self.save(context_size=8192)
        self.assertEqual(self.manager.stop()["state"], "stopped")
        self.assertIsNone(self.manager.status()["pid"])
        self.assertEqual(self.manager.stop()["state"], "stopped")
        self.manager.start()
        self.ready()
        self.manager.stop()
        self.assertIn("fake server stderr", (self.data / "llama-server.log").read_text())

    def test_loading_is_not_ready_and_can_be_stopped(self):
        self.options(delay=30)
        self.save()
        self.manager.start()
        wait_for(lambda: "HTTP 503" in self.manager.status()["health_detail"])
        self.assertEqual(self.manager.status()["state"], "starting")
        self.assertEqual(self.manager.stop()["state"], "stopped")

    def test_model_deleted_after_save_is_rejected(self):
        self.save()
        self.model.unlink()
        with self.assertRaisesRegex(ManagerError, "does not exist"):
            self.manager.start()
        self.assertIsNone(self.manager.status()["pid"])

    def test_occupied_port_does_not_launch_or_stop_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self.save(port=listener.getsockname()[1])
            with patch("manager.subprocess.Popen") as spawn:
                with self.assertRaisesRegex(ManagerError, "occupied"):
                    self.manager.start()
                spawn.assert_not_called()
            self.manager.stop()
            with socket.create_connection(listener.getsockname(), timeout=1):
                pass

    def test_launch_failure_is_reported_and_logged(self):
        self.save()
        with patch("manager.subprocess.Popen", side_effect=PermissionError("cannot execute")):
            with self.assertRaisesRegex(ManagerError, "Could not launch"):
                self.manager.start()
        self.assertEqual(self.manager.status()["state"], "error")
        self.assertIn("cannot execute", self.manager.status()["error"])
        self.assertIn("cannot execute", "\n".join(self.manager.logs()))

    def test_immediate_and_runtime_exits(self):
        for options in [{"exit_immediately": 9}, {"exit_after": 0.4}]:
            with self.subTest(options=options):
                self.options(**options)
                self.save()
                self.manager.start()
                wait_for(lambda: self.manager.status()["state"] == "error")
                code = 9 if "exit_immediately" in options else 7
                self.assertEqual(self.manager.status()["exit_code"], code)
                self.assertIn(f"code {code}", self.manager.status()["error"])
                self.assertIsNone(self.manager.status()["pid"])
                wait_for(lambda: "deliberate" in "\n".join(self.manager.logs()))

    def test_close_stops_only_owned_process(self):
        external_port = free_port()
        external = subprocess.Popen(
            [sys.executable, str(FAKE), "--model", str(self.model), "--host", "127.0.0.1",
             "--port", str(external_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop_external():
            if external.poll() is None:
                external.terminate()
            external.wait(timeout=5)

        self.addCleanup(stop_external)
        wait_for(lambda: self.manager._health(external_port)[0])
        self.save()
        self.manager.start()
        self.ready()
        owned = self.manager._process
        self.manager.close()
        self.assertIsNotNone(owned.poll())
        self.assertIsNone(external.poll())
        self.assertTrue(self.manager._health(external_port)[0])
        with self.assertRaisesRegex(ManagerError, "shutting down"):
            self.manager.start()

    @unittest.skipIf(os.name == "nt", "POSIX graceful termination and forced-kill fallback")
    def test_force_kills_only_stubborn_owned_process(self):
        self.options(ignore_terminate=True)
        self.save()
        self.manager.start()
        self.ready()
        owned = self.manager._process
        self.manager.stop()
        self.assertIsNotNone(owned.poll())
        self.assertIn("killing the owned process", "\n".join(self.manager.logs()))

    def test_logs_are_bounded_and_rotated(self):
        self.manager._log_handler.maxBytes = 1024
        for index in range(750):
            self.manager._log(f"line {index} " + "x" * 100)
        self.assertEqual(len(self.manager.logs()), 500)
        self.assertIn("line 749", self.manager.logs()[-1])
        self.assertTrue((self.data / "llama-server.log.1").exists())
        self.assertEqual(len(list(self.data.glob("llama-server.log*"))), 3)

    def test_log_write_failure_is_visible(self):
        with patch.object(self.manager._log_handler, "emit", side_effect=OSError("disk full")):
            self.manager._log("test output")
        self.assertIn("disk full", self.manager.status()["error"])

    def test_failed_save_preserves_previous_configuration(self):
        self.save()
        before = (self.data / "config.json").read_bytes()
        with patch("manager.os.replace", side_effect=PermissionError("read-only")):
            with self.assertRaisesRegex(ManagerError, "Cannot save configuration"):
                self.save(port=free_port())
        self.assertEqual((self.data / "config.json").read_bytes(), before)
        self.assertEqual(self.manager.get_config(), json.loads(before))
        self.assertEqual(list(self.data.glob("tmp*")), [])


class RouteTests(ManagerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = DashboardServer(0, self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_dashboard)
        self.port = self.server.server_port
        self.token = self.server.csrf_token

    def stop_dashboard(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def request(self, path, body=None, headers=None, method=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        defaults = {}
        if body is not None:
            method = method or "POST"
            body = json.dumps(body).encode() if not isinstance(body, bytes) else body
            defaults = {"Content-Type": "application/json", "X-CSRF-Token": self.token}
        defaults.update(headers or {})
        try:
            connection.request(method or "GET", path, body=body, headers=defaults)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_assets_status_config_and_logs_routes(self):
        for path, expected_type in [("/", "text/html"), ("/static/app.js", "text/javascript"),
                                    ("/static/options.js", "text/javascript"),
                                    ("/static/decision.js", "text/javascript"),
                                    ("/static/theme.js", "text/javascript"),
                                    ("/static/style.css", "text/css")]:
            with self.subTest(path=path):
                code, headers, body = self.request(path)
                self.assertEqual(code, 200)
                self.assertTrue(headers["Content-Type"].startswith(expected_type))
                self.assertTrue(body)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(json.loads(self.request("/api/status")[2])["state"], "stopped")
        data = json.loads(self.request("/api/config")[2])
        self.assertEqual(data["csrf_token"], self.token)
        self.assertEqual(data["config"], asdict(LaunchConfig()))
        self.assertEqual(json.loads(self.request("/api/logs")[2]), {"lines": []})
        self.assertEqual(self.request("/../manager.py")[0], 404)
        self.assertEqual(self.request("/api/missing")[0], 404)

    def test_full_lifecycle_over_http(self):
        code, _, body = self.request("/api/config", self.config)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["config"]["model"], str(self.model))
        self.assertEqual(self.request("/api/start", {})[0], 200)
        wait_for(lambda: json.loads(self.request("/api/status")[2])["state"] == "running")
        self.assertEqual(self.request("/api/start", {})[0], 409)
        self.assertEqual(self.request("/api/config", self.config)[0], 409)
        wait_for(lambda: "fake server stdout" in self.request("/api/logs")[2].decode())
        code, _, body = self.request("/api/stop", {})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["state"], "stopped")
        self.assertIsNone(json.loads(body)["pid"])

    def test_file_picker_route_selection_cancel_errors_and_lock(self):
        with patch("app.select_model_file", return_value=str(self.model)) as picker:
            code, _, body = self.request("/api/pick-model", {})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body), {"path": str(self.model)})
            picker.assert_called_once_with()
            picker.return_value = None
            self.assertEqual(json.loads(self.request("/api/pick-model", {})[2]), {"path": None})
            picker.side_effect = ManagerError("No desktop display.", 500)
            self.assertEqual(self.request("/api/pick-model", {})[0], 500)
            self.assertFalse(self.server.file_picker_lock.locked())
            picker.reset_mock()
            self.server.file_picker_lock.acquire()
            try:
                self.assertEqual(self.request("/api/pick-model", {})[0], 409)
                picker.assert_not_called()
                self.assertEqual(self.request("/api/status")[0], 200)
            finally:
                self.server.file_picker_lock.release()

    def test_file_picker_route_requires_trusted_empty_post(self):
        with patch("app.select_model_file") as picker:
            self.assertEqual(self.request("/api/pick-model")[0], 404)
            self.assertEqual(self.request("/api/pick-model", {"path": "anything"})[0], 400)
            self.assertEqual(self.request("/api/pick-model", {},
                                          {"X-CSRF-Token": ""})[0], 403)
            self.assertEqual(self.request("/api/pick-model", {},
                                          {"Origin": "https://evil.example"})[0], 403)
            picker.assert_not_called()

    def test_cross_origin_host_and_token_rejected(self):
        for path, body, headers in [
            ("/api/config", None, {"Host": "evil.example"}),
            ("/api/config", None, {"Origin": "https://evil.example"}),
            ("/api/config", None, {"Sec-Fetch-Site": "cross-site"}),
            ("/api/start", {}, {"X-CSRF-Token": ""}),
            ("/api/stop", {}, {"X-CSRF-Token": "wrong"}),
            ("/api/start", {}, {"X-CSRF-Token": "\u00e9"}),
            ("/api/start", {}, {"Origin": "http://127.0.0.1:1"}),
        ]:
            with self.subTest(headers=headers):
                code, _, response = self.request(path, body, headers)
                self.assertEqual(code, 403)
                self.assertIn("error", json.loads(response))
        self.assertIsNone(self.manager.status()["pid"])
        self.assertEqual(self.request("/api/config",
                                      headers={"Host": f"localhost:{self.port}"})[0], 200)
        self.assertEqual(self.request("/api/stop", {},
                                      {"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)
        code, _, body = self.request("/", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(code, 200)
        self.assertNotIn(self.token.encode(), body)
        self.assertEqual(self.request("/", headers={"Host": "evil.example"})[0], 403)

    def test_invalid_post_requests_are_explicit(self):
        for path, body, headers, expected in [
            ("/api/config", b"{bad", {}, 400),
            ("/api/config", b"\xff", {}, 400),
            ("/api/config", [], {}, 400),
            ("/api/config", self.config, {"Content-Type": "text/plain"}, 415),
            ("/api/start", {"extra": 1}, {}, 400),
            ("/api/stop", [], {}, 400),
            ("/api/start", b"", {}, 413),
            ("/api/start", b"x" * 16385, {}, 413),
            ("/api/start", {}, {"Transfer-Encoding": "chunked"}, 400),
            ("/api/start", {}, {"Content-Length": "invalid"}, 400),
            ("/api/missing", {}, {}, 404),
        ]:
            with self.subTest(path=path, expected=expected):
                code, _, response = self.request(path, body, headers)
                self.assertEqual(code, expected)
                self.assertIn("error", json.loads(response))

    def test_occupied_server_port_and_missing_paths_over_http(self):
        code, _, body = self.request("/api/config", {**self.config, "model": str(self.base / "no.gguf")})
        self.assertEqual(code, 400)
        self.assertIn("does not exist", json.loads(body)["error"])
        config = {**self.config, "port": self.port}
        self.assertEqual(self.request("/api/config", config)[0], 200)
        code, _, body = self.request("/api/start", {})
        self.assertEqual(code, 409)
        self.assertIn("occupied", json.loads(body)["error"])
        self.assertEqual(self.request("/api/status")[0], 200)

    def test_duplicate_dashboard_port_fails(self):
        with self.assertRaises(OSError):
            DashboardServer(self.port, self.manager)


class CommandLineTests(unittest.TestCase):
    def run_app(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "app.py"), *args],
                              capture_output=True, text=True, timeout=10)

    def test_help_and_invalid_port(self):
        result = self.run_app("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("default: 1101", result.stdout)
        self.assertIn("--open-browser", result.stdout)
        self.assertNotIn("--api-port", result.stdout)
        result = self.run_app("--port", "0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("between 1 and 65535", result.stderr)

    @patch("app.webbrowser.open", return_value=True)
    def test_open_dashboard_uses_default_browser(self, browser_open):
        url = "http://127.0.0.1:1101"
        self.assertTrue(open_dashboard(url))
        browser_open.assert_called_once_with(url)

    @patch("app.webbrowser.open", return_value=False)
    def test_open_dashboard_reports_failure(self, browser_open):
        with patch("sys.stderr") as stderr:
            self.assertFalse(open_dashboard("http://127.0.0.1:1101"))
        self.assertTrue(stderr.write.called)

    def test_cli_occupied_port_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            result = self.run_app("--port", str(listener.getsockname()[1]), "--data-dir", directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Dashboard port", result.stderr)
            self.assertIn("occupied", result.stderr)
            (Path(directory) / "config.json").write_text("{invalid")
            result = self.run_app("--port", str(free_port()), "--data-dir", directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Error:", result.stderr)


if __name__ == "__main__":
    unittest.main()
