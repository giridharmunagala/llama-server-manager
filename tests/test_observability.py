from http.client import HTTPConnection
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from app import ROOT
from manager import ManagerError
from observability import LogObserver, Observability
from test_dashboard import ManagerFixture, wait_for
import test_dashboard
from test_features import RouterFixture


class LogTests(ManagerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = self.manager.observability
        self.reader = LogObserver(self.store)

    def line(self, message, task=0, port=1448, slot=1):
        self.reader.feed(f"\x1b[32m[ {port}] 0.10.892.720 I slot print_timing: "
                         f"id {slot:2} | task {task} | {message}\x1b[0m")

    def request(self, task=0, port=1448, input_tokens=120, prefill=80, output=30):
        self.line("processing task, is_child = 0", task, port)
        if input_tokens is not None:
            self.line(f"new prompt, n_ctx_slot = 4096, n_keep = 0, task.n_tokens = {input_tokens}", task, port)
        self.line(f"prompt eval time = 100.00 ms / {prefill} tokens (1.25 ms per token, 800.00 tokens per second)", task, port)
        self.line(f"eval time = 200.00 ms / {output} tokens (6.67 ms per token, 150.00 tokens per second)", task, port)
        self.line("total time = 300.00 ms / 110 tokens", task, port)
        self.line("draft acceptance = 0.50000 (10 accepted / 20 generated), mean len = 2.00", task, port)
        self.line("stop processing: n_tokens = 150, truncated = 0", task, port)

    def test_real_build_format_metadata_rates_deduplication_and_persistence(self):
        self.reader.feed("0.40.498.054 I srv proxy_reques: proxying request to model gemma-4-e4b on port 1448")
        self.request()
        self.request()
        summary = self.store.summary()
        totals = summary["totals"]
        self.assertEqual(totals["requests"], 1)
        self.assertEqual(totals["input_tokens"], 120)
        self.assertEqual(totals["prefill_tokens"], 80)
        self.assertEqual(totals["output_tokens"], 30)
        self.assertEqual(totals["cached_tokens"], 40)
        self.assertAlmostEqual(totals["cache_hit_rate"], 1 / 3)
        self.assertEqual(totals["draft_acceptance"], 0.5)
        self.assertEqual(totals["prefill_tps"], 800)
        self.assertEqual(totals["generation_tps"], 150)
        self.assertIsNone(totals["ttft_ms"])
        self.assertEqual(summary["history"][0]["model"], "gemma-4-e4b")
        self.assertEqual(summary["history"][0]["status"], "measured")
        self.assertEqual(len(summary["series"]), 1)
        self.manager.close()
        reopened = Observability(self.data / "observability.sqlite3")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.summary()["totals"]["requests"], 1)

    def test_concurrent_workers_slots_and_reused_ports_do_not_mix(self):
        for name, port in [("first", 1448), ("second", 1449)]:
            self.reader.feed(f"srv load: spawning server instance with name={name} on port {port}")
        self.line("processing task, is_child = 0")
        self.line("processing task, is_child = 0", port=1449)
        self.request(port=1449, input_tokens=200, prefill=50, output=60)
        self.request()
        self.reader.feed("srv load: spawning server instance with name=third on port 1448")
        self.request()
        self.assertEqual(self.store.summary()["totals"]["requests"], 3)
        self.assertEqual(self.store.summary("second")["totals"]["output_tokens"], 60)
        self.assertEqual(self.store.summary("third")["totals"]["input_tokens"], 120)
        self.assertEqual(self.store.summary("first")["totals"]["input_tokens"], 120)

    def test_weighted_rates_and_partial_coverage_are_not_averages_of_ratios(self):
        self.request()
        self.request(task=1, input_tokens=200, prefill=50, output=60)
        self.request(task=2, input_tokens=None, prefill=100, output=90)
        totals = self.store.summary()["totals"]
        self.assertEqual(totals["cache_samples"], 2)
        self.assertEqual(totals["input_tokens"], 320)
        self.assertAlmostEqual(totals["cache_hit_rate"], 190 / 320)
        self.assertEqual(totals["generation_tps"], 300)

    def test_missing_values_stay_unknown_and_zero_is_valid(self):
        self.reader.feed("private prompt content with prompt eval time = 5 ms / 8 tokens")
        self.request(input_tokens=None)
        totals = self.store.summary()["totals"]
        self.assertIsNone(totals["input_tokens"])
        self.assertIsNone(totals["cached_tokens"])
        self.assertIsNone(totals["cache_hit_rate"])
        self.assertNotIn("private", json.dumps(self.store.summary()))
        self.request(task=1, input_tokens=80, prefill=80, output=0)
        history = self.store.summary()["history"][0]
        self.assertEqual(history["cached_tokens"], 0)
        self.assertEqual(history["output_tokens"], 0)

    def test_filters_time_range_unknown_model_and_invalid_queries(self):
        self.request()
        self.request(task=1)
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE observations SET model='older', started_at=? WHERE task=0",
                                  (time.time() - 172800,))
        self.assertEqual(self.store.summary(period="24h")["totals"]["requests"], 1)
        self.assertEqual(self.store.summary(period="7d")["totals"]["requests"], 2)
        self.assertEqual(self.store.summary("older", "all")["totals"]["requests"], 1)
        self.assertEqual(self.store.summary("", "all")["totals"]["requests"], 1)
        self.assertEqual(self.store.summary("unknown")["totals"]["requests"], 0)
        for period in ("bad", "", "24h' OR 1=1"):
            with self.assertRaises(ManagerError):
                self.store.summary(period=period)
        with self.assertRaises(ManagerError):
            self.store.summary("x" * 257)

    def test_malformed_inconsistent_and_truncated_metrics(self):
        self.line("prompt eval time = nan ms / 80 tokens")
        self.line("draft acceptance = 2.0 (30 accepted / 20 generated)")
        self.line("eval time = 10 ms / 999999999999999999999999 tokens")
        self.request(input_tokens=50, prefill=80)
        self.assertIsNone(self.store.summary()["totals"]["cached_tokens"])
        self.assertGreaterEqual(self.store.summary()["rejected_metrics"], 4)
        self.assertTrue(self.store.summary()["warning"])
        self.request(task=1)
        self.line("stop processing: n_tokens = 150, truncated = 1", task=1)
        self.assertIsNone(self.store.summary()["history"][0]["input_tokens"])

    def test_oversized_numbers_do_not_break_collection(self):
        self.line("eval time = 10 ms / " + "9" * 5000 + " tokens")
        self.line("eval time = " + "9" * 1000 + " ms / 30 tokens")
        self.reader.feed("slot print_timing: id 0 | task " + "9" * 5000 + " | processing task")
        self.request()
        self.assertEqual(self.store.summary()["totals"]["measured"], 1)
        self.assertEqual(self.store.summary()["rejected_metrics"], 3)

    def test_interrupted_tasks_finalize_at_eof_and_restart(self):
        self.line("processing task, is_child = 0")
        self.reader.close()
        self.assertEqual(self.store.summary()["totals"]["interrupted"], 1)
        self.reader = LogObserver(self.store)
        self.line("processing task, is_child = 0")
        self.manager.close()
        reopened = Observability(self.data / "observability.sqlite3")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.summary()["totals"]["interrupted"], 2)

    def test_late_attribution_and_single_server(self):
        self.request()
        self.reader.feed("srv proxy: proxying request to model late on port 1448")
        self.assertEqual(self.store.summary("late")["totals"]["requests"], 1)
        single = LogObserver(self.store, "single-model")
        single.feed("slot launch: id 0 | task 0 | processing task, is_child = 0")
        single.feed("slot release: id 0 | task 0 | stop processing: n_tokens = 0, truncated = 0")
        self.assertEqual(self.store.summary("single-model")["history"][0]["status"], "unmeasured")

    def test_parallel_wall_clock_throughput_and_completed_call_rates(self):
        with patch("observability.time.time", return_value=1200):
            for port in range(1448, 1452):
                self.reader.feed(f"srv load: spawning server instance with name=gemma on port {port}")
                self.request(port=port, output=60)
            self.line("processing task", task=1)
            self.line("stop processing: n_tokens = 0", task=1)
            self.line("processing task", task=2)
            self.reader.close()
        with patch("observability.time.time", return_value=1230):
            data = self.store.summary("gemma", "1h")
            self.assertEqual(data["totals"]["completed"], 5)
            self.assertEqual(data["totals"]["calls_per_minute"], 5)
            self.assertAlmostEqual(data["totals"]["calls_per_second"], 5 / 60)
            self.assertEqual(data["totals"]["aggregate_tps"], 4)
            self.assertEqual(data["models"][0]["aggregate_tps"], 4)
            self.assertEqual(data["totals"]["aggregate_input_tps"], 8)
            self.assertAlmostEqual(data["totals"]["aggregate_prefill_tps"], 320 / 60)
            self.assertEqual(data["models"][0]["aggregate_input_tps"], 8)
            self.assertEqual(data["series"][0]["aggregate_input_tps"], 16)
            self.assertEqual(data["totals"]["generation_tps"], 300)
            self.assertAlmostEqual(data["series"][0]["calls_per_second"], 5 / 30)
        with patch("observability.time.time", return_value=1261):
            totals = self.store.summary("gemma")["totals"]
            self.assertEqual(totals["completed"], 0)
            self.assertEqual(totals["calls_per_second"], 0)
            self.assertIsNone(totals["aggregate_tps"])
            self.assertIsNone(totals["aggregate_input_tps"])
            self.assertIsNone(totals["aggregate_prefill_tps"])


class DirectCaptureTests(RouterFixture, unittest.TestCase):
    def test_direct_client_capture_survives_stop_and_manager_close(self):
        self.setup_router(observability=True)
        for name in ("first", "second"):
            connection = HTTPConnection("127.0.0.1", self.config["port"], timeout=5)
            try:
                connection.request("POST", "/v1/chat/completions", json.dumps({"model": name}),
                                   {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
            finally:
                connection.close()
        wait_for(lambda: self.manager.observability.summary()["totals"]["measured"] == 2)
        self.assertEqual(self.manager.observability.summary("second")["totals"]["cached_tokens"], 40)
        self.assertEqual(self.manager.ledger.summary()["totals"]["requests"], 0)
        process = self.manager._process
        self.manager.close()
        self.assertIsNotNone(process.poll())
        with self.assertRaises(OSError):
            HTTPConnection("127.0.0.1", self.config["port"], timeout=1).connect()
        reopened = Observability(self.data / "observability.sqlite3")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.summary()["totals"]["requests"], 2)

    def test_persistence_failure_is_visible_and_output_reader_keeps_draining(self):
        with patch.object(self.manager.observability, "record", side_effect=sqlite3.OperationalError("disk full")):
            self.setup_router(observability=True)
            connection = HTTPConnection("127.0.0.1", self.config["port"], timeout=5)
            try:
                connection.request("POST", "/v1/chat/completions", json.dumps({"model": "first"}),
                                   {"Content-Type": "application/json"})
                self.assertEqual(connection.getresponse().status, 200)
            finally:
                connection.close()
            wait_for(lambda: "Cannot persist observability" in (self.manager.status()["error"] or ""))
            wait_for(lambda: any("stop processing:" in line for line in self.manager.logs()))

    @unittest.skipUnless(os.name == "nt", "Windows kill-on-close ownership")
    def test_abrupt_manager_exit_kills_router_and_owned_worker(self):
        import ctypes
        from ctypes import wintypes
        self.options(spawn_worker=True)
        source = self.base / "owned-models.ini"
        source.write_text(f"[*]\nctx-size=4096\n[first]\nmodel={self.model}\n")
        marker = self.base / "owned-pids.json"
        script = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_dashboard import FakeManager, wait_for
manager = FakeManager(Path(sys.argv[1]), Path.cwd())
manager.import_catalog(sys.argv[2])
manager.save_workspace({'mode': 'router', 'executable': sys.executable, 'port': int(sys.argv[3])})
manager.start()
wait_for(lambda: manager.status()['state'] == 'running')
line = wait_for(lambda: next((line for line in manager.logs() if 'owned worker PID ' in line), None))
Path(sys.argv[4]).write_text(json.dumps([manager.status()['pid'], int(line.rsplit(' ', 1)[1])]))
time.sleep(600)
"""
        parent = subprocess.Popen([sys.executable, "-c", script, str(self.base / "owned-data"),
                                   str(source), str(self.config["port"]), str(marker)], cwd=ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(parent.communicate, timeout=5)
        self.addCleanup(lambda: parent.kill() if parent.poll() is None else None)
        wait_for(marker.exists)
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handles = [api.OpenProcess(0x100000, False, pid) for pid in json.loads(marker.read_text())]
        try:
            self.assertTrue(all(handles))
            parent.kill()
            parent.wait(timeout=5)
            for handle in handles:
                self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)
        finally:
            for handle in handles:
                if handle:
                    api.CloseHandle(handle)


class ObservabilityRoutes(test_dashboard.RouteTests):
    def test_observability_api_validation_trust_and_errors(self):
        code, _, body = self.request("/api/observability?range=all")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["totals"]["requests"], 0)
        self.assertIsNone(data["totals"]["output_tokens"])
        for query in ("range=bad", "range=1h&range=7d", "unknown=1", "model=" + "x" * 257):
            self.assertEqual(self.request("/api/observability?" + query)[0], 400)
        self.assertEqual(self.request("/api/observability", headers={"Origin": "https://evil.test"})[0], 403)
        with patch.object(self.manager.observability, "summary", side_effect=sqlite3.OperationalError("disk full")):
            code, _, body = self.request("/api/observability")
            self.assertEqual(code, 500)
            self.assertIn("disk full", json.loads(body)["error"])

    def test_dashboard_has_diagnostics_page_with_live_logs_off_by_default(self):
        body = self.request("/")[2]
        self.assertIn(b'id="view-observability"', body)
        self.assertIn(b'id="view-diagnostics"', body)
        self.assertIn(b'href="#diagnostics"', body)
        self.assertIn(b'id="refresh-logs"', body)
        self.assertNotIn(b'id="logs-dialog"', body)
        follow = body.split(b'id="follow-logs"', 1)[1].split(b">", 1)[0]
        self.assertNotIn(b"checked", follow)
        self.assertEqual(self.request("/static/observability.js")[0], 200)


if __name__ == "__main__":
    unittest.main()
