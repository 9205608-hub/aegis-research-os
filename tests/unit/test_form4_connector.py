"""Tests for SEC Form 4 Insider Trading Connector.

Round 25 — Insider Trading Data.
Tests cover:
- InsiderTransaction / InsiderSummary dataclass creation
- Form 4 XML parsing (_parse_form4_xml)
- Summary computation (net values, sentiment, cluster detection)
- is_csuite helper
- Management Analyst insider observations/inferences
- HTML report insider card rendering
"""

import pytest
from dataclasses import dataclass

from aegis.core.acquisition.connectors.sec_form4_connector import (
    InsiderTransaction,
    InsiderSummary,
    SECForm4Connector,
    is_csuite,
    _safe_float,
)


# ============================================================
# Sample Form 4 XML for testing
# ============================================================

SAMPLE_FORM4_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>John Smith</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer>
      <officerTitle>CEO</officerTitle>
      <isDirector>1</isDirector>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2025-06-15</value></transactionDate>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>150.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2025-07-10</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>160.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>45000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

SAMPLE_FORM4_GIFT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>Jane Doe</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2025-08-01</value></transactionDate>
      <transactionCoding>
        <transactionCode>G</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>10000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

SAMPLE_FORM4_EMPTY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>Bob Builder</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship/>
  </reportingOwner>
</ownershipDocument>
"""


# ============================================================
# Helper Tests
# ============================================================

class TestHelpers:

    def test_is_csuite_ceo(self):
        assert is_csuite("CEO") is True

    def test_is_csuite_cfo(self):
        assert is_csuite("Chief Financial Officer") is True

    def test_is_csuite_president(self):
        assert is_csuite("President and COO") is True

    def test_is_csuite_director_only(self):
        assert is_csuite("Director") is False

    def test_is_csuite_vp(self):
        assert is_csuite("VP of Engineering") is False

    def test_is_csuite_empty(self):
        assert is_csuite("") is False

    def test_safe_float_valid(self):
        import xml.etree.ElementTree as ET
        el = ET.fromstring("<value>123.45</value>")
        assert _safe_float(el) == 123.45

    def test_safe_float_none(self):
        assert _safe_float(None) == 0.0

    def test_safe_float_empty_text(self):
        import xml.etree.ElementTree as ET
        el = ET.fromstring("<value></value>")
        assert _safe_float(el) == 0.0

    def test_safe_float_invalid(self):
        import xml.etree.ElementTree as ET
        el = ET.fromstring("<value>N/A</value>")
        assert _safe_float(el) == 0.0


# ============================================================
# InsiderTransaction Dataclass Tests
# ============================================================

class TestInsiderTransaction:

    def test_creation(self):
        t = InsiderTransaction(
            filer_name="John Smith",
            filer_title="CEO",
            transaction_date="2025-06-15",
            transaction_type="P",
            shares=10000,
            price_per_share=150.0,
            total_value=1_500_000,
            shares_owned_after=50000,
            is_direct=True,
            filing_date="2025-06-17",
            accession_number="0001234-25-001234",
        )
        assert t.filer_name == "John Smith"
        assert t.transaction_type == "P"
        assert t.total_value == 1_500_000

    def test_sale_transaction(self):
        t = InsiderTransaction(
            filer_name="Jane Doe",
            filer_title="CFO",
            transaction_date="2025-07-10",
            transaction_type="S",
            shares=5000,
            price_per_share=160.0,
            total_value=800_000,
            shares_owned_after=45000,
            is_direct=True,
            filing_date="2025-07-12",
            accession_number="0001234-25-001235",
        )
        assert t.transaction_type == "S"
        assert t.total_value == 800_000


# ============================================================
# XML Parsing Tests
# ============================================================

class TestForm4XMLParsing:

    def setup_method(self):
        self.connector = SECForm4Connector()

    def test_parse_buy_and_sell(self):
        txns = self.connector._parse_form4_xml(
            SAMPLE_FORM4_XML, "2025-07-12", "0001234-25-001234"
        )
        assert len(txns) == 2

        buy = txns[0]
        assert buy.filer_name == "John Smith"
        assert buy.filer_title == "CEO, Director"
        assert buy.transaction_type == "P"
        assert buy.shares == 10000
        assert buy.price_per_share == 150.0
        assert buy.total_value == 1_500_000
        assert buy.is_direct is True

        sell = txns[1]
        assert sell.transaction_type == "S"
        assert sell.shares == 5000
        assert sell.price_per_share == 160.0

    def test_parse_gift_skipped(self):
        """Gift transactions (code=G) should be skipped."""
        txns = self.connector._parse_form4_xml(
            SAMPLE_FORM4_GIFT_XML, "2025-08-03", "0001234-25-001236"
        )
        assert len(txns) == 0

    def test_parse_empty_document(self):
        txns = self.connector._parse_form4_xml(
            SAMPLE_FORM4_EMPTY_XML, "2025-08-01", "0001234-25-001237"
        )
        assert txns == []

    def test_parse_invalid_xml(self):
        txns = self.connector._parse_form4_xml(
            "not valid xml <<>>", "2025-08-01", "x"
        )
        assert txns == []

    def test_parse_filer_title_extraction(self):
        txns = self.connector._parse_form4_xml(
            SAMPLE_FORM4_XML, "2025-07-12", "acc"
        )
        assert "CEO" in txns[0].filer_title
        assert "Director" in txns[0].filer_title

    def test_parse_shares_owned_after(self):
        txns = self.connector._parse_form4_xml(
            SAMPLE_FORM4_XML, "2025-07-12", "acc"
        )
        assert txns[0].shares_owned_after == 50000
        assert txns[1].shares_owned_after == 45000


# ============================================================
# Summary Computation Tests
# ============================================================

class TestSummaryComputation:

    def setup_method(self):
        self.connector = SECForm4Connector()

    def _make_txn(self, name="John", title="CEO", txn_type="P",
                  shares=1000, price=100.0, date="2025-06-15"):
        return InsiderTransaction(
            filer_name=name, filer_title=title,
            transaction_date=date, transaction_type=txn_type,
            shares=shares, price_per_share=price,
            total_value=shares * price,
            shares_owned_after=10000, is_direct=True,
            filing_date=date, accession_number="acc",
        )

    def test_all_buys_bullish(self):
        txns = [
            self._make_txn("Alice", "CEO", "P", 10000, 100.0),
        ]
        summary = self.connector._compute_summary("AAPL", txns, 12)
        assert summary.buy_count == 1
        assert summary.sell_count == 0
        assert summary.sentiment == "bullish"
        assert summary.net_value == 1_000_000

    def test_all_sells_large_bearish(self):
        txns = [
            self._make_txn("Bob", "CFO", "S", 100000, 150.0),
        ]
        summary = self.connector._compute_summary("AAPL", txns, 12)
        assert summary.sell_count == 1
        assert summary.total_sell_value == 15_000_000
        assert summary.sentiment == "bearish"

    def test_small_sells_neutral(self):
        """Small selling (<$10M) without buys is neutral (normal vesting)."""
        txns = [
            self._make_txn("Bob", "VP", "S", 1000, 100.0),
        ]
        summary = self.connector._compute_summary("AAPL", txns, 12)
        assert summary.sentiment == "neutral"

    def test_mixed_sentiment(self):
        txns = [
            self._make_txn("Alice", "CEO", "P", 5000, 100.0),
            self._make_txn("Bob", "CFO", "S", 6000, 100.0),
        ]
        summary = self.connector._compute_summary("AAPL", txns, 12)
        assert summary.sentiment == "mixed"

    def test_notable_transactions(self):
        txns = [
            self._make_txn("Alice", "CEO", "P", 20000, 100.0),  # $2M
            self._make_txn("Bob", "CFO", "P", 500, 100.0),      # $50K
        ]
        summary = self.connector._compute_summary("AAPL", txns, 12)
        assert len(summary.notable_transactions) == 1
        assert summary.notable_transactions[0].filer_name == "Alice"

    def test_empty_transactions(self):
        summary = self.connector._compute_summary("AAPL", [], 12)
        assert summary.sentiment == "neutral"
        assert summary.net_value == 0
        assert summary.buy_count == 0
        assert summary.sell_count == 0


# ============================================================
# Cluster Detection Tests
# ============================================================

class TestClusterDetection:

    def setup_method(self):
        self.connector = SECForm4Connector()

    def _make_txn(self, name, txn_type="P", date="2025-06-15"):
        return InsiderTransaction(
            filer_name=name, filer_title="Director",
            transaction_date=date, transaction_type=txn_type,
            shares=1000, price_per_share=100.0,
            total_value=100_000, shares_owned_after=5000,
            is_direct=True, filing_date=date, accession_number="acc",
        )

    def test_cluster_buy_detected(self):
        """3+ unique buyers within 30 days → cluster."""
        txns = [
            self._make_txn("Alice", "P", "2025-06-01"),
            self._make_txn("Bob", "P", "2025-06-10"),
            self._make_txn("Charlie", "P", "2025-06-20"),
        ]
        assert self.connector._detect_cluster(txns) is True

    def test_no_cluster_too_few(self):
        """2 buyers is not enough."""
        txns = [
            self._make_txn("Alice", "P", "2025-06-01"),
            self._make_txn("Bob", "P", "2025-06-10"),
        ]
        assert self.connector._detect_cluster(txns) is False

    def test_no_cluster_too_spread(self):
        """3 buyers but > 30 days apart."""
        txns = [
            self._make_txn("Alice", "P", "2025-01-01"),
            self._make_txn("Bob", "P", "2025-03-15"),
            self._make_txn("Charlie", "P", "2025-06-01"),
        ]
        assert self.connector._detect_cluster(txns) is False

    def test_cluster_sell_detected(self):
        txns = [
            self._make_txn("Alice", "S", "2025-06-01"),
            self._make_txn("Bob", "S", "2025-06-05"),
            self._make_txn("Charlie", "S", "2025-06-10"),
        ]
        assert self.connector._detect_cluster(txns) is True

    def test_no_cluster_mixed_directions(self):
        """3 people but different directions → no cluster."""
        txns = [
            self._make_txn("Alice", "P", "2025-06-01"),
            self._make_txn("Bob", "S", "2025-06-05"),
            self._make_txn("Charlie", "P", "2025-06-10"),
        ]
        assert self.connector._detect_cluster(txns) is False

    def test_empty_transactions(self):
        assert self.connector._detect_cluster([]) is False


# ============================================================
# Management Analyst Integration Tests
# ============================================================

class TestManagementAnalystInsider:

    def test_insider_observations_generated(self):
        from aegis.core.agents.management_analyst.agent import ManagementAnalyst
        from aegis.core.agents.base import AgentInput

        inp = AgentInput(
            entity_id="aapl",
            run_id="test-run",
            question_id="test-q",
            metric_results={"roic": 0.15},
            evidence_packets=[],
            entity_relationships=[],
            macro_context={
                "insider_trading": {
                    "sentiment": "bullish",
                    "net_value": 5_000_000,
                    "buy_count": 3,
                    "sell_count": 1,
                    "total_buy_value": 6_000_000,
                    "total_sell_value": 1_000_000,
                    "cluster_detected": True,
                    "notable_transactions": [
                        {"name": "Tim Cook", "title": "CEO",
                         "type": "P", "value": 2_000_000, "date": "2025-06-15"},
                    ],
                },
            },
        )

        agent = ManagementAnalyst()
        obs = agent._extract_observations(inp)

        # Should have insider-related observations
        insider_obs = [o for o in obs if "form4:" in ",".join(o.source_ids)]
        assert len(insider_obs) >= 2  # summary + cluster + notable

        # Check summary observation
        summary_obs = [o for o in insider_obs if "net buying" in o.text.lower() or "net selling" in o.text.lower()]
        assert len(summary_obs) == 1
        assert "3 purchases" in summary_obs[0].text

        # Check cluster observation
        cluster_obs = [o for o in insider_obs if "cluster" in o.text.lower()]
        assert len(cluster_obs) == 1

    def test_insider_inferences_bullish(self):
        from aegis.core.agents.management_analyst.agent import ManagementAnalyst
        from aegis.core.agents.base import AgentInput

        inp = AgentInput(
            entity_id="aapl",
            run_id="test-run",
            question_id="test-q",
            metric_results={},
            evidence_packets=[],
            entity_relationships=[],
            macro_context={
                "insider_trading": {
                    "sentiment": "bullish",
                    "net_value": 5_000_000,
                    "buy_count": 3,
                    "sell_count": 0,
                    "total_buy_value": 5_000_000,
                    "total_sell_value": 0,
                    "cluster_detected": True,
                    "notable_transactions": [],
                },
            },
        )

        agent = ManagementAnalyst()
        obs = agent._extract_observations(inp)
        infs = agent._derive_inferences(obs, inp)

        insider_infs = [i for i in infs if "insider" in i.text.lower() or "cluster" in i.text.lower()]
        assert len(insider_infs) >= 1
        # Should have bullish confidence signal
        assert any("confidence" in i.text.lower() or "buying" in i.text.lower() for i in insider_infs)

    def test_no_insider_data_no_crash(self):
        """Without insider data, agent should work normally."""
        from aegis.core.agents.management_analyst.agent import ManagementAnalyst
        from aegis.core.agents.base import AgentInput

        inp = AgentInput(
            entity_id="aapl",
            run_id="test-run",
            question_id="test-q",
            metric_results={"roic": 0.10},
            evidence_packets=[],
            entity_relationships=[],
        )

        agent = ManagementAnalyst()
        obs = agent._extract_observations(inp)
        infs = agent._derive_inferences(obs, inp)
        # Should still produce ROIC-based observations
        assert len(obs) >= 1

# NOTE (Phase 0, DESIGN_2.0): TestInsiderTradingCard was removed together
# with html_report_legacy.py — it exercised `_build_insider_trading_card`,
# a legacy-renderer-only HTML helper with no production caller.
