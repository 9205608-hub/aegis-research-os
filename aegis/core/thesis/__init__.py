"""Thesis 持久化子包 — Aegis 2.0 Phase 2 任务 B.

- :mod:`.monitorables` —— monitorables 封闭目录（设计红线 6：LLM 只许
  选型号填阈值，目录外降级「人工关注」）；
- :mod:`.persistence` —— ThesisContract 构建 + append-only JSONL 版本链
  （不建正式状态机，设计红线 10）。
"""

from aegis.core.thesis.monitorables import (
    CATALOG,
    build_monitorables,
    monitorable_model_id,
    normalize_model_id,
)
from aegis.core.thesis.persistence import (
    build_thesis_contract,
    history,
    load_latest,
    save_thesis_version,
)

__all__ = [
    "CATALOG",
    "build_monitorables",
    "normalize_model_id",
    "monitorable_model_id",
    "build_thesis_contract",
    "save_thesis_version",
    "load_latest",
    "history",
]
