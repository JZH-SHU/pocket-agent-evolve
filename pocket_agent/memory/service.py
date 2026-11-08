from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pocket_agent.storage.database import Database, now_ms


VALID_MEMORY_TYPES = {"fact", "episodic", "dance"}


def _chinese_bigrams(query: str) -> list[str]:
    chinese = re.findall(r"[\u4e00-\u9fff]", query)
    if len(chinese) < 2:
        return [query.strip()] if query.strip() else []
    return ["".join(chinese[index : index + 2]) for index in range(len(chinese) - 1)]


def rrf_merge(
    lexical: list[dict[str, Any]],
    semantic: list[dict[str, Any]],
    *,
    top_n: int = 5,
    k: int = 60,
) -> list[dict[str, Any]]:
    scores: dict[str, dict[str, Any]] = {}
    for rank, item in enumerate(lexical):
        memory_id = str(item["id"])
        scores[memory_id] = {"item": item, "score": 1 / (k + rank + 1)}
    for rank, item in enumerate(semantic):
        memory_id = str(item["id"])
        entry = scores.setdefault(
            memory_id, {"item": item, "score": 0.0}
        )
        similarity = float(item.get("similarity", 0.5))
        entry["score"] += (1 / (k + rank + 1)) * (0.5 + similarity)
    current = now_ms()
    for entry in scores.values():
        item = entry["item"]
        importance = max(1, min(10, int(item.get("importance") or 5)))
        accessed = int(item.get("last_access_at") or item.get("created_at") or current)
        hours = max(0.0, (current - accessed) / 3_600_000)
        recency = math.pow(0.995, hours)
        entry["score"] = entry["score"] * (0.8 + importance * 0.04) + 0.002 * recency
    ranked = sorted(scores.values(), key=lambda item: item["score"], reverse=True)
    return [entry["item"] for entry in ranked[:top_n]]


class MemoryService:
    """SQLite source of truth for factual and episodic memories."""

    def __init__(
        self,
        database: Database,
        vector_search: Callable[[str, int, str | None], list[dict[str, Any]]] | None = None,
        vector_add: Callable[[str, str], bool] | None = None,
    ):
        self.database = database
        self.vector_search = vector_search
        self.vector_add = vector_add

    def save(
        self,
        content: str,
        *,
        kind: str = "fact",
        importance: int = 5,
        conversation_id: str = "",
        scope: str = "global",
        confidence: float = 1.0,
        source_run_id: str = "",
        summary: str = "",
        expires_at: int | None = None,
    ) -> str:
        if kind not in VALID_MEMORY_TYPES:
            raise ValueError(f"unsupported memory type: {kind}")
        content = content.strip()
        if not content:
            raise ValueError("memory content cannot be empty")
        memory_id = f"memory_{uuid.uuid4().hex}"
        created = now_ms()
        self.database.execute(
            """
            INSERT INTO memories(
                id, conversation_id, kind, scope, content, summary,
                importance, confidence, source_run_id, created_at, updated_at,
                last_access_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                memory_id,
                conversation_id or None,
                kind,
                scope,
                content,
                summary,
                max(1, min(10, int(importance))),
                max(0.0, min(1.0, float(confidence))),
                source_run_id or None,
                created,
                created,
                expires_at,
            ),
        )
        if self.vector_add:
            try:
                self.vector_add(memory_id, content[:2000])
            except Exception:
                pass
        return memory_id

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        scope: str | None = None,
        days: int | None = None,
        top_n: int = 5,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        filters = []
        params: list[Any] = []
        if kind:
            filters.append("m.kind=?")
            params.append(kind)
        if scope:
            filters.append("m.scope=?")
            params.append(scope)
        if days:
            filters.append("m.created_at>=?")
            params.append(now_ms() - int(days) * 86_400_000)
        filters.append("(m.expires_at IS NULL OR m.expires_at>?)")
        params.append(now_ms())
        filter_sql = " AND " + " AND ".join(filters)

        lexical: list[dict[str, Any]] = []
        try:
            with self.database.connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT m.* FROM memories_fts f
                    JOIN memories m ON m.id=f.memory_id
                    WHERE memories_fts MATCH ? {filter_sql}
                    ORDER BY rank LIMIT 20
                    """,
                    [query] + params,
                ).fetchall()
                lexical = [dict(row) for row in rows]
        except Exception:
            lexical = []

        if not lexical:
            tokens = list(dict.fromkeys(_chinese_bigrams(query) or query.split()))
            tokens = [token for token in tokens if token]
            if tokens:
                like_sql = " OR ".join("m.content LIKE ?" for _ in tokens)
                like_params = [f"%{token}%" for token in tokens]
                lexical = self.database.fetch_all(
                    f"""
                    SELECT m.* FROM memories m
                    WHERE ({like_sql}) {filter_sql}
                    ORDER BY m.importance DESC, m.created_at DESC LIMIT 20
                    """,
                    like_params + params,
                )

        semantic: list[dict[str, Any]] = []
        if self.vector_search:
            try:
                semantic = self.vector_search(query, 20, kind)
            except Exception:
                semantic = []
        results = rrf_merge(lexical, semantic, top_n=top_n)
        accessed = now_ms()
        for item in results:
            self.database.execute(
                "UPDATE memories SET last_access_at=? WHERE id=?",
                (accessed, item["id"]),
            )
        return results

    def delete(self, memory_id: str) -> bool:
        return bool(
            self.database.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        )


class ProfileStore:
    """Human-readable, low-volume user profile source of truth."""

    DEFAULT = """# 用户画像

## 基本信息

## 沟通偏好

## 行为要求

## 反馈

## 其他
"""

    def __init__(self, path: str | Path, max_bytes: int = 4096):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(self.DEFAULT, encoding="utf-8")

    def read(self) -> str:
        content = self.path.read_text(encoding="utf-8")
        encoded = content.encode("utf-8")
        if len(encoded) <= self.max_bytes:
            return content
        return encoded[: self.max_bytes].decode("utf-8", errors="ignore")

    def update(self, section: str, content: str) -> None:
        if re.search(r"(api[_ -]?key|password|身份证|银行卡|支付密码)", content, re.I):
            raise ValueError("用户画像禁止保存密钥或高敏感身份/支付信息")
        lines = self.read().splitlines()
        result: list[str] = []
        in_target = False
        found = False
        for line in lines:
            if line == f"## {section}":
                found = True
                in_target = True
                result.extend([line, "", content.strip()])
                continue
            if line.startswith("## ") and in_target:
                in_target = False
            if not in_target:
                result.append(line)
        if not found:
            result.extend(["", f"## {section}", "", content.strip()])
        rendered = "\n".join(result).rstrip() + "\n"
        if len(rendered.encode("utf-8")) > self.max_bytes:
            raise ValueError(f"用户画像不能超过 {self.max_bytes} 字节")
        self.path.write_text(rendered, encoding="utf-8")


@dataclass(frozen=True)
class WorkingMemoryPolicy:
    context_window_tokens: int = 128_000
    summarize_ratio: float = 0.60
    recent_turns: int = 12
    artifact_threshold_bytes: int = 16 * 1024

    def should_summarize(self, estimated_tokens: int) -> bool:
        return estimated_tokens >= int(
            self.context_window_tokens * self.summarize_ratio
        )

    def select_recent(self, messages: list[Any]) -> list[Any]:
        return messages[-(self.recent_turns * 2) :]
