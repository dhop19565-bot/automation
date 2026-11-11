"""SQLite persistence: execution records, the audit log and the month-end checklist.

Two properties are enforced by the database itself rather than by application
code, so they hold even if a caller forgets to check:

* duplicate prevention - partial unique indexes on ``runs``;
* audit immutability - triggers reject UPDATE and DELETE on ``audit_log``, and
  each row is chained to the previous one by hash.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

# A run in one of these states blocks an identical submission.
LIVE_STATUSES = ("queued", "running", "validating", "awaiting_approval",
                 "approved", "posting", "posting_failed", "posted")
# At most one run per workflow/entity/period may be in one of these states.
COMMITTED_STATUSES = ("approved", "posting", "posting_failed", "posted")
DEAD_STATUSES = ("failed", "validation_failed", "rejected")


def _sql_list(values: Iterable[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    id                TEXT PRIMARY KEY,
    workflow          TEXT NOT NULL,
    provider          TEXT NOT NULL,
    execution_mode    TEXT NOT NULL,
    status            TEXT NOT NULL,
    entity            TEXT NOT NULL,
    period            TEXT NOT NULL,
    dedupe_key        TEXT NOT NULL,
    input_hash        TEXT NOT NULL,
    requested_by      TEXT NOT NULL,
    options_json      TEXT NOT NULL DEFAULT '{{}}',
    input_json        TEXT NOT NULL,
    proposal_json     TEXT,
    proposal_hash     TEXT,
    validation_json   TEXT,
    validation_passed INTEGER,
    provider_meta_json TEXT,
    posting_json      TEXT,
    error_code        TEXT,
    error_message     TEXT,
    approved_by       TEXT,
    approved_at       TEXT,
    approval_comment  TEXT,
    rejected_by       TEXT,
    rejected_at       TEXT,
    rejection_reason  TEXT,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    completed_at      TEXT,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_live_duplicate
    ON runs (dedupe_key) WHERE status IN ({_sql_list(LIVE_STATUSES)});
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_one_committed_per_period
    ON runs (workflow, entity, period) WHERE status IN ({_sql_list(COMMITTED_STATUSES)});
CREATE INDEX IF NOT EXISTS ix_runs_created ON runs (created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_run ON audit_log (run_id, seq);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TABLE IF NOT EXISTS checklist_items (
    entity      TEXT NOT NULL,
    period      TEXT NOT NULL,
    task_key    TEXT NOT NULL,
    sort_order  INTEGER NOT NULL,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    workflow    TEXT,
    status      TEXT NOT NULL,
    run_id      TEXT,
    note        TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (entity, period, task_key)
);

CREATE TABLE IF NOT EXISTS sim_d365_batches (
    batch_number TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL UNIQUE,
    workflow     TEXT NOT NULL,
    entity       TEXT NOT NULL,
    period       TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    posted_at    TEXT NOT NULL,
    UNIQUE (workflow, entity, period)
);
"""

GENESIS_HASH = "0" * 64
_JSON_COLUMNS = {
    "options_json": "options", "input_json": "input", "proposal_json": "proposal",
    "validation_json": "validation", "provider_meta_json": "provider_meta", "posting_json": "posting",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: Any) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _audit_hash(prev_hash: str, run_id: str | None, ts: str, actor: str, event: str, detail_json: str) -> str:
    return sha256_hex(canonical_json([prev_hash, run_id, ts, actor, event, detail_json]))


class Database:
    """One connection, serialised by a lock. Every call is a short transaction."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ runs

    def insert_run(self, row: dict[str, Any]) -> None:
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            self._conn.execute(f"INSERT INTO runs ({columns}) VALUES ({marks})", list(row.values()))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _decode_run(row) if row else None

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [_decode_run(r) for r in rows]

    def runs_in_status(self, statuses: Iterable[str]) -> list[dict[str, Any]]:
        statuses = list(statuses)
        marks = ", ".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM runs WHERE status IN ({marks}) ORDER BY created_at", statuses).fetchall()
        return [_decode_run(r) for r in rows]

    def runs_for_scope(self, workflow: str, entity: str, period: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE workflow = ? AND entity = ? AND period = ? "
                "ORDER BY created_at DESC, id DESC", (workflow, entity, period)).fetchall()
        return [_decode_run(r) for r in rows]

    def find_live_duplicate(self, dedupe_key: str) -> dict[str, Any] | None:
        marks = ", ".join("?" for _ in LIVE_STATUSES)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM runs WHERE dedupe_key = ? AND status IN ({marks})",
                (dedupe_key, *LIVE_STATUSES)).fetchone()
        return _decode_run(row) if row else None

    def transition(self, run_id: str, from_statuses: Iterable[str], to_status: str, now: str,
                   **fields: Any) -> bool:
        """Atomic compare-and-swap on status. Returns False if the run was not
        in one of ``from_statuses`` - which is how concurrent approvals lose."""
        from_statuses = list(from_statuses)
        fields = {**fields, "status": to_status, "updated_at": now}
        assignments = ", ".join(f"{name} = ?" for name in fields)
        marks = ", ".join("?" for _ in from_statuses)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE runs SET {assignments} WHERE id = ? AND status IN ({marks})",
                [*fields.values(), run_id, *from_statuses])
            return cursor.rowcount == 1

    # ------------------------------------------------------------------ audit

    def append_audit(self, *, run_id: str | None, ts: str, actor: str, event: str,
                     detail: dict[str, Any] | None = None) -> None:
        detail_json = canonical_json(detail or {})
        with self._lock:
            last = self._conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = last["hash"] if last else GENESIS_HASH
            self._conn.execute(
                "INSERT INTO audit_log (run_id, ts, actor, event, detail_json, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, ts, actor, event, detail_json, prev_hash,
                 _audit_hash(prev_hash, run_id, ts, actor, event, detail_json)))

    def audit_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        return [_decode_audit(r) for r in rows]

    def recent_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [_decode_audit(r) for r in rows]

    def verify_audit_chain(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
        prev_hash = GENESIS_HASH
        for row in rows:
            expected = _audit_hash(prev_hash, row["run_id"], row["ts"], row["actor"], row["event"], row["detail_json"])
            if row["prev_hash"] != prev_hash or row["hash"] != expected:
                return {"intact": False, "entries": len(rows), "first_broken_seq": row["seq"]}
            prev_hash = row["hash"]
        return {"intact": True, "entries": len(rows), "first_broken_seq": None}

    # ------------------------------------------------------------------ checklist

    def seed_checklist(self, entity: str, period: str, tasks: list[dict[str, Any]], now: str) -> None:
        with self._lock:
            for order, task in enumerate(tasks):
                self._conn.execute(
                    "INSERT OR IGNORE INTO checklist_items "
                    "(entity, period, task_key, sort_order, title, kind, workflow, status, note, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'not_started', ?, ?)",
                    (entity, period, task["task_key"], order, task["title"], task["kind"],
                     task.get("workflow"), task.get("note"), now))

    def get_checklist(self, entity: str, period: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM checklist_items WHERE entity = ? AND period = ? ORDER BY sort_order",
                (entity, period)).fetchall()
        return [dict(r) for r in rows]

    def checklist_scopes(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT entity, period FROM checklist_items ORDER BY period DESC, entity").fetchall()
        return [dict(r) for r in rows]

    def set_checklist_item(self, entity: str, period: str, task_key: str, *, status: str,
                           run_id: str | None, note: str | None, now: str) -> bool:
        """Returns True if anything changed."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE checklist_items SET status = ?, run_id = ?, note = ?, updated_at = ? "
                "WHERE entity = ? AND period = ? AND task_key = ? "
                "AND (status IS NOT ? OR run_id IS NOT ? OR note IS NOT ?)",
                (status, run_id, note, now, entity, period, task_key, status, run_id, note))
            return cursor.rowcount == 1

    # ------------------------------------------------------------------ simulated ledger

    def get_sim_batch_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sim_d365_batches WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def count_sim_batches(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM sim_d365_batches").fetchone()[0]

    def insert_sim_batch(self, *, batch_number: str, run_id: str, workflow: str, entity: str, period: str,
                         result: dict[str, Any], posted_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sim_d365_batches (batch_number, run_id, workflow, entity, period, result_json, posted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_number, run_id, workflow, entity, period, canonical_json(result), posted_at))


def _decode_run(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in _JSON_COLUMNS:
            out[_JSON_COLUMNS[key]] = json.loads(value) if value is not None else None
        elif key == "validation_passed":
            out[key] = None if value is None else bool(value)
        else:
            out[key] = value
    return out


def _decode_audit(row: sqlite3.Row) -> dict[str, Any]:
    return {"seq": row["seq"], "run_id": row["run_id"], "ts": row["ts"], "actor": row["actor"],
            "event": row["event"], "detail": json.loads(row["detail_json"]), "hash": row["hash"]}
