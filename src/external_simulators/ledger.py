"""SQLite commits an effect and its indefinite idempotency ledger together."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from workflow_engine.domain import canonical_hash, canonical_json


class KeyConflict(Exception):
    pass


class Ledger:
    def __init__(self, path: str | Path, initial_stock: dict[str, int] | None = None):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, outcome TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effects (
                    id TEXT PRIMARY KEY, logical_action_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stock (part_id TEXT PRIMARY KEY, quantity INTEGER NOT NULL);
            """)
            for part, quantity in (initial_stock or {}).items():
                db.execute("INSERT OR IGNORE INTO stock VALUES (?, ?)", (part, quantity))
            db.commit()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    def apply(self, key: str, payload: dict, operation) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_hash, outcome FROM operations WHERE key=?", (key,)).fetchone()
            digest = canonical_hash(payload)
            if row:
                if row[0] != digest:
                    raise KeyConflict("Key already used with another payload")
                return json.loads(row[1])
            outcome = operation(db)
            db.execute("INSERT INTO operations VALUES (?, ?, ?)", (key, digest, canonical_json(outcome)))
            db.commit()
            return outcome

    def effect(self, db, kind: str, payload: dict) -> dict:
        if kind == "reservation":
            result = db.execute("UPDATE stock SET quantity=quantity-? WHERE part_id=? AND quantity>=?", (payload["quantity"], payload["part_id"], payload["quantity"]))
            if result.rowcount == 0:
                return {"status": "SKIPPED", "result": {"reason": "stock_unavailable"}}
        external_id = uuid4().hex
        db.execute("INSERT INTO effects VALUES (?, ?, ?, ?)", (external_id, payload["logical_action_id"], kind, canonical_json(payload)))
        return {"status": "CONFIRMED", "result": {"id": external_id}}

    def lookup(self, key: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT outcome FROM operations WHERE key=?", (key,)).fetchone()
            return {"status": "CONFIRMED", "outcome": json.loads(row[0])} if row else {"status": "NOT_FOUND"}

    def effect_counts(self) -> dict[str, int]:
        with self.connect() as db:
            return dict(db.execute("SELECT logical_action_id, COUNT(*) FROM effects GROUP BY logical_action_id").fetchall())

    def stock(self, part_id: str) -> int:
        with self.connect() as db:
            row = db.execute("SELECT quantity FROM stock WHERE part_id=?", (part_id,)).fetchone()
            return row[0] if row else 0
