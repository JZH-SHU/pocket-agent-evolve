from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator


def now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    """Small SQLite gateway with idempotent, versioned migrations."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migration_lock = threading.Lock()
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        with self._migration_lock:
            with self.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at INTEGER NOT NULL
                    )
                    """
                )
                applied = {
                    int(row["version"])
                    for row in conn.execute("SELECT version FROM schema_migrations")
                }
                if 1 not in applied:
                    self._migration_v1(conn)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                        (now_ms(),),
                    )
                if 2 not in applied:
                    conn.executescript("""
                        CREATE TABLE IF NOT EXISTS executor_jobs (
                            run_id TEXT PRIMARY KEY REFERENCES runs(id),
                            task_json TEXT NOT NULL, task_hash TEXT NOT NULL,
                            actor_id TEXT NOT NULL, skill_hash TEXT NOT NULL,
                            backend_id TEXT NOT NULL, created_at INTEGER NOT NULL,
                            lease_owner TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0
                        );
                        CREATE TABLE IF NOT EXISTS action_ledger (
                            run_id TEXT NOT NULL REFERENCES runs(id),
                            operation_id TEXT NOT NULL, intent_hash TEXT NOT NULL,
                            intent_json TEXT NOT NULL, status TEXT NOT NULL,
                            result_json TEXT, updated_at INTEGER NOT NULL,
                            PRIMARY KEY(run_id, operation_id)
                        );
                        CREATE TABLE IF NOT EXISTS fixture_states (
                            scope TEXT PRIMARY KEY, state_json TEXT NOT NULL
                        );
                        CREATE TABLE IF NOT EXISTS evaluation_holdouts (
                            suite_hash TEXT PRIMARY KEY, candidate_hash TEXT NOT NULL,
                            consumed_at INTEGER NOT NULL
                        );
                    """)
                    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(2, ?)", (now_ms(),))
                self._ensure_memory_fts(conn)
                self._import_legacy_memories(conn)

    @staticmethod
    def _migration_v1(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                permission_mode TEXT NOT NULL DEFAULT 'default',
                plan_json TEXT NOT NULL DEFAULT '[]',
                current_step INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                completed_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_runs_conversation
                ON runs(conversation_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS run_steps (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                precondition TEXT NOT NULL DEFAULT '',
                postcondition TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                evidence_artifact_id TEXT,
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(run_id, seq),
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tool_calls (
                id TEXT PRIMARY KEY,
                run_id TEXT,
                step_id TEXT,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL DEFAULT '{}',
                risk_level TEXT NOT NULL DEFAULT 'low',
                decision TEXT NOT NULL DEFAULT 'allow',
                status TEXT NOT NULL,
                result_summary TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE SET NULL,
                FOREIGN KEY(step_id) REFERENCES run_steps(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                run_id TEXT,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                run_id TEXT,
                subject_type TEXT NOT NULL,
                subject_name TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                decided_at INTEGER,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_approvals_status
                ON approvals(status, created_at DESC);

            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'global',
                content TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                importance INTEGER NOT NULL DEFAULT 5,
                confidence REAL NOT NULL DEFAULT 1.0,
                source_run_id TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_access_at INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER,
                legacy_message_id INTEGER UNIQUE,
                FOREIGN KEY(source_run_id) REFERENCES runs(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memories_filter
                ON memories(kind, scope, created_at DESC);

            CREATE TABLE IF NOT EXISTS skills (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'candidate',
                current_version TEXT NOT NULL DEFAULT '0.1.0',
                scope TEXT NOT NULL DEFAULT 'project',
                source_run_id TEXT,
                content_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                suggested_action TEXT NOT NULL DEFAULT 'add',
                merge_target TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                activated_at INTEGER,
                FOREIGN KEY(source_run_id) REFERENCES runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS skill_versions (
                id TEXT PRIMARY KEY,
                skill_id TEXT NOT NULL,
                version TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                created_at INTEGER NOT NULL,
                UNIQUE(skill_id, version),
                FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS skill_usage (
                skill_id TEXT PRIMARY KEY,
                retrieved INTEGER NOT NULL DEFAULT 0,
                used INTEGER NOT NULL DEFAULT 0,
                succeeded INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                retries INTEGER NOT NULL DEFAULT 0,
                last_used_at INTEGER,
                FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE
            );
            """
        )

    @staticmethod
    def _ensure_memory_fts(conn: sqlite3.Connection) -> None:
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        if existing:
            return
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE memories_fts USING fts5(
                    memory_id UNINDEXED,
                    content,
                    summary,
                    tokenize='trigram'
                )
                """
            )
        except sqlite3.OperationalError:
            conn.execute(
                """
                CREATE VIRTUAL TABLE memories_fts USING fts5(
                    memory_id UNINDEXED,
                    content,
                    summary,
                    tokenize='unicode61'
                )
                """
            )
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(memory_id, content, summary)
                VALUES (new.id, new.content, new.summary);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
                DELETE FROM memories_fts WHERE memory_id = old.id;
            END;
            CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
                DELETE FROM memories_fts WHERE memory_id = old.id;
                INSERT INTO memories_fts(memory_id, content, summary)
                VALUES (new.id, new.content, new.summary);
            END;
            """
        )
        conn.execute(
            """
            INSERT INTO memories_fts(memory_id, content, summary)
            SELECT id, content, summary FROM memories
            WHERE id NOT IN (SELECT memory_id FROM memories_fts)
            """
        )

    @staticmethod
    def _import_legacy_memories(conn: sqlite3.Connection) -> None:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        if not table:
            return
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "memory_type" not in columns:
            return
        rows = conn.execute(
            """
            SELECT id, conversation_id, content, timestamp,
                   COALESCE(importance, 5) AS importance,
                   COALESCE(memory_type, 'fact') AS memory_type,
                   COALESCE(last_access_at, 0) AS last_access_at
            FROM messages WHERE role='memory'
            """
        ).fetchall()
        for row in rows:
            memory_id = f"legacy-{row['id']}"
            conn.execute(
                """
                INSERT OR IGNORE INTO memories(
                    id, conversation_id, kind, scope, content, summary,
                    importance, confidence, created_at, updated_at,
                    last_access_at, legacy_message_id
                ) VALUES (?, ?, ?, 'global', ?, '', ?, 1.0, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    row["conversation_id"],
                    row["memory_type"],
                    row["content"],
                    row["importance"],
                    row["timestamp"],
                    row["timestamp"],
                    row["last_access_at"],
                    row["id"],
                ),
            )

    def execute(
        self, sql: str, params: Iterable[Any] = ()
    ) -> int:
        with self.connection() as conn:
            cursor = conn.execute(sql, tuple(params))
            return cursor.rowcount

    def fetch_one(
        self, sql: str, params: Iterable[Any] = ()
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def fetch_all(
        self, sql: str, params: Iterable[Any] = ()
    ) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row) for row in conn.execute(sql, tuple(params)).fetchall()
            ]

    @staticmethod
    def json_dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
