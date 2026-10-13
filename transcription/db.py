from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    current_step TEXT NOT NULL DEFAULT '', error TEXT,
                    files_json TEXT NOT NULL DEFAULT '[]', cancel_requested INTEGER NOT NULL DEFAULT 0,
                    config_json TEXT NOT NULL DEFAULT '{}'
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "name" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN name TEXT NOT NULL DEFAULT ''")
            if "brief_description" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN brief_description TEXT NOT NULL DEFAULT ''")

    def create(self, job_id: str, name: str, kind: str, files: list[str], config: dict, brief_description: str = "") -> dict:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs(id,name,kind,status,created_at,files_json,config_json,brief_description) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, name, kind, "queued", utcnow(), json.dumps(files), json.dumps(config), brief_description),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode(row) if row else None

    def list(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._decode(row) for row in rows]

    def update(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status", "started_at", "finished_at", "current_step", "error",
            "cancel_requested", "files_json", "config_json", "brief_description",
        }
        fields = {key: value for key, value in fields.items() if key in allowed}
        if not fields:
            return
        sql = ", ".join(f"{key}=?" for key in fields)
        with self._lock:
            with self._connect() as conn:
                conn.execute(f"UPDATE jobs SET {sql} WHERE id=?", (*fields.values(), job_id))

    def request_cancel(self, job_id: str) -> None:
        self.update(job_id, status="stopping", cancel_requested=1, current_step="Stopping…")

    def delete(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def mark_abandoned_jobs(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', finished_at=?, error='Application restarted while job was running' "
                "WHERE status IN ('queued','running','stopping')", (utcnow(),)
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["files"] = json.loads(item.pop("files_json"))
        item["config"] = json.loads(item.pop("config_json"))
        item["cancel_requested"] = bool(item["cancel_requested"])
        item["name"] = item.get("name", "").strip() or "Untitled job"
        return item
