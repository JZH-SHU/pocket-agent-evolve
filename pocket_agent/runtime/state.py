from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class RunStatus(str, Enum):
    INTERRUPTED = "interrupted"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class AgentState:
    conversation_id: str
    run_id: str
    objective: str
    plan: list[dict[str, Any]] = field(default_factory=list)
    current_step: int = 0
    step_results: list[dict[str, Any]] = field(default_factory=list)
    memory_refs: list[str] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    permission_mode: str = "default"
    status: str = RunStatus.PLANNED.value
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
