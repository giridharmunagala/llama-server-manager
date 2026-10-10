from copy import deepcopy
from http.client import HTTPConnection
import json
import os
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch

from catalog import Catalog
from ledger import DEFAULT_COSTS, Ledger
from manager import ManagerError, ServerManager
from test_dashboard import FakeManager, ManagerFixture, wait_for
import test_dashboard
from tracking import TrackingServer, UsageReader


class CatalogTests(ManagerFixture, unittest.TestCase):
    def source(self):
        source = self.base / "models.ini"
        source.write_text(f"""version = 1
[*]
ctx-size = 8192
temp = 1.0
[first]
model = {self.model}
alias = first
ctx-size = 4096
[remote]
hf-repo = public/example:Q4_K_M
""")
        return source

    def test_import_preserves_original_and_filters_active_ini(self):
        source = self.source()
        original = source.read_bytes()
        result = self.manager.import_catalog(str(source))["catalog"]
        self.assertEqual(result["defaults"]["ctx-size"], "8192")
        self.assertEqual(result["models"]["first"]["ctx-size"], "4096")
        self.assertNotIn("alias", result["models"]["first"])
        result["enabled"] = ["first"]
        self.manager.save_catalog(result)
        active = self.manager.catalog.write_active()
        self.assertNotIn("[remote]", self.manager.catalog.active_path.read_text())
        self.assertIn("[first]", self.manager.catalog.active_path.read_text())
        self.assertEqual(source.read_bytes(), original)
        reopened = Catalog(self.data)
        self.assertEqual(reopened.value, result)
        self.assertEqual(active, str(reopened.active_path))

    def test_invalid_catalog_and_failed_atomic_write(self):
        self.manager.import_catalog(str(self.source()))
        original = deepcopy(self.manager.catalog.value)
        for changes in [{"enabled": ["unknown"]}, {"max_loaded": 0}, {"autoload": 1},
                        {"defaults": {"host": "0.0.0.0"}}, {"defaults": {"hft": "sensitive"}},
                        {"defaults": {"LLAMA_API_KEY": "sensitive"}},
                        {"models": {"bad]name": {"model": "x"}}},
                        {"models": {"bad": {}}}, {"defaults": {"temp": "1\nhost=remote"}}]:
            with self.subTest(changes=changes), self.assertRaises(ManagerError):
                self.manager.save_catalog({**original, **changes})
        with patch("catalog.os.replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ManagerError, "Cannot save"):
                self.manager.save_catalog({**original, "max_loaded": 2})
        self.assertEqual(self.manager.catalog.value, original)
        self.assertEqual(list(self.data.glob("tmp*")), [])
        for text in ["[same]\nmodel=x\n[same]\nmodel=y", "[DEFAULT]\nmodel=x\n[first]\n",
                     "version=2\n[first]\nmodel=x"]:
            source = self.base / "bad.ini"
            source.write_text(text)
            with self.assertRaises(ManagerError):
                self.manager.import_catalog(str(source))

    def test_decision_preset_roundtrip_preserves_chat_defaults_and_original_ini(self):
        source = self.source()
        original = source.read_bytes()
        value = self.manager.import_catalog(str(source))["catalog"]
        chat = deepcopy(value["models"]["first"])
        defaults = deepcopy(value["defaults"])
        decision = {"hf-repo": "ggml-org/Clef-Flash-GGUF:Q4_K_M", "tags": "decision-model",
                    "ctx-size": "4096", "batch-size": "4096", "ubatch-size": "4096",
                    "parallel": "1", "cache-type-k": "f16", "cache-type-v": "f16",
                    "mmproj-auto": "true", "mmproj-offload": "true", "spec-type": "none"}
        value["models"]["clef-flash"] = decision
        value["enabled"].append("clef-flash")
        self.manager.save_catalog(value)
        self.manager.catalog.write_active()
        self.assertEqual(Catalog(self.data).value["models"]["clef-flash"], decision)
        self.assertEqual(value["models"]["first"], chat)
        self.assertEqual(value["defaults"], defaults)
        self.assertEqual(source.read_bytes(), original)
        exported = self.manager.catalog.active_path.read_text()
        self.assertIn("[clef-flash]", exported)
        self.assertIn("tags = decision-model", exported)
        self.assertIn("ubatch-size = 4096", exported)
        self.manager.import_catalog(str(self.manager.catalog.active_path))
        self.assertEqual(self.manager.catalog.value["models"]["clef-flash"], decision)


class ExecutableDiscoveryTests(ManagerFixture, unittest.TestCase):
    def fake_install(self, *names):
        folder = self.base / "bin"
        folder.mkdir(exist_ok=True)
        paths = []
        for name in names:
            executable = folder / (name + ".exe" if os.name == "nt" else name)
            executable.write_bytes(b"MZ" if os.name == "nt" else b"#!/bin/sh\n")
            executable.chmod(0o755)
            paths.append(executable)
        return folder, paths

    def isolated(self, folder, locations=()):
        # Keep the developer's real PATH (and registry PATH) out of discovery.
        return patch("manager._search_path", return_value=str(folder)), \
            patch("manager._common_locations", return_value=list(locations))

    def test_unified_llama_binary_is_preferred_and_launched_with_serve(self):
        folder, (llama, _) = self.fake_install("llama", "llama-server")
        search, common = self.isolated(folder)
        with search, common:
            saved = self.manager.save_workspace({"mode": "router", "executable": "llama",
                                                 "port": self.config["port"]})
            self.assertEqual(saved["settings"]["executable"], "llama")
            detected = self.manager.detect_executable()
            self.assertTrue(detected["automatic"])
            self.assertEqual(detected["resolved"], str(llama.resolve()))
            self.assertEqual(detected["command"], "llama serve")
            config = self.manager._validate_paths(self.manager._effective_config(), router=True)
            self.assertEqual(config.executable, str(llama.resolve()))
            command = ServerManager.command(self.manager, config)
            self.assertEqual(command[:2], [str(llama.resolve()), "serve"])
            self.assertIn("--models-preset", command)

    def test_legacy_llama_server_is_used_without_serve(self):
        folder, (server,) = self.fake_install("llama-server")
        search, common = self.isolated(folder)
        with search, common:
            for configured in ("llama", "llama-server"):
                with self.subTest(configured=configured):
                    self.manager.save_workspace({"mode": "router", "executable": configured,
                                                 "port": self.config["port"]})
                    config = self.manager._validate_paths(self.manager._effective_config(), router=True)
                    self.assertEqual(config.executable, str(server.resolve()))
                    self.assertNotIn("serve", ServerManager.command(self.manager, config))
                    self.assertEqual(self.manager.detect_executable()["command"], "llama-server")

    def test_missing_command_explains_how_to_install(self):
        with patch("manager.find_executable", return_value=None):
            self.manager.save_workspace({"mode": "router", "executable": "llama",
                                         "port": self.config["port"]})
            self.assertIsNone(self.manager.detect_executable()["resolved"])
            with self.assertRaisesRegex(ManagerError, "neither 'llama' nor 'llama-server'"):
                self.manager._validate_paths(self.manager._effective_config(), router=True)

    def test_common_install_location_is_used_when_not_on_path(self):
        folder, (llama,) = self.fake_install("llama")
        search, common = self.isolated(self.base / "empty", [llama])
        with search, common:
            from manager import find_executable
            self.assertEqual(find_executable("llama"), llama.resolve())

class RouterFixture(ManagerFixture):
    def setup_router(self, **options):
        self.options(**options)
        source = self.base / "models.ini"
        source.write_text(f"[*]\nctx-size=4096\n[first]\nmodel={self.model}\n[second]\nmodel={self.model}\n")
        self.manager.import_catalog(str(source))
        self.manager.save_workspace({"mode": "router", "executable": sys.executable,
                                     "port": self.config["port"]})
        self.manager.start()
        self.ready()


class RouterTests(RouterFixture, unittest.TestCase):
    def test_direct_router_requests_hot_swap_without_manager_proxy(self):
        self.setup_router()
        for model in ("first", "second"):
            connection = HTTPConnection("127.0.0.1", self.config["port"], timeout=5)
            try:
                connection.request("POST", "/v1/chat/completions", json.dumps({"model": model}),
                                   {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["usage"]["completion_tokens"], 30)
            finally:
                connection.close()
        models = self.manager.router_models()["data"]
        self.assertEqual(models[0]["status"]["value"], "unloaded")
        self.assertEqual(models[1]["status"]["value"], "loaded")
        self.assertEqual(self.manager.ledger.summary()["totals"]["requests"], 0)
        value = deepcopy(self.manager.catalog.value)
        value["models"]["second"]["ctx-size"] = "8192"
        self.manager.save_catalog(value)
        connection = HTTPConnection("127.0.0.1", self.config["port"], timeout=5)
        try:
            connection.request("POST", "/v1/chat/completions", json.dumps({"model": "second"}),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        finally:
            connection.close()
        self.manager.router_action("apply")
        self.assertFalse(self.manager.get_workspace()["pending_apply"])

    def test_live_load_swap_unload_apply_and_restart(self):
        self.setup_router()
        command = self.manager.command(self.manager._effective_config())
        self.assertNotIn("--model", command)
        self.assertNotIn("--ctx-size", command)
        self.assertNotIn("--api-key", command)
        self.manager.router_action("load", "first")
        self.manager.router_action("load", "second")
        models = self.manager.router_models()["data"]
        self.assertEqual(models[0]["status"]["value"], "unloaded")
        self.assertEqual(models[1]["status"]["value"], "loaded")
        with self.assertRaises(ManagerError):
            self.manager.router_action("load", "unknown")
        value = deepcopy(self.manager.catalog.value)
        value["models"]["second"]["ctx-size"] = "8192"
        self.manager.save_catalog(value)
        with self.assertRaisesRegex(ManagerError, "Apply"):
            self.manager.router_action("load", "first")
        with self.assertRaisesRegex(ManagerError, "capacity"):
            self.manager.save_catalog({**value, "max_loaded": 2})
        self.manager.router_action("apply")
        self.assertEqual(self.manager.router_models()["data"][1]["status"]["value"], "unloaded")
        self.manager.router_action("load", "first")
        self.manager.router_action("unload", "first")
        self.manager.stop()
        self.manager.start()
        self.ready()
        self.assertFalse(self.manager.get_workspace()["pending_apply"])

    def test_apply_and_unload_do_not_interrupt_tracked_generations(self):
        self.setup_router()
        request_id, _, _ = self.manager.begin_generation("first")
        self.addCleanup(self.manager.end_generation)
        for action in ("apply", "unload"):
            with self.assertRaisesRegex(ManagerError, "tracked requests"):
                self.manager.router_action(action, "first")
        self.manager.ledger.finish_request(request_id, "complete", 200, 1, None, None)

    @unittest.skipUnless(os.name == "nt", "Windows job ownership")
    def test_router_stop_kills_owned_descendant(self):
        import ctypes
        from ctypes import wintypes
        self.setup_router(spawn_worker=True)
        line = wait_for(lambda: next((line for line in self.manager.logs()
                                     if "owned worker PID " in line), None))
        pid = int(line.rsplit(" ", 1)[1])
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        self.assertTrue(handle)
        try:
            self.assertEqual(api.WaitForSingleObject(handle, 0), 258)
            self.manager.stop()
            self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)
        finally:
            api.CloseHandle(handle)

    def test_job_assignment_failure_does_not_leave_suspended_process(self):
        if os.name != "nt":
            self.skipTest("Windows job ownership")
        self.setup_router()
        self.manager.stop()
        with patch("windows_job.OwnedJob.attach_and_resume", side_effect=OSError("job assignment failed")):
            with self.assertRaisesRegex(ManagerError, "job assignment failed"):
                self.manager.start()
        self.assertIsNone(self.manager.status()["pid"])
        self.assertIsNone(self.manager._job)


class TrackingTests(RouterFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.api = TrackingServer(0, self.manager)
        self.thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_api)

    def close_api(self):
        self.api.shutdown()
        self.api.server_close()
        self.thread.join(timeout=3)

    def request(self, value=None, path="/v1/chat/completions", headers=None):
        connection = HTTPConnection("127.0.0.1", self.api.server_port, timeout=5)
        defaults = {"Content-Type": "application/json"}
        defaults.update(headers or {})
        try:
            connection.request("POST" if value is not None else "GET", path,
                               json.dumps(value) if value is not None else None, defaults)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_json_stream_tokens_errors_missing_usage_and_privacy(self):
        self.setup_router()
        for value in [
            {"model": "first", "messages": [{"role": "user", "content": "private prompt sentinel"}]},
            {"model": "second", "stream": True},
            {"model": "first", "test_case": "error"},
            {"model": "first", "test_case": "missing"},
            {"model": "first", "stream": True, "test_case": "missing"},
            {"model": "first", "stream": True, "test_case": "truncated"},
            {"model": "first", "stream": True, "test_case": "stream_error"},
        ]:
            code, body = self.request(value)
            self.assertEqual(code, 500 if value.get("test_case") == "error" else 200)
            self.assertTrue(body)
        wait_for(lambda: self.manager.ledger.summary()["totals"]["in_progress"] == 0)
        summary = self.manager.ledger.summary()
        self.assertEqual(summary["totals"]["requests"], 7)
        self.assertEqual(summary["totals"]["failed"], 3)
        self.assertEqual(summary["totals"]["missing_usage"], 3)
        self.assertEqual(summary["totals"]["prompt_tokens"], 480)
        self.assertEqual(summary["totals"]["completion_tokens"], 120)
        self.assertEqual(len(summary["models"]), 2)
        self.assertNotIn("private prompt sentinel", json.dumps(summary))
        self.assertNotIn("private generated text", json.dumps(summary))
        self.assertNotIn("private upstream error", json.dumps(summary))
        self.assertEqual(self.manager.status()["inflight"], 0)

    def test_keyless_local_access_allowlist_validation_and_pending_changes(self):
        self.setup_router()
        self.assertEqual(self.request(path="/v1/models")[0], 200)
        self.assertEqual(self.request(path="/v1/models", headers={"Authorization": ""})[0], 200)
        self.assertEqual(self.request(path="/v1/models", headers={"Authorization": "Bearer unused"})[0], 200)
        self.assertEqual(self.request(path="/v1/models", headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request(path="/v1/models", headers={"Host": "evil.test"})[0], 403)
        catalog = deepcopy(self.manager.catalog.value)
        catalog["enabled"] = ["first"]
        self.manager.save_catalog(catalog)
        code, body = self.request(path="/v1/models")
        self.assertEqual(code, 200)
        self.assertEqual([row["id"] for row in json.loads(body)["data"]], ["first"])
        self.assertEqual(self.request({"model": "first"})[0], 409)
        self.manager.router_action("apply")
        self.assertEqual(self.request({"model": "second"})[0], 409)
        for value in [{}, {"model": 1}, {"model": "first", "stream": "yes"},
                      {"model": "first", "stream": True, "stream_options": []}]:
            self.assertEqual(self.request(value)[0], 400)
        self.assertEqual(self.request({"model": "first"}, path="/v1/embeddings")[0], 404)
        self.assertEqual(self.manager.ledger.summary()["totals"]["requests"], 0)

    def test_client_token_file_is_not_required_or_read(self):
        self.assertFalse((self.data / "api-token").exists())
        self.close_api()
        (self.data / "api-token").write_text("obsolete-invalid-token", encoding="ascii")
        self.api = TrackingServer(0, self.manager)
        self.thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.request(path="/v1/models")[0], 200)

    def test_single_mode_tracking_and_restart_persistence(self):
        self.save()
        self.manager.start()
        self.ready()
        self.assertEqual(self.request({"model": "local"})[0], 200)
        wait_for(lambda: self.manager.status()["inflight"] == 0)
        self.manager.stop()
        before = self.manager.ledger.summary()
        self.manager.close()
        reopened = FakeManager(self.data, self.base)
        self.addCleanup(reopened.close)
        after = reopened.ledger.summary()
        self.assertEqual(after["totals"]["completion_tokens"], 30)
        self.assertEqual(after["models"][0]["model"], str(self.model))
        self.assertEqual(after["totals"]["local_cost"], before["totals"]["local_cost"])

    def test_active_request_blocks_apply(self):
        self.setup_router()
        result = []
        worker = threading.Thread(target=lambda: result.append(
            self.request({"model": "first", "test_case": "delay"})))
        worker.start()
        try:
            wait_for(lambda: self.manager.status()["inflight"] == 1)
            with self.assertRaisesRegex(ManagerError, "tracked requests"):
                self.manager.router_action("apply")
        finally:
            worker.join(timeout=5)
        self.assertEqual(result[0][0], 200)
        wait_for(lambda: self.manager.status()["inflight"] == 0)


class AccountingTests(ManagerFixture, unittest.TestCase):
    def test_exact_runtime_energy_costs_and_rate_changes(self):
        costs = {**DEFAULT_COSTS, "electricity_per_kwh": 10, "system_watts": 200,
                 "hardware_cost": 100000, "hardware_lifetime_hours": 10000,
                 "cloud_input_per_million": 100, "cloud_output_per_million": 400}
        self.manager.ledger.save_costs(costs)
        with patch("ledger.time.monotonic", return_value=0):
            self.manager.ledger.start_session("router")
        with patch("ledger.time.monotonic", return_value=3600):
            self.manager.ledger.tick()
            self.manager.ledger.save_costs({**costs, "electricity_per_kwh": 20})
        with patch("ledger.time.monotonic", return_value=7200):
            self.manager.ledger.end_session("stopped")
        request_id = self.manager.ledger.begin_request("first")
        self.manager.ledger.finish_request(request_id, "complete", 200, 10,
                                           {"prompt_tokens": 1000000, "completion_tokens": 500000}, None)
        totals = self.manager.ledger.summary()["totals"]
        self.assertAlmostEqual(totals["energy_kwh"], 0.4)
        self.assertAlmostEqual(totals["local_cost"], 26)
        self.assertAlmostEqual(totals["cloud_cost"], 300)
        self.assertAlmostEqual(totals["estimated_savings"], 274)
        self.assertEqual(totals["runtime_seconds"], 7200)
        self.assertEqual(totals["unpriced_seconds"], 0)
        with self.assertRaisesRegex(ManagerError, "Currency"):
            self.manager.ledger.save_costs({**costs, "currency": "USD"})

    def test_invalid_costs_usage_and_interruption_recovery(self):
        for changes in [{"system_watts": float("nan")}, {"system_watts": True},
                        {"hardware_lifetime_hours": 0}, {"currency": "rupees"}]:
            with self.assertRaises(ManagerError):
                self.manager.ledger.save_costs({**DEFAULT_COSTS, **changes})
        ledger = Ledger(self.data / "recovery.sqlite3")
        ledger.start_session("router")
        request_id = ledger.begin_request("first")
        ledger.finish_request(request_id, "complete", 200, 1,
                              {"prompt_tokens": True, "completion_tokens": 10}, None)
        ledger.begin_request("second")
        ledger.tick()
        ledger.close()
        recovered = Ledger(self.data / "recovery.sqlite3")
        self.addCleanup(recovered.close)
        summary = recovered.summary()
        self.assertEqual(summary["totals"]["failed"], 1)
        self.assertEqual(summary["totals"]["missing_usage"], 2)
        self.assertEqual(summary["sessions"][0]["outcome"], "interrupted")
        self.assertEqual(summary["totals"]["completion_tokens"], 0)

    def test_fragmented_sse_usage_reader(self):
        reader = UsageReader()
        raw = b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2}}\r\n\r\ndata: [DONE]\n\n'
        for index in range(0, len(raw), 3):
            reader.feed(raw[index:index + 3])
        self.assertTrue(reader.done)
        self.assertEqual(reader.usage["completion_tokens"], 2)
        with self.assertRaises(ManagerError):
            UsageReader().feed(b"data: invalid\n")

    def test_unpriced_runtime_stays_unknown_and_config_revisions_are_retained(self):
        ledger = self.manager.ledger
        with patch("ledger.time.monotonic", return_value=0):
            ledger.start_session("router")
        with patch("ledger.time.monotonic", return_value=3600):
            ledger.save_costs({**DEFAULT_COSTS, "electricity_per_kwh": 10, "system_watts": 200})
        with patch("ledger.time.monotonic", return_value=7200):
            ledger.end_session("stopped")
        totals = ledger.summary()["totals"]
        self.assertEqual(totals["unpriced_seconds"], 3600)
        self.assertAlmostEqual(totals["local_cost"], 2)
        first = ledger.begin_request("first", {"ctx-size": "4096"})
        second = ledger.begin_request("first", {"ctx-size": "4096"})
        third = ledger.begin_request("first", {"ctx-size": "8192"})
        with ledger.lock:
            rows = ledger.db.execute("SELECT config_revision FROM requests WHERE id IN (?,?,?) "
                                     "ORDER BY id", (first, second, third)).fetchall()
            self.assertEqual(rows[0][0], rows[1][0])
            self.assertNotEqual(rows[1][0], rows[2][0])
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM configurations").fetchone()[0], 2)


class FeatureRouteTests(ManagerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = test_dashboard.DashboardServer(0, self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_dashboard)
        self.port = self.server.server_port
        self.token = self.server.csrf_token

    stop_dashboard = test_dashboard.RouteTests.stop_dashboard
    request = test_dashboard.RouteTests.request

    def test_dashboard_does_not_expose_a_client_api_key(self):
        code, _, body = self.request("/api/workspace")
        self.assertEqual(code, 200)
        self.assertNotIn("api_key", json.loads(body))
        self.assertEqual(json.loads(body)["api_url"], "http://127.0.0.1:8080/v1")
        code, _, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertNotIn(b'id="api-key"', body)
        self.assertIn(b"No proxy or API key.", body)
        for element in (b'id="option-editor"', b'id="model-sidebar"', b'id="raw-options"',
                        b'id="import-form"', b'id="save-catalog"', b'id="apply-catalog"',
                        b'src="/static/options.js"'):
            self.assertIn(element, body)

    def test_feature_routes_and_validation(self):
        for path in ("/api/workspace", "/api/statistics", "/api/models", "/api/detect"):
            self.assertEqual(self.request(path)[0], 200)
            self.assertEqual(self.request(path, headers={"Origin": "https://evil.test"})[0], 403)
        for path, value in [
            ("/api/import", {}), ("/api/catalog", {}), ("/api/workspace", {}),
            ("/api/costs", {}), ("/api/router", {"action": [], "model": None}),
        ]:
            self.assertEqual(self.request(path, value)[0], 400)
            self.assertEqual(self.request(path, value, headers={"X-CSRF-Token": ""})[0], 403)
        self.assertEqual(self.request("/api/costs", DEFAULT_COSTS)[0], 200)
        self.assertEqual(self.request("/api/router", {"action": "load", "model": "first"})[0], 409)
        with patch.object(self.manager.ledger, "summary", side_effect=sqlite3.OperationalError("disk full")):
            code, _, body = self.request("/api/statistics")
            self.assertEqual(code, 500)
            self.assertIn("disk full", json.loads(body)["error"])
