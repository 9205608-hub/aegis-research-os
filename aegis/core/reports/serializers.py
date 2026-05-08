"""Report Layer Serializers — Section 25.

Pure serialization layer — ZERO creative authority.

Strictly prohibited:
- Changing numbers, adding numbers, changing definitions
- Bypassing block/downgrade
- Writing open questions as definite conclusions
- Writing low confidence as conviction
- Hiding cognitive bias warnings
- Hiding edge decay risk
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any


class ReportSerializer:
    """Base serializer — converts structured data to report formats.

    Section 25: Report Layer is pure serialization, zero creative authority.
    """

    def investment_memo(self, thesis_decision: Any, context: dict) -> dict:
        """Generate investment memo from thesis decision."""
        td = thesis_decision
        return {
            "report_type": "investment_memo",
            "generated_at": datetime.now(datetime.now().astimezone().tzinfo).isoformat(),
            "entity_id": getattr(td, "entity_id", ""),
            "sections": {
                "executive_summary": {
                    "core_thesis": getattr(td, "core_thesis", ""),
                    "variant": getattr(td, "my_variant", ""),
                    "confidence": getattr(td, "confidence_bucket", ""),
                    "bias_check_status": getattr(td, "bias_check_status", ""),
                },
                "market_context": {
                    "market_implied_story": getattr(td, "market_implied_story", ""),
                    "macro_dependency": getattr(td, "macro_dependency", ""),
                    "sector_position": getattr(td, "sector_cycle_position", ""),
                },
                "valuation": {
                    "bear_case": getattr(td, "bear_case_value", None),
                    "base_case": getattr(td, "base_case_value", None),
                    "bull_case": getattr(td, "bull_case_value", None),
                    "probability_weighted_value": getattr(td, "probability_weighted_value", None),
                    "scenario_narratives": getattr(td, "scenario_narratives", {}),
                    "scenario_probabilities": getattr(td, "scenario_probabilities", {}),
                    "primary_swing_factor": getattr(td, "primary_swing_factor", ""),
                    "key_disagreement": getattr(td, "key_assumption_disagreement", ""),
                    "forecast_bridge": {
                        "projections": getattr(td, "dcf_projections", []),
                        "assumptions": getattr(td, "dcf_assumptions", {}),
                        "terminal_value_pct": getattr(td, "tv_pct", None),
                        "sensitivity_rankings": getattr(td, "sensitivity_rankings", []),
                        "sensitivity_table": getattr(td, "sensitivity_table", None),
                    },
                    "variant_bridge": getattr(td, "variant_decomposition", []),
                },
                "edge_assessment": self._serialize_edge(getattr(td, "edge_assessment", None)),
                "risks": {
                    "counter_thesis": getattr(td, "counter_thesis", ""),
                    "kill_criteria": getattr(td, "kill_criteria", []),
                    "fragility_points": getattr(td, "fragility_points", []),
                },
                "management": {
                    "quality_summary": getattr(td, "management_quality_summary", ""),
                    "capital_allocation": getattr(td, "capital_allocation_assessment", ""),
                },
                "monitoring": {
                    "monitorables": getattr(td, "monitorables", []),
                },
                "critic_summary": getattr(td, "critic_summary", {}),
                "unresolved_conflicts": [
                    {"topic": c.topic, "description": c.description}
                    for c in getattr(td, "unresolved_conflicts", [])
                ],
            },
        }

    def one_page_note(self, thesis_decision: Any) -> dict:
        """Generate one-page variant note."""
        td = thesis_decision
        return {
            "report_type": "one_page_variant_note",
            "entity_id": getattr(td, "entity_id", ""),
            "thesis": getattr(td, "core_thesis", ""),
            "variant": getattr(td, "my_variant", ""),
            "variant_magnitude": getattr(td, "variant_magnitude", ""),
            "bear_base_bull": [
                getattr(td, "bear_case_value", None),
                getattr(td, "base_case_value", None),
                getattr(td, "bull_case_value", None),
            ],
            "probability_weighted_value": getattr(td, "probability_weighted_value", None),
            "scenario_narratives": getattr(td, "scenario_narratives", {}),
            "edge": self._serialize_edge(getattr(td, "edge_assessment", None)),
            "key_risk": getattr(td, "counter_thesis", ""),
            "confidence": getattr(td, "confidence_bucket", ""),
            "bias_warnings": getattr(td, "bias_check_status", ""),
        }

    def dashboard_json(self, thesis_decision: Any) -> dict:
        """Generate dashboard-consumable JSON."""
        td = thesis_decision
        return {
            "report_type": "dashboard_json",
            "entity_id": getattr(td, "entity_id", ""),
            "status": getattr(td, "publishing_status", ""),
            "publishable": getattr(td, "publishable", False),
            "confidence": getattr(td, "confidence_bucket", ""),
            "bear": getattr(td, "bear_case_value", None),
            "base": getattr(td, "base_case_value", None),
            "bull": getattr(td, "bull_case_value", None),
            "probability_weighted_value": getattr(td, "probability_weighted_value", None),
            "scenario_probabilities": getattr(td, "scenario_probabilities", {}),
            "bias_status": getattr(td, "bias_check_status", ""),
            "kill_criteria_count": len(getattr(td, "kill_criteria", [])),
            "monitorable_count": len(getattr(td, "monitorables", [])),
            "conflict_count": len(getattr(td, "unresolved_conflicts", [])),
        }

    def comparison_table(self, comparison_matrix: Any) -> dict:
        """Generate comparison table from ComparisonMatrix."""
        cm = comparison_matrix
        return {
            "report_type": "sector_comparison_table",
            "theme": getattr(cm, "theme", ""),
            "entities": getattr(cm, "entity_ids", []),
            "dimensions": [
                {"name": d.dimension, "rankings": d.rankings, "values": d.values}
                for d in getattr(cm, "dimensions", [])
            ],
            "top_picks": getattr(cm, "top_picks", []),
            "rationale": getattr(cm, "top_pick_rationale", ""),
        }

    def _serialize_edge(self, edge: Any) -> dict:
        if not edge:
            return {"available": False}
        return {
            "available": True,
            "type": str(getattr(edge, "primary_edge_type", "")),
            "source": getattr(edge, "edge_source", ""),
            "durability": str(getattr(edge, "edge_durability", "")),
            "decay_trigger": getattr(edge, "edge_decay_trigger", ""),
            "why_market_wrong": getattr(edge, "why_market_is_wrong", ""),
            "what_changes_mind": getattr(edge, "what_would_change_my_mind", ""),
        }
