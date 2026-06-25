from __future__ import annotations

from sqlmodel import Session

from avarch.adapters.manual_validation import SchedulerManualValidationWorker
from avarch.adapters.promotion import PromotionWorkflowAdapter
from avarch.adapters.scheduler_process import SchedulerProcessAdapter
from avarch.adapters.scheduler_run import SchedulerRuntimeAdapter
from avarch.adapters.sqlite.enqueue import SqliteEnqueueStore
from avarch.adapters.sqlite.job_control_store import SqliteJobControlStore
from avarch.adapters.sqlite.job_views import SqliteJobViewStore
from avarch.adapters.sqlite.queue_control import SqliteQueueControlStore, SqliteQueueRetryStore
from avarch.adapters.sqlite.scheduler_control import SqliteSchedulerControlStore
from avarch.adapters.sqlite.scheduler_status import SqliteSchedulerStatusStore
from avarch.config import AppConfig


def enqueue_store(session: Session) -> SqliteEnqueueStore:
    return SqliteEnqueueStore(session)


def job_control_store(session: Session) -> SqliteJobControlStore:
    return SqliteJobControlStore(session)


def job_view_store(session: Session) -> SqliteJobViewStore:
    return SqliteJobViewStore(session)


def queue_control_store(session: Session) -> SqliteQueueControlStore:
    return SqliteQueueControlStore(session)


def queue_retry_store(session: Session, *, config: AppConfig) -> SqliteQueueRetryStore:
    return SqliteQueueRetryStore(session, config=config)


def scheduler_control_store(session: Session) -> SqliteSchedulerControlStore:
    return SqliteSchedulerControlStore(session)


def scheduler_status_store(session: Session) -> SqliteSchedulerStatusStore:
    return SqliteSchedulerStatusStore(session)


def manual_validation_worker() -> SchedulerManualValidationWorker:
    return SchedulerManualValidationWorker()


def promotion_workflow() -> PromotionWorkflowAdapter:
    return PromotionWorkflowAdapter()


def scheduler_process_controller() -> SchedulerProcessAdapter:
    return SchedulerProcessAdapter()


def scheduler_runner() -> SchedulerRuntimeAdapter:
    return SchedulerRuntimeAdapter()
