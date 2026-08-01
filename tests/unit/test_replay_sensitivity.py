"""replay_from_cache 敏感性重算回归（HANDOFF 待办: replay 敏感性表缺参）.

Bug 链条: scripts/replay_from_cache.py 的 DCF 重算路径里
`sa.two_way_table(dcf_input_flat, "wacc", "terminal_growth_rate")` 少传了
var1_range/var2_range 两个必填参数 → TypeError 被裸 `except: pass` 吞掉 →
过期缓存表配上新算的 dcf_output → 发布门槛 dcf_integrity_gate 要么 skip
（replay 只喂了 run_manifest_id）要么失配；gate skip 走
DecisionEngine._determine_confidence 的 gate_skipped_count>0 分支封顶 medium。

本文件覆盖:
1. flat DCF: _recompute_sensitivity_flat 产出非空 rankings + WACC×TGR dict，
   base 单元与 DCFEngine 每股值一致（dcf_integrity_gate 实测通过、不 skip）。
2. 旧缺参签名回归: 少 range 的调用必然 TypeError（若引擎签名改为可选参数，
   本用例提醒同步清理脚本注释）。
3. segment DCF: _scale_sensitivity_for_segment 保留 None 单元（不可行
   WACC/g 组合，AUDIT 2026-07），其余按 share_ratio 缩放；旧代码
   `cell * share_ratio` 遇 None 直接 TypeError 炸掉整个重算块。
4. gate 上下文: _build_gate_context 提供主流程 Step 12 同款全部键；
   只喂 run_manifest_id 的旧调用会让 dcf_integrity_gate skip（并被
   gate_skipped_names 收割谓词命中）→ 封顶链条的实证。

不依赖网络与任何 LLM key。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "replay_from_cache.py"


# ---------------------------------------------------------------------------
# 夹具：从文件路径加载 replay_from_cache.py（scripts/ 非包）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def replay_mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("replay_from_cache_ut", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_flat_input(**overrides):
    from aegis.core.truth.scenario_engine.dcf_engine import DCFInput

    kwargs = dict(
        base_revenue=1e9,
        revenue_growth_path=[0.10, 0.08, 0.06, 0.05, 0.04],
        operating_margin_path=[0.20] * 5,
        capex_to_revenue_path=[0.05] * 5,
        effective_tax_rate=0.21,
        nwc_to_revenue_delta=0.02,
        terminal_growth_rate=0.025,
        wacc=0.09,
        sbc_to_revenue=0.02,
        dilution_rate_annual=0.01,
        shares_outstanding=1e8,
        net_debt=2e8,
        horizon_years=5,
        base_depreciation=4e7,
    )
    kwargs.update(overrides)
    return DCFInput(**kwargs)


# ---------------------------------------------------------------------------
# 1. flat DCF 路径
# ---------------------------------------------------------------------------

class TestFlatRecompute:
    def test_rankings_and_table_nonempty_with_correct_shape(self, replay_mod):
        inp = _make_flat_input()
        rankings, table = replay_mod._recompute_sensitivity_flat(inp)

        assert rankings, "flat 重算必须产出非空 sensitivity_rankings"
        for r in rankings:
            assert {"assumption", "impact_pct", "signed_impact_pct",
                    "base_per_share", "shocked_per_share"} <= set(r)

        # 表必须是 dict（html_report_v2 对非 dict 静默丢弃；publish gate
        # 也按 dict 取 matrix/var1_values/var2_values）
        assert isinstance(table, dict), "两维敏感性表必须是 dict，非 SensitivityTable 数据类"
        assert table["variable_1"] == "wacc"
        assert table["variable_2"] == "terminal_growth_rate"
        assert table["var1_values"] and table["var2_values"]
        assert len(table["matrix"]) == len(table["var1_values"])
        for row in table["matrix"]:
            assert len(row) == len(table["var2_values"])
        # base 假设值在网格内（居中网格）
        assert round(inp.wacc, 4) in table["var1_values"]
        assert round(inp.terminal_growth_rate, 4) in table["var2_values"]
        # 至少 base 单元非空
        flat_cells = [c for row in table["matrix"] for c in row]
        assert any(c is not None for c in flat_cells)

    def test_base_cell_matches_dcf_output_and_gate_passes(self, replay_mod):
        """dcf_integrity_gate 同款校验：base WACC/g 单元 ≈ 每股基准值."""
        from aegis.core.publish_gate import PublishGate
        from aegis.core.truth.scenario_engine.dcf_engine import DCFEngine

        inp = _make_flat_input()
        out = DCFEngine().compute_dcf(inp)
        _rankings, table = replay_mod._recompute_sensitivity_flat(inp)
        assert table is not None

        check = PublishGate()._dcf_integrity_gate({
            "dcf_input": inp,
            "dcf_output": out,
            "sensitivity_table": table,
        })
        assert check.passed, f"dcf_integrity_gate 应通过: {check.message}"
        assert "skipped" not in check.message

    def test_infeasible_cells_are_none_not_crash(self, replay_mod):
        """低 WACC 网格里 tg ≥ wacc 的单元填 None，函数不得抛异常."""
        inp = _make_flat_input(wacc=0.045, terminal_growth_rate=0.03)
        rankings, table = replay_mod._recompute_sensitivity_flat(inp)
        assert rankings
        assert table is not None
        cells = [c for row in table["matrix"] for c in row]
        assert any(c is None for c in cells), "发散 Gordon 单元应为 None"
        assert any(c is not None for c in cells)

    def test_old_missing_range_signature_raises_typeerror(self):
        """缺参回归：旧调用形态（无 range 参数）必然 TypeError.

        若本用例开始失败，说明 two_way_table 签名改成了可选参数——
        届时同步检查 replay 脚本与本测试的注释是否过期。
        """
        from aegis.core.truth.scenario_engine.sensitivity_analyzer import (
            SensitivityAnalyzer,
        )

        inp = _make_flat_input()
        with pytest.raises(TypeError):
            SensitivityAnalyzer().two_way_table(inp, "wacc", "terminal_growth_rate")


# ---------------------------------------------------------------------------
# 2. segment DCF 路径
# ---------------------------------------------------------------------------

class TestSegmentScaling:
    def test_none_cells_preserved_and_values_scaled(self, replay_mod):
        rankings = [
            {"assumption": "wacc", "impact_pct": 0.155, "signed_impact_pct": -0.155,
             "base_per_share": 100.0, "shocked_per_share": 84.5},
            {"assumption": "revenue_growth", "impact_pct": 0.12,
             "signed_impact_pct": 0.12, "base_per_share": 100.0,
             "shocked_per_share": 112.0},
        ]
        table = {
            "variable_1": "wacc",
            "variable_2": "terminal_growth_rate",
            "var1_values": [0.08, 0.09],
            "var2_values": [0.02, 0.025],
            "matrix": [[100.0, None], [80.0, 60.0]],
        }
        share_ratio = 1.25

        s_rankings, s_table = replay_mod._scale_sensitivity_for_segment(
            rankings, table, share_ratio)

        assert [r["base_per_share"] for r in s_rankings] == [125.0, 125.0]
        assert [r["shocked_per_share"] for r in s_rankings] == [
            pytest.approx(105.625), pytest.approx(140.0)]
        # 非数值字段原样保留
        assert s_rankings[0]["assumption"] == "wacc"
        assert s_rankings[0]["impact_pct"] == 0.155

        assert s_table["matrix"] == [[125.0, None], [100.0, 75.0]]
        # 轴与变量名不缩放
        assert s_table["var1_values"] == [0.08, 0.09]
        assert s_table["variable_1"] == "wacc"

    def test_empty_or_missing_inputs_do_not_crash(self, replay_mod):
        s_rankings, s_table = replay_mod._scale_sensitivity_for_segment(
            None, None, 1.5)
        assert s_rankings == []
        assert s_table is None

    def test_scaled_table_keeps_gate_consistency_with_replaced_output(self, replay_mod):
        """segment 路径全链一致性：矩阵缩放 + dcf_output per_share 替换后，
        dcf_integrity_gate 仍通过（修复前二者失配 / gate skip）。"""
        import dataclasses

        from aegis.core.publish_gate import PublishGate
        from aegis.core.truth.scenario_engine.dcf_engine import DCFEngine

        inp = _make_flat_input()
        out = DCFEngine().compute_dcf(inp)
        _r, table = replay_mod._recompute_sensitivity_flat(inp)
        assert table is not None

        # 模拟 replay segment 路径：股本变动 → per_share 调整 + 矩阵等比缩放
        share_ratio = 0.8
        new_ps = out.per_share_value * share_ratio
        _sr, s_table = replay_mod._scale_sensitivity_for_segment([], table, share_ratio)
        new_out = dataclasses.replace(out, per_share_value=new_ps)

        check = PublishGate()._dcf_integrity_gate({
            "dcf_input": inp,
            "dcf_output": new_out,
            "sensitivity_table": s_table,
        })
        assert check.passed, f"缩放后 gate 应通过: {check.message}"
        assert "skipped" not in check.message


# ---------------------------------------------------------------------------
# 3. PublishGate 上下文（同类缺参：replay 旧代码只传 run_manifest_id）
# ---------------------------------------------------------------------------

class TestGateContext:
    ORCHESTRATOR_STEP12_KEYS = {
        "run_manifest_id", "__data_quality_issues", "meta_facts",
        "computed_metrics", "market_data", "segment_detail",
        "segment_projections", "scenarios", "dcf_input", "dcf_output",
        "sensitivity_table",
    }

    def test_context_has_all_orchestrator_keys(self, replay_mod):
        state = {
            "run_id": "run-1",
            "meta_facts": {"__data_quality_issues": [{"severity": "warn", "code": "X"}]},
            "computed_metrics": {"pe_ratio": 10.0},
            "market_data": {"current_price": 50.0},
            "segment_detail": {"seg": {}},
            "segment_projections_data": None,
            "scenarios": {"base_value": 60.0},
            "dcf_input_flat": "DCF_INPUT_SENTINEL",
            "dcf_output": "DCF_OUTPUT_SENTINEL",
            "sensitivity_table": {"matrix": [[1.0]]},
        }
        ctx = replay_mod._build_gate_context(state)
        assert set(ctx) == self.ORCHESTRATOR_STEP12_KEYS
        # 键名映射正确（state 键 → gate 键）
        assert ctx["dcf_input"] == "DCF_INPUT_SENTINEL"
        assert ctx["dcf_output"] == "DCF_OUTPUT_SENTINEL"
        assert ctx["segment_projections"] is None
        assert ctx["__data_quality_issues"] == [{"severity": "warn", "code": "X"}]

    def test_full_context_unskips_dcf_integrity_gate(self, replay_mod):
        from aegis.core.publish_gate import PublishGate
        from aegis.core.truth.scenario_engine.dcf_engine import DCFEngine

        inp = _make_flat_input()
        out = DCFEngine().compute_dcf(inp)
        _r, table = replay_mod._recompute_sensitivity_flat(inp)
        state = {
            "run_id": "run-1",
            "meta_facts": {},
            "computed_metrics": {},
            "market_data": {},
            "segment_detail": None,
            "segment_projections_data": None,
            "scenarios": {},
            "dcf_input_flat": inp,
            "dcf_output": out,
            "sensitivity_table": table,
        }
        check = PublishGate()._dcf_integrity_gate(replay_mod._build_gate_context(state))
        assert check.passed
        assert "skipped" not in check.message

    def test_legacy_context_skips_and_matches_harvest_predicate(self):
        """旧 replay 只传 run_manifest_id → gate skip，且被
        gate_skipped_names 收割谓词命中 → 置信度封顶 medium 的链条实证."""
        from aegis.core.publish_gate import PublishGate

        check = PublishGate()._dcf_integrity_gate({"run_manifest_id": "run-1"})
        assert check.passed and check.severity == "warn"
        assert "skipped" in check.message
        # replay/orchestrator 共用的收割谓词
        harvested = (check.passed and check.severity == "warn"
                     and "skipped" in check.message)
        assert harvested, "skip 检查必须能被 gate_skipped_names 谓词收割"

    def test_gate_skipped_count_caps_confidence_at_medium(self):
        """引擎端链条终点：有 skip 时置信度桶不得为 high（AUDIT B4）."""
        from aegis.core.decision_engine import DecisionEngine

        de = DecisionEngine()
        # 空判断/空批评 → 基线 70 分。无 skip 时为 medium 属正常；
        # 关键断言是有 skip 时绝不越过 medium。
        with_skip = de._determine_confidence(
            [], [], publishing_status="published",
            publish_gate_passed=True, gate_skipped_count=2,
        )
        assert with_skip in ("very_low", "low", "medium")
