from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from app.services.broker import QueuedJob, SecRequestBroker

logger = logging.getLogger(__name__)

JobHandler = Callable[[QueuedJob], None]


class BrokerWorker:
    def __init__(self, broker: SecRequestBroker) -> None:
        self.broker = broker
        self._handlers: dict[str, JobHandler] = {}
        self._stop_event = threading.Event()
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active_job_key: str | None = None

    def register_handler(self, task_name: str, handler: JobHandler) -> None:
        self._handlers[task_name] = handler

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="sec-alert-broker-worker",
            daemon=True,
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        with self._lock:
            self._active_job_key = None
        self._idle_event.set()

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            broker_snapshot = self.broker.snapshot()
            if (
                self._idle_event.wait(timeout=0.05)
                and broker_snapshot["backlog_size"] == 0
                and snapshot["active_job_key"] is None
            ):
                return True
        return False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "started": self._thread is not None and self._thread.is_alive(),
                "active_job_key": self._active_job_key,
                "idle": self._idle_event.is_set(),
            }

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._idle_event.clear()
            job = self.broker.pop_next()
            if job is None:
                self._idle_event.set()
                time.sleep(0.05)
                continue

            handler = self._handlers.get(job.task_name)
            if handler is None:
                logger.warning("No handler registered for broker task '%s'.", job.task_name)
                continue

            self._idle_event.clear()
            with self._lock:
                self._active_job_key = job.job_key
            try:
                handler(job)
            except Exception:
                logger.exception("Broker job '%s' failed.", job.job_key)
            finally:
                with self._lock:
                    self._active_job_key = None
