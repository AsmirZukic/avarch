from __future__ import annotations

import hashlib
import shlex

from avarch.config import EncodingProfile
from avarch.serialization import canonical_json


def parse_encoder_args(value: str) -> list[str]:
    return shlex.split(value)


def build_profile_hash(profile: EncodingProfile) -> str:
    payload = b"profile-v1\0" + canonical_json(profile).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()
