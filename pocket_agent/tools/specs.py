from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    category: str = "general"
    read_only: bool = False
    side_effect: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    timeout: float = 30.0
    idempotent: bool = False
    requires_confirmation: bool = False


@dataclass
class ToolResult:
    ok: bool
    summary: str
    data: Any = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    retryable: bool = False
    state_changes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
