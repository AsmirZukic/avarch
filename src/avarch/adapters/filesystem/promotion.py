from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol

from avarch.models.promotion import PromotionJournal
from avarch.serialization import canonical_json

PROMOTION_CONTENT_HASH_CONTRACT = "promotion-content-v1"
PROMOTION_DIGEST_CHUNK_SIZE = 8 * 1024 * 1024


class PromotionContentDigest(Protocol):
    def update(self, data: bytes, /) -> object: ...

    def hexdigest(self) -> str: ...


def create_promotion_digest() -> PromotionContentDigest:
    digest = hashlib.blake2b(digest_size=32)
    digest.update(f"{PROMOTION_CONTENT_HASH_CONTRACT}\0".encode())
    return digest


def calculate_promotion_digest(
    path: Path,
    *,
    chunk_size: int = PROMOTION_DIGEST_CHUNK_SIZE,
) -> str:
    digest = create_promotion_digest()
    with path.open("rb") as input_file:
        while chunk := input_file.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def write_promotion_journal(path: Path, journal: PromotionJournal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{journal.operation_id}.tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
        output_file.write(canonical_json(journal))
        output_file.write("\n")
        output_file.flush()
        os.fsync(output_file.fileno())
    os.replace(temporary_path, path)
    fsync_directory(path.parent)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
