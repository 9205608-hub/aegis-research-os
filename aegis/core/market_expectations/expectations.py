"""Market Expectations Layer — Section 14.

Core insight: investment research is about
  Variant = My View − What Market Already Prices

This layer captures what the market prices in, so agents can identify
where their view diverges.

Components:
- Consensus snapshot management
- Revision tracking
- Implied assumption solver (wraps ReverseDCFSolver)
- Priced-in object construction
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from aegis.data_contracts.consensus_snapshot_schema import ConsensusSnapshot
from aegis.data_contracts.scenario_schema import KeyAssumptionDisagreement


@dataclass(frozen=True)
class PricedInAssumptions:
    """What the market is currently pricing in for an entity."""

    entity_id: str
    as_of: datetime
    current_price: float
    implied_revenue_growth: float | None = None
    implied_terminal_growth: float | None = None
    implied_margin: float | None = None
    consensus_eps_fwd: float | None = None
    consensus_revenue_fwd: float | None = None
    pe_ratio_fwd: float | None = None
    ev_to_revenue_fwd: float | None = None
    revision_momentum: str | None = None  # "positive", "negative", "flat"
    methodology_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConsensusRevisionSignal:
    """Tracks direction and magnitude of consensus revisions."""

    entity_id: str
    metric_id: str
    period: str
    revision_1w_pct: float | None = None
    revision_1m_pct: float | None = None
    revision_3m_pct: float | None = None
    momentum: str = "flat"  # "positive", "negative", "flat"
    breadth: str = "unknown"  # "broad_upgrade", "broad_downgrade", "mixed"
    acceleration: str = "unknown"  # "accelerating", "decelerating", "stable"


class MarketExpectationsLayer:
    """Captures what the market currently prices and consensus expects.

    Section 14.3 principles:
    1. variant_analyst cannot just guess at consensus — must use data.
    2. No thesis can claim to be a "variant" without a priced_in_object.
    3. Consensus data must have explicit snapshot timestamp.
    """

    def __init__(self) -> None:
        self._consensus: dict[str, list[ConsensusSnapshot]] = {}  # entity_id -> snapshots
        self._price_cache: dict[str, float] = {}  # entity_id -> current price

    def add_consensus(self, snapshot: ConsensusSnapshot) -> None:
        """Add a consensus snapshot."""
        self._consensus.setdefault(snapshot.entity_id, []).append(snapshot)

    def set_current_price(self, entity_id: str, price: float) -> None:
        """Set the current market price for an entity."""
        self._price_cache[entity_id] = price

    def get_consensus_for_metric(
        self, entity_id: str, metric_id: str, period: str
    ) -> ConsensusSnapshot | None:
        """Get the latest consensus for a specific metric and period."""
        snapshots = self._consensus.get(entity_id, [])
        matching = [
            s for s in snapshots
            if s.metric_id == metric_id and s.period == period
        ]
        if not matching:
            return None
        return max(matching, key=lambda s: s.snapshot_timestamp)

    def get_revision_signal(
        self, entity_id: str, metric_id: str, period: str
    ) -> ConsensusRevisionSignal | None:
        """Derive a revision signal from consensus data."""
        cs = self.get_consensus_for_metric(entity_id, metric_id, period)
        if not cs:
            return None

        # Determine momentum from revisions
        revisions = [cs.revision_1w, cs.revision_1m, cs.revision_3m]
        non_null = [r for r in revisions if r is not None]
        if not non_null:
            momentum = "flat"
        elif all(r > 0 for r in non_null):
            momentum = "positive"
        elif all(r < 0 for r in non_null):
            momentum = "negative"
        else:
            momentum = "flat"

        # Determine acceleration: is the revision pace speeding up or slowing?
        acceleration = "stable"
        if cs.revision_1w is not None and cs.revision_1m is not None:
            # Compare weekly pace to monthly pace (annualized)
            weekly_annualized = cs.revision_1w * 4  # rough monthly equivalent
            if abs(weekly_annualized) > abs(cs.revision_1m) * 1.2:
                acceleration = "accelerating"
            elif abs(weekly_annualized) < abs(cs.revision_1m) * 0.5:
                acceleration = "decelerating"

        return ConsensusRevisionSignal(
            entity_id=entity_id,
            metric_id=metric_id,
            period=period,
            revision_1w_pct=cs.revision_1w,
            revision_1m_pct=cs.revision_1m,
            revision_3m_pct=cs.revision_3m,
            momentum=momentum,
            acceleration=acceleration,
        )

    def get_aggregate_revision_signal(
        self, entity_id: str,
    ) -> ConsensusRevisionSignal | None:
        """Compute breadth-aware revision signal across all metrics/periods.

        Aggregates individual metric signals to determine whether revisions
        are broadly upgrading, broadly downgrading, or mixed.
        """
        snapshots = self._consensus.get(entity_id, [])
        if not snapshots:
            return None

        # Collect individual signals
        signals: list[ConsensusRevisionSignal] = []
        seen: set[tuple[str, str]] = set()
        for s in snapshots:
            key = (s.metric_id, s.period)
            if key not in seen:
                seen.add(key)
                sig = self.get_revision_signal(entity_id, s.metric_id, s.period)
                if sig:
                    signals.append(sig)

        if not signals:
            return None

        # Determine breadth
        momenta = [s.momentum for s in signals]
        pos_count = momenta.count("positive")
        neg_count = momenta.count("negative")
        total = len(momenta)

        if pos_count >= total * 0.7:
            breadth = "broad_upgrade"
        elif neg_count >= total * 0.7:
            breadth = "broad_downgrade"
        else:
            breadth = "mixed"

        # Pick revenue signal as the primary for top-level fields
        primary = next(
            (s for s in signals if s.metric_id == "revenue"),
            signals[0],
        )

        return ConsensusRevisionSignal(
            entity_id=entity_id,
            metric_id="aggregate",
            period="all",
            revision_1w_pct=primary.revision_1w_pct,
            revision_1m_pct=primary.revision_1m_pct,
            revision_3m_pct=primary.revision_3m_pct,
            momentum=primary.momentum,
            breadth=breadth,
            acceleration=primary.acceleration,
        )

    def ingest_consensus_estimates(
        self,
        entity_id: str,
        estimates: list,
    ) -> int:
        """Populate the layer from ConsensusEstimate objects (from OpenBB connector).

        Converts ConsensusEstimate → ConsensusSnapshot and stores in memory.
        Returns the count of snapshots added.
        """
        from aegis.data_contracts.consensus_snapshot_schema import ConsensusSnapshot

        count = 0
        for est in estimates:
            # Build a snapshot from the estimate
            snapshot = ConsensusSnapshot(
                snapshot_id=f"cs_{entity_id}_{est.metric}_{est.period}_{est.fetched_at or 'now'}",
                entity_id=entity_id,
                snapshot_timestamp=datetime.now(timezone.utc),
                metric_id=est.metric,
                definition_id=f"def_{est.metric}",
                period=est.period,
                period_type=getattr(est, "period_type", "annual"),
                consensus_mean=est.consensus_mean,
                consensus_median=est.consensus_mean,  # yfinance doesn't provide median
                estimate_count=est.analyst_count,
                high_estimate=est.consensus_high,
                low_estimate=est.consensus_low,
                standard_deviation=abs(est.consensus_high - est.consensus_low) / 4
                    if (est.consensus_high and est.consensus_low) else 0.0,
                unit="USD",
                source=getattr(est, "source", "yfinance"),
                source_tier=2,
                ingestion_batch_id=f"batch_{entity_id}",
            )
            self.add_consensus(snapshot)
            count += 1

        return count

    def build_priced_in_object(
        self,
        entity_id: str,
        implied_growth: float | None = None,
        implied_terminal_growth: float | None = None,
        implied_margin: float | None = None,
        wacc: float | None = None,
        # BUG-Y20 follow-up (2026-05-06): caller passes True when the
        # bisection didn't converge so the printed notes don't carry a
        # fake-clean value like "50.0%".
        implied_growth_unreliable: bool = False,
    ) -> PricedInAssumptions:
        """Build a PricedInAssumptions from consensus + reverse DCF results.

        The implied_growth/terminal_growth should come from ReverseDCFSolver
        (already built in Phase 1), not computed here.
        """
        price = self._price_cache.get(entity_id, 0.0)

        # Gather consensus metrics
        eps_cs = self.get_consensus_for_metric(entity_id, "eps", "FY_forward")
        rev_cs = self.get_consensus_for_metric(entity_id, "revenue", "FY_forward")
        rev_signal = self.get_revision_signal(entity_id, "revenue", "FY_forward")

        consensus_eps = eps_cs.consensus_mean if eps_cs else None
        consensus_rev = rev_cs.consensus_mean if rev_cs else None
        pe_fwd = (price / consensus_eps) if (consensus_eps and price > 0) else None

        notes = []
        if implied_growth_unreliable:
            notes.append(
                "Implied revenue growth from reverse DCF: n/a "
                "(bisection non-convergent; price-vs-growth non-monotonic for "
                "loss-making/high-capex profile)"
            )
        elif implied_growth is not None:
            notes.append(f"Implied revenue growth from reverse DCF: {implied_growth:.2%}")
        if implied_terminal_growth is not None:
            notes.append(f"Implied terminal growth: {implied_terminal_growth:.2%}")

        return PricedInAssumptions(
            entity_id=entity_id,
            as_of=datetime.now(timezone.utc),
            current_price=price,
            implied_revenue_growth=implied_growth,
            implied_terminal_growth=implied_terminal_growth,
            implied_margin=implied_margin,
            consensus_eps_fwd=consensus_eps,
            consensus_revenue_fwd=consensus_rev,
            pe_ratio_fwd=pe_fwd,
            revision_momentum=rev_signal.momentum if rev_signal else None,
            methodology_notes=notes,
        )

    def build_key_assumption_disagreements(
        self,
        entity_id: str,
        my_assumptions: dict[str, dict],
    ) -> list[KeyAssumptionDisagreement]:
        """Build disagreement objects comparing our view to market-implied.

        my_assumptions format:
        {
            "revenue_growth": {
                "bear": "12%", "base": "18%", "bull": "25%",
                "market_implied": "15%", "my_view": "18%",
                "is_variant": True,
            }
        }
        """
        disagreements = []
        for assumption, values in my_assumptions.items():
            disagreements.append(KeyAssumptionDisagreement(
                assumption=assumption,
                bear_value=values.get("bear", ""),
                base_value=values.get("base", ""),
                bull_value=values.get("bull", ""),
                market_implied=values.get("market_implied", ""),
                my_view=values.get("my_view", ""),
                this_is_the_variant=values.get("is_variant", False),
            ))
        return disagreements
