"""definition_gate 武装回归（2026-08-01）。

背景：主流程 Step 12 起给 ctx 传 registered_metric_ids（Step 5 seed 的
22 个 *_v1 definition_id）。离线对齐率测量（30 个缓存 run / 210 份
judgment）显示 judgment.used_metric_ids 携带的是去版本裸名（"roe"、
"net_debt"——_compute_metrics 用 replace("_v1","") 作 computed_metrics
的 key，agent 从该 key 集合自报），原样精确比对对齐率 0%、每 run 必
block；版本归一化后 95.5%（按 distinct id）/ 99.8%（按出现次数），唯一
残留 pe_ratio_ttm（orchestrator 运行时注入的实时 TTM 市盈率，registry
无条目）。因此 gate 侧：
1. 比对前双侧剥离尾部 _v<N>；
2. 未注册 id 默认 severity="warn" 不 block（policy 开关
   definition_gate_block=False，软着陆），拧成 True 才 block。

本文件锁定武装后的行为矩阵；未传 registry 的调用方（replay_from_cache）
行为不变，由 test_publish_gate_quality.py 的
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


def test_armed_gate_unregistered_id_warns_but_does_not_block_by_default():
    """软着陆：pe_ratio_ttm（实测唯一残留）记 warn，run 仍可发布。"""
    result = PublishGate().evaluate(
        [_judgment(["roe", "pe_ratio_ttm"])], [_bias_pass()], context=_ctx(),
    )

    check = _definition_check(result)
    assert not check.passed
    assert check.severity == "warn"
    assert "pe_ratio_ttm" in check.message
    assert "definition_gate" not in result.blocked_by
    assert any("pe_ratio_ttm" in w for w in result.warnings)
    # 失败分支也不得被 B4 skip 收割谓词命中（谓词要求 passed=True）
    harvested = [
        c.gate_name for c in result.checks
        if c.passed and c.severity == "warn" and "skipped" in c.message
    ]
    assert "definition_gate" not in harvested


def test_armed_gate_blocks_when_policy_tightened():
    """definition_gate_block=True 拧紧后未注册 id 直接 block。"""
    gate = PublishGate(policy={"definition_gate_block": True})
    result = gate.evaluate(
        [_judgment(["roe", "pe_ratio_ttm"])], [_bias_pass()], context=_ctx(),
    )

    check = _definition_check(result)
    assert not check.passed
    assert check.severity == "block"
    assert "definition_gate" in result.blocked_by
    assert not result.publishable


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
