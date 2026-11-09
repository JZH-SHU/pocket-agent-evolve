"""PocketAgent's verified execution entry point. Legacy chat remains `python main.py`."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pocket_agent.bootstrap import get_runtime_services
from pocket_agent.agents.contracts import canonical_hash
from pocket_agent.evaluation.harness import BenchmarkRunner, BenchmarkSuite, summarize
from pocket_agent.runtime.backends import FixtureBackend, MCPBackend
from pocket_agent.runtime.executor import DurableExecutor, TaskSpec
from pocket_agent.skills.frontmatter import parse_frontmatter
from pocket_agent.skills.optimization import GEPASkillOptimizer


class OptimizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite: BenchmarkSuite
    max_metric_calls: int = Field(default=40, ge=4, le=1000)
    max_actor_calls: int = Field(default=500, ge=1, le=5000)
    max_reflections: int = Field(default=20, ge=1, le=100)
    auto_promote: bool = False
    allow_device_eval: bool = False

    @model_validator(mode="after")
    def device_scope(self):
        if self.suite.environment == "mcp_device" and (not self.allow_device_eval or self.auto_promote):
            raise ValueError("device trials need allow_device_eval; automatic device promotion is disabled")
        return self


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: TaskSpec
    initial_state: dict = Field(default_factory=dict)
    backend: str = "fixture"
    skill_id: str = ""
    optimization: OptimizationRequest | None = None

    @model_validator(mode="after")
    def learning_scope(self):
        if self.optimization and not self.task.learn:
            raise ValueError("optimization after a task requires learning enabled")
        return self


def state_services(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    return get_runtime_services(directory / "pocket.db", directory)


def load_actor():
    from langchain_openai import ChatOpenAI
    model = os.environ.get("EXECUTOR_MODEL") or os.environ.get("LLM_MODEL")
    key = os.environ.get("EXECUTOR_API_KEY") or os.environ.get("LLM_API_KEY")
    if not model or not key:
        raise ValueError("set LLM_MODEL and LLM_API_KEY (or EXECUTOR_MODEL/EXECUTOR_API_KEY)")
    base = os.environ.get("EXECUTOR_LLM_BASE_URL") or os.environ.get("LLM_BASE_URL") or os.environ.get("DEFAULT_LLM_BASE_URL")
    return ChatOpenAI(model=model, api_key=key, base_url=base, temperature=0,
                      max_tokens=1500, timeout=30, max_retries=0), model


def skill_body(services, skill_id: str, allowed_tools: list[str] | None = None) -> str:
    if not skill_id:
        return ""
    skill = services.skills.get(skill_id)
    if not skill or skill["status"] != "active":
        raise ValueError("skill must be active")
    content = Path(skill["content_path"]).read_text(encoding="utf-8")
    from pocket_agent.skills.optimization import text_hash
    if text_hash(content) != skill["content_hash"]:
        raise ValueError("skill hash mismatch")
    meta, body = parse_frontmatter(content)
    declared = {name for name in meta.get("allowed-tools", "").split(",") if name}
    if allowed_tools and not set(allowed_tools) <= declared:
        raise ValueError("task tools exceed skill permissions")
    return body


async def make_backend(services, request: RunRequest, run_id: str):
    if request.backend == "fixture":
        return FixtureBackend(services.database, run_id, request.initial_state)
    if request.backend == "mcp":
        # Server URL and auth are operator configuration, never model-generated arguments.
        url = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:7474/mcp")
        headers = {}
        if os.environ.get("MCP_BEARER_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["MCP_BEARER_TOKEN"]
        services.mcp.register_server("device", url, headers=headers,
                                    transport=os.environ.get("POCKET_MCP_TRANSPORT", "sdk"))
        return await MCPBackend(services.mcp, "device").prepare()
    raise ValueError("backend must be fixture or mcp")


class DemoActor:
    """Explicit scripted smoke actor, not model learning or benchmark evidence."""
    async def ainvoke(self, prompt):
        if not prompt.startswith("{"):
            return SimpleNamespace(content="{}")  # Deterministic Curator fallback, no hidden model call.
        data = json.loads(prompt)
        matches = [e for e in data["observation"]["elements"] if e["text"] in data["objective"]]
        action = {"tool": "android_tap", "arguments": {"element_id": matches[0]["id"]}} if len(matches) == 1 else {"tool": "finish"}
        return SimpleNamespace(content=json.dumps(action))

    def invoke(self, prompt):
        return "Observe state; find the element whose text matches the objective; tap only if needed."


async def execute_request(services, request: RunRequest, actor, actor_id: str, run_id: str, *, resume=False):
    backend = await make_backend(services, request, run_id)
    body = skill_body(services, request.skill_id, request.task.allowed_tools)
    executor = DurableExecutor(services, backend, actor, actor_id=actor_id,
                              checkpoint_path=services.artifacts.root.parent / "checkpoints.db",
                              curation_model=actor, skill_id=request.skill_id)
    result = await executor.run(request.task, skill=body, run_id=run_id, resume=resume)
    return await continue_learning(services, request, result, actor, actor_id)


async def continue_learning(services, request: RunRequest, result: dict, actor, actor_id: str) -> dict:
    """Optional, explicitly budgeted acquisition -> optimization -> release stage."""
    spec = request.optimization
    if spec is None:
        return result
    learning = result.get("learning", {})
    if learning.get("status") != "candidate":
        result["optimization"] = {"status": "skipped", "reason": "no_new_seed"}
        return result
    db, run_id = services.database, result["run_id"]
    digest = canonical_hash({"config": spec.model_dump(), "actor_id": actor_id})
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM skill_acquisitions WHERE run_id=?", (run_id,)).fetchone()
        saved = json.loads(row["decision_json"])
        previous = saved.get("optimization")
        if previous:
            result["optimization"] = previous if previous["config_hash"] == digest else {
                "status": "refused", "reason": "optimization_config_changed"}
            return result
        stage = {"status": "running", "config_hash": digest}
        saved["optimization"] = stage
        conn.execute("UPDATE skill_acquisitions SET decision_json=? WHERE run_id=?", (db.json_dumps(saved), run_id))
    try:
        backend_factory = None
        if spec.suite.environment == "mcp_device":
            async def backend_factory(case, trial_id):
                return await make_backend(services, RunRequest(task=case.task, backend="mcp"), trial_id)
        optimizer = GEPASkillOptimizer(services, actor, lambda prompt: actor.invoke(prompt), actor_id=actor_id,
            max_actor_calls=spec.max_actor_calls, max_reflections=spec.max_reflections, backend_factory=backend_factory)
        report = await asyncio.to_thread(optimizer.optimize, learning["candidate_id"], spec.suite,
                                        max_metric_calls=spec.max_metric_calls, auto_promote=spec.auto_promote)
        stage = {**stage, "status": "published" if report.get("release") else "evaluated",
                 **{key: report[key] for key in ("eligible", "reason", "candidate_id", "release_mode", "metrics", "artifact")},
                 "release": report.get("release")}
    except Exception as exc:
        stage = {**stage, "status": "failed", "reason": type(exc).__name__}
    saved["optimization"] = stage
    db.execute("UPDATE skill_acquisitions SET decision_json=? WHERE run_id=?", (db.json_dumps(saved), run_id))
    result["optimization"] = stage
    return result


def main():
    parser = argparse.ArgumentParser(description="Verified, durable Android-agent runtime")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("POCKET_STATE_DIR", ".runtime/pocket")))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="isolated scripted smoke test, no model/device")
    sub.add_parser("demo-learning", help="scripted task -> draft -> real GEPA -> first publication; isolated temporary state")
    run = sub.add_parser("run", help="run a contracted task with the configured real model")
    run.add_argument("request", type=Path)
    run.add_argument("--run-id", default="")
    resume = sub.add_parser("resume")
    resume.add_argument("request", type=Path)
    resume.add_argument("run_id")
    for name in ("approve", "rollback"):
        command = sub.add_parser(name)
        command.add_argument("skill_id")
        if name == "rollback":
            command.add_argument("version")
    imp = sub.add_parser("import-skill")
    imp.add_argument("file", type=Path)
    imp.add_argument("--activate", action="store_true")
    opt = sub.add_parser("optimize", help="real GEPA train/dev search and one-use frozen test")
    opt.add_argument("--skill", required=True)
    opt.add_argument("--suite", type=Path, required=True)
    opt.add_argument("--max-metric-calls", type=int, default=40)
    opt.add_argument("--max-actor-calls", type=int, default=500)
    opt.add_argument("--auto-promote", action="store_true", help="only repeated local-fixture validation is pre-authorized")
    opt.add_argument("--allow-device-eval", action="store_true", help="allow explicitly resettable MCP device trials")
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--suite", type=Path, required=True)
    evaluate.add_argument("--skill", default="")
    evaluate.add_argument("--allow-device-eval", action="store_true")
    args = parser.parse_args()
    if args.command in {"demo", "demo-learning"}:
        args.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.state_dir, prefix="demo-") as directory:
            services = state_services(Path(directory))
            request = RunRequest(task=TaskSpec(objective="Enable Wi-Fi", allowed_tools=["android_tap", "android_get_ui_tree"],
                contract={"postconditions": [{"path": "/elements", "op": "contains", "value": {"id": "wifi", "value": True}}],
                          "invariants": [{"path": "/elements/0/value", "op": "unchanged"}]}),
                initial_state={"package": "local.settings", "elements": [
                    {"id": "decoy", "text": "Bluetooth", "value": False, "protected": True},
                    {"id": "wifi", "text": "Wi-Fi", "value": False}]})
            if args.command == "demo-learning":
                cases = []
                for split in ("train", "dev", "holdout"):
                    for index in range(2):
                        label = f"{split}-network-{index}"
                        state = json.loads(json.dumps(request.initial_state))
                        state["elements"][1]["text"] = label
                        cases.append({"id": f"{split}-{index}", "group": f"{split}-{index}", "split": split,
                                      "task": request.task.model_copy(update={"objective": f"Enable {label}"}),
                                      "initial_state": state})
                suite = BenchmarkSuite(name="scripted-lifecycle-demo", cases=cases)
                request.optimization = OptimizationRequest(suite=suite, max_metric_calls=16,
                                                           max_actor_calls=150, auto_promote=True)
            result = asyncio.run(execute_request(services, request, DemoActor(), "scripted-demo", "demo"))
            output = {key: result.get(key) for key in ("verified", "status", "environment", "actor_id", "tool_calls", "tokens", "learning")}
            output["scope"] = "scripted integration smoke; not LLM learning or Android performance"
            output["candidate_persisted"] = False
            if "optimization" in result:
                output["optimization"] = {key: result["optimization"].get(key) for key in
                                           ("status", "eligible", "release_mode", "reason", "candidate_id")}
                release = result["optimization"].get("release") or {}
                output["active_skill"] = {key: release.get(key) for key in ("id", "current_version", "status")}
            print(json.dumps(output, ensure_ascii=False, indent=2))
        return
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    services = state_services(args.state_dir)
    try:
        if args.command == "import-skill":
            content = args.file.read_text(encoding="utf-8")
            meta, body = parse_frontmatter(content)
            candidate = services.skills.create_candidate(name=meta.get("name", args.file.stem),
                display_name=meta.get("title", args.file.stem), description=meta.get("description", "Operator supplied baseline"),
                body=body, source_run_id="", allowed_tools=[x.strip() for x in meta.get("allowed-tools", "").split(",") if x.strip()])
            result = services.skills.approve(candidate["id"]) if args.activate else candidate
        elif args.command == "approve":
            result = services.skills.approve(args.skill_id)
        elif args.command == "rollback":
            result = services.skills.rollback(args.skill_id, args.version)
        else:
            actor, actor_id = load_actor()
            if args.command in {"run", "resume"}:
                request = RunRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
                result = asyncio.run(execute_request(services, request, actor, actor_id,
                    args.run_id or "run_" + uuid.uuid4().hex, resume=args.command == "resume"))
            else:
                suite = BenchmarkSuite.model_validate_json(args.suite.read_text(encoding="utf-8"))
                backend_factory = None
                if suite.environment == "mcp_device":
                    if not args.allow_device_eval:
                        raise ValueError("pass --allow-device-eval for repeated device actions")
                    async def backend_factory(case, run_id):
                        return await make_backend(services, RunRequest(task=case.task, backend="mcp"), run_id)
                if args.command == "optimize":
                    result = GEPASkillOptimizer(services, actor, actor.invoke, actor_id=actor_id,
                        max_actor_calls=args.max_actor_calls, backend_factory=backend_factory).optimize(args.skill, suite,
                            max_metric_calls=args.max_metric_calls, auto_promote=args.auto_promote)
                else:
                    runner = BenchmarkRunner(services, actor, actor_id=actor_id,
                                             checkpoint_path=args.state_dir / "evaluation-checkpoints.db",
                                             environment=suite.environment, backend_factory=backend_factory)
                    trials = asyncio.run(runner.evaluate([c for c in suite.cases if c.split == "holdout"], skill_body(services, args.skill)))
                    result = {"environment": suite.environment, "metrics": summarize(trials), "trials": trials}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
