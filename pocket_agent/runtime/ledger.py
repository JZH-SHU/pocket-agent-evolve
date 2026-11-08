"""At-most-one automatic dispatch per durable action intent, not exactly-once."""
from __future__ import annotations

import json

from pocket_agent.agents.contracts import canonical_hash
from pocket_agent.storage.database import Database, now_ms


class OutcomeUnknown(RuntimeError):
    pass


class ActionLedger:
    def __init__(self, database: Database):
        self.database = database

    def claim(self, run_id: str, operation_id: str, intent: dict) -> dict | None:
        """Return a confirmed result or reserve dispatch. Never replay an uncertain write."""
        digest = canonical_hash(intent)
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM action_ledger WHERE run_id=? AND operation_id=?",
                               (run_id, operation_id)).fetchone()
            if row:
                if row["intent_hash"] != digest:
                    raise ValueError("operation_id was reused for a different intent")
                if row["status"] == "confirmed":
                    return json.loads(row["result_json"])
                raise OutcomeUnknown(f"{operation_id}: prior dispatch has no confirmed result")
            conn.execute("INSERT INTO action_ledger VALUES(?, ?, ?, ?, 'submitted', NULL, ?)",
                         (run_id, operation_id, digest, Database.json_dumps(intent), now_ms()))
        return None

    def finish(self, run_id: str, operation_id: str, result: dict) -> None:
        # Even a known tool failure is a confirmed response; replay returns that failure.
        if not self.database.execute("UPDATE action_ledger SET status='confirmed', result_json=?, updated_at=? "
                "WHERE run_id=? AND operation_id=? AND status='submitted'",
                (Database.json_dumps(result), now_ms(), run_id, operation_id)):
            raise ValueError("action is not awaiting confirmation")

    def mark_unknown(self, run_id: str, operation_id: str) -> None:
        self.database.execute("UPDATE action_ledger SET status='unknown', updated_at=? "
            "WHERE run_id=? AND operation_id=? AND status='submitted'", (now_ms(), run_id, operation_id))
