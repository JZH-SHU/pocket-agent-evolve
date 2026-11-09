from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pocket_agent.agents.contracts import OutcomeContract, canonical_hash
from pocket_agent.runtime.backends import FixtureBackend
from pocket_agent.runtime.executor import DurableExecutor, TaskSpec
from pocket_agent.runtime.executor import Action
from pocket_agent.runtime.context import runtime_context


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    group: str = Field(min_length=1, max_length=100)
    split: Literal["train", "dev", "holdout"]
    task: TaskSpec
    initial_state: dict
    reset_actions: list[Action] = Field(default_factory=list, max_length=20)
    reset_contract: OutcomeContract | None = None


class BenchmarkSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    environment: Literal["local_fixture", "mcp_device"] = "local_fixture"
    cases: list[BenchmarkCase] = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def split_integrity(self):
        ids, groups, examples = set(), {}, {}
        for case in self.cases:
            if self.environment == "mcp_device":
                from pocket_agent.agents.contracts import OutcomeContract
                if case.reset_contract is None:
                    raise ValueError("device evaluation requires an explicit reset contract")
                OutcomeContract.model_validate(case.reset_contract)
            digest = canonical_hash({"task": case.task.model_dump(exclude={"learn"}), "state": case.initial_state})
            if case.id in ids or case.group in groups and groups[case.group] != case.split:
                raise ValueError("case ids must be unique and groups cannot cross splits")
            if digest in examples and examples[digest] != case.split:
                raise ValueError("identical examples cannot cross splits")
            ids.add(case.id)
            groups[case.group], examples[digest] = case.split, case.split
        if {c.split for c in self.cases} != {"train", "dev", "holdout"}:
            raise ValueError("all three splits are required")
        return self

    @property
    def digest(self) -> str:
        return canonical_hash(self.model_dump())


def evaluator_fingerprint() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = ["agents/contracts.py", "runtime/executor.py", "runtime/backends.py", "runtime/ledger.py",
             "evaluation/harness.py", "skills/optimization.py"]
    return canonical_hash({path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths})


class BenchmarkRunner:
    def __init__(self, services, actor, *, actor_id: str, checkpoint_path: Path, repetitions: int = 2,
                 environment: str = "local_fixture", backend_factory=None):
        if not 1 <= repetitions <= 5:
            raise ValueError("repetitions must be in [1, 5]")
        self.services, self.actor, self.actor_id = services, actor, actor_id
        self.checkpoint_path, self.repetitions = checkpoint_path, repetitions
        self.environment, self.backend_factory = environment, backend_factory

    async def evaluate(self, cases: list[BenchmarkCase], skill: str) -> list[dict]:
        trials = []
        for case in cases:
            for repetition in range(self.repetitions):
                run_id = "eval_" + uuid.uuid4().hex
                if self.environment == "local_fixture":
                    backend = FixtureBackend(self.services.database, run_id, case.initial_state)
                else:
                    if self.backend_factory is None:
                        raise ValueError("device evaluation requires an operator-configured backend factory")
                    backend = await self.backend_factory(case, run_id)
                    from pocket_agent.agents.contracts import OutcomeContract
                    reset = OutcomeContract.model_validate(case.reset_contract)
                    with runtime_context(permission_mode="auto_safe"):
                        before = await backend.observe()
                        if not reset.evaluate(before, before)[0]:
                            for action in case.reset_actions:
                                spec = backend.specs.get(action.tool)
                                if not spec:
                                    raise ValueError("reset tool not available")
                                decision = self.services.policy.evaluate(spec, action.arguments)
                                if not decision.allowed:
                                    raise ValueError("reset requires approval; automatic evaluation refused")
                                response = await backend.call(action.tool, action.arguments, authorized_decision=decision)
                                if not response.ok or response.state_changes.get("outcome_unknown"):
                                    raise ValueError("reset failed or outcome unknown; no retry")
                        if not reset.evaluate(await backend.observe(), before)[0]:
                            raise ValueError("reset contract not satisfied")
                result = await DurableExecutor(self.services, backend, self.actor, actor_id=self.actor_id,
                    checkpoint_path=self.checkpoint_path).run(case.task.model_copy(update={"learn": False}),
                                                             skill=skill, run_id=run_id)
                trials.append({**result, "case_id": case.id, "group": case.group,
                    "split": case.split, "repetition": repetition,
                    "violations": int(result["status"] == "outcome_unknown" or "invariant:" in result.get("error", "")),
                    "objective": case.task.objective})
        return trials


def summarize(trials: list[dict]) -> dict:
    valid = [t for t in trials if t["status"] != "infra_error"]
    return {"trials": len(trials), "tasks": len({t["case_id"] for t in trials}),
        "groups": len({t["group"] for t in trials}),
        "successes": sum(t["verified"] for t in trials),
        "all_attempt_success_rate": sum(t["verified"] for t in trials) / len(trials) if trials else None,
        "valid_environment_success_rate": sum(t["verified"] for t in valid) / len(valid) if valid else None,
        "infra_errors": len(trials) - len(valid), "violations": sum(t["violations"] for t in trials),
        "tool_calls": sum(t["tool_calls"] for t in trials),
        "tokens": sum(t["tokens"] for t in trials) if trials and all(t["tokens"] is not None for t in trials) else None}
