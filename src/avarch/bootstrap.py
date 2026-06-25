from __future__ import annotations

from sqlmodel import Session

from avarch.adapters.sqlite.enqueue import SqliteEnqueueStore
from avarch.adapters.sqlite.job_control_store import SqliteJobControlStore
from avarch.adapters.sqlite.queue_control import SqliteQueueControlStore, SqliteQueueRetryStore
from avarch.adapters.sqlite.scheduler_control import SqliteSchedulerControlStore
from avarch.config import AppConfig


def enqueue_store(session: Session) -> SqliteEnqueueStore:
    return SqliteEnqueueStore(session)


def job_control_store(session: Session) -> SqliteJobControlStore:
    return SqliteJobControlStore(session)


def queue_control_store(session: Session) -> SqliteQueueControlStore:
    return SqliteQueueControlStore(session)


def queue_retry_store(session: Session, *, config: AppConfig) -> SqliteQueueRetryStore:
    return SqliteQueueRetryStore(session, config=config)


def scheduler_control_store(session: Session) -> SqliteSchedulerControlStore:
    return SqliteSchedulerControlStore(session)
