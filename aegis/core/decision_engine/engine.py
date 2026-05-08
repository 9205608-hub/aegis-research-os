"""Decision Engine — Section 21.

Responsibilities:
1. Aggregate observations and supported inferences
2. Explicitly list unresolved conflicts
3. Determine publishing status via Publish Gate
4. Generate standardized thesis object (with edge assessment)
5. Generate scenario matrix reference
6. Generate portfolio signal
7. Register monitorables and kill criteria

Principles:
- No pseudo-mathematical total score
- Only integrate validated and reviewed objects
- Unresolved conflicts must be explicitly exposed
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from aegis.data_contracts.critic_result_schema import CriticResult
from aegis.data_contracts.edge_assessment_schema import EdgeAssessment
from aegis.data_contracts.judgment_schema import JudgmentContract
from aegis.data_contracts.scenario_schema import ScenarioMatrix


@dataclass
class UnresolvedConflict:
    """A conflict between agent judgments that was not resolved."""

    topic: str
    conflicting_judgment_ids: list[str]
    description: str
    resolution_suggestion: str


@dataclass
class ThesisDecision:
    """The decision engine's output — everything needed to construct a thesis."""

    entity_id: str
    run_id: str
    publishable: bool
    publishing_status: str  # "published", "blocked", "downgraded"

    # Aggregated content
    core_thesis: str
    my_variant: str
    variant_magnitude: str
    why_now: str
    market_implied_story: str
    counter_thesis: str
    key_assumption_disagreement: str

    # Edge
    edge_assessment: EdgeAssessment | None = None

    # Scenarios
    scenario_matrix_id: str = ""
    bear_case_value: float | None = None
    base_case_value: float | None = None
    bull_case_value: float | None = None
    scenario_narratives: dict[str, str] = field(default_factory=dict)
    scenario_probabilities: dict[str, float] = field(default_factory=dict)
    probability_weighted_value: float | None = None
    primary_swing_factor: str = ""

    # Risk
    kill_criteria: list[dict] = field(default_factory=list)
    monitorables: list[dict] = field(default_factory=list)
    fragility_points: list[str] = field(default_factory=list)

    # Quality
    unresolved_conflicts: list[UnresolvedConflict] = field(default_factory=list)
    critic_summary: dict[str, str] = field(default_factory=dict)
    confidence_bucket: str = "medium"
    bias_check_status: str = "passed"

    # Forecast bridge (DCF projections + assumptions)
    dcf_projections: list[dict] = field(default_factory=list)
    dcf_assumptions: dict = field(default_factory=dict)
    tv_pct: float | None = None
    sensitivity_rankings: list[dict] = field(default_factory=list)
    sensitivity_table: dict | None = None

    # Variant decomposition
    variant_decomposition: list[dict] = field(default_factory=list)

    # Open research questions from agent follow-ups
    open_questions: list[dict] = field(default_factory=list)

    # Metadata
    macro_dependency: str = ""
    sector_cycle_position: str = ""
    management_quality_summary: str = ""
    capital_allocation_assessment: str = ""


class DecisionEngine:
    """Aggregates agent judgments and critic reviews into a thesis decision.

    Section 21: no pseudo-math scores, only structured aggregation.
    """

    def decide(
        self,
        entity_id: str,
        run_id: str,
        judgments: list[JudgmentContract],
        critic_results: list[CriticResult],
        publish_gate_passed: bool,
        context: dict[str, Any] | None = None,
        synthesized_thesis: Any | None = None,
    ) -> ThesisDecision:
        ctx = context or {}

        # Detect unresolved conflicts
        conflicts = self._detect_conflicts(judgments)

        # Determine publishing status
        if not publish_gate_passed:
            status = "blocked"
        elif conflicts:
            status = "downgraded"
        else:
            status = "published"

        # Aggregate bias check
        bias_status = self._aggregate_bias_status(critic_results)

        # Use LLM-synthesized thesis if available, otherwise fall back to keyword extraction
        if synthesized_thesis is not None:
            summaries = self._from_synthesized_thesis(synthesized_thesis)
        else:
            summaries = self._extract_summaries(judgments)

        # Build edge assessment from context (or synthesized thesis)
        edge = ctx.get("edge_assessment")

        # If we have a synthesized thesis, upgrade edge assessment with LLM-generated content
        if synthesized_thesis is not None and edge is not None:
            # EdgeAssessment is a frozen model — use model_copy to update fields
            updates: dict = {}
            synth_fields = [
                ('why_market_is_wrong', 'why_market_is_wrong'),
                ('what_would_change_my_mind', 'what_would_change_my_mind'),
                ('edge_source', 'edge_source'),
                ('edge_durability', 'edge_durability'),
            ]
            for edge_field, synth_field in synth_fields:
                synth_val = getattr(synthesized_thesis, synth_field, None)
                if synth_val:
                    updates[edge_field] = synth_val
            if updates:
                if isinstance(edge, dict):
                    # dict-based edge from auto_research
                    for k, v in updates.items():
                        edge[k] = v
                else:
                    # Pydantic frozen model — must use model_copy
                    try:
                        edge = edge.model_copy(update=updates)
                    except Exception:
                        # Fallback: convert to dict, update, leave as dict
                        edge_dict = edge.model_dump() if hasattr(edge, 'model_dump') else dict(edge)
                        edge_dict.update(updates)
                        edge = edge_dict

        # Scenarios from context
        scenarios = ctx.get("scenarios", {})

        # Build critic summary
        critic_summary = {}
        for cr in critic_results:
            critic_summary[cr.critic_type] = (
                f"{'BLOCK' if cr.block_publish else 'PASS'} "
                f"({len(cr.issues)} issues, risk={cr.overall_risk})"
            )

        return ThesisDecision(
            entity_id=entity_id,
            run_id=run_id,
            publishable=publish_gate_passed and not any(
                c for c in conflicts if c.topic == "critical"
            ),
            publishing_status=status,
            core_thesis=summaries.get("core_thesis", ""),
            my_variant=summaries.get("variant", ""),
            variant_magnitude=summaries.get("variant_magnitude", ""),
            why_now=summaries.get("why_now", ""),
            market_implied_story=summaries.get("market_implied", ""),
            counter_thesis=summaries.get("counter_thesis", ""),
            key_assumption_disagreement=summaries.get("key_disagreement", ""),
            edge_assessment=edge,
            scenario_matrix_id=scenarios.get("matrix_id", ""),
            bear_case_value=scenarios.get("bear_value"),
            base_case_value=scenarios.get("base_value"),
            bull_case_value=scenarios.get("bull_value"),
            scenario_narratives={
                "bear": scenarios.get("bear_narrative", ""),
                "base": scenarios.get("base_narrative", ""),
                "bull": scenarios.get("bull_narrative", ""),
            },
            scenario_probabilities={
                "bear": scenarios.get("bear_probability", 0.25),
                "base": scenarios.get("base_probability", 0.50),
                "bull": scenarios.get("bull_probability", 0.25),
            },
            probability_weighted_value=scenarios.get("probability_weighted_value"),
            primary_swing_factor=scenarios.get("primary_swing_factor", ""),
            kill_criteria=self._extract_kill_criteria(judgments),
            monitorables=self._extract_monitorables(judgments),
            fragility_points=self._extract_fragility_points(judgments),
            unresolved_conflicts=conflicts,
            critic_summary=critic_summary,
            confidence_bucket=self._determine_confidence(
                judgments, critic_results,
                publishing_status=status,
                publish_gate_passed=publish_gate_passed,
            ),
            bias_check_status=bias_status,
            dcf_projections=ctx.get("dcf_projections_base", []),
            dcf_assumptions=ctx.get("dcf_assumptions", {}),
            tv_pct=ctx.get("tv_pct"),
            sensitivity_rankings=ctx.get("sensitivity_rankings", []),
            sensitivity_table=ctx.get("sensitivity_table"),
            variant_decomposition=ctx.get("variant_decomposition", []),
            open_questions=ctx.get("open_questions", []),
            macro_dependency=ctx.get("macro_dependency", ""),
            sector_cycle_position=ctx.get("sector_cycle_position", ""),
            management_quality_summary=summaries.get("management", ""),
            capital_allocation_assessment=summaries.get("capital_allocation", ""),
        )

    def _from_synthesized_thesis(self, st: Any) -> dict[str, str]:
        """Extract summaries from LLM-synthesized thesis (replaces keyword matching)."""
        return {
            "core_thesis": st.core_thesis,
            "variant": st.my_variant,
            "variant_magnitude": st.variant_magnitude,
            "why_now": st.why_now,
            "market_implied": st.market_implied_story,
            "counter_thesis": st.counter_thesis,
            "key_disagreement": st.key_assumption_disagreement,
            "management": st.management_quality_summary,
            "capital_allocation": st.capital_allocation_assessment,
        }

    def _detect_conflicts(
        self, judgments: list[JudgmentContract]
    ) -> list[UnresolvedConflict]:
        """Detect contradictory inferences across agents.

        The previous implementation used naive bag-of-words matching: if an
        inference contained ANY topic keyword AND ANY sentiment keyword, it
        was labeled pos or neg on that topic. This produced false conflicts:
          - accounting_analyst: "if export controls force a greater revenue
            mix to [the] upside, margins compress" — labeled POS-growth
            because 'growth' + 'upside' both appear, but the inference is
            actually describing a downside risk.
          - One agent saying "historical growth was strong" + another saying
            "future growth will decelerate" — not a conflict, these are
            describing different time periods.

        New logic:
        1. Proximity: sentiment word must appear within 40 chars of the
           topic keyword (same clause / short phrase). This prevents "upside"
           in one clause from being glued to "growth" in another.
        2. Negation skip: if "not", "no", "never", or "without" appears
           within 10 chars before the sentiment word, ignore this inference.
        3. Noise floor: require ≥ 2 agents on each side before calling it a
           conflict. A single contrarian is not an "unresolved conflict",
           it's a minority view.
        """
        import re
        conflicts = []

        # BUG-Y47 (2026-05-06): keyword extraction was EN-only (matched
        # NVIDIA's English narratives but never CN runs). When synthesizer
        # fails for an A-share entity, this fallback path determined the
        # decision engine's contradiction detection — silently skipped.
        # Add 中文 equivalents for topics, pos/neg sentiment, and negation
        # prefixes. Topic IDs stay English so downstream rendering doesn't
        # change.
        positive: dict[str, set[str]] = {}
        negative: dict[str, set[str]] = {}
        topics_en = {
            "moat": ["moat", "competitive advantage", "pricing power"],
            "earnings_quality": ["earnings quality", "accrual quality"],
            "growth": ["growth", "accelerat", "decelerat"],
            "leverage": ["leverage", "balance sheet", "debt load"],
        }
        topics_zh = {
            "moat": ["护城河", "竞争优势", "定价权", "壁垒"],
            "earnings_quality": ["盈利质量", "应计", "现金流转化", "利润含金量"],
            "growth": ["增长", "增速", "扩张", "提速", "放缓", "降速"],
            "leverage": ["杠杆", "负债", "资产负债表", "偿债", "净负债"],
        }
        pos_en = ("strong", "durable", "sound", "robust", "healthy", "net cash")
        pos_zh = ("强劲", "稳健", "出色", "优秀", "坚挺", "韧性", "净现金", "稳固")
        neg_en = ("weak", "poor", "below par", "elevated risk", "overleveraged")
        neg_zh = ("弱", "差", "不足", "脆弱", "薄弱", "高杠杆", "过度举债")
        neg_prefixes_en = ("not ", "no ", "never ", "without ", "isn't ",
                           "is not ", "won't ", "cannot ")
        neg_prefixes_zh = ("不", "无", "并非", "并未", "并无", "毫无",
                            "尚未", "没有", "缺乏", "缺少")

        def _label(text: str, keyword: str, sentiment_en: tuple,
                   sentiment_zh: tuple) -> bool:
            """True if any sentiment word appears within 40 chars of keyword,
            and is not preceded by a negation prefix within 10 chars."""
            for km in re.finditer(re.escape(keyword), text):
                kstart = km.start()
                window_start = max(0, kstart - 40)
                window_end = min(len(text), kstart + len(keyword) + 40)
                window = text[window_start:window_end]
                # English sentiment (text is already lowered)
                for sw in sentiment_en:
                    sm = window.find(sw)
                    if sm == -1:
                        continue
                    neg_win = window[max(0, sm - 10):sm]
                    if any(p in neg_win for p in neg_prefixes_en):
                        continue
                    return True
                # Chinese sentiment (also in lowered text — CJK is case-invariant
                # so .lower() doesn't break Chinese matching)
                for sw in sentiment_zh:
                    sm = window.find(sw)
                    if sm == -1:
                        continue
                    neg_win = window[max(0, sm - 6):sm]  # CJK 1-2 chars
                    if any(p in neg_win for p in neg_prefixes_zh):
                        continue
                    return True
            return False

        for j in judgments:
            for inf in j.inferences:
                text = inf.text.lower()
                # Iterate combined English + Chinese topic keys per topic
                for topic in topics_en:
                    keywords = list(topics_en[topic]) + list(topics_zh[topic])
                    for kw in keywords:
                        if kw not in text:
                            continue
                        if _label(text, kw, pos_en, pos_zh):
                            positive.setdefault(topic, set()).add(j.agent_name)
                            break
                        if _label(text, kw, neg_en, neg_zh):
                            negative.setdefault(topic, set()).add(j.agent_name)
                            break

        # Noise floor: require ≥ 2 distinct agents on each side
        for topic in set(positive) & set(negative):
            pos_agents = positive[topic]
            neg_agents = negative[topic]
            if len(pos_agents) < 2 or len(neg_agents) < 2:
                continue
            all_agents = list(pos_agents | neg_agents)
            conflicts.append(UnresolvedConflict(
                topic=topic,
                conflicting_judgment_ids=all_agents,
                description=(
                    f"Conflicting signals on '{topic}': "
                    f"{len(pos_agents)} positive, {len(neg_agents)} negative"
                ),
                resolution_suggestion="Review which agents' analyses are better supported by evidence",
            ))

        return conflicts

    def _aggregate_bias_status(self, critic_results: list[CriticResult]) -> str:
        for cr in critic_results:
            if cr.critic_type == "cognitive_bias_critic":
                if cr.block_publish:
                    return "blocked"
                if cr.overall_risk in ("medium", "high"):
                    return "warned"
        return "passed"

    def _extract_summaries(self, judgments: list[JudgmentContract]) -> dict[str, str]:
        summaries: dict[str, str] = {}
        for j in judgments:
            if j.agent_name == "variant_analyst":
                for inf in j.inferences:
                    if "variant" in inf.text.lower():
                        summaries["variant"] = inf.text
                    if "upside" in inf.text.lower() or "downside" in inf.text.lower():
                        summaries["variant_magnitude"] = inf.text
            elif j.agent_name == "valuation_analyst":
                for inf in j.inferences:
                    if "market" in inf.text.lower() and "implies" in inf.text.lower():
                        summaries["market_implied"] = inf.text
            elif j.agent_name == "business_analyst":
                for inf in j.inferences:
                    if "moat" in inf.text.lower() or "business" in inf.text.lower():
                        summaries["core_thesis"] = inf.text
            elif j.agent_name == "management_analyst":
                for inf in j.inferences:
                    if "capital allocation" in inf.text.lower():
                        summaries["management"] = inf.text
                        summaries["capital_allocation"] = inf.text
            elif j.agent_name == "risk_analyst":
                counterargs = [ca.text for ca in j.counterarguments]
                if counterargs:
                    summaries["counter_thesis"] = counterargs[0]
        return summaries

    def _extract_kill_criteria(self, judgments: list[JudgmentContract]) -> list[dict]:
        criteria = []
        for j in judgments:
            if j.agent_name == "risk_analyst":
                for dt in j.disconfirming_triggers:
                    criteria.append({
                        "description": dt.text,
                        "threshold": "trigger event",
                        "check_frequency": dt.check_frequency,
                    })
        return criteria

    def _extract_monitorables(self, judgments: list[JudgmentContract]) -> list[dict]:
        monitorables = []
        seen_descriptions: set[str] = set()
        for j in judgments:
            for dt in j.disconfirming_triggers:
                if dt.monitorable:
                    desc_key = dt.text.strip().lower()
                    if desc_key not in seen_descriptions:
                        seen_descriptions.add(desc_key)
                        monitorables.append({
                            "description": dt.text,
                            "check_frequency": dt.check_frequency,
                            "source_agent": j.agent_name,
                        })
        return monitorables

    def _extract_fragility_points(self, judgments: list[JudgmentContract]) -> list[str]:
        points = []
        for j in judgments:
            for unc in j.self_reported_uncertainties:
                points.append(unc)
        return points[:10]  # Cap at 10

    def _determine_confidence(
        self,
        judgments: list[JudgmentContract],
        critic_results: list[CriticResult],
        publishing_status: str = "published",
        publish_gate_passed: bool = True,
    ) -> str:
        """Determine confidence bucket based on evidence and critic results.

        Uses a scoring system instead of binary block/pass:
        - Blocks reduce confidence but don't hard-floor to very_low
        - Evidence strength can offset moderate issues
        - Only extreme block counts force very_low

        BUG-37: confidence now takes publishing_status into account. A
        "downgraded" or "blocked" run cannot emit `high` confidence because
        the combination "I am highly confident in a thesis the system
        decided not to publish" is internally contradictory.
        """
        total_warns = sum(
            sum(1 for i in cr.issues if i.severity == "warn")
            for cr in critic_results
        )
        total_blocks = sum(
            sum(1 for i in cr.issues if i.severity == "block")
            for cr in critic_results
        )

        # Evidence and narrative strength
        total_evidence = sum(len(j.used_evidence_ids) for j in judgments)
        total_observations = sum(len(j.observations) for j in judgments)
        has_narratives = sum(1 for j in judgments if getattr(j, 'narrative_supplement', None))

        # Scoring: start at 70, adjust based on quality signals.
        # Block penalty is moderate (2pts) because many blocks come from
        # mock fallback observations lacking source_ids — these are systemic
        # false positives, not analytical failures.
        score = 70
        score -= total_blocks * 2        # Each block costs 2 points
        score -= total_warns * 0.5       # Each warn costs 0.5 points
        score += min(total_evidence, 20) * 1   # Evidence adds up to 20 points
        score += min(total_observations, 60) * 0.3  # Observations add up to 18
        score += has_narratives * 5      # Each narrative adds 5 points (high quality signal)

        if score >= 80:
            bucket = "high"
        elif score >= 60:
            bucket = "medium"
        elif score >= 40:
            bucket = "low"
        else:
            bucket = "very_low"

        # BUG-37: cap confidence based on publishing status.
        # - blocked  → cap at "low" (pipeline refused to publish)
        # - downgraded → cap at "medium" (pipeline flagged unresolved conflicts)
        # - published → no cap
        # Also: if publish_gate itself failed, the decision is structurally
        # unreliable regardless of score.
        order = ["very_low", "low", "medium", "high"]
        def cap(cur: str, ceiling: str) -> str:
            return cur if order.index(cur) <= order.index(ceiling) else ceiling

        if not publish_gate_passed or publishing_status == "blocked":
            bucket = cap(bucket, "low")
        elif publishing_status == "downgraded":
            bucket = cap(bucket, "medium")

        return bucket
