"""python -m pocket_agent.skills.rsi: isolated demo or opt-in LLM evolution."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from pocket_agent.bootstrap import get_runtime_services, reset_runtime_services

from .evaluation import TaskSuite, UI_TOOLS
from .evolution import SkillEvolution
from .frontmatter import parse_frontmatter


class DemoModel:
    """Scripted test double, NOT evidence of LLM learning or device reliability."""

    async def ainvoke(self, prompt):
        data = json.loads(prompt)
        if "schema" in data:
            value = {
                "display_name": "按目标文字唯一定位开关",
                "description": "先观察当前界面，再按用户指定的文字唯一定位开关并核验结果。",
                "use_cases": ["切换模拟界面的指定开关"], "allowed_tools": sorted(UI_TOOLS),
                "steps": [
                    {"order": 1, "title": "观察界面", "action": "读取当前界面的所有开关和值。",
                     "tool": "android_get_ui_tree", "verification": "确认当前元素列表可读。"},
                    {"order": 2, "title": "定位目标", "action": "按目标文字唯一定位，不点击第一个元素。",
                     "tool": "android_find_elements", "verification": "目标文字对应唯一元素。"},
                    {"order": 3, "title": "切换核验", "action": "点击目标一次后重新观察结果。",
                     "tool": "android_tap", "verification": "目标值变为用户要求的状态，其他开关未改变。"},
                ], "success_criteria": ["目标开关正确，其他值不变"],
                "failure_recovery": ["歧义时停止并重新观察"], "safety_notes": ["禁止点击受保护的控件"],
            }
        else:
            trace = data["trace"]
            if not trace:
                value = {"tool": "android_get_ui_tree", "arguments": {}}
            elif any(event.get("tool") == "android_tap" for event in trace):
                value = {"tool": "finish"}
            else:
                elements = trace[-1]["result"]["elements"]
                # Deliberately poor seed, then scripted response to the candidate's instructions.
                target = next((e for e in elements if e["text"] == data["objective"]), None) if (
                    "按目标文字唯一定位" in data["skill"]) else elements[0]
                value = {"tool": "android_tap", "arguments": {"element_id": target["id"]}} if target else {"tool": "finish"}
        return SimpleNamespace(content=json.dumps(value, ensure_ascii=False))


def demo_suite() -> TaskSuite:
    return TaskSuite.model_validate({"name": "scripted-toggle-demo-v1", "cases": [
        {"id": f"{split}-{index}", "group": f"{split}-layout-{index}", "split": split,
         "objective": f"target-{split}-{index}", "elements": [
             {"id": "decoy", "text": "unrelated control"},
             {"id": "target", "text": f"target-{split}-{index}"}], "expected": {"target": True}}
        for split in ("train", "dev", "holdout") for index in (1, 2)
    ]})


async def demo(root: Path) -> dict:
    services = get_runtime_services(root / "pocket.db", root)
    baseline = services.skills.create_candidate(
        name="toggle-demo", display_name="模拟开关基线", description="故意先点击第一个控件的演示基线",
        body="# 模拟基线\n读取界面，点击第一个元素。", source_run_id="", allowed_tools=sorted(UI_TOOLS),
    )
    active = services.skills.approve(baseline["id"])
    report = await SkillEvolution(services.skills, services.artifacts, DemoModel(),
        model_id="scripted-demo (not a real LLM)", iterations=2).evolve(active["id"], demo_suite())
    # Demo stops at awaiting approval; tests exercise release and rollback separately.
    return report


def main():
    parser = argparse.ArgumentParser(description="受控 Skill RSI：离线评测后人工启用")
    commands = parser.add_subparsers(dest="command", required=True)
    demo_parser = commands.add_parser("demo", help="脚本模型 + 模拟 UI，隔离临时数据库，无需 API key")
    demo_parser.add_argument("--output", type=Path, required=True, help="保存演示报告；不修改项目技能")
    evolve_parser = commands.add_parser("evolve", help="调用配置的 LLM，迭代现有 Skill 候选或 active 版本")
    source_group = evolve_parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--skill", help="现有候选或 active Skill ID")
    source_group.add_argument("--seed", type=Path, help="导入 SKILL.md 为未启用种子")
    evolve_parser.add_argument("--suite", type=Path, required=True)
    evolve_parser.add_argument("--iterations", type=int, default=2)
    evolve_parser.add_argument("--repetitions", type=int, default=2)
    evolve_parser.add_argument("--max-calls", type=int, default=160)
    approve_parser = commands.add_parser("approve", help="人工发布评测通过的候选")
    approve_parser.add_argument("skill_id")
    rollback_parser = commands.add_parser("rollback", help="人工回滚 active Skill 版本")
    rollback_parser.add_argument("skill_id")
    rollback_parser.add_argument("version")
    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory(prefix="pocket-rsi-") as tmp:
            report = asyncio.run(demo(Path(tmp)))
            # Durable copies survive cleanup of the isolated demo store.
            report.pop("artifact", None)
            report.update(demo=True, candidate_persisted=False)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        reset_runtime_services()
        print(json.dumps({"environment": report["environment"], "eligible": report["eligible"],
                          "model_id": report["model_id"], "report": str(args.output.resolve())}, ensure_ascii=False))
        return
    root = Path(__file__).resolve().parents[2]
    # Configuration is only loaded for explicit live operations, never for the demo.
    from dotenv import load_dotenv
    load_dotenv(root / ".env", override=False)
    services = get_runtime_services()
    if args.command == "approve":
        result = services.skills.approve(args.skill_id)
    elif args.command == "rollback":
        result = services.skills.rollback(args.skill_id, args.version)
    else:
        from langchain_openai import ChatOpenAI
        model_name = os.getenv("EXECUTOR_MODEL") or os.getenv("LLM_MODEL")
        api_key = os.getenv("EXECUTOR_API_KEY") or os.getenv("LLM_API_KEY")
        if not model_name or not api_key:
            parser.error("evolve requires LLM_MODEL/LLM_API_KEY (or EXECUTOR_MODEL/EXECUTOR_API_KEY)")
        model = ChatOpenAI(model=model_name, api_key=api_key,
            base_url=os.getenv("EXECUTOR_LLM_BASE_URL") or os.getenv("LLM_BASE_URL") or None,
            temperature=0, max_tokens=4000, max_retries=0, timeout=30)
        suite = TaskSuite.model_validate_json(args.suite.read_text(encoding="utf-8"))
        skill_id = args.skill
        if args.seed:
            meta, body = parse_frontmatter(args.seed.read_text(encoding="utf-8"))
            tools = {tool.strip() for tool in meta.get("allowed-tools", "").split(",") if tool.strip()}
            if not tools or not tools <= UI_TOOLS:
                parser.error("seed must declare only supported simulated UI tools")
            seed = services.skills.create_candidate(name=meta.get("name", args.seed.stem),
                display_name=meta.get("title", args.seed.stem), description=meta.get("description", "RSI imported seed"),
                body=body, source_run_id="", allowed_tools=sorted(tools), evidence=[{"type": "manual_seed"}])
            skill_id = seed["id"]
        result = asyncio.run(SkillEvolution(services.skills, services.artifacts, model, model_id=model_name,
            iterations=args.iterations, repetitions=args.repetitions, max_calls=args.max_calls).evolve(skill_id, suite))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
