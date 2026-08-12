"""definition_gate 武装回归（2026-08-01）+ 拧紧（2026-08-13）。

背景：主流程 Step 12 起给 ctx 传 registered_metric_ids（Step 5 seed 的
*_v1 definition_id）。judgment.used_metric_ids 携带去版本裸名，gate 双侧
剥 _v<N> 再比对。2026-08-13 普查（30 个缓存 run / 210 份 judgment）在
seed 补 pe_ratio_ttm_v1 后 distinct-id 对齐率 100%，默认政策翻成
definition_gate_block=True：未注册 id → severity=block。

未传 registry 的调用方仍走 "not armed" 分支（措辞契约），由
test_publish_gate_quality.py 的
test_unarmed_definition_gate_not_harvested_as_skip 锁定。
"""

from aegis.core.publish_gate import PublishGate
from aegis.core.truth.registry.seed_metrics import create_seeded_registry
from aegis.data_contracts.critic_result_schema import CriticResult
from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    JudgmentContract,
    Observation,
)

SEEDED_IDS = [d.definition_id for d in create_seeded_registry().list_all()]


def _bias_pass() -> CriticResult:
    return CriticResult(
        critic_id="critic_bias_test",
        critic_type="cognitive_bias_critic",
        issues=[],
        block_publish=False,
        overall_risk="low",
    )


def _judgment(used_metric_ids: list[str], jid: str = "j_test_1") -> JudgmentContract:
    return JudgmentContract(
        judgment_id=jid,
        agent_name="test_agent",
        agent_version="1.0",
        question_id="q1",
        run_id="run_test",
        observations=[Observation(text="obs", source_ids=["fact:revenue:t:FY1"])],
        used_metric_ids=used_metric_ids,
        cognitive_bias_self_check=CognitiveBiasSelfCheck(
            anchoring_risk="low",
            confirmation_bias_risk="low",
            recency_bias_risk="low",
            narrative_fallacy_risk="low",
        ),
        judgment_status="complete",
    )


def _ctx(**overrides):
    ctx = {
        "run_manifest_id": "run_test",
        "registered_metric_ids": list(SEEDED_IDS),
    }
    ctx.update(overrides)
    return ctx


def _definition_check(result):
    checks = [c for c in result.checks if c.gate_name == "definition_gate"]
    assert len(checks) == 1
    return checks[0]


def test_armed_gate_accepts_bare_names_against_versioned_registry():
    """实测主路径：judgment 裸名 vs registry 版本化 id → 归一化后全过。"""
    judgments = [
        _judgment(["roe", "net_debt", "gross_margin", "cfo_to_net_income"]),
        _judgment(["roic", "ev_to_ebitda"], jid="j_test_2"),
    ]
    result = PublishGate().evaluate(judgments, [_bias_pass()], context=_ctx())

    check = _definition_check(result)
    assert check.passed
    assert "definition_gate" not in result.blocked_by
    # 措辞契约：pass 分支不得含 "skipped"，否则被 B4 收割谓词误收
    assert "skipped" not in check.message


def test_armed_gate_accepts_exact_versioned_ids_too():
    """归一化是双侧的：将来若 judgment 携带版本化 id 也不会误报。"""
    result = PublishGate().evaluate(
        [_judgment(["roe_v1", "net_debt_v1"])], [_bias_pass()], context=_ctx(),
    )
    assert _definition_check(result).passed


def test_unregistered_id_blocks_when_policy_true():
    """默认政策 True：未注册 id（幻觉/拼写变体，不进 seed）直接 block。"""
    result = PublishGate().evaluate(
        [_judgment(["roe", "not_a_real_metric"])], [_bias_pass()], context=_ctx(),
    )

    check = _definition_check(result)
    assert not check.passed
    assert check.severity == "block"
    assert "not_a_real_metric" in check.message
    assert "definition_gate" in result.blocked_by
    assert not result.publishable
    harvested = [
        c.gate_name for c in result.checks
        if c.passed and c.severity == "warn" and "skipped" in c.message
    ]
    assert "definition_gate" not in harvested


def test_armed_gate_blocks_when_policy_tightened():
    """显式 definition_gate_block=True 未注册 id 直接 block。"""
    gate = PublishGate(policy={"definition_gate_block": True})
    result = gate.evaluate(
        [_judgment(["roe", "not_a_real_metric"])], [_bias_pass()], context=_ctx(),
    )

    check = _definition_check(result)
    assert not check.passed
    assert check.severity == "block"
    assert "definition_gate" in result.blocked_by
    assert not result.publishable


def test_pe_ratio_ttm_now_registered_passes():
    """2026-08-13 seed 补 pe_ratio_ttm_v1 后，裸名与版本化 id 均过。"""
    for used in (["roe", "pe_ratio_ttm"], ["pe_ratio_ttm_v1"]):
        result = PublishGate().evaluate(
            [_judgment(used)], [_bias_pass()], context=_ctx(),
        )
        check = _definition_check(result)
        assert check.passed, check.message
        assert "definition_gate" not in result.blocked_by
        assert "skipped" not in check.message


def test_replay_ctx_includes_registered_metric_ids():
    """replay _build_gate_context 与主流程同款传入 registry id。"""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "replay_from_cache.py"
    spec = importlib.util.spec_from_file_location("replay_from_cache_ut", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ctx = mod._build_gate_context({"run_id": "run_test", "meta_facts": {}})
    assert "registered_metric_ids" in ctx
    ids = ctx["registered_metric_ids"]
    assert "pe_ratio_ttm_v1" in ids
    assert "pe_ratio_v1" in ids


def test_armed_gate_empty_used_metric_ids_not_penalized():
    """空 used_metric_ids 不误伤：没有 id 可校验 = 通过。"""
    result = PublishGate().evaluate(
        [_judgment([])], [_bias_pass()], context=_ctx(),
    )
    assert _definition_check(result).passed
    assert "definition_gate" not in result.blocked_by


def test_armed_gate_message_lists_at_most_ten_unregistered_ids():
    fake_ids = [f"bogus_metric_{i:02d}" for i in range(12)]
    result = PublishGate().evaluate(
        [_judgment(fake_ids)], [_bias_pass()], context=_ctx(),
    )

    check = _definition_check(result)
    assert not check.passed
    for mid in sorted(fake_ids)[:10]:
        assert mid in check.message
    assert sorted(fake_ids)[10] not in check.message
    assert "(+2 more)" in check.message


def test_seeded_registry_shape_matches_gate_expectation():
    """接线契约：list_all() 元素带 definition_id，且 seed 集非空、全部 *_v<N>。"""
    assert len(SEEDED_IDS) >= 22
    assert all(isinstance(d, str) and d for d in SEEDED_IDS)
    import re
    assert all(re.search(r"_v\d+$", d) for d in SEEDED_IDS)
