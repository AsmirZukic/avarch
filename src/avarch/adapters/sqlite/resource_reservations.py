from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import JobAttempt, ResourceReservation
from avarch.domain.jobs import AttemptStatus, ResourceClass
from avarch.domain.scheduler import JobResourceReservation

ACTIVE_RESERVATION_STATUS = "active"
RELEASED_RESERVATION_STATUS = "released"


class ResourceReservationPersistenceError(RuntimeError):
    pass


def acquire_attempt_reservation(
    session: Session,
    *,
    job_id: int,
    attempt_id: int,
    scheduler_session_id: int | None,
    resource_class: ResourceClass,
    reservation: JobResourceReservation,
    now: datetime,
) -> ResourceReservation:
    existing = reservation_for_attempt(session, attempt_id=attempt_id)
    if existing is not None:
        return existing
    row = ResourceReservation(
        job_id=job_id,
        attempt_id=attempt_id,
        scheduler_session_id=scheduler_session_id,
        resource_class=resource_class,
        cpu_reserved=reservation.cpu,
        memory_bytes_reserved=reservation.memory_bytes,
        exclusive=reservation.exclusive,
        status=ACTIVE_RESERVATION_STATUS,
        created_at=now,
    )
    session.add(row)
    session.flush()
    if row.id is None:
        raise ResourceReservationPersistenceError("Resource reservation id was not assigned.")
    return row


def release_attempt_reservation(
    session: Session,
    *,
    attempt_id: int,
    now: datetime,
    reason: str,
) -> bool:
    row = reservation_for_attempt(session, attempt_id=attempt_id)
    if row is None or row.status == RELEASED_RESERVATION_STATUS:
        return False
    row.status = RELEASED_RESERVATION_STATUS
    row.released_at = now
    row.release_reason = reason
    session.add(row)
    return True


def reservation_for_attempt(
    session: Session,
    *,
    attempt_id: int,
) -> ResourceReservation | None:
    return session.exec(
        select(ResourceReservation).where(ResourceReservation.attempt_id == attempt_id)
    ).first()


def active_reservations(session: Session) -> list[ResourceReservation]:
    return list(
        session.exec(
            select(ResourceReservation)
            .where(ResourceReservation.status == ACTIVE_RESERVATION_STATUS)
            .order_by(col(ResourceReservation.created_at).asc(), col(ResourceReservation.id).asc())
        ).all()
    )


def reconcile_stale_reservations(session: Session, *, now: datetime) -> int:
    released = 0
    for reservation in active_reservations(session):
        attempt = session.get(JobAttempt, reservation.attempt_id)
        if attempt is not None and AttemptStatus(attempt.status) == AttemptStatus.RUNNING:
            continue
        if release_attempt_reservation(
            session,
            attempt_id=reservation.attempt_id,
            now=now,
            reason="reconciled_stale_attempt",
        ):
            released += 1
    return released
