from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_conversation_id: ContextVar[str] = ContextVar("conversation_id", default="")
_run_id: ContextVar[str] = ContextVar("run_id", default="")
_permission_mode: ContextVar[str] = ContextVar("permission_mode", default="default")
_pending_tasks: ContextVar[list[dict] | None] = ContextVar(
    "pending_tasks", default=None
)


def get_conversation_id() -> str:
    return _conversation_id.get()


def set_conversation_id(value: str) -> None:
    _conversation_id.set(value or "")


def get_run_id() -> str:
    return _run_id.get()


def set_run_id(value: str) -> None:
    _run_id.set(value or "")


def get_permission_mode() -> str:
    return _permission_mode.get()


def set_permission_mode(value: str) -> None:
    _permission_mode.set(value or "default")


def add_pending_task(task: dict) -> None:
    tasks = list(_pending_tasks.get() or [])
    tasks.append(task)
    _pending_tasks.set(tasks)


def consume_pending_tasks() -> list[dict]:
    tasks = list(_pending_tasks.get() or [])
    _pending_tasks.set([])
    return tasks


@contextmanager
def runtime_context(
    *,
    conversation_id: str = "",
    run_id: str = "",
    permission_mode: str = "default",
) -> Iterator[None]:
    conversation_token = _conversation_id.set(conversation_id)
    run_token = _run_id.set(run_id)
    permission_token = _permission_mode.set(permission_mode)
    pending_token = _pending_tasks.set([])
    try:
        yield
    finally:
        _pending_tasks.reset(pending_token)
        _permission_mode.reset(permission_token)
        _run_id.reset(run_token)
        _conversation_id.reset(conversation_token)
