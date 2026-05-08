"""Macro Context Layer — Section 13.

Principles:
1. Macro layer does NOT forecast — it describes current state and maps transmission paths.
2. Macro judgments must be annotated with confidence and source.
3. Agents cannot ignore macro context when doing entity-level valuation.
4. Macro snapshots must refresh at least weekly; stale (>30 days) snapshots are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from aegis.data_contracts.macro_snapshot_schema import MacroSnapshot


@dataclass(frozen=True)
class MacroTransmissionPath:
    """Defines how a macro variable transmits to a sector/entity."""

    macro_variable: str
    transmission_channel: str  # e.g. "higher rates → higher discount rate → lower DCF values"
    affected_sectors: list[str]
    impact_direction: str  # "positive", "negative", "ambiguous"
    lag_estimate: str  # e.g. "1-2 quarters"
    strength: str  # "strong", "moderate", "weak"


@dataclass(frozen=True)
class CycleAssessment:
    """Assessment of current macro cycle position for a region."""

    region: str
    phase: str  # "early_expansion", "mid_expansion", "late_expansion", "contraction", "recovery"
    confidence: str  # "low", "medium", "high"
    key_signals: list[str]
    implications_for_equities: str


class MacroContextLayer:
    """Maintains regional macro snapshots and provides context for research.

    Section 13.3: this layer describes state, NOT forecasts.
    """

    MAX_SNAPSHOT_AGE_DAYS = 30

    def __init__(self) -> None:
        self._snapshots: dict[str, MacroSnapshot] = {}  # region -> latest snapshot
        self._transmission_paths: list[MacroTransmissionPath] = []
        self._register_default_transmissions()

    def update_snapshot(self, snapshot: MacroSnapshot) -> None:
        """Update the macro snapshot for a region."""
        self._snapshots[snapshot.region] = snapshot

    def get_snapshot(self, region: str) -> MacroSnapshot | None:
        """Get the latest macro snapshot for a region."""
        return self._snapshots.get(region)

    def is_stale(self, region: str, as_of: datetime | None = None) -> bool:
        """Check if a region's macro snapshot is stale (>30 days old)."""
        snapshot = self._snapshots.get(region)
        if not snapshot:
            return True
        reference = as_of or datetime.now(timezone.utc)
        age = reference - snapshot.snapshot_timestamp
        return age > timedelta(days=self.MAX_SNAPSHOT_AGE_DAYS)

    def get_cycle_assessment(self, region: str) -> CycleAssessment | None:
        """Derive cycle assessment from the latest macro snapshot."""
        snapshot = self._snapshots.get(region)
        if not snapshot:
            return None

        phase = snapshot.cycle_phase_estimate
        signals = []
        implications = ""

        if region == "US":
            signals = self._derive_us_signals(snapshot)
            implications = self._derive_us_equity_implications(snapshot)
        elif region == "CN":
            signals = self._derive_cn_signals(snapshot)
            implications = self._derive_cn_equity_implications(snapshot)
        else:
            signals = [f"Cycle phase: {phase}"]
            implications = f"Region {region} in {phase} phase"

        return CycleAssessment(
            region=region,
            phase=phase,
            confidence="medium",
            key_signals=signals,
            implications_for_equities=implications,
        )

    def get_transmission_paths(self, sector: str) -> list[MacroTransmissionPath]:
        """Get macro-to-micro transmission paths relevant to a sector."""
        return [
            p for p in self._transmission_paths
            if sector.lower() in [s.lower() for s in p.affected_sectors]
        ]

    def get_context_for_entity(
        self, entity_id: str, market_region: str, sector: str
    ) -> dict[str, Any]:
        """Build a macro context dict suitable for agent input."""
        snapshot = self._snapshots.get(market_region)
        cycle = self.get_cycle_assessment(market_region)
        transmissions = self.get_transmission_paths(sector)

        return {
            "region": market_region,
            "snapshot_available": snapshot is not None,
            "stale": self.is_stale(market_region),
            "cycle_phase": cycle.phase if cycle else "unknown",
            "cycle_confidence": cycle.confidence if cycle else "low",
            "key_signals": cycle.key_signals if cycle else [],
            "equity_implications": cycle.implications_for_equities if cycle else "",
            "transmission_paths": [
                {
                    "variable": t.macro_variable,
                    "channel": t.transmission_channel,
                    "direction": t.impact_direction,
                    "strength": t.strength,
                }
                for t in transmissions
            ],
            "snapshot_data": {
                "pmi_manufacturing": snapshot.pmi_manufacturing if snapshot else None,
                "pmi_services": snapshot.pmi_services if snapshot else None,
                "cpi_yoy": snapshot.cpi_yoy if snapshot else None,
                "vix": snapshot.vix if snapshot else None,
                "fed_funds_rate": snapshot.fed_funds_rate if snapshot else None,
                "lpr_1y": snapshot.lpr_1y if snapshot else None,
            },
        }

    # --- Internal helpers ---

    def _derive_us_signals(self, s: MacroSnapshot) -> list[str]:
        signals = []
        if s.pmi_manufacturing is not None:
            status = "expansionary" if s.pmi_manufacturing > 50 else "contractionary"
            signals.append(f"Manufacturing PMI: {s.pmi_manufacturing} ({status})")
        if s.yield_curve_slope_2s10s is not None:
            curve = "inverted" if s.yield_curve_slope_2s10s < 0 else "normal"
            signals.append(f"Yield curve (2s10s): {s.yield_curve_slope_2s10s:.0f}bps ({curve})")
        if s.vix is not None:
            vol = "elevated" if s.vix > 25 else "low" if s.vix < 15 else "normal"
            signals.append(f"VIX: {s.vix:.1f} ({vol})")
        if s.cpi_yoy is not None:
            signals.append(f"CPI YoY: {s.cpi_yoy:.1%}")
        return signals or ["Insufficient data for US signal assessment"]

    def _derive_cn_signals(self, s: MacroSnapshot) -> list[str]:
        signals = []
        if s.cn_pmi_official is not None:
            status = "expansionary" if s.cn_pmi_official > 50 else "contractionary"
            signals.append(f"Official PMI: {s.cn_pmi_official} ({status})")
        if s.cn_pmi_caixin is not None:
            signals.append(f"Caixin PMI: {s.cn_pmi_caixin}")
        if s.credit_pulse is not None:
            pulse = "expanding" if s.credit_pulse > 0 else "contracting"
            signals.append(f"Credit pulse: {s.credit_pulse:.1%} ({pulse})")
        if s.lpr_1y is not None:
            signals.append(f"LPR 1Y: {s.lpr_1y:.2%}")
        return signals or ["Insufficient data for CN signal assessment"]

    def _derive_us_equity_implications(self, s: MacroSnapshot) -> str:
        phase = s.cycle_phase_estimate
        if phase == "late_expansion":
            return "Late-cycle: favor quality, pricing power, low leverage"
        elif phase == "contraction":
            return "Contraction: defensive positioning, cash-rich balance sheets"
        elif phase == "early_expansion":
            return "Early-cycle: cyclicals, operating leverage, growth"
        return f"US in {phase} phase — standard positioning"

    def _derive_cn_equity_implications(self, s: MacroSnapshot) -> str:
        phase = s.cycle_phase_estimate
        if "contraction" in phase or "slowdown" in phase:
            return "CN slowdown: watch policy stimulus signals, favor state-supported sectors"
        elif "recovery" in phase:
            return "CN recovery: consumer, property-adjacent, platform economy beneficiaries"
        return f"CN in {phase} phase — monitor policy direction"

    def _register_default_transmissions(self) -> None:
        """Register standard macro-to-sector transmission paths."""
        self._transmission_paths = [
            MacroTransmissionPath(
                macro_variable="fed_funds_rate",
                transmission_channel="Higher rates → higher discount rate → lower DCF; "
                                     "higher borrowing costs for leveraged companies",
                affected_sectors=["SaaS", "Technology", "REITs", "Banking"],
                impact_direction="negative",
                lag_estimate="1-2 quarters",
                strength="strong",
            ),
            MacroTransmissionPath(
                macro_variable="pmi_manufacturing",
                transmission_channel="PMI expansion → higher industrial demand → "
                                     "semiconductor/industrial revenue growth",
                affected_sectors=["Semiconductor", "Industrial"],
                impact_direction="positive",
                lag_estimate="0-1 quarters",
                strength="strong",
            ),
            MacroTransmissionPath(
                macro_variable="cpi_yoy",
                transmission_channel="Higher inflation → consumer spending shift → "
                                     "pressure on discretionary, benefit to staples",
                affected_sectors=["Consumer Staples", "E-commerce", "Ad Platform"],
                impact_direction="ambiguous",
                lag_estimate="1-2 quarters",
                strength="moderate",
            ),
            MacroTransmissionPath(
                macro_variable="credit_pulse",
                transmission_channel="CN credit expansion → property/consumer recovery → "
                                     "platform economy transaction volume",
                affected_sectors=["China Internet", "China Consumer", "E-commerce"],
                impact_direction="positive",
                lag_estimate="1-3 quarters",
                strength="strong",
            ),
            MacroTransmissionPath(
                macro_variable="usd_dxy",
                transmission_channel="Strong USD → EM earnings headwind from translation; "
                                     "weak USD → EM tailwind",
                affected_sectors=["China Internet", "Semiconductor", "Technology"],
                impact_direction="negative",
                lag_estimate="0-1 quarters",
                strength="moderate",
            ),
        ]
