"""In-process async job queue for valuation execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("avis.api.valuation_jobs")

ProcessFn = Callable[[object, int], None]
RecoverFn = Callable[[object], list[int]]


class ValuationJobQueue:
    """Simple in-process worker queue backed by durable DB run state."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        process_fn: ProcessFn,
        recover_fn: RecoverFn,
    ) -> None:
        self._session_factory = session_factory
        self._process_fn = process_fn
        self._recover_fn = recover_fn
        self._queue: asyncio.Queue[int | None] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._recover_pending_runs()
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        await self._queue.put(None)
        if self._worker_task is not None:
            with suppress(asyncio.CancelledError):
                await self._worker_task

    async def enqueue(self, val_run_id: int) -> None:
        await self._queue.put(val_run_id)

    async def _recover_pending_runs(self) -> None:
        async with self._session_factory() as session:
            queued_ids = await session.run_sync(self._recover_fn)
            await session.commit()
        for val_run_id in queued_ids:
            await self._queue.put(val_run_id)

    async def _worker_loop(self) -> None:
        while True:
            val_run_id = await self._queue.get()
            if val_run_id is None:
                self._queue.task_done()
                break
            try:
                async with self._session_factory() as session:
                    await session.run_sync(self._process_fn, val_run_id)
                    await session.commit()
            except Exception:
                logger.exception("valuation_job_failed val_run_id=%s", val_run_id)
            finally:
                self._queue.task_done()
