from __future__ import annotations

from sqlalchemy import inspect

from avarch.adapters.sqlite.db import create_db_engine, verify_database_revision
from avarch.adapters.sqlite.migrations import get_current_revision


class SqliteDatabaseAdmin:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def verify_revision(self) -> None:
        verify_database_revision(create_db_engine(self._database_url))

    def current_revision(self) -> str | None:
        return get_current_revision(self._database_url)

    def check_connection(self) -> None:
        engine = create_db_engine(self._database_url)
        with engine.connect():
            pass

    def table_names(self) -> set[str]:
        return set(inspect(create_db_engine(self._database_url)).get_table_names())
