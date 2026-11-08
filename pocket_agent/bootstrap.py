from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from pocket_agent.mcp.manager import MCPManager
from pocket_agent.memory.service import MemoryService, ProfileStore
from pocket_agent.runtime.events import EventBus
from pocket_agent.runtime.store import RunStore
from pocket_agent.skills.repository import SkillRepository
from pocket_agent.storage.database import Database
from pocket_agent.tools.artifacts import ArtifactStore
from pocket_agent.tools.policy import PolicyEngine


@dataclass
class RuntimeServices:
    database: Database
    runs: RunStore
    events: EventBus
    policy: PolicyEngine
    artifacts: ArtifactStore
    memory: MemoryService
    profile: ProfileStore
    skills: SkillRepository
    mcp: MCPManager


_instances: dict[str, RuntimeServices] = {}
_lock = threading.Lock()


def get_runtime_services(
    db_path: str | Path | None = None,
    project_root: str | Path | None = None,
) -> RuntimeServices:
    root = Path(
        project_root
        or os.environ.get("POCKET_PROJECT_ROOT")
        or Path(__file__).resolve().parents[1]
    ).resolve()
    database_path = Path(
        db_path or os.environ.get("POCKET_DB_PATH") or root / "pocket_agent.db"
    ).resolve()
    key = str(database_path)
    with _lock:
        if key in _instances:
            return _instances[key]
        database = Database(database_path)
        runs = RunStore(database)
        events = EventBus()
        policy = PolicyEngine(database)
        artifacts = ArtifactStore(database, root / "artifacts")
        memory = MemoryService(database)
        profile = ProfileStore(root / "memory" / "user_profile.md")
        skills = SkillRepository(
            database,
            root / "agent" / "skills" / "candidates",
            root / "agent" / "skills" / "auto-skills" / "executor",
        )
        mcp = MCPManager(policy, runs, artifacts)
        default_mcp = os.environ.get(
            "MCP_SERVER_URL", "http://127.0.0.1:7474/mcp"
        )
        if default_mcp:
            mcp.register_server("neuralbridge", default_mcp)
        services = RuntimeServices(
            database=database,
            runs=runs,
            events=events,
            policy=policy,
            artifacts=artifacts,
            memory=memory,
            profile=profile,
            skills=skills,
            mcp=mcp,
        )
        _instances[key] = services
        return services


def reset_runtime_services() -> None:
    with _lock:
        _instances.clear()
