"""Durable metadata from managed llama.cpp worker logs, without an inference proxy."""

import math
import re
import sqlite3
import threading
import time
import uuid

from manager import ManagerError


RANGES = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000, "all": None}
FIELDS = ("input_tokens", "prefill_tokens", "output_tokens", "cached_tokens",
          "draft_tokens", "accepted_tokens", "prefill_ms", "generation_ms", "total_ms")
AGGREGATE = """
    count(*) AS requests,
    coalesce(sum(status='in_progress'),0) AS in_progress,
    coalesce(sum(status='measured'),0) AS measured,
    coalesce(sum(status='interrupted'),0) AS interrupted,
    coalesce(sum(status='unmeasured'),0) AS unmeasured,
    count(input_tokens) AS input_samples, sum(input_tokens) AS input_tokens,
    count(prefill_tokens) AS prefill_samples, sum(prefill_tokens) AS prefill_tokens,
    count(output_tokens) AS output_samples, sum(output_tokens) AS output_tokens,
    count(cached_tokens) AS cache_samples, sum(cached_tokens) AS cached_tokens,
    sum(CASE WHEN cached_tokens IS NOT NULL THEN input_tokens END) AS cache_input_tokens,
    count(draft_tokens) AS draft_samples, sum(draft_tokens) AS draft_tokens,
    sum(accepted_tokens) AS accepted_tokens,
    avg(prefill_ms) AS prefill_ms, avg(total_ms) AS total_ms,
    sum(CASE WHEN prefill_ms>0 THEN prefill_tokens END) AS timed_prefill_tokens,
    sum(CASE WHEN prefill_ms>0 THEN prefill_ms END) AS timed_prefill_ms,
    sum(CASE WHEN generation_ms>0 THEN output_tokens END) AS timed_output_tokens,
    sum(CASE WHEN generation_ms>0 THEN generation_ms END) AS timed_generation_ms
"""


def rates(row):
    def ratio(numerator, denominator, scale=1):
        return row[numerator] * scale / row[denominator] if row[denominator] else None
    row["cache_hit_rate"] = ratio("cached_tokens", "cache_input_tokens")
    row["draft_acceptance"] = ratio("accepted_tokens", "draft_tokens")
    row["prefill_tps"] = ratio("timed_prefill_tokens", "timed_prefill_ms", 1000)
    row["generation_tps"] = ratio("timed_output_tokens", "timed_generation_ms", 1000)
    row["ttft_ms"] = None
    return row


class Observability:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, run TEXT NOT NULL, worker TEXT NOT NULL,
                slot INTEGER NOT NULL, task INTEGER NOT NULL, model TEXT,
                started_at REAL NOT NULL, ended_at REAL, status TEXT NOT NULL,
                input_tokens INTEGER, prefill_tokens INTEGER, output_tokens INTEGER,
                cached_tokens INTEGER, draft_tokens INTEGER, accepted_tokens INTEGER,
                prefill_ms REAL, generation_ms REAL, total_ms REAL,
                UNIQUE(run, worker, slot, task));
            CREATE INDEX IF NOT EXISTS observation_time ON observations(started_at);
            CREATE INDEX IF NOT EXISTS observation_model_time ON observations(model, started_at);
        """)
        with self.db:
            self.db.execute("UPDATE observations SET status='interrupted', ended_at=? "
                            "WHERE status='in_progress'", (time.time(),))
        self.warning = None
        self.rejected = 0

    def reject(self):
        with self.lock:
            self.rejected += 1
            self.warning = "A malformed or inconsistent metric was ignored. Inspect diagnostics for build compatibility."

    def record(self, run, worker, slot, task, model, values):
        for key, value in values.items():
            if key in FIELDS and value is not None and (
                    not 0 <= value <= 9223372036854775807 or not math.isfinite(value)):
                self.reject()
                return
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO observations (run,worker,slot,task,model,started_at,status) "
                "VALUES (?,?,?,?,?,?,'in_progress')", (run, worker, slot, task, model, time.time()))
            assignments = ",".join(f"{key}=?" for key in values)
            if assignments:
                self.db.execute(f"UPDATE observations SET {assignments} "
                                "WHERE run=? AND worker=? AND slot=? AND task=?",
                                (*values.values(), run, worker, slot, task))
            row = self.db.execute(
                "SELECT input_tokens,prefill_tokens FROM observations "
                "WHERE run=? AND worker=? AND slot=? AND task=?", (run, worker, slot, task)).fetchone()
            if row["input_tokens"] is not None and row["prefill_tokens"] is not None:
                cached = row["input_tokens"] - row["prefill_tokens"]
                if cached < 0:
                    self.reject()
                    cached = None
                    self.db.execute("UPDATE observations SET input_tokens=NULL "
                                    "WHERE run=? AND worker=? AND slot=? AND task=?", (run, worker, slot, task))
                self.db.execute("UPDATE observations SET cached_tokens=? "
                                "WHERE run=? AND worker=? AND slot=? AND task=?",
                                (cached, run, worker, slot, task))

    def attribute(self, run, worker, model):
        with self.lock, self.db:
            self.db.execute("UPDATE observations SET model=? WHERE run=? AND worker=? AND model IS NULL",
                            (model, run, worker))

    def release(self, run, worker, slot, task, truncated=False):
        with self.lock, self.db:
            if truncated:
                self.db.execute("UPDATE observations SET input_tokens=NULL,cached_tokens=NULL "
                                "WHERE run=? AND worker=? AND slot=? AND task=?", (run, worker, slot, task))
            self.db.execute(
                "UPDATE observations SET ended_at=?, status=CASE WHEN output_tokens IS NOT NULL "
                "AND prefill_tokens IS NOT NULL THEN 'measured' ELSE 'unmeasured' END "
                "WHERE run=? AND worker=? AND slot=? AND task=? AND status='in_progress'",
                (time.time(), run, worker, slot, task))

    def end_run(self, run, worker=None):
        with self.lock, self.db:
            clause = " AND worker=?" if worker is not None else ""
            self.db.execute("UPDATE observations SET status='interrupted', ended_at=? "
                            "WHERE run=? AND status='in_progress'" + clause,
                            (time.time(), run, *([worker] if worker is not None else [])))

    def summary(self, model=None, period="24h"):
        if period not in RANGES:
            raise ManagerError("Time range must be 1h, 24h, 7d, 30d or all.")
        if model is not None and (not isinstance(model, str) or len(model) > 256 or "\0" in model):
            raise ManagerError("Model filter must be at most 256 characters without NUL.")
        now = time.time()
        since = now - RANGES[period] if RANGES[period] else 0
        conditions, params = ["started_at>=?"], [since]
        if model is not None:
            conditions.append("model IS NULL" if model == "" else "model=?")
            if model:
                params.append(model)
        where = " WHERE " + " AND ".join(conditions)
        with self.lock:
            totals = rates(dict(self.db.execute(
                f"SELECT {AGGREGATE} FROM observations{where}", params).fetchone()))
            models = [rates(dict(row)) for row in self.db.execute(
                f"SELECT model,{AGGREGATE} FROM observations{where} GROUP BY model ORDER BY model", params)]
            earliest = self.db.execute(
                "SELECT min(started_at) FROM observations" + where, params).fetchone()[0]
            span = RANGES[period] or max(3600, now - (earliest or now))
            bucket = max(60, math.ceil(span / 90 / 60) * 60)
            series = [rates(dict(row)) for row in self.db.execute(
                f"SELECT CAST(started_at / ? AS INTEGER)*? AS timestamp,{AGGREGATE} "
                f"FROM observations{where} GROUP BY timestamp ORDER BY timestamp", [bucket, bucket, *params])]
            history = [dict(row) for row in self.db.execute(
                "SELECT * FROM observations" + where + " ORDER BY started_at DESC,id DESC LIMIT 100", params)]
            choices = [row[0] for row in self.db.execute(
                "SELECT DISTINCT model FROM observations ORDER BY model")]
            return {"totals": totals, "models": models, "series": series, "history": history,
                    "model_choices": choices, "bucket_seconds": bucket, "range": period,
                    "source": "llama.cpp worker logs", "warning": self.warning, "rejected_metrics": self.rejected,
                    "limitations": [
                        "Counts represent observed slot tasks, not every HTTP request. Multi-completion requests may create multiple tasks; failures before slot assignment are not visible.",
                        "Input/cache counts require prompt-length log lines. Missing metrics are unknown, not zero. Cache hit rate is cached input tokens / known input tokens.",
                        "Client time to first token is unavailable without client/proxy instrumentation. Prefill time excludes queuing, model loading and network delay.",
                        "Timing completion is not proof of HTTP success. Only activity logged while this manager owns the server is captured.",
                    ]}

    def close(self):
        with self.lock:
            self.db.close()


class LogObserver:
    ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    PORT = re.compile(r"\[\s*(\d{1,5})\s*\]")
    MODEL = re.compile(r"(?:spawning server instance with name=(.+)|proxying request to model (.+)) on port (\d{1,5})\s*$")
    SLOT = re.compile(r"\bslot\s+\S+:\s+id\s+(\d+)\s*\|\s*task\s+(-?\d+)\s*\|\s*(.*)$")
    TIMING = re.compile(r"^(prompt eval|eval|total) time\s*=\s*(\d+(?:\.\d+)?) ms\s*/\s*(\d+) tokens")
    DRAFT = re.compile(r"^draft acceptance\s*=\s*[\d.]+\s*\(\s*(\d+) accepted\s*/\s*(\d+) generated\)")
    PROMPT = re.compile(r"^(?:new prompt,.*?task\.n_tokens\s*=\s*(\d+)|prompt done,.*?n_tokens\s*=\s*(\d+))")

    def __init__(self, store, model=None):
        self.store = store
        self.model = model
        self.run = uuid.uuid4().hex
        self.workers = {}
        self.names = {}
        self.serial = 0

    def feed(self, line):
        line = self.ANSI.sub("", line)
        mapping = self.MODEL.search(line)
        if mapping:
            name = mapping[1] or mapping[2]
            port = mapping[3]
            if len(name) > 256 or not 1 <= int(port) <= 65535:
                self.store.reject()
                return
            if mapping[1] and port in self.workers:
                self.store.end_run(self.run, self.workers[port])
                del self.workers[port]
            worker = self.worker(port)
            self.names[port] = name
            self.store.attribute(self.run, worker, name)
            return
        match = self.SLOT.search(line)
        if not match:
            return
        if any(len(match[index]) > 10 for index in (1, 2)):
            self.store.reject()
            return
        if int(match[2]) < 0:
            return
        if any(int(match[index]) > 2147483647 for index in (1, 2)):
            self.store.reject()
            return
        port_match = self.PORT.search(line[:match.start()])
        port = port_match[1] if port_match else "single"
        worker = self.worker(port)
        model = self.model if port == "single" else self.names.get(port)
        slot, task, message = int(match[1]), int(match[2]), match[3].strip()
        values = {}
        timing, draft, prompt = self.TIMING.match(message), self.DRAFT.match(message), self.PROMPT.match(message)
        counts = ([timing[3]] if timing else [draft[1], draft[2]] if draft else
                  [prompt[1] or prompt[2]] if prompt else [])
        if any(len(count) > 19 for count in counts):
            self.store.reject()
            return
        if timing:
            kind, ms, tokens = timing[1], float(timing[2]), int(timing[3])
            if kind == "prompt eval":
                values = {"prefill_ms": ms, "prefill_tokens": tokens}
            elif kind == "eval":
                values = {"generation_ms": ms, "output_tokens": tokens}
            else:
                values = {"total_ms": ms}
        elif draft:
            accepted, generated = int(draft[1]), int(draft[2])
            if accepted > generated:
                self.store.reject()
                return
            values = {"accepted_tokens": accepted, "draft_tokens": generated}
        elif prompt:
            values = {"input_tokens": int(prompt[1] or prompt[2])}
        elif message.startswith("stop processing:"):
            self.store.release(self.run, worker, slot, task, bool(re.search(r"\btruncated\s*=\s*1\b", message)))
            return
        elif not message.startswith("processing task"):
            if re.match(r"^(?:prompt eval time|eval time|total time|draft acceptance)\s*=", message) or message.startswith(
                    ("new prompt", "prompt done")):
                self.store.reject()
            return
        self.store.record(self.run, worker, slot, task, model, values)

    def worker(self, port):
        if port not in self.workers:
            self.serial += 1
            self.workers[port] = f"{port}:{self.serial}"
        return self.workers[port]

    def close(self):
        self.store.end_run(self.run)
