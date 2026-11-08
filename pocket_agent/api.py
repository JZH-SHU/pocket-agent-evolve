"""Authenticated task API; SSE/HTTP connections do not own execution lifetime."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets
import uuid

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from pocket_agent.cli import RunRequest, load_actor, make_backend, skill_body, state_services
from pocket_agent.runtime.executor import DurableExecutor
from pocket_agent.storage.database import now_ms


class ApprovalRequest(BaseModel):
    approved: bool


def create_app(state_dir: Path | None = None, *, actor_factory=load_actor) -> FastAPI:
    directory = state_dir or Path(os.environ.get("POCKET_STATE_DIR", ".runtime/pocket"))
    services = state_services(directory)
    pending: dict[str, asyncio.Task] = {}
    bearer = HTTPBearer(auto_error=False)

    async def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        token = os.environ.get("POCKET_ADMIN_TOKEN", "")
        if not token:
            raise HTTPException(503, "Set POCKET_ADMIN_TOKEN before enabling the task API")
        if not credentials or not secrets.compare_digest(credentials.credentials, token):
            raise HTTPException(401, "Invalid bearer token")

    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in list(pending.values()):
            task.cancel()
        await asyncio.gather(*pending.values(), return_exceptions=True)

    app = FastAPI(title="PocketAgent Evolve", version="0.3.0", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.3.0"}

    async def schedule(request: RunRequest, run_id: str, resume: bool):
        if run_id in pending and not pending[run_id].done():
            raise HTTPException(409, "Task already running")
        try:
            actor, actor_id = actor_factory()
            backend = await make_backend(services, request, run_id)
            body = skill_body(services, request.skill_id, request.task.allowed_tools)
            executor = DurableExecutor(services, backend, actor, actor_id=actor_id,
                                      checkpoint_path=directory / "checkpoints.db")
            if not resume:
                executor.submit(request.task, skill=body, run_id=run_id)
            elif not services.runs.get_run(run_id):
                raise HTTPException(404, "Unknown task")
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc

        async def execute():
            try:
                await executor.run(request.task, skill=body, run_id=run_id, resume=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Preserve the job and evidence; never auto-submit the task again.
                services.database.execute("UPDATE runs SET error=?, updated_at=? WHERE id=?",
                    (f"worker:{type(exc).__name__}", now_ms(), run_id))
            finally:
                pending.pop(run_id, None)

        pending[run_id] = asyncio.create_task(execute())
        return {"run_id": run_id, "status": "accepted"}

    @app.post("/v3/tasks", status_code=202, dependencies=[Depends(authorize)])
    async def submit(request: RunRequest):
        return await schedule(request, "run_" + uuid.uuid4().hex, False)

    @app.post("/v3/tasks/{run_id}/resume", status_code=202, dependencies=[Depends(authorize)])
    async def resume(run_id: str, request: RunRequest):
        return await schedule(request, run_id, True)

    @app.get("/v3/tasks/{run_id}", dependencies=[Depends(authorize)])
    async def get_task(run_id: str):
        run = services.runs.get_run(run_id)
        if not run:
            raise HTTPException(404, "Unknown task")
        run["actions"] = services.database.fetch_all("SELECT * FROM action_ledger WHERE run_id=?", (run_id,))
        return run

    @app.post("/v3/approvals/{approval_id}", dependencies=[Depends(authorize)])
    async def approve(approval_id: str, request: ApprovalRequest):
        try:
            return services.policy.decide(approval_id, request.approved)
        except KeyError as exc:
            raise HTTPException(404, "Unknown approval") from exc

    return app
