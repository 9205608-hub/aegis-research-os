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
