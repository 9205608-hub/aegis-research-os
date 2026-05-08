"""Artifact hashing for reproducibility guarantees.

Every research run output must have a deterministic content hash
that can be used to verify reproducibility.
"""

import hashlib
import json
from typing import Any


def compute_artifact_hash(data: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hash of a JSON-serializable artifact.

    Args:
        data: The artifact data to hash. Must be JSON-serializable.

    Returns:
        Hash string in format "sha256:<hex>".
    """
    canonical = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def verify_artifact_hash(data: dict[str, Any], expected_hash: str) -> bool:
    """Verify that data matches its expected hash."""
    return compute_artifact_hash(data) == expected_hash
