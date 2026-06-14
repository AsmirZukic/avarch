from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Column, String
from sqlmodel import Field, SQLModel


class AppMeta(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class MediaFileStatus(StrEnum):
    ADDED = "added"
    PRESENT = "present"
    CHANGED = "changed"
    MISSING = "missing"


class MediaFile(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(index=True, unique=True)
    size_bytes: int
    mtime_ns: int
    device_id: int
    inode: int
    content_key: str = Field(index=True)
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: MediaFileStatus = Field(sa_column=Column(String(), nullable=False))


class ProbeResult(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    media_file_id: int = Field(foreign_key="mediafile.id", index=True)
    ffprobe_json: str
    normalized_json: str
    probe_hash: str = Field(index=True)
    created_at: datetime
