"""Architecture serialization and hashing utilities.

Provides a canonical serialization for architecture config dataclasses and a
deterministic short hash derived from it.  These are used across the NAS
pipeline for cache keys, deduplication, file naming, and logging.
"""

import hashlib
import json
from dataclasses import asdict
from typing import Any


def serialize_arch(arch_config: Any) -> str:
    """Serialize an architecture config dataclass into a stable JSON string.

    The returned string is deterministic (keys sorted) and lossless — two
    configs produce the same string if and only if they are structurally
    identical.  Use this when exact identity matters: evaluation cache keys,
    candidate deduplication, and architecture export payloads.

    Args:
        arch_config: A dataclass instance representing the architecture.
    """
    return json.dumps(asdict(arch_config), sort_keys=True)


def hash_arch(arch_config: Any, *, length: int = 16) -> str:
    """Return a short fixed-length hex hash identifying an architecture config.

    Internally calls `serialize_arch` and returns the first `length` hex
    characters of its SHA-256 digest.  Use this where a compact,
    fixed-length identifier is needed: file names, log tags, and
    subdirectory names.  Unlike `serialize_arch`, the hash is lossy and
    can theoretically collide, so it should not be used for deduplication
    or cache keying.

    Args:
        arch_config: A dataclass instance representing the architecture.
        length: Number of hex characters to return (default 16).
    """
    return hashlib.sha256(serialize_arch(arch_config).encode()).hexdigest()[:length]
