"""Device backends used by the same executor in deployment and evaluation."""
from __future__ import annotations

import json
from typing import Any

from pocket_agent.agents.contracts import canonical_hash
from pocket_agent.storage.database import Database
from pocket_agent.tools.specs import RiskLevel, ToolResult, ToolSpec


UI_TOOLS = {"android_get_ui_tree", "android_find_elements", "android_tap"}


class FixtureBackend:
    """Persistent local UI fixture, explicitly NOT Android/MobileGym or an LLM."""
    environment = "local_fixture"
    identity = "local_fixture:v1"
    observation_tool = "android_get_ui_tree"

    def __init__(self, database: Database, scope: str, initial_state: dict):
        self.database, self.scope = database, scope
        self.identity = f"local_fixture:v1:{scope}"
        self.database.execute("INSERT OR IGNORE INTO fixture_states VALUES(?, ?)",
                              (scope, Database.json_dumps(initial_state)))
        self.specs = {
            "android_get_ui_tree": ToolSpec("android_get_ui_tree", "Read current UI state", read_only=True, idempotent=True),
            "android_find_elements": ToolSpec("android_find_elements", "Find elements by exact text; takes text", read_only=True, idempotent=True),
            "android_tap": ToolSpec("android_tap", "Toggle one element; takes element_id", side_effect=True, risk_level=RiskLevel.MEDIUM),
        }

    async def observe(self) -> dict:
        row = self.database.fetch_one("SELECT state_json FROM fixture_states WHERE scope=?", (self.scope,))
        return json.loads(row["state_json"])

    async def call(self, name: str, arguments: dict, *, authorized_decision=None) -> ToolResult:
        state = await self.observe()
        if name == "android_get_ui_tree" and not arguments:
            return ToolResult(True, "UI observed", data=state)
        if name == "android_find_elements" and set(arguments) == {"text"}:
            return ToolResult(True, "Elements found", data={"elements": [e for e in state["elements"]
                if e["text"] == arguments["text"]]})
        if name == "android_tap" and set(arguments) == {"element_id"}:
            for element in state["elements"]:
                if element["id"] == arguments["element_id"]:
                    if element.get("protected"):
                        return ToolResult(False, "Protected control", error="protected_control")
                    element["value"] = not element["value"]
                    self.database.execute("UPDATE fixture_states SET state_json=? WHERE scope=?",
                                          (Database.json_dumps(state), self.scope))
                    return ToolResult(True, "Element toggled", data={"element_id": element["id"]})
        return ToolResult(False, "Invalid fixture action", error="invalid_action")


def structured_state(result: ToolResult) -> dict:
    if not result.ok:
        raise ValueError("observation_failed")
    raw = result.data
    if isinstance(raw, dict) and "content" in raw:
        if raw.get("isError"):
            raise ValueError("observation_failed")
        structured = raw.get("structuredContent")
        if structured is None:
            text = "\n".join(item.get("text", "") for item in raw["content"] if item.get("type") == "text")
            structured = json.loads(text)
        raw = structured
    if not isinstance(raw, dict) or raw.get("ok", True) is False or raw.get("error"):
        raise ValueError("observation_has_no_valid_structured_state")
    state = raw.get("state", raw.get("data", raw))
    if not isinstance(state, dict):
        raise ValueError("observation_state_must_be_object")
    return state


class MCPBackend:
    environment = "mcp_device"

    def __init__(self, manager, server_name: str, *, observation_tool: str = "android_get_ui_tree"):
        self.manager, self.server_name, self.observation_tool = manager, server_name, observation_tool
        self.specs: dict[str, ToolSpec] = {}
        self.identity = "mcp:" + canonical_hash({"url": manager.servers[server_name].url,
                                                 "observation_tool": observation_tool})

    async def prepare(self):
        await self.manager.discover_tools(self.server_name)
        self.specs = self.manager.servers[self.server_name].tools
        if self.observation_tool not in self.specs or not self.specs[self.observation_tool].read_only:
            raise ValueError("a locally classified read-only observation tool is required")
        self.identity += ":" + canonical_hash({name: {"schema": spec.input_schema, "read_only": spec.read_only,
            "idempotent": spec.idempotent, "risk": spec.risk_level.value} for name, spec in sorted(self.specs.items())})
        return self

    async def observe(self) -> dict:
        return structured_state(await self.call(self.observation_tool, {}))

    async def call(self, name: str, arguments: dict, *, authorized_decision=None) -> ToolResult:
        return await self.manager.call(self.server_name, name, arguments, authorized_decision=authorized_decision)
