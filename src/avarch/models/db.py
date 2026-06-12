from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class AppMeta(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class MediaFile(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    path: str
    size_bytes: int
    mtime_ns: int
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
