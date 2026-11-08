from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from pocket_agent.storage.database import Database, now_ms

from .state import RunStatus


_ALLOWED_TRANSITIONS = {
    RunStatus.PLANNED.value: {
        RunStatus.AWAITING_APPROVAL.value,
        RunStatus.EXECUTING.value,
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    },
    RunStatus.AWAITING_APPROVAL.value: {
        RunStatus.EXECUTING.value,
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    },
    RunStatus.EXECUTING.value: {
        RunStatus.AWAITING_APPROVAL.value,
        RunStatus.INTERRUPTED.value,
        RunStatus.VERIFYING.value,
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    },
    RunStatus.VERIFYING.value: {
        RunStatus.EXECUTING.value,
        RunStatus.COMPLETED.value,
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    },
    RunStatus.COMPLETED.value: set(),
    RunStatus.INTERRUPTED.value: {
        RunStatus.EXECUTING.value, RunStatus.CANCELLED.value, RunStatus.FAILED.value,
    },
    RunStatus.FAILED.value: set(),
    RunStatus.CANCELLED.value: set(),
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class RunStore:
    def __init__(self, database: Database):
        self.database = database

    def create_run(
        self,
        conversation_id: str,
        objective: str,
        plan: list[dict[str, Any]] | None = None,
        permission_mode: str = "default",
        run_id: str | None = None,
    ) -> str:
        run_id = run_id or new_id("run")
        now = now_ms()
        plan = plan or []
        with self.database.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs(
                    id, conversation_id, objective, status, permission_mode,
                    plan_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    conversation_id,
                    objective,
                    RunStatus.PLANNED.value,
                    permission_mode,
                    Database.json_dumps(plan),
                    now,
                    now,
                ),
            )
            self._replace_steps(conn, run_id, plan)
        return run_id

    @staticmethod
    def _replace_steps(conn, run_id: str, plan: list[dict[str, Any]]) -> None:
        conn.execute("DELETE FROM run_steps WHERE run_id=?", (run_id,))
        now = now_ms()
        for index, step in enumerate(plan, start=1):
            seq = int(step.get("id") or index)
            conn.execute(
                """
                INSERT INTO run_steps(
                    id, run_id, seq, description, status, precondition,
                    postcondition, attempts, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{run_id}:step:{seq}",
                    run_id,
                    seq,
                    str(step.get("desc") or step.get("description") or ""),
                    str(step.get("status") or "pending"),
                    str(step.get("precondition") or ""),
                    str(step.get("postcondition") or ""),
                    int(step.get("attempts") or 0),
                    str(step.get("error") or ""),
                    now,
                    now,
                ),
            )

    def transition(self, run_id: str, target: str, error: str = "") -> None:
        run = self.get_run(run_id)
        if not run:
            raise KeyError(f"run not found: {run_id}")
        current = str(run["status"])
        if target == current:
            return
        if target not in _ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid run transition: {current} -> {target}")
        completed_at = now_ms() if target in {
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        } else None
        self.database.execute(
            """
            UPDATE runs SET status=?, error=?, updated_at=?, completed_at=?
            WHERE id=?
            """,
            (target, error, now_ms(), completed_at, run_id),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.database.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            return None
        run["plan"] = json.loads(run.pop("plan_json") or "[]")
        run["steps"] = self.database.fetch_all(
            "SELECT * FROM run_steps WHERE run_id=? ORDER BY seq", (run_id,)
        )
        run["tool_calls"] = self.database.fetch_all(
            "SELECT * FROM tool_calls WHERE run_id=? ORDER BY created_at", (run_id,)
        )
        run["artifacts"] = self.database.fetch_all(
            "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at", (run_id,)
        )
        return run

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def sync_task_export(self, task_data: dict[str, Any]) -> str:
        run_id = str(task_data.get("run_id") or task_data.get("task_id") or "")
        if not run_id:
            raise ValueError("task export is missing run_id/task_id")
        existing = self.get_run(run_id)
        plan = list(task_data.get("steps") or [])
        if not existing:
            self.create_run(
                conversation_id=str(task_data.get("conversation_id") or "local-cli"),
                objective=str(task_data.get("objective") or "delegated task"),
                plan=plan,
                run_id=run_id,
            )
        with self.database.connection() as conn:
            conn.execute(
                "UPDATE runs SET plan_json=?, updated_at=? WHERE id=?",
                (Database.json_dumps(plan), now_ms(), run_id),
            )
            self._replace_steps(conn, run_id, plan)
        return run_id

    def sync_task_file(self, path: str | Path) -> str:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return self.sync_task_export(data)

    def record_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        status: str,
        run_id: str = "",
        step_id: str = "",
        risk_level: str = "low",
        decision: str = "allow",
        summary: str = "",
        error: str = "",
        latency_ms: int = 0,
    ) -> str:
        call_id = new_id("call")
        created = now_ms()
        self.database.execute(
            """
            INSERT INTO tool_calls(
                id, run_id, step_id, tool_name, arguments_json, risk_level,
                decision, status, result_summary, error, latency_ms,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                run_id or None,
                step_id or None,
                tool_name,
                Database.json_dumps(arguments),
                risk_level,
                decision,
                status,
                summary[:1000],
                error[:1000],
                latency_ms,
                created,
                created if status in {"completed", "failed", "denied"} else None,
            ),
        )
        return call_id
