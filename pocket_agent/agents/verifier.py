from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import OutcomeContract


_OBSERVATION_TOOLS = {
    "android_get_ui_tree",
    "android_get_screen_context",
    "android_find_elements",
    "android_screenshot",
    "android_get_recent_toasts",
    "android_wait_for_element",
    "android_wait_for_gone",
    "android_wait_for_idle",
}


@dataclass(frozen=True)
class VerificationReport:
    verified: bool
    reason: str
    observations: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)


class ExecutionVerifier:
    """Deterministic gate before a run can teach the skill system."""

    def verify(
        self,
        task_data: dict[str, Any],
        trace: list[dict[str, Any]],
    ) -> VerificationReport:
        steps = list(task_data.get("steps") or [])
        incomplete = [
            step
            for step in steps
            if step.get("status") not in {"completed", "skipped"}
        ]
        if incomplete:
            return VerificationReport(
                False, f"仍有 {len(incomplete)} 个步骤未完成"
            )
        if not trace:
            return VerificationReport(False, "没有观察到任何工具调用")

        # A tool name proves neither successful observation nor the user's outcome.
        # Legacy tasks without an explicit contract are deliberately unverified.
        try:
            contract = OutcomeContract.model_validate(task_data.get("contract"))
        except ValueError:
            return VerificationReport(False, "缺少有效的目标状态契约，不能确认任务成功")
        observations: list[str] = []
        artifacts: list[str] = []
        last_state = None
        last_observation = -1
        last_action = -1
        for index, item in enumerate(trace):
            tool_name = str(
                item.get("remote_tool")
                or item.get("tool_name")
                or item.get("name")
                or ""
            )
            result = item.get("result")
            if isinstance(result, str):
                import json
                try:
                    result = json.loads(result)
                except (ValueError, TypeError):
                    result = None
            if tool_name in _OBSERVATION_TOOLS:
                if (item.get("ok") is True and isinstance(result, dict)
                        and result.get("ok", True) is not False and not result.get("isError")):
                    last_state = result.get("state", result.get("data", result))
                    last_observation = index
                    observations.append(f"{tool_name}: 结构化观察成功")
            elif not item.get("read_only", False):
                last_action = index
            for artifact in item.get("artifacts", []) or []:
                path = artifact.get("path") if isinstance(artifact, dict) else artifact
                if path:
                    artifacts.append(str(path))
        if not isinstance(last_state, dict) or last_observation < last_action:
            return VerificationReport(
                False,
                "任务步骤已标记完成，但缺少 UI 树、截图或 Toast 等操作后证据",
            )
        verified, failures = contract.evaluate(last_state, task_data.get("initial_state"))
        return VerificationReport(
            verified,
            "目标状态与不变量验证通过" if verified else "目标状态未满足: " + ", ".join(failures),
            observations=observations,
            artifacts=artifacts,
        )
