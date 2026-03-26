from __future__ import annotations

import heapq
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class BrokerPriority(IntEnum):
    P1 = 1
    P2 = 2
    P3 = 3


@dataclass(order=True)
class QueuedJob:
    priority: int
    sequence: int
    created_monotonic: float
    job_key: str = field(compare=False)
    task_name: str = field(compare=False)
    source_name: str | None = field(compare=False, default=None)
    payload: dict[str, Any] = field(compare=False, default_factory=dict)


@dataclass
class EnqueueResult:
    accepted: bool
    job_key: str
    reason: str


class SecRequestBroker:
    def __init__(self, rate_limit_rps: float) -> None:
        self.rate_limit_rps = rate_limit_rps
        self._tokens = rate_limit_rps
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._queue: list[QueuedJob] = []
        self._job_keys: set[str] = set()
        self._inflight_job_keys: set[str] = set()
        self._sequence = 0
        self._active_runs: set[str] = set()
        self._recent_403_count = 0
        self._recent_429_count = 0
        self._last_successful_poll: dict[str, float] = {}

    def enqueue(
        self,
        *,
        task_name: str,
        priority: BrokerPriority,
        job_key: str,
        source_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> EnqueueResult:
        with self._lock:
            if job_key in self._job_keys or job_key in self._inflight_job_keys:
                return EnqueueResult(accepted=False, job_key=job_key, reason="duplicate")
            self._sequence += 1
            heapq.heappush(
                self._queue,
                QueuedJob(
                    priority=int(priority),
                    sequence=self._sequence,
                    created_monotonic=time.monotonic(),
                    job_key=job_key,
                    task_name=task_name,
                    source_name=source_name,
                    payload=payload or {},
                ),
            )
            self._job_keys.add(job_key)
            return EnqueueResult(accepted=True, job_key=job_key, reason="queued")

    def pop_next(self) -> QueuedJob | None:
        with self._lock:
            if not self._queue:
                return None
            job = heapq.heappop(self._queue)
            self._job_keys.discard(job.job_key)
            self._inflight_job_keys.add(job.job_key)
            return job

    def complete(self, job_key: str) -> None:
        with self._lock:
            self._inflight_job_keys.discard(job_key)

    def can_issue_request(self, now: float | None = None) -> bool:
        with self._lock:
            current = time.monotonic() if now is None else now
            elapsed = max(0.0, current - self._last_refill)
            self._tokens = min(
                self.rate_limit_rps,
                self._tokens + (elapsed * self.rate_limit_rps),
            )
            self._last_refill = current
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def start_run(self, run_key: str) -> bool:
        with self._lock:
            if run_key in self._active_runs:
                return False
            self._active_runs.add(run_key)
            return True

    def finish_run(self, run_key: str) -> None:
        with self._lock:
            self._active_runs.discard(run_key)

    def mark_source_success(self, source_name: str) -> None:
        with self._lock:
            self._last_successful_poll[source_name] = time.time()

    def record_http_status(self, status_code: int) -> None:
        with self._lock:
            if status_code == 403:
                self._recent_403_count += 1
            if status_code == 429:
                self._recent_429_count += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            oldest_age = 0.0
            if self._queue:
                oldest_age = now - min(job.created_monotonic for job in self._queue)
            queued_by_priority = {
                "P1": sum(1 for job in self._queue if job.priority == BrokerPriority.P1),
                "P2": sum(1 for job in self._queue if job.priority == BrokerPriority.P2),
                "P3": sum(1 for job in self._queue if job.priority == BrokerPriority.P3),
            }
            return {
                "backlog_size": len(self._queue),
                "oldest_queued_age_seconds": round(oldest_age, 3),
                "queued_by_priority": queued_by_priority,
                "recent_403_count": self._recent_403_count,
                "recent_429_count": self._recent_429_count,
                "active_runs": sorted(self._active_runs),
                "inflight_job_keys": sorted(self._inflight_job_keys),
                "last_successful_poll": dict(self._last_successful_poll),
            }

    def has_queued_higher_priority_than(self, priority: BrokerPriority) -> bool:
        with self._lock:
            return any(job.priority < int(priority) for job in self._queue)
