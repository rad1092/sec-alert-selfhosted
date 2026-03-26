from __future__ import annotations

from app.services.broker import BrokerPriority, SecRequestBroker


def test_broker_priority_ordering():
    broker = SecRequestBroker(rate_limit_rps=2)
    broker.enqueue(task_name="repair", priority=BrokerPriority.P3, job_key="repair-1")
    broker.enqueue(task_name="detail", priority=BrokerPriority.P1, job_key="detail-1")
    broker.enqueue(task_name="poll", priority=BrokerPriority.P2, job_key="poll-1")

    assert broker.pop_next().job_key == "detail-1"
    assert broker.pop_next().job_key == "poll-1"
    assert broker.pop_next().job_key == "repair-1"


def test_broker_duplicate_enqueue_collapse():
    broker = SecRequestBroker(rate_limit_rps=2)
    first = broker.enqueue(task_name="poll", priority=BrokerPriority.P2, job_key="poll-aapl")
    second = broker.enqueue(task_name="poll", priority=BrokerPriority.P2, job_key="poll-aapl")
    assert first.accepted is True
    assert second.accepted is False
    assert broker.snapshot()["backlog_size"] == 1


def test_broker_manual_trigger_coalescing():
    broker = SecRequestBroker(rate_limit_rps=2)
    assert broker.start_run("manual-ingest") is True
    assert broker.start_run("manual-ingest") is False
    broker.finish_run("manual-ingest")
    assert broker.start_run("manual-ingest") is True


def test_broker_records_status_counts():
    broker = SecRequestBroker(rate_limit_rps=2)
    broker.record_http_status(403)
    broker.record_http_status(429)
    snapshot = broker.snapshot()
    assert snapshot["recent_403_count"] == 1
    assert snapshot["recent_429_count"] == 1


def test_broker_dedupes_inflight_job_keys():
    broker = SecRequestBroker(rate_limit_rps=2)
    first = broker.enqueue(task_name="poll", priority=BrokerPriority.P2, job_key="poll-aapl")
    assert first.accepted is True
    job = broker.pop_next()
    assert job is not None
    duplicate = broker.enqueue(task_name="poll", priority=BrokerPriority.P2, job_key="poll-aapl")
    assert duplicate.accepted is False
    broker.complete(job.job_key)
    accepted_again = broker.enqueue(
        task_name="poll",
        priority=BrokerPriority.P2,
        job_key="poll-aapl",
    )
    assert accepted_again.accepted is True


def test_broker_reports_higher_priority_work():
    broker = SecRequestBroker(rate_limit_rps=2)
    broker.enqueue(task_name="repair", priority=BrokerPriority.P3, job_key="repair-1")
    broker.enqueue(task_name="poll", priority=BrokerPriority.P2, job_key="poll-1")
    assert broker.has_queued_higher_priority_than(BrokerPriority.P3) is True
    assert broker.has_queued_higher_priority_than(BrokerPriority.P2) is False
