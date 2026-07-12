"""R5-L4/L3（2026-07-12 平台期突破杠杆）回归测试。

锁定的行为：

① derive_product_form 确定性规则：估值失配 / 证据缺口 / blocked ·
   needs_review · under_review / downgraded+待解问题堆积 → 观察框架；
   干净发布 → 投资论点（reason 为 None，无需自我声明）；
② ThesisContract 新字段 product_form / product_form_reason：默认值兼容
   旧 JSONL；build_thesis_contract 优先消费 orchestrator 盖进 scenarios
   的章，缺章时按可得信号降级重算；A 股取中文 reason、美股取英文；
③ build_report_dict 的 productForm 键：观察框架票出 {form,label,reason}
   横幅数据，投资论点票为 None（前端隐藏）；replay 缺章时从 decision
   降级重算；
④ 失配票 synthesizer 事前注入新增 PRODUCT FORM 规则（rule 7），常态票无；
⑤ audit_scores.extract_score：从审计 md 提取 0-10 可信度分（取最后一个
   N/10 匹配），多次采样按票聚合均值±极差，run 文件存在时忽略
   _audit.md 兼容副本。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from aegis.core.thesis.persistence import build_thesis_contract
from aegis.core.thesis.product_form import (
    DOWNGRADED_OPEN_QUESTION_THRESHOLD,
    INVESTMENT_THESIS,
    OBSERVATION_FRAMEWORK,
    derive_product_form,
)
from aegis.data_contracts.thesis_schema import ThesisContract


# ---------------------------------------------------------------------------
# ① derive_product_form 规则
# ---------------------------------------------------------------------------

class TestDeriveProductForm:

    def test_clean_published_is_investment_thesis(self):
        pf = derive_product_form(publishing_status="published")
        assert pf["form"] == INVESTMENT_THESIS
        assert pf["reason_zh"] is None and pf["reason_en"] is None

    def test_valuation_mismatch_forces_observation(self):
        pf = derive_product_form(
            valuation_mismatch=True, publishing_status="published")
        assert pf["form"] == OBSERVATION_FRAMEWORK
        assert "估值失配" in pf["reason_zh"]
        assert "监控" in pf["reason_zh"]

    def test_evidence_gap_forces_observation(self):
        pf = derive_product_form(
            evidence_gap_hits=2, publishing_status="published")
        assert pf["form"] == OBSERVATION_FRAMEWORK
        assert "证据缺口 2 处" in pf["reason_zh"]

    @pytest.mark.parametrize("status", ["blocked", "needs_review", "under_review"])
    def test_review_statuses_force_observation(self, status):
        pf = derive_product_form(publishing_status=status)
        assert pf["form"] == OBSERVATION_FRAMEWORK
        assert status in pf["reason_zh"]

    def test_downgraded_with_open_questions_is_observation(self):
        pf = derive_product_form(
            publishing_status="downgraded",
            open_question_count=DOWNGRADED_OPEN_QUESTION_THRESHOLD,
        )
        assert pf["form"] == OBSERVATION_FRAMEWORK
        assert "待解问题" in pf["reason_zh"]

    def test_downgraded_with_few_questions_stays_thesis(self):
        pf = derive_product_form(
            publishing_status="downgraded",
            open_question_count=DOWNGRADED_OPEN_QUESTION_THRESHOLD - 1,
        )
        assert pf["form"] == INVESTMENT_THESIS

    def test_reasons_accumulate(self):
        pf = derive_product_form(
            valuation_mismatch=True, evidence_gap_hits=1,
            publishing_status="blocked")
        assert pf["reason_zh"].count("；") >= 2  # 三条理由拼接
        assert pf["signals"] == {
            "valuation_mismatch": True,
            "evidence_gap_hits": 1,
            "publishing_status": "blocked",
            "open_question_count": 0,
        }

    def test_garbage_inputs_are_tolerated(self):
        pf = derive_product_form(
            valuation_mismatch=False, evidence_gap_hits=-3,
            publishing_status=None, open_question_count=-1)
        assert pf["form"] == INVESTMENT_THESIS


# ---------------------------------------------------------------------------
# ② 合约字段 + build_thesis_contract 接线
# ---------------------------------------------------------------------------

class TestContractPlumbing:

    def test_old_jsonl_without_field_loads_with_default(self):
        c = build_thesis_contract(entity_id="600519", run_id="run_x")
        payload = json.loads(c.model_dump_json())
        payload.pop("product_form")
        payload.pop("product_form_reason")
        loaded = ThesisContract.model_validate(payload)
        assert loaded.product_form == "investment_thesis"
        assert loaded.product_form_reason is None

    def test_prefers_orchestrator_stamp(self):
        stamp = derive_product_form(
            valuation_mismatch=True, evidence_gap_hits=1,
            publishing_status="blocked")
        c = build_thesis_contract(
            entity_id="300750", run_id="run_x",
            scenarios={"product_form": stamp},
            publishing_status="blocked",
        )
        assert c.product_form == OBSERVATION_FRAMEWORK
        assert "证据缺口" in c.product_form_reason

    def test_fallback_derives_from_sanity_and_status(self):
        # replay / 旧路径：scenarios 无章，但有 valuation_sanity 失配
        c = build_thesis_contract(
            entity_id="002594", run_id="run_x",
            scenarios={"valuation_sanity": {"mismatch": True, "ratio": 18.2}},
            publishing_status="blocked",
        )
        assert c.product_form == OBSERVATION_FRAMEWORK
        assert "估值失配" in c.product_form_reason

    def test_clean_published_contract_has_no_reason(self):
        c = build_thesis_contract(
            entity_id="600519", run_id="run_x",
            publishing_status="published",
        )
        assert c.product_form == INVESTMENT_THESIS
        assert c.product_form_reason is None

    def test_us_market_gets_english_reason(self):
        c = build_thesis_contract(
            entity_id="NVDA", run_id="run_x",
            scenarios={"valuation_sanity": {"mismatch": True}},
            publishing_status="blocked", market_id="us",
        )
        assert c.product_form == OBSERVATION_FRAMEWORK
        assert "observation framework" in c.product_form_reason

    def test_contract_rejects_unknown_form(self):
        c = build_thesis_contract(entity_id="600519", run_id="run_x")
        payload = json.loads(c.model_dump_json())
        payload["product_form"] = "sell_side_note"
        with pytest.raises(Exception):
            ThesisContract.model_validate(payload)


# ---------------------------------------------------------------------------
# ③ build_report_dict 的 productForm 键
# ---------------------------------------------------------------------------

class TestReportBanner:

    def _report(self, *, scenarios=None, decision=None):
        from aegis.core.reports.html_report_v2 import build_report_dict
        return build_report_dict(
            entity_id="300750.SS",
            entity_name="宁德时代",
            scenarios=scenarios or {},
            decision=decision,
            meta_facts={"currency": "CNY"},
        )

    def test_stamped_observation_ticket_renders_banner(self):
        stamp = derive_product_form(
            valuation_mismatch=True, publishing_status="blocked")
        rpt = self._report(
            scenarios={"currency": "CNY", "product_form": stamp})
        assert rpt["productForm"]["form"] == OBSERVATION_FRAMEWORK
        assert "监控合约" in rpt["productForm"]["label"]
        assert "估值失配" in rpt["productForm"]["reason"]

    def test_investment_thesis_hides_banner(self):
        stamp = derive_product_form(publishing_status="published")
        rpt = self._report(
            scenarios={"currency": "CNY", "product_form": stamp})
        assert rpt["productForm"] is None

    def test_replay_without_stamp_derives_from_decision(self):
        class _Decision:
            publishing_status = "blocked"
            unresolved_conflicts = []
            open_questions = []
            entity_id = "300750"
        rpt = self._report(
            scenarios={"currency": "CNY",
                       "valuation_sanity": {"mismatch": True, "ratio": 11.6}},
            decision=_Decision(),
        )
        assert rpt["productForm"]["form"] == OBSERVATION_FRAMEWORK

    def test_no_stamp_no_decision_is_none(self):
        rpt = self._report(scenarios={"currency": "CNY"})
        assert rpt["productForm"] is None


# ---------------------------------------------------------------------------
# ④ 失配票事前注入 PRODUCT FORM 规则
# ---------------------------------------------------------------------------

class TestSynthesizerInjection:

    def test_mismatch_block_contains_product_form_rule(self):
        from aegis.core.chief_analyst.thesis_synthesizer import (
            valuation_constraint_block,
        )
        block = valuation_constraint_block(
            {"base_value": 4064.0,
             "valuation_sanity": {
                 "mismatch": True, "ratio": 11.65,
                 "base_value": 4064.0, "market_price": 349.0}},
            {"current_price": 349.0},
        )
        assert "PRODUCT FORM" in block
        assert "OBSERVATION" in block

    def test_normal_block_has_no_product_form_rule(self):
        from aegis.core.chief_analyst.thesis_synthesizer import (
            valuation_constraint_block,
        )
        block = valuation_constraint_block(
            {"base_value": 100.0,
             "valuation_sanity": {"mismatch": False, "ratio": 1.1}},
            {"current_price": 90.0},
        )
        assert "PRODUCT FORM" not in block


# ---------------------------------------------------------------------------
# ⑤ audit_scores 分数提取 + 聚合
# ---------------------------------------------------------------------------

def _load_audit_scores():
    path = (Path(__file__).resolve().parents[2] / "scripts" / "audit_scores.py")
    spec = importlib.util.spec_from_file_location("audit_scores", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAuditScores:

    def test_extract_score_bold_style(self):
        mod = _load_audit_scores()
        assert mod.extract_score("### 可用于真实投研的可信度：**3 / 10**") == 3.0

    def test_extract_score_takes_last_match(self):
        mod = _load_audit_scores()
        text = "正文里提过 8/10 的别家评分。\n\n## 总评\n可信度 **3.5 / 10**"
        assert mod.extract_score(text) == 3.5

    def test_extract_score_rejects_out_of_range_and_empty(self):
        mod = _load_audit_scores()
        assert mod.extract_score("涨了 15 / 10 倍不算分") is None
        assert mod.extract_score("") is None

    def test_collect_dedupes_plain_copy_and_aggregates(self, tmp_path):
        mod = _load_audit_scores()
        (tmp_path / "300750_audit_run1.md").write_text("**4 / 10**", encoding="utf-8")
        (tmp_path / "300750_audit_run2.md").write_text("**3 / 10**", encoding="utf-8")
        # run1 的兼容副本——不得重复计数
        (tmp_path / "300750_audit.md").write_text("**4 / 10**", encoding="utf-8")
        # 单次采样的旧式产物
        (tmp_path / "600519_audit.md").write_text("可信度：3.5/10", encoding="utf-8")
        # 干扰文件：prompt 不参与
        (tmp_path / "300750_prompt.md").write_text("9 / 10", encoding="utf-8")
        scores = mod.collect_scores(tmp_path)
        assert scores == {"300750": [4.0, 3.0], "600519": [3.5]}
        summary = mod.summarize(scores)
        assert summary["tickers"]["300750"]["mean"] == 3.5
        assert summary["tickers"]["300750"]["n"] == 2
        assert summary["overall_mean"] == 3.5
        assert summary["n_tickers"] == 2
