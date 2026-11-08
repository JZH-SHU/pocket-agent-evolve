"""Explicit, bounded real-model smoke using only the synthetic public fixture."""
import argparse
import asyncio
import json
from pathlib import Path
import uuid

from dotenv import load_dotenv

from pocket_agent.cli import RunRequest, execute_request, load_actor, state_services


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    run_id = "live_smoke_" + uuid.uuid4().hex
    services = state_services(root / ".runtime" / run_id)
    request = RunRequest.model_validate_json((root / "examples/settings_task.json").read_text(encoding="utf-8"))
    request.task.max_steps = 2
    actor, actor_id = load_actor()
    result = asyncio.run(execute_request(services, request, actor, actor_id, run_id))
    summary = {key: result.get(key) for key in ("verified", "status", "environment", "tool_calls", "tokens", "elapsed_ms", "error")}
    summary.update(scope="One real-model synthetic-fixture smoke; not Android or learning evidence", actor="operator-configured model")
    (root / ".runtime/live_model_smoke.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
