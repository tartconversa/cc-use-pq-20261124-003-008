from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS event(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT NOT NULL,entity_id TEXT NOT NULL,
 revision INTEGER NOT NULL,source_ref TEXT NOT NULL,digest TEXT NOT NULL,payload TEXT NOT NULL,
 observed_at TEXT NOT NULL,UNIQUE(event_type,entity_id,revision,source_ref)
);
CREATE INDEX IF NOT EXISTS event_asof ON event(seq,event_type,entity_id);
CREATE TABLE IF NOT EXISTS analysis(
 analysis_id TEXT NOT NULL,revision INTEGER NOT NULL,cut INTEGER NOT NULL,as_of TEXT NOT NULL,
 policy TEXT NOT NULL,document TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,
 created_at TEXT NOT NULL,PRIMARY KEY(analysis_id,revision)
);
CREATE TABLE IF NOT EXISTS decisions(
 decision_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,role TEXT NOT NULL,reviewer TEXT NOT NULL,
 finding TEXT NOT NULL,decided_at TEXT NOT NULL,rationale TEXT NOT NULL,evidence_refs TEXT NOT NULL,digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remediation(
 action_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,provider_id TEXT NOT NULL,owner TEXT NOT NULL,
 due_at TEXT NOT NULL,action TEXT NOT NULL,evidence_ref TEXT,verified_by TEXT,verified_at TEXT,
 status TEXT NOT NULL,digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals(
 approval_id TEXT PRIMARY KEY,analysis_id TEXT NOT NULL,reviewer TEXT NOT NULL,role TEXT NOT NULL,
 approved INTEGER NOT NULL,analysis_digest TEXT NOT NULL,rationale TEXT NOT NULL,digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bulletin(
 analysis_id TEXT NOT NULL,revision INTEGER NOT NULL,analysis_digest TEXT NOT NULL,
 result TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(analysis_id,revision)
);
PRAGMA user_version=1;
"""

class Store:
    def __init__(self, location: str | Path):
        self.location = Path(location)
        self.location.parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        finally:
            db.close()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.location, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self, write=False) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
