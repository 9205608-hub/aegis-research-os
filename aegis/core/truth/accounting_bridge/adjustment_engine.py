"""Accounting Standard Bridge — Section 6.2 & 6.3.

Cross-standard comparisons MUST go through this bridge.
Without it, critics should block the thesis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from aegis.data_contracts.common import AccountingStandard


class AdjustmentType(str, Enum):
    """Types of cross-standard adjustments."""

    R_AND_D_CAPITALIZATION = "r_and_d_capitalization"
    LEASE_RECLASSIFICATION = "lease_reclassification"
    INVENTORY_METHOD = "inventory_method"
    GOVERNMENT_SUBSIDY = "gov_subsidy_reclassification"
    ASSET_REVALUATION = "asset_revaluation"
    SBC_MEASUREMENT = "sbc_measurement"
    REVENUE_RECOGNITION = "revenue_recognition"
    RELATED_PARTY_DISCLOSURE = "related_party_disclosure"


@dataclass(frozen=True)
class AdjustmentRule:
    """A single cross-standard adjustment rule."""

    adjustment_id: str
    adjustment_type: AdjustmentType
    source_standard: AccountingStandard
    target_standard: AccountingStandard
    affected_metrics: list[str]
    description: str
    confidence: str  # "high", "medium", "low"
    reversible: bool = True


@dataclass(frozen=True)
class AdjustmentResult:
    """Result of applying a cross-standard adjustment."""

    adjustment_id: str
    metric: str
    original_value: float
    adjusted_value: float
    adjustment_amount: float
    source_standard: AccountingStandard
    target_standard: AccountingStandard
    confidence: str
    notes: str = ""


@dataclass(frozen=True)
class ComparabilityFlag:
    """Flag indicating cross-standard comparability status — Section 6.3."""

    comparability_flag: str  # "direct", "adjustment_required", "not_comparable"
    source_standard: AccountingStandard
    target_standard: AccountingStandard
    affected_metrics: list[str]
    adjustment_type: AdjustmentType | None = None
    adjustment_available: bool = False
    adjustment_id: str | None = None
    confidence_in_adjustment: str = "low"
    notes: str = ""


class AccountingBridge:
    """Cross-standard accounting adjustment engine.

    Section 6.4 principles:
    1. Every market must have an independent adapter config.
    2. Cross-market comparison must go through this bridge, or critic blocks.
    3. Market-specific risks must be flagged.
    4. Unsupported standards must raise explicit errors.
    """

    def __init__(self) -> None:
        self._rules: dict[str, AdjustmentRule] = {}
        self._register_core_rules()

    def _register_core_rules(self) -> None:
        """Register the core cross-standard adjustment rules from Section 6.2."""

        # R&D: US GAAP expenses all; IFRS/CAS capitalize development phase
        self._add_rule(AdjustmentRule(
            adjustment_id="adj_rd_usgaap_to_ifrs_v1",
            adjustment_type=AdjustmentType.R_AND_D_CAPITALIZATION,
            source_standard=AccountingStandard.US_GAAP,
            target_standard=AccountingStandard.IFRS,
            affected_metrics=["r_and_d_intensity", "operating_margin", "roic", "total_assets"],
            description=(
                "US GAAP expenses all R&D. IFRS/CAS allow capitalization of development costs. "
                "Adjustment: add back capitalized dev cost to OPEX when converting IFRS->US GAAP, "
                "or capitalize eligible dev cost when converting US GAAP->IFRS."
            ),
            confidence="medium",
        ))
        self._add_rule(AdjustmentRule(
            adjustment_id="adj_rd_usgaap_to_cas_v1",
            adjustment_type=AdjustmentType.R_AND_D_CAPITALIZATION,
            source_standard=AccountingStandard.US_GAAP,
            target_standard=AccountingStandard.CAS,
            affected_metrics=["r_and_d_intensity", "operating_margin", "roic", "total_assets"],
            description=(
                "US GAAP expenses all R&D. CAS allows capitalization of development costs. "
                "Same adjustment logic as US GAAP vs IFRS."
            ),
            confidence="medium",
        ))
        self._add_rule(AdjustmentRule(
            adjustment_id="adj_rd_cas_to_ifrs_v1",
            adjustment_type=AdjustmentType.R_AND_D_CAPITALIZATION,
            source_standard=AccountingStandard.CAS,
            target_standard=AccountingStandard.IFRS,
            affected_metrics=["r_and_d_intensity", "operating_margin", "roic"],
            description=(
                "CAS and IFRS both allow development cost capitalization. "
                "Mostly comparable, but capitalization criteria may differ in practice."
            ),
            confidence="high",
        ))

        # Inventory: US GAAP allows LIFO; IFRS/CAS prohibit LIFO
        self._add_rule(AdjustmentRule(
            adjustment_id="adj_inventory_lifo_to_fifo_v1",
            adjustment_type=AdjustmentType.INVENTORY_METHOD,
            source_standard=AccountingStandard.US_GAAP,
            target_standard=AccountingStandard.IFRS,
            affected_metrics=["gross_margin", "inventory_turnover", "cogs"],
            description=(
                "US GAAP allows LIFO. IFRS/CAS prohibit LIFO. "
                "For LIFO companies, use LIFO reserve to adjust to FIFO."
            ),
            confidence="high",
        ))

        # Government subsidy: CAS often in operating income; US GAAP varies
        self._add_rule(AdjustmentRule(
            adjustment_id="adj_cas_gov_subsidy_v1",
            adjustment_type=AdjustmentType.GOVERNMENT_SUBSIDY,
            source_standard=AccountingStandard.CAS,
            target_standard=AccountingStandard.US_GAAP,
            affected_metrics=["operating_margin", "net_margin", "revenue"],
            description=(
                "CAS: government subsidies often in '其他收益' (other income, operating). "
                "US GAAP: typically offset against expense or in other income (non-operating). "
                "Adjustment: reclassify to non-operating for comparability."
            ),
            confidence="medium",
        ))

        # Asset revaluation: IFRS allows (IAS 16); US GAAP/CAS do not
        self._add_rule(AdjustmentRule(
            adjustment_id="adj_asset_reval_ifrs_to_usgaap_v1",
            adjustment_type=AdjustmentType.ASSET_REVALUATION,
            source_standard=AccountingStandard.IFRS,
            target_standard=AccountingStandard.US_GAAP,
            affected_metrics=["total_assets", "roe", "roa", "book_value"],
            description=(
                "IFRS allows asset revaluation (IAS 16). US GAAP and CAS do not. "
                "Flag entities using revaluation model; adjust by removing revaluation surplus."
            ),
            confidence="medium",
        ))

        # Lease: All three have similar right-of-use treatment post ASC842/IFRS16/CAS21
        # but IFRS 16 scope is broader
        self._add_rule(AdjustmentRule(
            adjustment_id="adj_lease_ifrs16_scope_v1",
            adjustment_type=AdjustmentType.LEASE_RECLASSIFICATION,
            source_standard=AccountingStandard.IFRS,
            target_standard=AccountingStandard.US_GAAP,
            affected_metrics=["ebitda", "operating_margin", "total_debt", "net_debt"],
            description=(
                "IFRS 16 has broader scope than ASC 842 (no operating lease exception for lessee). "
                "When comparing: note that IFRS companies may show higher EBITDA and higher debt."
            ),
            confidence="high",
        ))

    def _add_rule(self, rule: AdjustmentRule) -> None:
        self._rules[rule.adjustment_id] = rule

    def get_comparability_flags(
        self,
        source_standard: AccountingStandard,
        target_standard: AccountingStandard,
        metrics: list[str],
    ) -> list[ComparabilityFlag]:
        """Get comparability flags for a set of metrics between two standards.

        This is the primary interface — call this before any cross-standard comparison.
        """
        if source_standard == target_standard:
            return [ComparabilityFlag(
                comparability_flag="direct",
                source_standard=source_standard,
                target_standard=target_standard,
                affected_metrics=metrics,
            )]

        flags = []
        flagged_metrics: set[str] = set()

        for rule in self._rules.values():
            # Check both directions
            matches_forward = (
                rule.source_standard == source_standard
                and rule.target_standard == target_standard
            )
            matches_reverse = (
                rule.source_standard == target_standard
                and rule.target_standard == source_standard
            )
            if not (matches_forward or matches_reverse):
                continue

            affected = [m for m in metrics if m in rule.affected_metrics]
            if affected:
                flagged_metrics.update(affected)
                flags.append(ComparabilityFlag(
                    comparability_flag="adjustment_required",
                    source_standard=source_standard,
                    target_standard=target_standard,
                    affected_metrics=affected,
                    adjustment_type=rule.adjustment_type,
                    adjustment_available=True,
                    adjustment_id=rule.adjustment_id,
                    confidence_in_adjustment=rule.confidence,
                    notes=rule.description,
                ))

        # Metrics with no known adjustment rule
        unflagged = [m for m in metrics if m not in flagged_metrics]
        if unflagged:
            flags.append(ComparabilityFlag(
                comparability_flag="direct",
                source_standard=source_standard,
                target_standard=target_standard,
                affected_metrics=unflagged,
                notes="No known cross-standard adjustment required for these metrics.",
            ))

        return flags

    def apply_adjustment(
        self,
        adjustment_id: str,
        metric: str,
        value: float,
        adjustment_inputs: dict[str, float],
    ) -> AdjustmentResult:
        """Apply a specific cross-standard adjustment.

        Args:
            adjustment_id: The registered adjustment rule to apply.
            metric: The metric being adjusted.
            value: The original value.
            adjustment_inputs: Additional inputs needed (e.g., capitalized_dev_cost).

        Returns:
            AdjustmentResult with full audit trail.
        """
        if adjustment_id not in self._rules:
            raise KeyError(f"Adjustment rule '{adjustment_id}' not found.")

        rule = self._rules[adjustment_id]

        # Apply type-specific adjustment logic
        adjusted_value = value
        adjustment_amount = 0.0
        notes = ""

        if rule.adjustment_type == AdjustmentType.R_AND_D_CAPITALIZATION:
            cap_dev = adjustment_inputs.get("capitalized_dev_cost", 0)
            if metric in ("operating_margin", "r_and_d_intensity"):
                # Expense the capitalized amount for US GAAP comparability
                adjustment_amount = -cap_dev
                adjusted_value = value + adjustment_amount
                notes = f"Added back capitalized dev cost ({cap_dev}) to expense"

        elif rule.adjustment_type == AdjustmentType.GOVERNMENT_SUBSIDY:
            subsidy = adjustment_inputs.get("government_subsidy", 0)
            if metric == "operating_margin":
                adjustment_amount = -subsidy
                adjusted_value = value + adjustment_amount
                notes = f"Reclassified gov subsidy ({subsidy}) from operating to non-operating"

        elif rule.adjustment_type == AdjustmentType.INVENTORY_METHOD:
            lifo_reserve = adjustment_inputs.get("lifo_reserve", 0)
            if metric == "gross_margin":
                adjustment_amount = lifo_reserve
                adjusted_value = value + adjustment_amount
                notes = f"LIFO to FIFO adjustment using LIFO reserve ({lifo_reserve})"

        return AdjustmentResult(
            adjustment_id=adjustment_id,
            metric=metric,
            original_value=value,
            adjusted_value=adjusted_value,
            adjustment_amount=adjustment_amount,
            source_standard=rule.source_standard,
            target_standard=rule.target_standard,
            confidence=rule.confidence,
            notes=notes,
        )

    def list_rules(self) -> list[AdjustmentRule]:
        """List all registered adjustment rules."""
        return list(self._rules.values())
