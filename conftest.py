from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def network_tests_enabled() -> bool:
    return os.environ.get("AEGIS_RUN_NETWORK_TESTS", "").lower() in {"1", "true", "yes"}


def require_network() -> None:
    if not network_tests_enabled():
        pytest.skip("network tests disabled; set AEGIS_RUN_NETWORK_TESTS=1 to enable")
