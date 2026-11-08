from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class InteractionRequest:
    source: str
    text: str
    conversation_id: str
    future: asyncio.Future[Any]


class InteractionQueue:
    """Serializes CLI and browser requests through one Agent runtime."""

    def __init__(
        self,
        handler: Callable[[InteractionRequest], Awaitable[Any]],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        self.handler = handler
        self.loop = loop
        self.queue: asyncio.Queue[InteractionRequest | None] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._current_handler: asyncio.Task | None = None

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self.loop = self.loop or asyncio.get_running_loop()
            self._worker = self.loop.create_task(self._run())

    async def submit(
        self, source: str, text: str, conversation_id: str
    ) -> Any:
        self.start()
        future = asyncio.get_running_loop().create_future()
        await self.queue.put(
            InteractionRequest(source, text, conversation_id, future)
        )
        return await future

    async def enqueue(
        self, source: str, text: str, conversation_id: str
    ) -> None:
        self.start()
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        await self.queue.put(
            InteractionRequest(source, text, conversation_id, future)
        )

    def submit_threadsafe(
        self, source: str, text: str, conversation_id: str
    ):
        if self.loop is None:
            raise RuntimeError("interaction queue has not been started")
        return asyncio.run_coroutine_threadsafe(
            self.enqueue(source, text, conversation_id), self.loop
        )

    async def _run(self) -> None:
        while True:
            request = await self.queue.get()
            if request is None:
                return
            self._current_handler = asyncio.create_task(self.handler(request))
            try:
                result = await self._current_handler
                if not request.future.done():
                    request.future.set_result(result)
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.cancel()
            except Exception as exc:
                if not request.future.done():
                    request.future.set_exception(exc)
            finally:
                self._current_handler = None

    def cancel_current(self) -> bool:
        if self._current_handler and not self._current_handler.done():
            self._current_handler.cancel()
            return True
        return False

    async def stop(self) -> None:
        if self._worker and not self._worker.done():
            self.cancel_current()
            while True:
                try:
                    pending = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if pending is not None and not pending.future.done():
                    pending.future.cancel()
            await self.queue.put(None)
            await self._worker
