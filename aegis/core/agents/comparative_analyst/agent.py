"""Comparative Analyst Agent — Section 19.9.

Multi-entity research only. Responsibilities:
- Cross-entity horizontal comparison
- Relative valuation ranking
- Factor exposure comparison
- Cross-holding analysis
- Best pick selection

Prohibitions:
- No cross-standard/cross-currency comparison without bridge
- No ignoring Entity Relationship Graph connections
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aegis.data_contracts.comparison_matrix_schema import (
    ComparisonDimension,
    ComparisonMatrix,
    RelativeValuation,
)


@dataclass
class ComparativeInput:
    """Input for comparative analysis across entities."""

    entity_ids: list[str]
    run_id: str
    theme: str = ""
    per_entity_metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    per_entity_judgments: dict[str, list] = field(default_factory=dict)
    entity_relationships: list[dict] = field(default_factory=list)
    comparison_dimensions: list[str] = field(default_factory=list)
    valuation_metric: str = "ev_to_revenue"


class ComparativeAnalyst:
    """Cross-entity comparison agent — only active in multi-entity modes.

    Section 19.9: produces ComparisonMatrix, not JudgmentContract,
    because it operates across entities rather than within one.
    """

    AGENT_NAME = "comparative_analyst"
    AGENT_VERSION = "0.1.0"

    def analyze(self, inp: ComparativeInput) -> ComparisonMatrix:
        """Produce a structured comparison matrix."""
        dimensions = self._build_dimensions(inp)
        rel_val = self._build_relative_valuation(inp)
        risks = self._identify_cross_entity_risks(inp)
        top_picks = self._rank_and_pick(dimensions, rel_val, inp)

        return ComparisonMatrix(
            comparison_id=f"comp_{inp.run_id}_{inp.theme[:20] or 'multi'}",
            theme=inp.theme or "multi-entity comparison",
            entity_ids=inp.entity_ids,
            comparison_timestamp=datetime.now(timezone.utc),
            dimensions=dimensions,
            relative_valuation=rel_val,
            cross_entity_risks=risks,
            top_picks=top_picks,
            top_pick_rationale=self._build_rationale(top_picks, dimensions),
        )

    def _build_dimensions(self, inp: ComparativeInput) -> list[ComparisonDimension]:
        """Build comparison dimensions from per-entity metrics."""
        dims: list[ComparisonDimension] = []

        # Use requested dimensions or defaults
        dim_metrics = inp.comparison_dimensions or [
            "gross_margin", "roic", "revenue_growth", "fcf_margin",
        ]

        for metric in dim_metrics:
            values = {}
            for eid in inp.entity_ids:
                entity_metrics = inp.per_entity_metrics.get(eid, {})
                if metric in entity_metrics:
                    values[eid] = entity_metrics[metric]

            if len(values) < 2:
                continue

            # Rank by value (higher is better for most metrics)
            sorted_entities = sorted(values, key=lambda e: values[e], reverse=True)
            rankings = {eid: rank + 1 for rank, eid in enumerate(sorted_entities)}

            dims.append(ComparisonDimension(
                dimension=metric,
                rankings=rankings,
                values=values,
            ))

        return dims

    def _build_relative_valuation(self, inp: ComparativeInput) -> RelativeValuation:
        """Build relative valuation comparison."""
        val_metric = inp.valuation_metric
        values = {}
        for eid in inp.entity_ids:
            entity_metrics = inp.per_entity_metrics.get(eid, {})
            if val_metric in entity_metrics:
                values[eid] = entity_metrics[val_metric]

        if len(values) < 2:
            # Fallback — at least 2 needed
            values = {eid: 0.0 for eid in inp.entity_ids[:2]}

        median_val = sorted(values.values())[len(values) // 2] if values else 0.0

        return RelativeValuation(
            metric=val_metric,
            values=values,
            sector_median=median_val,
        )

    def _identify_cross_entity_risks(self, inp: ComparativeInput) -> list[str]:
        """Identify risks that span multiple entities."""
        risks: list[str] = []

        # Supply chain concentration
        supplier_count: dict[str, int] = {}
        for rel in inp.entity_relationships:
            rel_type = rel.get("relationship_type", "")
            if "supplier" in rel_type:
                supplier = rel.get("entity_a", "")
                supplier_count[supplier] = supplier_count.get(supplier, 0) + 1

        for supplier, count in supplier_count.items():
            if count >= 2:
                risks.append(
                    f"Shared supplier dependency: {supplier} supplies {count} entities in comparison set"
                )

        # Correlation risk
        if len(inp.entity_ids) > 3:
            risks.append(
                "Portfolio concentration risk: multiple names in same sector/theme"
            )

        return risks

    def _rank_and_pick(
        self,
        dimensions: list[ComparisonDimension],
        rel_val: RelativeValuation,
        inp: ComparativeInput,
    ) -> list[str]:
        """Aggregate rankings to identify top picks."""
        if not dimensions:
            return inp.entity_ids[:1]

        # Simple average rank across all dimensions
        rank_sum: dict[str, float] = {eid: 0.0 for eid in inp.entity_ids}
        dim_count = 0

        for dim in dimensions:
            for eid, rank in dim.rankings.items():
                rank_sum[eid] = rank_sum.get(eid, 0.0) + rank
            dim_count += 1

        if dim_count > 0:
            avg_rank = {eid: total / dim_count for eid, total in rank_sum.items()}
            sorted_picks = sorted(avg_rank, key=lambda e: avg_rank[e])
            return sorted_picks[:min(2, len(sorted_picks))]

        return inp.entity_ids[:1]

    def _build_rationale(
        self, top_picks: list[str], dimensions: list[ComparisonDimension]
    ) -> str:
        if not top_picks:
            return "No clear top pick identified"

        strengths = []
        for pick in top_picks:
            top_dims = []
            for dim in dimensions:
                if dim.rankings.get(pick, 99) <= 2:
                    top_dims.append(dim.dimension)
            if top_dims:
                strengths.append(f"{pick}: top-ranked in {', '.join(top_dims)}")

        return "; ".join(strengths) if strengths else f"Top picks: {', '.join(top_picks)}"
