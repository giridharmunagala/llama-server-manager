"""Persistent metadata-only inference and process accounting."""

from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3
import threading
import time

from manager import ManagerError


DEFAULT_COSTS = {"currency": "INR", "electricity_per_kwh": 0.0, "system_watts": 0.0,
                 "hardware_cost": 0.0, "hardware_lifetime_hours": 10000.0,
                 "cloud_input_per_million": 0.0, "cloud_output_per_million": 0.0}


class Ledger:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT,
                mode TEXT, seconds REAL DEFAULT 0, outcome TEXT,
                energy_kwh REAL DEFAULT 0, local_cost REAL DEFAULT 0,
                unpriced_seconds REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY, session_id INTEGER, started_at TEXT, model TEXT,
                status TEXT, http_status INTEGER, seconds REAL DEFAULT 0,
                prompt_tokens INTEGER, completion_tokens INTEGER, error TEXT,
                config_revision TEXT);
            CREATE TABLE IF NOT EXISTS configurations (revision TEXT PRIMARY KEY, snapshot TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        if "config_revision" not in {row["name"] for row in self.db.execute("PRAGMA table_info(requests)")}:
            self.db.execute("ALTER TABLE requests ADD COLUMN config_revision TEXT")
        if "unpriced_seconds" not in {row["name"] for row in self.db.execute("PRAGMA table_info(sessions)")}:
            self.db.execute("ALTER TABLE sessions ADD COLUMN unpriced_seconds REAL DEFAULT 0")
            self.db.execute("UPDATE sessions SET unpriced_seconds=seconds WHERE local_cost=0")
        with self.db:
            self.db.execute("UPDATE sessions SET outcome='interrupted', ended_at=? "
                            "WHERE ended_at IS NULL", (self.now(),))
            self.db.execute("UPDATE requests SET status='interrupted', error=? "
                            "WHERE status='in_progress'", ("Dashboard stopped before completion.",))
        self.session_id = None
        self.last_tick = None

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def costs(self):
        with self.lock:
            rows = self.db.execute("SELECT key, value FROM settings").fetchall()
            return {**DEFAULT_COSTS, **{row["key"]: (row["value"] if row["key"] == "currency"
                                                    else float(row["value"])) for row in rows}}

    def save_costs(self, value):
        if not isinstance(value, dict) or set(value) != set(DEFAULT_COSTS):
            raise ManagerError("Cost settings must contain all documented fields.")
        if (not isinstance(value["currency"], str) or not value["currency"].isascii()
                or not value["currency"].isalpha() or len(value["currency"]) != 3):
            raise ManagerError("Currency must be a three-letter code.")
        for key in set(DEFAULT_COSTS) - {"currency"}:
            item = value[key]
            if type(item) not in (int, float) or item < 0 or item > 1e12 or not math.isfinite(item):
                raise ManagerError(f"{key} must be a finite number between 0 and 1e12.")
        if value["hardware_lifetime_hours"] <= 0:
            raise ManagerError("Hardware lifetime hours must be greater than zero.")
        with self.lock, self.db:
            old = self.costs()
            if value["currency"] != old["currency"]:
                used = self.db.execute("SELECT count(*) FROM sessions").fetchone()[0]
                if used:
                    raise ManagerError("Currency cannot change after recording sessions; use a new data directory.")
            self.tick()
            self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                                [(key, str(item)) for key, item in value.items()])
        return self.costs()

    def start_session(self, mode):
        with self.lock, self.db:
            self.session_id = self.db.execute(
                "INSERT INTO sessions (started_at, mode, outcome) VALUES (?,?,'running')",
                (self.now(), mode)).lastrowid
            self.last_tick = time.monotonic()

    def tick(self):
        with self.lock:
            if self.session_id is None:
                return
            current = time.monotonic()
            seconds = current - self.last_tick
            costs = self.costs()
            energy = costs["system_watts"] * seconds / 3600000
            local = energy * costs["electricity_per_kwh"] + (
                costs["hardware_cost"] / costs["hardware_lifetime_hours"] * seconds / 3600)
            unpriced = seconds if costs["system_watts"] <= 0 or costs["electricity_per_kwh"] <= 0 else 0
            with self.db:
                self.db.execute("UPDATE sessions SET seconds=seconds+?, energy_kwh=energy_kwh+?, "
                                "local_cost=local_cost+?, unpriced_seconds=unpriced_seconds+? WHERE id=?",
                                (seconds, energy, local, unpriced, self.session_id))
            self.last_tick = current

    def end_session(self, outcome):
        with self.lock:
            try:
                self.tick()
                with self.db:
                    self.db.execute("UPDATE sessions SET ended_at=?, outcome=? WHERE id=?",
                                    (self.now(), outcome, self.session_id))
            finally:
                self.session_id = None
                self.last_tick = None

    def begin_request(self, model, configuration=None):
        with self.lock, self.db:
            revision = None
            if configuration is not None:
                snapshot = json.dumps(configuration, sort_keys=True)
                revision = hashlib.sha256(snapshot.encode()).hexdigest()
                self.db.execute("INSERT OR IGNORE INTO configurations VALUES (?,?)", (revision, snapshot))
            return self.db.execute(
                "INSERT INTO requests (session_id, started_at, model, status, config_revision) "
                "VALUES (?,?,?,'in_progress',?)",
                (self.session_id, self.now(), model, revision)).lastrowid

    def finish_request(self, request_id, status, http_status, seconds, usage, error):
        prompt = completion = None
        if isinstance(usage, dict):
            values = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
            if all(type(item) is int and 0 <= item <= 9223372036854775807 for item in values):
                prompt, completion = values
        with self.lock, self.db:
            self.db.execute("UPDATE requests SET status=?, http_status=?, seconds=?, "
                            "prompt_tokens=?, completion_tokens=?, error=? WHERE id=?",
                            (status, http_status, seconds, prompt, completion, error, request_id))

    def summary(self):
        with self.lock:
            self.tick()
            costs = self.costs()
            totals = dict(self.db.execute(
                "SELECT count(*) AS sessions, coalesce(sum(seconds),0) AS runtime_seconds, "
                "coalesce(sum(energy_kwh),0) AS energy_kwh, coalesce(sum(local_cost),0) AS local_cost, "
                "coalesce(sum(unpriced_seconds),0) AS unpriced_seconds "
                "FROM sessions").fetchone())
            aggregate = """count(*) AS requests,
                coalesce(sum(status NOT IN ('complete','in_progress')),0) AS failed,
                coalesce(sum(status='in_progress'),0) AS in_progress,
                coalesce(sum(status!='in_progress' AND prompt_tokens IS NULL),0) AS missing_usage,
                coalesce(sum(prompt_tokens),0) AS prompt_tokens,
                coalesce(sum(completion_tokens),0) AS completion_tokens,
                coalesce(sum(seconds),0) AS request_seconds"""
            totals.update(dict(self.db.execute(f"SELECT {aggregate} FROM requests").fetchone()))
            models = [dict(row) for row in self.db.execute(
                f"SELECT model, {aggregate} FROM requests GROUP BY model ORDER BY model")]
            for row in [totals, *models]:
                row["cloud_cost"] = (row["prompt_tokens"] * costs["cloud_input_per_million"]
                                     + row["completion_tokens"] * costs["cloud_output_per_million"]) / 1000000
                row["output_tokens_per_second"] = (
                    row["completion_tokens"] / row["request_seconds"] if row["request_seconds"] else None)
            totals["estimated_savings"] = totals["cloud_cost"] - totals["local_cost"]
            return {"totals": totals, "models": models, "costs": costs,
                    "history": [dict(row) for row in self.db.execute(
                        "SELECT * FROM requests ORDER BY id DESC LIMIT 100")],
                    "sessions": [dict(row) for row in self.db.execute(
                        "SELECT * FROM sessions ORDER BY id DESC LIMIT 30")]}

    def close(self):
        with self.lock:
            self.db.close()
