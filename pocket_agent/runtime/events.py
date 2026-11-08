from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Callable


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    """In-process typed event stream shared by CLI, browser, and FastAPI."""

    def __init__(self, history_size: int = 200):
        self._history: dict[str, deque[RunEvent]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent]]] = defaultdict(set)
        self._sinks: list[Callable[[RunEvent], None]] = []

    def add_sink(self, sink: Callable[[RunEvent], None]) -> None:
        self._sinks.append(sink)

    async def publish(
        self, run_id: str, event_type: str, data: dict[str, Any] | None = None
    ) -> RunEvent:
        event = RunEvent(run_id=run_id, type=event_type, data=data or {})
        self._history[run_id].append(event)
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:
                continue
        for queue in list(self._subscribers.get(run_id, set())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return event

    def history(self, run_id: str) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._history.get(run_id, ())]

    async def subscribe(self, run_id: str) -> AsyncIterator[RunEvent]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=100)
        self._subscribers[run_id].add(queue)
        try:
            for event in self._history.get(run_id, ()):
                yield event
            while True:
                yield await queue.get()
        finally:
            self._subscribers[run_id].discard(queue)
