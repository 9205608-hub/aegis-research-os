"""Tests for core data contracts — schema validation correctness."""

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from aegis.data_contracts import (
    AccountingStandard,
    AtomicAccountingFact,
    EntityContract,
    MarketId,
    MetricDefinition,
    RunManifest,
    ResearchMode,
    SourceTier,
)


class TestEntityContract:
    def test_valid_entity(self):
        entity = EntityContract(
            entity_id="meta_platforms_inc",
            issuer_legal_name="Meta Platforms, Inc.",
            ticker="META",
            exchange="NASDAQ",
            primary_security_id="us02079k3059",
            reporting_currency="USD",
            price_currency="USD",
            fiscal_year_end="12-31",
            accounting_standard=AccountingStandard.US_GAAP,
            market_id=MarketId.US,
            sector_scheme="GICS_2025",
            sector="Communication Services",
            industry_group="Interactive Media & Services",
            sector_pack_id="sp_ad_platform_v2",
            country_of_domicile="US",
            country_of_primary_listing="US",
            functional_currencies=["USD"],
            dual_class_structure=True,
            active_from=date(2012, 5, 18),
        )
        assert entity.entity_id == "meta_platforms_inc"
        assert entity.dual_class_structure is True

    def test_invalid_entity_id_rejects_uppercase(self):
        with pytest.raises(ValidationError):
            EntityContract(
                entity_id="Meta_Platforms",  # uppercase not allowed
                issuer_legal_name="Meta",
                ticker="META",
                exchange="NASDAQ",
                primary_security_id="x",
                reporting_currency="USD",
                price_currency="USD",
                fiscal_year_end="12-31",
                accounting_standard=AccountingStandard.US_GAAP,
                market_id=MarketId.US,
                sector_scheme="GICS",
                sector="Tech",
                industry_group="Media",
                sector_pack_id="sp",
                country_of_domicile="US",
                country_of_primary_listing="US",
                functional_currencies=["USD"],
                active_from=date(2012, 1, 1),
            )

    def test_frozen_model_rejects_mutation(self):
        entity = EntityContract(
            entity_id="test_co",
            issuer_legal_name="Test Co",
            ticker="TEST",
            exchange="NYSE",
            primary_security_id="x",
            reporting_currency="USD",
            price_currency="USD",
            fiscal_year_end="12-31",
            accounting_standard=AccountingStandard.US_GAAP,
            market_id=MarketId.US,
            sector_scheme="GICS",
            sector="Tech",
            industry_group="Software",
            sector_pack_id="sp",
            country_of_domicile="US",
            country_of_primary_listing="US",
            functional_currencies=["USD"],
            active_from=date(2020, 1, 1),
        )
        with pytest.raises(ValidationError):
            entity.ticker = "CHANGED"

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            EntityContract(
                entity_id="test_co",
                issuer_legal_name="Test",
                ticker="T",
                exchange="NYSE",
                primary_security_id="x",
                reporting_currency="USD",
                price_currency="USD",
                fiscal_year_end="12-31",
                accounting_standard=AccountingStandard.US_GAAP,
                market_id=MarketId.US,
                sector_scheme="GICS",
                sector="Tech",
                industry_group="Software",
                sector_pack_id="sp",
                country_of_domicile="US",
                country_of_primary_listing="US",
                functional_currencies=["USD"],
                active_from=date(2020, 1, 1),
                random_field="should_fail",  # Not in schema
            )


class TestAtomicFact:
    def test_valid_fact(self):
        fact = AtomicAccountingFact(
            fact_id="meta_2025_fy_cfo_v1",
            entity_id="meta_platforms_inc",
            market_id=MarketId.US,
            statement_type="cash_flow",
            source_concept_id="NetCashProvidedByUsedInOperatingActivities",
            canonical_concept_id="cfo",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            fiscal_period="FY2025",
            period_type="duration",
            value_raw=115800000000,
            unit="USD",
            scale_hint=0,
            sign_convention="company_reported",
            accession_no="0001326801-26-0000xx",
            accepted_at=datetime(2026, 2, 2, 21, 12, tzinfo=timezone.utc),
            effective_at=datetime(2026, 2, 2, 21, 12, tzinfo=timezone.utc),
            as_reported=True,
            accounting_standard=AccountingStandard.US_GAAP,
            parser_version="xbrl_parser_v4",
            source_ref="10K_2025_cf_stmt",
            source_hash="sha256:" + "a" * 64,
            fact_version=1,
            ingestion_batch_id="batch_20260202_edgar_001",
            source_tier=SourceTier.TIER_1,
        )
        assert fact.value_raw == 115800000000
        assert fact.source_tier == SourceTier.TIER_1

    def test_invalid_hash_format_rejected(self):
        with pytest.raises(ValidationError):
            AtomicAccountingFact(
                fact_id="test",
                entity_id="test_co",
                market_id=MarketId.US,
                statement_type="income",
                source_concept_id="Revenue",
                canonical_concept_id="revenue",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                fiscal_period="FY2025",
                period_type="duration",
                value_raw=1000,
                unit="USD",
                sign_convention="company_reported",
                accession_no="x",
                accepted_at=datetime.now(timezone.utc),
                effective_at=datetime.now(timezone.utc),
                as_reported=True,
                accounting_standard=AccountingStandard.US_GAAP,
                parser_version="v1",
                source_ref="ref",
                source_hash="bad_hash",  # Invalid format
                fact_version=1,
                ingestion_batch_id="batch_1",
                source_tier=SourceTier.TIER_1,
            )


class TestMetricDefinition:
    def test_valid_definition(self):
        defn = MetricDefinition(
            metric_name="free_cash_flow",
            display_name="Free Cash Flow",
            definition_id="fcf_company_official_v1",
            definition_status="approved",
            formula_version=1,
            expression="cfo - capex_ppe - finance_lease_principal",
            allowed_inputs=["cfo", "capex_ppe", "finance_lease_principal"],
            disallowed_inputs=["sbc", "share_repurchases"],
            unit_policy="currency",
            period_compatibility=["quarterly", "annual"],
            accounting_standard_compatibility=[
                AccountingStandard.US_GAAP,
                AccountingStandard.IFRS,
            ],
            sector_applicability=["internet_platform", "software"],
            quality_tier="A",
            publishable=True,
            effective_from=date(2020, 1, 1),
        )
        assert defn.publishable is True
        assert defn.formula_version == 1


class TestRunManifest:
    def test_valid_manifest(self):
        now = datetime.now(timezone.utc)
        manifest = RunManifest(
            run_id="run_20260411_001",
            run_mode=ResearchMode.SINGLE_ENTITY,
            entity_ids=["meta_platforms_inc"],
            question_id="q_meta_valuation",
            research_timestamp=now,
            price_timestamp=now,
            filing_cutoff_timestamp=now,
            consensus_snapshot_timestamp=now,
            macro_snapshot_timestamp=now,
            model_profile_id="default_v1",
            prompt_bundle_version="v1.0",
            parser_version="xbrl_parser_v1",
            formula_registry_version="formula_v1",
            metric_registry_version="metric_v1",
            evidence_extractor_version="evidence_v1",
            critic_policy_version="critic_v1",
            scenario_model_version="scenario_engine_v1",
            market_adapter_id=MarketId.US,
            artifact_hash="sha256:" + "b" * 64,
        )
        assert manifest.run_mode == ResearchMode.SINGLE_ENTITY
        assert len(manifest.entity_ids) == 1
