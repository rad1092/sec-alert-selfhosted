from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import Settings
from app.services.broker import BrokerPriority, SecRequestBroker


class SchedulerService:
    def __init__(self, settings: Settings, broker: SecRequestBroker) -> None:
        self.settings = settings
        self.broker = broker
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.last_heartbeat_at: datetime | None = None
        self.started = False

    def _heartbeat(self) -> None:
        self.last_heartbeat_at = datetime.now(tz=UTC)
        self.broker.mark_source_success("scheduler-heartbeat")
        self.broker.enqueue(
            task_name="live-poll-placeholder",
            priority=BrokerPriority.P2,
            job_key="scheduler-heartbeat-live-poll",
            source_name="scheduler-heartbeat",
            payload={"scheduled": True},
        )

    def start(self) -> None:
        if not self.settings.scheduler_enabled or self.started:
            return
        self.scheduler.add_job(
            self._heartbeat,
            trigger="interval",
            seconds=self.settings.sec_poll_interval_seconds,
            id="scheduler-heartbeat",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        self.started = True

    def shutdown(self) -> None:
        if self.started:
            self.scheduler.shutdown(wait=False)
        self.started = False

    def snapshot(self) -> dict[str, Any]:
        jobs = []
        if self.started:
            for job in self.scheduler.get_jobs():
                jobs.append(
                    {
                        "id": job.id,
                        "next_run_time": job.next_run_time.isoformat()
                        if job.next_run_time
                        else None,
                    },
                )
        return {
            "enabled": self.settings.scheduler_enabled,
            "started": self.started,
            "last_heartbeat_at": self.last_heartbeat_at.isoformat()
            if self.last_heartbeat_at
            else None,
            "jobs": jobs,
        }
