from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from pocket_agent.runtime.context import get_permission_mode, get_run_id
from pocket_agent.storage.database import Database, now_ms

from .specs import RiskLevel, ToolSpec


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    reason: str
    approval_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


_EXTERNAL_ACTIONS = re.compile(
    r"(send|message|comment|post|publish|share|apply|call|sms|email|发送|评论|发布|投递)",
    re.IGNORECASE,
)
_DESTRUCTIVE_ACTIONS = re.compile(
    r"(delete|remove|purchase|buy|pay|install|uninstall|force_stop|shutdown|删除|购买|支付|安装|卸载)",
    re.IGNORECASE,
)
_READ_ACTIONS = re.compile(
    r"(get|list|find|search|screenshot|tree|context|notification|recent|query|read|获取|查询|搜索|截图)",
    re.IGNORECASE,
)
_DANGEROUS_SHELL = re.compile(
    r"(^|\s)(rm|del|rmdir|format|shutdown|reboot|su|sudo)\b|git\s+push|pip\s+install|pkg\s+install",
    re.IGNORECASE,
)


class PolicyEngine:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
        raw = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def infer_spec(
        tool_name: str,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> ToolSpec:
        lowered = f"{tool_name} {description}"
        if _DESTRUCTIVE_ACTIONS.search(lowered):
            risk = RiskLevel.CRITICAL
            read_only = False
            side_effect = True
        elif _EXTERNAL_ACTIONS.search(lowered):
            risk = RiskLevel.HIGH
            read_only = False
            side_effect = True
        elif _READ_ACTIONS.search(lowered):
            risk = RiskLevel.LOW
            read_only = True
            side_effect = False
        else:
            risk = RiskLevel.MEDIUM
            read_only = False
            side_effect = True
        return ToolSpec(
            name=tool_name,
            description=description,
            input_schema=input_schema or {},
            category="mcp" if tool_name.startswith("mcp__") else "local",
            read_only=read_only,
            side_effect=side_effect,
            risk_level=risk,
            timeout=30.0,
            idempotent=read_only,
            requires_confirmation=risk in {RiskLevel.HIGH, RiskLevel.CRITICAL},
        )

    def evaluate(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any] | None = None,
        *,
        mode: str | None = None,
        run_id: str | None = None,
    ) -> PolicyDecision:
        arguments = arguments or {}
        mode = mode or get_permission_mode()
        run_id = run_id if run_id is not None else get_run_id()

        if spec.name == "shell_exec":
            command = str(arguments.get("command") or "")
            if _DANGEROUS_SHELL.search(command):
                spec = ToolSpec(
                    **{
                        **spec.__dict__,
                        "risk_level": RiskLevel.CRITICAL,
                        "requires_confirmation": True,
                        "side_effect": True,
                    }
                )

        if mode == "plan" and spec.side_effect:
            return PolicyDecision("deny", "计划模式禁止执行有副作用的工具")
        if spec.read_only and spec.risk_level == RiskLevel.LOW:
            return PolicyDecision("allow", "只读低风险工具")
        if mode == "auto_safe" and spec.risk_level in {
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
        } and not spec.requires_confirmation:
            return PolicyDecision("allow", "自动安全模式允许低/中风险操作")
        if spec.requires_confirmation or spec.risk_level in {
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        }:
            fingerprint = self.fingerprint(spec.name, arguments)
            approved = self.database.fetch_one(
                """
                SELECT id FROM approvals
                WHERE fingerprint=? AND run_id IS ? AND status='approved'
                ORDER BY decided_at DESC LIMIT 1
                """,
                (fingerprint, run_id or None),
            )
            if approved and self.database.execute(
                    "UPDATE approvals SET status='consumed' WHERE id=? AND status='approved'",
                    (approved["id"],),
                ):
                return PolicyDecision("allow", "用户已批准", approved["id"])
            approval_id = self._request_approval(
                run_id=run_id,
                tool_name=spec.name,
                arguments=arguments,
                fingerprint=fingerprint,
                reason=f"{spec.risk_level.value} 风险操作需要确认",
            )
            return PolicyDecision(
                "approval_required",
                "该操作会改变外部状态，需要用户批准",
                approval_id,
            )
        return PolicyDecision("allow", "默认模式允许普通本地操作")

    def _request_approval(
        self,
        *,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        fingerprint: str,
        reason: str,
    ) -> str:
        existing = self.database.fetch_one(
            """
            SELECT id FROM approvals
            WHERE fingerprint=? AND run_id IS ? AND status='pending'
            ORDER BY created_at DESC LIMIT 1
            """,
            (fingerprint, run_id or None),
        )
        if existing:
            return str(existing["id"])
        approval_id = f"approval_{uuid.uuid4().hex}"
        self.database.execute(
            """
            INSERT INTO approvals(
                id, run_id, subject_type, subject_name, fingerprint, reason,
                payload_json, status, created_at
            ) VALUES (?, ?, 'tool', ?, ?, ?, ?, 'pending', ?)
            """,
            (
                approval_id,
                run_id or None,
                tool_name,
                fingerprint,
                reason,
                Database.json_dumps(arguments),
                now_ms(),
            ),
        )
        return approval_id

    def decide(self, approval_id: str, approved: bool) -> dict[str, Any]:
        status = "approved" if approved else "denied"
        updated = self.database.execute(
            """
            UPDATE approvals SET status=?, decided_at=?
            WHERE id=? AND status='pending'
            """,
            (status, now_ms(), approval_id),
        )
        if not updated:
            raise KeyError(f"pending approval not found: {approval_id}")
        return self.database.fetch_one(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)
        ) or {}

    def list_pending(self) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at"
        )
