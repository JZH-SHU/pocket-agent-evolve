"""Versioned declarative outcome checks. No generated code is executed."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def pointer(value: Any, path: str) -> Any:
    """Resolve an RFC 6901 JSON pointer; missing paths are errors, not null."""
    if path == "":
        return value
    if not path.startswith("/"):
        raise ValueError("state path must be a JSON pointer")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def contains(value: Any, expected: Any) -> bool:
    if isinstance(value, list):
        return any(contains(item, expected) for item in value)
    if isinstance(expected, dict):
        return isinstance(value, dict) and all(key in value and contains(value[key], item)
                                             for key, item in expected.items())
    return type(value) is type(expected) and value == expected


class StateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(max_length=300)
    op: Literal["equals", "contains", "exists", "unchanged"] = "equals"
    value: Any = None

    @model_validator(mode="after")
    def validate_pointer(self):
        if self.path and not self.path.startswith("/"):
            raise ValueError("path must be a JSON pointer")
        canonical_hash(self.value)
        return self

    def matches(self, state: dict, initial: dict | None = None) -> bool:
        try:
            actual = pointer(state, self.path)
            if self.op == "exists":
                return True
            if self.op == "unchanged":
                expected = pointer(initial, self.path)
            else:
                expected = self.value
            if self.op == "contains":
                return contains(actual, expected)
            return type(actual) is type(expected) and actual == expected
        except (KeyError, IndexError, ValueError, TypeError):
            return False


class OutcomeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    postconditions: list[StateCheck] = Field(min_length=1, max_length=30)
    invariants: list[StateCheck] = Field(default_factory=list, max_length=30)

    def evaluate(self, state: dict, initial: dict | None = None) -> tuple[bool, list[str]]:
        failures = [f"postcondition:{i}:{check.path}" for i, check in enumerate(self.postconditions)
                    if not check.matches(state, initial)]
        failures += [f"invariant:{i}:{check.path}" for i, check in enumerate(self.invariants)
                     if not check.matches(state, initial)]
        return not failures, failures
