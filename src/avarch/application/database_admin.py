from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

ExceptionTypes = tuple[type[Exception], ...]


class DatabaseAdminError(RuntimeError):
    pass


class DatabaseAdmin(Protocol):
    def verify_revision(self) -> None: ...

    def current_revision(self) -> str | None: ...

    def check_connection(self) -> None: ...

    def table_names(self) -> set[str]: ...


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    missing_tables: frozenset[str]


def current_database_revision(
    admin: DatabaseAdmin,
    *,
    admin_errors: ExceptionTypes = (),
) -> str | None:
    try:
        admin.verify_revision()
        return admin.current_revision()
    except admin_errors as exc:
        raise DatabaseAdminError(str(exc)) from exc


def check_database_health(
    admin: DatabaseAdmin,
    *,
    required_tables: set[str] | frozenset[str],
    admin_errors: ExceptionTypes = (),
) -> DatabaseHealth:
    try:
        admin.check_connection()
        tables = admin.table_names()
    except admin_errors as exc:
        raise DatabaseAdminError(str(exc)) from exc
    return DatabaseHealth(missing_tables=frozenset(required_tables - tables))
