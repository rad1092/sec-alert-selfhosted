from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import Settings
from app.services.broker import BrokerPriority, SecRequestBroker

EASTERN_TZ = ZoneInfo("America/New_York")


class SchedulerService:
    def __init__(self, settings: Settings, broker: SecRequestBroker) -> None:
        self.settings = settings
        self.broker = broker
        self.scheduler = BackgroundScheduler(timezone=EASTERN_TZ)
        self.started = False
        self.last_enqueued_at: dict[str, datetime] = {}

    def enqueue_live_8k(self) -> None:
        self._enqueue_orchestration(
            task_name="orchestrate-live-poll-8k",
            priority=BrokerPriority.P2,
            job_key="orchestrate:live:8k",
            source_name="scheduler-live-8k",
        )

    def enqueue_live_form4(self) -> None:
        self._enqueue_orchestration(
            task_name="orchestrate-live-poll-form4",
            priority=BrokerPriority.P2,
            job_key="orchestrate:live:form4",
            source_name="scheduler-live-form4",
        )

    def enqueue_repair_recent(self) -> None:
        self._enqueue_orchestration(
            task_name="orchestrate-repair-recent",
            priority=BrokerPriority.P3,
            job_key="orchestrate:repair:recent",
            source_name="scheduler-repair-recent",
        )

    def _enqueue_orchestration(
        self,
        *,
        task_name: str,
        priority: BrokerPriority,
        job_key: str,
        source_name: str,
    ) -> None:
        enqueue_result = self.broker.enqueue(
            task_name=task_name,
            priority=priority,
            job_key=job_key,
            source_name=source_name,
            payload={"scheduled": True},
        )
        if enqueue_result.accepted:
            self.last_enqueued_at[source_name] = datetime.now(tz=UTC)
            self.broker.mark_source_success(source_name)

    def start(self) -> None:
        if not self.settings.scheduler_enabled or self.started:
            return
        self.scheduler.add_job(
            self.enqueue_live_8k,
            trigger="interval",
            seconds=self.settings.sec_poll_interval_seconds,
            id="live-poll-8k",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.enqueue_live_form4,
            trigger="interval",
            seconds=self.settings.sec_poll_interval_seconds,
            id="live-poll-form4",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.enqueue_repair_recent,
            trigger="cron",
            day_of_week="mon-fri",
            hour=23,
            minute=30,
            id="repair-recent",
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
            "last_enqueued_at": {
                key: value.isoformat() for key, value in self.last_enqueued_at.items()
            },
            "jobs": jobs,
        }
