"""Aegis 2.0 Phase 3 任务 B2 — runner 轻回归测试.

runner 真跑 orchestrator 是重依赖，这里只锁三件事（monkeypatch 掉 orchestrator，
绝不真跑复研）：

① 签名 / dataclass 字段正确、import 不崩；
② orchestrator.run 抛异常 → ok=False + error（永不 raise）；
③ 成功路径 → ok=True，读回本次成本 + 论点链最新版本号。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import aegis.core.orchestrator.auto_research as ar_mod
from aegis.core.monitor.runner import UpdateRunResult, run_update


def _append_thesis(thesis_dir: Path, entity: str, version: int,
                   thesis: dict) -> None:
    thesis_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "version": version,
        "created_at": "2026-07-11T00:00:00",
        "run_id": f"run_{version}",
        "parent_version": (version - 1) if version > 1 else None,
        "thesis": thesis,
    }
    with (thesis_dir / f"{entity}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# ① dataclass 字段
# ---------------------------------------------------------------------------

def test_update_run_result_fields():
    r = UpdateRunResult(ticker="002669", ok=True)
    assert r.ticker == "002669"
    assert r.ok is True
    assert r.run_id == ""
    assert r.entity_id == ""
    assert r.cost_usd == 0.0
    assert r.thesis_version is None
    assert r.error is None


def test_empty_ticker_degrades():
    r = run_update("   ")
    assert r.ok is False
    assert r.error


# ---------------------------------------------------------------------------
# ② orchestrator.run 抛异常 → ok=False（永不 raise）
# ---------------------------------------------------------------------------

def test_run_exception_degrades(monkeypatch):
    class BoomOrch:
        def run(self, config):
            raise RuntimeError("boom-run")

        def last_run_cost_usd(self) -> float:
            return 0.5

    monkeypatch.setattr(ar_mod, "AutoResearchOrchestrator", BoomOrch)

    r = run_update("002669", use_llm=False, trigger_zh="公告触发")
    assert r.ok is False
    assert "boom-run" in (r.error or "")
    # 失败也应读回本次已发生的成本（预算熔断需要）。
    assert r.cost_usd == 0.5


def test_config_construction_failure_degrades(monkeypatch):
    # ResearchConfig 构造抛非 TypeError → 走「构造失败」降级分支，不 raise。
    class BadConfig:
        def __init__(self, *a, **k):
            raise ValueError("bad config")

    class DummyOrch:
        def run(self, config):
            return SimpleNamespace(entity_id="x", run_id="y")

    monkeypatch.setattr(ar_mod, "AutoResearchOrchestrator", DummyOrch)
    monkeypatch.setattr(ar_mod, "ResearchConfig", BadConfig)

    r = run_update("002669", use_llm=False)
    assert r.ok is False
    assert r.error


# ---------------------------------------------------------------------------
# ③ 成功路径 → ok=True，读回成本 + 版本号
# ---------------------------------------------------------------------------

def test_success_reads_cost_and_version(monkeypatch, tmp_path):
    thesis_dir = tmp_path / "thesis"
    _append_thesis(thesis_dir, "002669", 1, {
        "thesis_id": "thesis_002669", "thesis_version": 1,
        "entity_id": "002669", "core_thesis": "v1 论点",
    })

    captured = {}

    class OkOrch:
        def __init__(self):
            self._cached_llm_client = None
            self._cached_fast_llm_client = None

        def run(self, config):
            captured["update_mode"] = getattr(config, "update_mode", None)
            captured["update_trigger"] = getattr(config, "update_trigger", None)
            return SimpleNamespace(entity_id="002669", run_id="run_abc")

        def last_run_cost_usd(self) -> float:
            return 0.03

    monkeypatch.setattr(ar_mod, "AutoResearchOrchestrator", OkOrch)

    r = run_update("002669", use_llm=False, trigger_zh="业绩预告偏离",
                   thesis_dir=str(thesis_dir))
    assert r.ok is True
    assert r.entity_id == "002669"
    assert r.run_id == "run_abc"
    assert r.cost_usd == 0.03
    assert r.thesis_version == 1
    # config 透传：增量模式 + 触发原因。
    assert captured["update_mode"] is True
    assert captured["update_trigger"] == "业绩预告偏离"


def test_cost_fallback_walks_cached_clients(monkeypatch, tmp_path):
    # 无 last_run_cost_usd 方法时，走 walk _cached_llm_client.cost_tracker 兜底。
    class Tracker:
        total_cost_usd = 0.12

    class OrchNoGetter:
        def __init__(self):
            self._cached_llm_client = SimpleNamespace(cost_tracker=Tracker())
            self._cached_fast_llm_client = None

        def run(self, config):
            return SimpleNamespace(entity_id="600519", run_id="run_z")

    monkeypatch.setattr(ar_mod, "AutoResearchOrchestrator", OrchNoGetter)

    r = run_update("600519", use_llm=False, thesis_dir=str(tmp_path / "t"))
    assert r.ok is True
    assert abs(r.cost_usd - 0.12) < 1e-9
