"""Publish gate quality checks for valuation integrity."""

from types import SimpleNamespace

from aegis.core.publish_gate import PublishGate
from aegis.data_contracts.critic_result_schema import CriticResult


def _bias_pass() -> CriticResult:
    return CriticResult(
        critic_id="critic_bias_test",
        critic_type="cognitive_bias_critic",
        issues=[],
        block_publish=False,
        overall_risk="low",
    )


def _base_context(**overrides):
    ctx = {
        "run_manifest_id": "run_test",
        "meta_facts": {"free_cash_flow": 10.0},
        "computed_metrics": {"capex_to_revenue": 0.08},
        "market_data": {"current_price": 100.0},
        "segment_detail": {},
        "segment_projections": {},
        "scenarios": {"base_value": 105.0, "probability_weighted_value": 105.0},
        "dcf_input": SimpleNamespace(wacc=0.095, terminal_growth_rate=0.03),
        "dcf_output": SimpleNamespace(
            per_share_value=105.0,
            enterprise_value=1000.0,
            pv_terminal_value=500.0,
        ),
        "sensitivity_table": {
            "var1_values": [0.09, 0.095, 0.10],
            "var2_values": [0.025, 0.03, 0.035],
            "matrix": [
                [110.0, 108.0, 106.0],
                [103.0, 105.0, 107.0],
                [96.0, 98.0, 100.0],
            ],
        },
    }
    ctx.update(overrides)
    return ctx


def test_dcf_integrity_blocks_mismatched_sensitivity_cell():
    ctx = _base_context(
        sensitivity_table={
            "var1_values": [0.09, 0.095, 0.10],
            "var2_values": [0.025, 0.03, 0.035],
            "matrix": [
                [110.0, 108.0, 106.0],
                [103.0, 80.0, 107.0],
                [96.0, 98.0, 100.0],
            ],
        }
    )

    result = PublishGate().evaluate([], [_bias_pass()], context=ctx)

    assert not result.publishable
    assert "dcf_integrity_gate" in result.blocked_by


def test_high_capex_negative_fcf_upside_requires_segment_support():
    ctx = _base_context(
        meta_facts={"free_cash_flow": -120.0},
        computed_metrics={"capex_to_revenue": 0.227},
        market_data={"current_price": 100.0},
        scenarios={"base_value": 140.0, "probability_weighted_value": 140.0},
        dcf_output=SimpleNamespace(
            per_share_value=140.0,
            enterprise_value=1000.0,
            pv_terminal_value=500.0,
        ),
        sensitivity_table={
            "var1_values": [0.095],
            "var2_values": [0.03],
            "matrix": [[140.0]],
        },
    )

    result = PublishGate().evaluate([], [_bias_pass()], context=ctx)

    assert not result.publishable
    assert "capex_attribution_gate" in result.blocked_by


def test_high_capex_upside_passes_with_sotp_proxy():
    ctx = _base_context(
        meta_facts={"free_cash_flow": -120.0},
        computed_metrics={"capex_to_revenue": 0.227},
        market_data={"current_price": 100.0},
        scenarios={"base_value": 140.0, "probability_weighted_value": 140.0},
        segment_detail={
            "product": {
                "segment_a": {"revenue": 60.0},
                "segment_b": {"revenue": 40.0},
            }
        },
        dcf_output=SimpleNamespace(
            per_share_value=140.0,
            enterprise_value=1000.0,
            pv_terminal_value=500.0,
        ),
        sensitivity_table={
            "var1_values": [0.095],
            "var2_values": [0.03],
            "matrix": [[140.0]],
        },
    )

    result = PublishGate().evaluate([], [_bias_pass()], context=ctx)

    assert "capex_attribution_gate" not in result.blocked_by


def test_terminal_value_dominated_high_risk_dcf_blocks():
    ctx = _base_context(
        meta_facts={"free_cash_flow": -10.0},
        computed_metrics={"capex_to_revenue": 0.18},
        dcf_input=SimpleNamespace(wacc=0.095, terminal_growth_rate=0.03),
        dcf_output=SimpleNamespace(
            per_share_value=105.0,
            enterprise_value=1000.0,
            pv_terminal_value=760.0,
        ),
    )

    result = PublishGate().evaluate([], [_bias_pass()], context=ctx)

    assert not result.publishable
    assert "terminal_value_gate" in result.blocked_by


# ---------------------------------------------------------------------------
# definition_gate 恒真 skip 回归（2026-08-01）
# ---------------------------------------------------------------------------
# 主流程 Step 12 从不给 ctx 传 registered_metric_ids，definition_gate 的
# no-registry 分支每次 run 必触发。B4 的 skip 收割谓词（orchestrator 与
# replay 各一份）按 `passed + severity=="warn" + "skipped" in message` 子串
# 匹配——若该分支的 message 含 "skipped"，则 gate_skipped_count>0 恒成立，
# 置信度被永久封顶 medium，"high" 档结构性不可达。以下锁定：未武装 ≠ 缺数据
# skip，该分支的措辞不得再命中收割谓词。


def test_unarmed_definition_gate_not_harvested_as_skip():
    result = PublishGate().evaluate([], [_bias_pass()], context=_base_context())

    def_checks = [c for c in result.checks if c.gate_name == "definition_gate"]
    assert len(def_checks) == 1
    check = def_checks[0]
    assert check.passed
    assert check.severity == "warn"
    # 收割谓词的判据是 "skipped" 子串——措辞是契约的一部分
    assert "skipped" not in check.message

    harvested = [
        c.gate_name for c in result.checks
        if c.passed and c.severity == "warn" and "skipped" in c.message
    ]
    assert "definition_gate" not in harvested
