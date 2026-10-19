"""SQLite run state. One database per run name; every batch commits atomically.

Tables
  reviews      every source row, exact field strings, row hash, text hash
  records      final status per review_id: pending | completed | quarantined
  calls        every attempted model call (success or failure) with usage and cost
  invocations  each start/stop of the program against this database (initial vs resume)
  artifacts    cached downstream outputs (verify/group/memo) keyed by input+config hash
  batches      submitted Message Batches: id, which review IDs each request carries, status. An interrupted
               run leaves open batches here; a resume collects their results instead of resubmitting.
"""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews(
  review_id TEXT PRIMARY KEY, row_idx INTEGER NOT NULL, source_sha256 TEXT NOT NULL, text_sha TEXT NOT NULL,
  review_text TEXT NOT NULL, review_rating TEXT, review_likes TEXT, app_version TEXT, review_timestamp TEXT,
  is_empty INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS reviews_text ON reviews(text_sha, row_idx);
CREATE TABLE IF NOT EXISTS records(
  review_id TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  label_config TEXT, topic TEXT, subtopic TEXT, intent TEXT, sentiment REAL, severity INTEGER,
  entities TEXT, evidence_quote TEXT, needs_review INTEGER, cache_source_id TEXT, label_source TEXT,
  detail TEXT, invocation INTEGER, completed_at REAL);
CREATE INDEX IF NOT EXISTS records_status ON records(status);
CREATE TABLE IF NOT EXISTS calls(
  call_id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, role TEXT, phase TEXT, invocation INTEGER,
  provider TEXT, model TEXT, model_resolved TEXT, settings TEXT, prompt_version TEXT, label_config TEXT,
  review_ids TEXT, outcome TEXT, error TEXT, attempt INTEGER, input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER, cost_usd REAL, started_at REAL, latency_ms INTEGER,
  batch_key TEXT);
CREATE TABLE IF NOT EXISTS invocations(
  invocation INTEGER PRIMARY KEY, phase TEXT, started_at REAL, ended_at REAL, status TEXT, argv TEXT,
  completed_before INTEGER, completed_after INTEGER);
CREATE TABLE IF NOT EXISTS artifacts(key TEXT PRIMARY KEY, kind TEXT, created_at REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS batches(
  batch_id TEXT PRIMARY KEY, invocation INTEGER, submitted_at REAL, ended_at REAL, status TEXT NOT NULL,
  request_count INTEGER, requests TEXT, reserved_usd REAL, label_config TEXT, retry_round INTEGER DEFAULT 0);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)  # explicit transactions
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # ---- meta ----
    def get_meta(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v)))

    # ---- invocations ----
    def start_invocation(self, argv):
        completed = self.count("completed")
        prior = self.db.execute("SELECT COUNT(*) FROM invocations WHERE phase IS NOT NULL").fetchone()[0]
        # 'resume' = an earlier invocation already saved completed work in this database
        phase = "resume" if (prior and completed) else "initial"
        cur = self.db.execute("INSERT INTO invocations(phase, started_at, status, argv, completed_before) VALUES(?,?,?,?,?)",
                              (phase, time.time(), "running", json.dumps(argv), completed))
        return cur.lastrowid, phase

    def end_invocation(self, inv, status):
        self.db.execute("UPDATE invocations SET ended_at=?, status=?, completed_after=? WHERE invocation=?",
                        (time.time(), status, self.count("completed"), inv))

    def count(self, status=None):
        if status:
            return self.db.execute("SELECT COUNT(*) FROM records WHERE status=?", (status,)).fetchone()[0]
        return self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def completed_ids(self):
        return [r[0] for r in self.db.execute("SELECT review_id FROM records WHERE status='completed' ORDER BY review_id")]

    # ---- calls ----
    def log_call(self, **c):
        c.setdefault("review_ids", [])
        c["review_ids"] = json.dumps(c["review_ids"])
        if isinstance(c.get("settings"), dict):
            c["settings"] = json.dumps(c["settings"], sort_keys=True)
        cols = ",".join(c)
        self.db.execute(f"INSERT INTO calls({cols}) VALUES({','.join('?' * len(c))})", tuple(c.values()))

    def spent_usd(self):
        return self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM calls").fetchone()[0]

    # ---- artifacts (downstream result cache) ----
    def get_artifact(self, key):
        r = self.db.execute("SELECT payload FROM artifacts WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def put_artifact(self, key, kind, payload):
        self.db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?)", (key, kind, time.time(), json.dumps(payload)))
