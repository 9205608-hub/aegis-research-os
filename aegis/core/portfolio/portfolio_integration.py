"""Portfolio Integration Interface — Section 22.

Generates PM-consumable signals from thesis decisions.

Section 22.3 principles:
1. Does NOT make final investment decisions.
2. Position sizing is a hint, not a directive — human PM decides.
3. Risk interaction check is mandatory.
4. Edge durability must be reflected in signal horizon.
5. Kill criteria and monitorables must propagate to signal.
6. Data quality tier reflects source + critic quality.
7. Catalyst calendar feeds signal review schedule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any


@dataclass(frozen=True)
class RiskInteraction:
    """A risk interaction between a new signal and existing portfolio."""

    interaction_type: str  # "revenue_overlap", "sector_concentration", "macro_factor", "supply_chain", "edge_type"
    description: str
    severity: str  # "low", "medium", "high"
    related_entity_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CatalystEvent:
    """A structured, time-bound catalyst event that could close the variant gap.

    Catalyst events feed into signal review scheduling and horizon management.
    """

    catalyst_id: str
    entity_id: str
    description: str
    expected_date: date | None = None  # None if timing uncertain
    date_confidence: str = "low"  # "low", "medium", "high"
    catalyst_type: str = "unknown"  # "earnings", "regulatory", "product_launch", "macro", "management", "other"
    impact_if_positive: str = ""  # What happens if catalyst is favorable
    impact_if_negative: str = ""  # What happens if catalyst is unfavorable
    source_agent: str = ""


@dataclass
class PortfolioSignalOutput:
    """Output signal for portfolio management consumption.

    Contains everything a PM needs to evaluate and act on a thesis:
    - Direction and sizing guidance
    - Risk/reward quantification
    - Kill criteria for stop-loss
    - Catalyst calendar for timing
    - Data quality assessment
    """

    signal_id: str
    entity_id: str
    thesis_id: str
    signal_type: str = "thesis_based"  # "thesis_based", "event_driven", "rebalance"
    direction: str = "no_signal"  # "long", "short", "no_signal"
    conviction: str = "medium"  # ConfidenceBucket values
    sizing_tier: str = "no_position"  # "full_position", "standard_position", "starter_position", "no_position"
    variant_magnitude: str = ""
    edge_type: str = ""
    edge_durability: str = ""
    upside_pct: float | None = None
    downside_pct: float | None = None
    risk_reward_ratio: float | None = None
    expected_value_per_share: float | None = None  # Probability-weighted scenario value
    thesis_horizon: str = ""
    risk_interactions: list[RiskInteraction] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    review_date: date = field(default_factory=date.today)

    # Kill criteria — PM must exit if any of these trigger
    kill_criteria: list[dict] = field(default_factory=list)
    # Monitorables — PM must track these
    monitorables_summary: list[str] = field(default_factory=list)
    # Catalyst calendar
    catalysts: list[CatalystEvent] = field(default_factory=list)
    next_catalyst_date: date | None = None

    # Data quality
    data_quality_tier: str = "C"  # A (highest) through D (lowest)
    quality_notes: list[str] = field(default_factory=list)

    # Publishing
    publishing_status: str = "draft"


class PortfolioIntegration:
    """Translates thesis decisions into portfolio management signals.

    Section 22: this interface is advisory, not executive.
    """

    def generate_signal(
        self,
        thesis_decision: Any,
        existing_positions: list[dict] | None = None,
        entity_relationships: list[dict] | None = None,
        scenario_weights: dict[str, float] | None = None,
    ) -> PortfolioSignalOutput:
        """Generate a portfolio signal from a thesis decision.

        Args:
            thesis_decision: ThesisDecision from DecisionEngine
            existing_positions: Current portfolio for risk interaction check
            entity_relationships: Entity graph for supply chain risk check
            scenario_weights: Optional probability weights for expected value
                e.g., {"bear": 0.25, "base": 0.50, "bull": 0.25}
        """
        td = thesis_decision

        # Determine direction
        direction = self._determine_direction(td)

        # Determine conviction and sizing
        conviction = getattr(td, "confidence_bucket", "medium")
        sizing = self._determine_sizing(td, direction)

        # Calculate risk/reward + expected value
        upside, downside, rr_ratio = self._calculate_risk_reward(td)
        expected_value = self._calculate_expected_value(td, scenario_weights)

        # Risk interaction check (Section 22.2)
        interactions = self._check_risk_interactions(
            td, existing_positions or [], entity_relationships or []
        )

        # Risk flags from fragility points
        risk_flags = list(getattr(td, "fragility_points", []))[:10]

        # Edge assessment
        edge_assessment = getattr(td, "edge_assessment", None)
        edge_type = edge_assessment.primary_edge_type if edge_assessment else "analytical"
        edge_dur = edge_assessment.edge_durability if edge_assessment else "medium_term"

        # Kill criteria propagation
        kill_criteria = getattr(td, "kill_criteria", [])

        # Monitorables summary (top items)
        monitorables = getattr(td, "monitorables", [])
        monitorables_summary = [
            m.get("description", "")[:100]
            for m in monitorables[:10]
            if isinstance(m, dict)
        ]

        # Catalyst extraction from agent inferences
        catalysts = self._extract_catalysts(td)
        next_catalyst = min(
            (c.expected_date for c in catalysts if c.expected_date),
            default=None,
        )

        # Data quality tier
        data_quality, quality_notes = self._compute_data_quality_tier(td)

        # Review date = next catalyst date or edge horizon
        horizon = self._horizon_from_edge(str(edge_dur))
        review_date = next_catalyst or (date.today() + self._horizon_to_delta(horizon))

        return PortfolioSignalOutput(
            signal_id=f"sig_{td.entity_id}_{td.run_id}",
            entity_id=td.entity_id,
            thesis_id=f"th_{td.entity_id}",
            signal_type="thesis_based",
            direction=direction,
            conviction=conviction,
            sizing_tier=sizing,
            variant_magnitude=getattr(td, "variant_magnitude", "unknown"),
            edge_type=str(edge_type),
            edge_durability=str(edge_dur),
            upside_pct=upside,
            downside_pct=downside,
            risk_reward_ratio=rr_ratio,
            expected_value_per_share=expected_value,
            thesis_horizon=horizon,
            risk_interactions=interactions,
            risk_flags=risk_flags,
            review_date=review_date,
            kill_criteria=kill_criteria,
            monitorables_summary=monitorables_summary,
            catalysts=catalysts,
            next_catalyst_date=next_catalyst,
            data_quality_tier=data_quality,
            quality_notes=quality_notes,
            publishing_status=getattr(td, "publishing_status", "draft"),
        )

    def _determine_direction(self, td: Any) -> str:
        """Map variant/thesis to direction signal.

        Uses variant text, DCF base vs price, and edge assessment.
        """
        if not getattr(td, "publishable", False):
            return "no_signal"

        variant = getattr(td, "my_variant", "").lower()

        # Check variant text for explicit direction
        if "upside" in variant or "undervalued" in variant:
            return "long"
        elif "downside" in variant or "overvalued" in variant:
            return "short"

        # Fallback: use DCF base case vs current price
        base_val = getattr(td, "base_case_value", None)
        bear_val = getattr(td, "bear_case_value", None)
        bull_val = getattr(td, "bull_case_value", None)
        edge = getattr(td, "edge_assessment", None)
        why_wrong = getattr(edge, "why_market_is_wrong", "") if edge else ""

        # Extract current price from why_market_is_wrong text if available
        import re
        price_match = re.search(r'current \$(\d+)', why_wrong)
        if base_val and price_match:
            current_price = float(price_match.group(1))
            if current_price <= 0:
                return "hold"
            gap_pct = (base_val - current_price) / current_price
            if gap_pct > 0.10:   # >10% upside
                return "long"
            elif gap_pct < -0.10:  # >10% downside
                return "short"

        # Published with no clear direction
        if getattr(td, "publishing_status", "") == "published":
            return "hold"
        return "no_signal"

    def _determine_sizing(self, td: Any, direction: str) -> str:
        """Section 22.1 sizing tiers.

        Sizing is determined by conviction × edge durability × fragility.
        """
        if direction == "no_signal":
            return "no_position"

        conviction = getattr(td, "confidence_bucket", "medium")
        edge = getattr(td, "edge_assessment", None)
        edge_dur = str(edge.edge_durability) if edge else "medium_term"
        fragility_count = len(getattr(td, "fragility_points", []))

        # Base sizing from conviction
        if conviction in ("high", "very_high"):
            base_tier = "full_position"
        elif conviction == "medium":
            base_tier = "standard_position"
        else:
            base_tier = "starter_position"

        # Edge durability derating: short-term edge → downgrade one tier
        if "short" in edge_dur and base_tier == "full_position":
            base_tier = "standard_position"

        # Fragility derating: many fragility points → downgrade
        if fragility_count > 7 and base_tier in ("full_position", "standard_position"):
            base_tier = "starter_position"

        return base_tier

    def _calculate_risk_reward(self, td: Any) -> tuple[float | None, float | None, float | None]:
        """Calculate upside/downside percentages and risk/reward ratio."""
        base = getattr(td, "base_case_value", None)
        bear = getattr(td, "bear_case_value", None)
        bull = getattr(td, "bull_case_value", None)

        if not base or not bear or not bull:
            return None, None, None

        upside = (bull - base) / base if base > 0 else None
        downside = (bear - base) / base if base > 0 else None
        rr_ratio = abs(upside / downside) if upside and downside and downside != 0 else None

        return upside, downside, rr_ratio

    def _calculate_expected_value(
        self, td: Any, weights: dict[str, float] | None = None
    ) -> float | None:
        """Compute probability-weighted expected value per share.

        Default weights: bear 25%, base 50%, bull 25%.
        """
        bear = getattr(td, "bear_case_value", None)
        base = getattr(td, "base_case_value", None)
        bull = getattr(td, "bull_case_value", None)

        if bear is None or base is None or bull is None:
            return None

        w = weights or {"bear": 0.25, "base": 0.50, "bull": 0.25}
        return (
            w.get("bear", 0.25) * bear
            + w.get("base", 0.50) * base
            + w.get("bull", 0.25) * bull
        )

    def _extract_catalysts(self, td: Any) -> list[CatalystEvent]:
        """Extract catalyst events from thesis decision context.

        Catalysts come from:
        1. Edge assessment (decay trigger is a negative catalyst)
        2. Monitorables with time-bound triggers
        3. Kill criteria with observable thresholds
        """
        catalysts: list[CatalystEvent] = []
        entity_id = getattr(td, "entity_id", "")

        # Edge decay as a catalyst to watch
        edge = getattr(td, "edge_assessment", None)
        if edge:
            decay = getattr(edge, "edge_decay_trigger", "")
            if decay:
                catalysts.append(CatalystEvent(
                    catalyst_id=f"cat_edge_decay_{entity_id}",
                    entity_id=entity_id,
                    description=f"Edge decay: {decay}",
                    catalyst_type="other",
                    impact_if_positive="Edge preserved — thesis intact",
                    impact_if_negative="Edge absorbed by market — variant closes",
                    source_agent="variant_analyst",
                ))

        # Quarterly monitorables as recurring catalysts
        for m in getattr(td, "monitorables", [])[:5]:
            if isinstance(m, dict) and m.get("check_frequency") in ("quarterly", "monthly"):
                catalysts.append(CatalystEvent(
                    catalyst_id=f"cat_monitor_{entity_id}_{len(catalysts)}",
                    entity_id=entity_id,
                    description=m.get("description", "")[:120],
                    catalyst_type="earnings" if "quarter" in m.get("description", "").lower() else "other",
                    source_agent=m.get("source_agent", "system"),
                ))

        return catalysts

    def _compute_data_quality_tier(self, td: Any) -> tuple[str, list[str]]:
        """Compute data quality tier from critic results and publishing status.

        A = Published with no warnings
        B = Published with warnings
        C = Downgraded (conflicts or moderate issues)
        D = Blocked (block-level issues)
        """
        notes: list[str] = []
        status = getattr(td, "publishing_status", "draft")
        bias_status = getattr(td, "bias_check_status", "passed")
        confidence = getattr(td, "confidence_bucket", "medium")

        if status == "blocked":
            notes.append("Thesis blocked by publish gate")
            return "D", notes
        if status == "downgraded":
            notes.append("Thesis downgraded due to unresolved conflicts")
            if bias_status == "warned":
                notes.append("Cognitive bias warnings present")
            return "C", notes
        if bias_status == "warned":
            notes.append("Cognitive bias warnings — review before acting")
            return "B", notes
        if confidence in ("high", "very_high"):
            return "A", ["Clean thesis with high confidence"]
        return "B", ["Published with standard confidence"]

    def _check_risk_interactions(
        self,
        td: Any,
        existing: list[dict],
        relationships: list[dict],
    ) -> list[RiskInteraction]:
        """Section 22.2: mandatory risk interaction check."""
        interactions: list[RiskInteraction] = []
        entity_id = td.entity_id

        # Sector concentration
        entity_sector = getattr(td, "sector_cycle_position", "")
        for pos in existing:
            if pos.get("sector", "") == entity_sector and entity_sector:
                interactions.append(RiskInteraction(
                    interaction_type="sector_concentration",
                    description=f"Same sector as existing position: {pos.get('entity_id', '')}",
                    severity="medium",
                    related_entity_ids=[pos.get("entity_id", "")],
                ))

        # Supply chain dependency
        for rel in relationships:
            rel_type = rel.get("relationship_type", "")
            other = rel.get("entity_b") if rel.get("entity_a") == entity_id else rel.get("entity_a", "")
            if other in [p.get("entity_id") for p in existing]:
                if "supplier" in rel_type or "customer" in rel_type:
                    interactions.append(RiskInteraction(
                        interaction_type="supply_chain",
                        description=f"Supply chain link with existing position: {other}",
                        severity="high",
                        related_entity_ids=[other],
                    ))

        return interactions

    def _horizon_from_edge(self, edge_durability: str) -> str:
        """Edge durability → signal horizon."""
        mapping = {
            "short_term": "3_months",
            "EdgeDurability.SHORT_TERM": "3_months",
            "medium_term": "12_months",
            "EdgeDurability.MEDIUM_TERM": "12_months",
            "long_term": "24_months",
            "EdgeDurability.LONG_TERM": "24_months",
        }
        return mapping.get(edge_durability, "12_months")

    @staticmethod
    def _horizon_to_delta(horizon: str) -> timedelta:
        """Convert horizon string to timedelta for review date calculation."""
        mapping = {
            "3_months": timedelta(days=90),
            "6_months": timedelta(days=180),
            "12_months": timedelta(days=365),
            "24_months": timedelta(days=730),
        }
        return mapping.get(horizon, timedelta(days=365))
