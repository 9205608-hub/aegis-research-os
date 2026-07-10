"""Aegis PIT 时点库 — 新数据（Phase 1 起）的唯一事实源。

见 DESIGN_2.0.md 理念 B：双时间戳 PIT 语义 + sqlite3 零依赖存储。
"""

from .store import (
    DEFAULT_DB_PATH,
    FISCAL_PERIODS,
    PITFact,
    PITStore,
    PITStoreError,
    UnknownConceptError,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "FISCAL_PERIODS",
    "PITFact",
    "PITStore",
    "PITStoreError",
    "UnknownConceptError",
]
