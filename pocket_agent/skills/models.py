from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:/-]{1,120}$")


class SkillVariable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Stable snake_case variable name.")
    description: str = Field(min_length=2, max_length=200)
    example: str = Field(default="", max_length=200)
    required: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _VARIABLE_NAME.fullmatch(normalized):
            raise ValueError("variable name must be lower snake_case")
        return normalized


class SkillStepDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1, le=50)
    title: str = Field(min_length=2, max_length=100)
    action: str = Field(min_length=4, max_length=800)
    tool: str = Field(default="", max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    verification: str = Field(min_length=4, max_length=500)
    on_failure: str = Field(default="", max_length=500)

    @field_validator("title", "action", "tool", "verification", "on_failure")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("tool")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not _TOOL_NAME.fullmatch(normalized):
            raise ValueError("tool contains unsupported characters")
        return normalized


class SkillDraft(BaseModel):
    """Validated semantic draft before it is rendered as SKILL.md."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=8, max_length=300)
    use_cases: list[str] = Field(min_length=1, max_length=6)
    prerequisites: list[str] = Field(default_factory=list, max_length=10)
    variables: list[SkillVariable] = Field(default_factory=list, max_length=12)
    allowed_tools: list[str] = Field(default_factory=list, max_length=40)
    steps: list[SkillStepDraft] = Field(min_length=3, max_length=30)
    success_criteria: list[str] = Field(min_length=1, max_length=10)
    failure_recovery: list[str] = Field(min_length=1, max_length=10)
    safety_notes: list[str] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=10)

    @field_validator(
        "display_name",
        "description",
    )
    @classmethod
    def strip_scalar_text(cls, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    @field_validator(
        "use_cases",
        "prerequisites",
        "success_criteria",
        "failure_recovery",
        "safety_notes",
        "limitations",
    )
    @classmethod
    def normalize_text_list(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in values:
            value = str(item).strip()
            if value and value not in result:
                result.append(value)
        return result

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in values:
            value = str(item).strip()
            if not _TOOL_NAME.fullmatch(value):
                raise ValueError(f"invalid tool name: {value!r}")
            if value not in result:
                result.append(value)
        return result

    @model_validator(mode="after")
    def validate_structure(self) -> "SkillDraft":
        variable_names = [item.name for item in self.variables]
        if len(variable_names) != len(set(variable_names)):
            raise ValueError("variable names must be unique")

        declared = set(variable_names)
        placeholders: set[str] = set()
        for step in self.steps:
            payload = f"{step.action} {step.arguments} {step.verification}"
            placeholders.update(re.findall(r"\{\{([a-z][a-z0-9_]{1,39})\}\}", payload))
        undeclared = placeholders - declared
        if undeclared:
            raise ValueError(
                f"undeclared variable placeholders: {', '.join(sorted(undeclared))}"
            )

        self.steps.sort(key=lambda item: item.order)
        for index, step in enumerate(self.steps, start=1):
            step.order = index
        return self
