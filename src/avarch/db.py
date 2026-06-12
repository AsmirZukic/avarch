from __future__ import annotations

from sqlalchemy import Engine
from sqlmodel import SQLModel, create_engine


def create_db_engine(database_url: str) -> Engine:
    return create_engine(database_url)


def create_db_schema(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)
