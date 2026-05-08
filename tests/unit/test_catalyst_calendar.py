"""Tests for the Catalyst Calendar system.

Round 22 — Catalyst Calendar.
Tests cover:
- CalendarEvent properties (days_until, urgency, is_upcoming)
- CatalystTimeline aggregation and filtering
- CatalystCalendar.build() with various data sources
- SEC filing date estimation
- Agent catalyst conversion
- Earnings call guidance extraction
- Timeline serialization for agent_macro
"""

import pytest
from datetime import date, timedelta
from dataclasses import dataclass

from aegis.core.catalyst_calendar import (
    CalendarEvent,
    CatalystCalendar,
    CatalystTimeline,
)


# ============================================================
# CalendarEvent Tests
# ============================================================

class TestCalendarEvent:

    def test_days_until_future(self):
        future = date.today() + timedelta(days=30)
        e = CalendarEvent(
            event_id="test", entity_id="meta", title="Earnings",
            description="Q1 earnings", expected_date=future, event_type="earnings",
        )
        assert e.days_until == 30
        assert e.is_upcoming is True

    def test_days_until_past(self):
        past = date.today() - timedelta(days=10)
        e = CalendarEvent(
            event_id="test", entity_id="meta", title="Past",
            description="Past event", expected_date=past,
        )
        assert e.days_until == -10
        assert e.is_upcoming is False

    def test_days_until_none(self):
        e = CalendarEvent(
            event_id="test", entity_id="meta", title="Undated",
            description="No date",
        )
        assert e.days_until is None
        assert e.is_upcoming is False

    def test_urgency_imminent(self):
        e = CalendarEvent(
            event_id="t", entity_id="m", title="t", description="d",
            expected_date=date.today() + timedelta(days=3),
        )
        assert e.urgency == "imminent"

    def test_urgency_near_term(self):
        e = CalendarEvent(
            event_id="t", entity_id="m", title="t", description="d",
            expected_date=date.today() + timedelta(days=15),
        )
        assert e.urgency == "near_term"

    def test_urgency_medium_term(self):
        e = CalendarEvent(
            event_id="t", entity_id="m", title="t", description="d",
            expected_date=date.today() + timedelta(days=60),
        )
        assert e.urgency == "medium_term"

    def test_urgency_long_term(self):
        e = CalendarEvent(
            event_id="t", entity_id="m", title="t", description="d",
            expected_date=date.today() + timedelta(days=120),
        )
        assert e.urgency == "long_term"

    def test_urgency_undated(self):
        e = CalendarEvent(
            event_id="t", entity_id="m", title="t", description="d",
        )
        assert e.urgency == "undated"


# ============================================================
# CatalystTimeline Tests
# ============================================================

class TestCatalystTimeline:

    def _make_events(self):
        today = date.today()
        return [
            CalendarEvent("e1", "meta", "Past Earnings", "d",
                          expected_date=today - timedelta(days=30), event_type="earnings"),
            CalendarEvent("e2", "meta", "Next Earnings", "d",
                          expected_date=today + timedelta(days=15), event_type="earnings"),
            CalendarEvent("e3", "meta", "10-Q Filing", "d",
                          expected_date=today + timedelta(days=40), event_type="filing"),
            CalendarEvent("e4", "meta", "Product Launch", "d",
                          expected_date=today + timedelta(days=90), event_type="product_launch"),
            CalendarEvent("e5", "meta", "Undated", "d", event_type="management"),
        ]

    def test_upcoming_filters_and_sorts(self):
        tl = CatalystTimeline(entity_id="meta", ticker="META",
                              events=self._make_events())
        upcoming = tl.upcoming
        # Should exclude past event and undated
        assert len(upcoming) == 3
        # Should be sorted by date
        assert upcoming[0].title == "Next Earnings"
        assert upcoming[1].title == "10-Q Filing"
        assert upcoming[2].title == "Product Launch"

    def test_next_catalyst(self):
        tl = CatalystTimeline(entity_id="meta", ticker="META",
                              events=self._make_events())
        assert tl.next_catalyst.title == "Next Earnings"

    def test_next_earnings(self):
        tl = CatalystTimeline(entity_id="meta", ticker="META",
                              events=self._make_events())
        assert tl.next_earnings.title == "Next Earnings"

    def test_events_within_days(self):
        tl = CatalystTimeline(entity_id="meta", ticker="META",
                              events=self._make_events())
        within_30 = tl.events_within_days(30)
        assert len(within_30) == 1  # Only "Next Earnings" (15d out)

        within_90 = tl.events_within_days(90)
        assert len(within_90) == 3

    def test_empty_timeline(self):
        tl = CatalystTimeline(entity_id="meta", ticker="META", events=[])
        assert tl.upcoming == []
        assert tl.next_catalyst is None
        assert tl.next_earnings is None

    def test_to_dict(self):
        tl = CatalystTimeline(entity_id="meta", ticker="META",
                              events=self._make_events())
        d = tl.to_dict()
        assert d["event_count"] == 5
        assert d["upcoming_count"] == 3
        assert d["next_catalyst"] is not None
        assert d["next_earnings"] is not None
        assert isinstance(d["timeline"], list)
        assert len(d["timeline"]) == 3  # Only upcoming

    def test_to_dict_json_serializable(self):
        tl = CatalystTimeline(entity_id="meta", ticker="META",
                              events=self._make_events())
        import json
        json_str = json.dumps(tl.to_dict(), default=str)
        assert len(json_str) > 10


# ============================================================
# CatalystCalendar.build() Tests
# ============================================================

class TestCatalystCalendarBuild:

    def test_build_empty(self):
        cal = CatalystCalendar()
        tl = cal.build(ticker="TEST", entity_id="test")
        assert isinstance(tl, CatalystTimeline)
        assert tl.ticker == "TEST"

    def test_sec_filing_events_generated(self):
        cal = CatalystCalendar()
        events = cal._sec_filing_events("META", "meta")
        assert len(events) >= 2  # At least some quarterly filings
        types = {e.event_type for e in events}
        assert "filing" in types

    def test_agent_catalyst_conversion_from_catalyst_event(self):
        """Convert CatalystEvent objects from portfolio_integration."""
        from aegis.core.portfolio.portfolio_integration import CatalystEvent

        agent_cats = [
            CatalystEvent(
                catalyst_id="cat_001",
                entity_id="meta",
                description="Reels monetization reaches Feed parity",
                expected_date=date.today() + timedelta(days=60),
                catalyst_type="product_launch",
                impact_if_positive="Revenue growth accelerates 3-5%",
                impact_if_negative="Growth remains dependent on Feed",
                source_agent="business_analyst",
            ),
        ]
        cal = CatalystCalendar()
        events = cal._agent_catalyst_events("meta", agent_cats)
        assert len(events) == 1
        assert events[0].event_type == "product_launch"
        assert "Reels" in events[0].title

    def test_agent_catalyst_conversion_from_dict(self):
        agent_cats = [
            {"description": "AI ad targeting improvement", "catalyst_type": "product_launch",
             "source_agent": "business_analyst"},
        ]
        cal = CatalystCalendar()
        events = cal._agent_catalyst_events("meta", agent_cats)
        assert len(events) == 1
        assert "AI ad targeting" in events[0].title

    def test_earnings_call_guidance_extraction(self):
        @dataclass
        class MockInsights:
            guidance_items: list

        insights = MockInsights(guidance_items=[
            {"metric": "Revenue", "direction": "raised",
             "guidance_text": "Revenue guidance raised to $42-44B for Q2"},
            {"metric": "Capex", "direction": "maintained",
             "guidance_text": "Capex guidance maintained at $35-40B"},
        ])

        cal = CatalystCalendar()
        events = cal._earnings_call_events("meta", insights)
        assert len(events) == 2
        assert events[0].impact_direction == "positive"  # raised
        assert events[1].impact_direction == "uncertain"  # maintained

    def test_sector_pack_events(self):
        sector_pack = {
            "catalyst_calendar": [
                {"title": "iOS Privacy Update", "type": "regulatory",
                 "description": "Apple may loosen ATT restrictions",
                 "date": (date.today() + timedelta(days=45)).isoformat(),
                 "impact_direction": "positive"},
                {"title": "EU Digital Markets Act deadline", "type": "regulatory",
                 "description": "Compliance deadline for large platforms",
                 "date_confidence": "high"},
            ]
        }
        cal = CatalystCalendar()
        events = cal._sector_pack_events("meta", sector_pack)
        assert len(events) == 2
        assert events[0].event_type == "regulatory"
        assert events[0].expected_date is not None
        assert events[1].expected_date is None  # No valid date

    def test_deduplication(self):
        cal = CatalystCalendar()
        # Build with same sector pack twice via overlapping sources
        sector_pack = {
            "catalyst_calendar": [
                {"title": "Duplicate Event", "type": "other",
                 "date": (date.today() + timedelta(days=10)).isoformat()},
                {"title": "Duplicate Event", "type": "other",
                 "date": (date.today() + timedelta(days=10)).isoformat()},
            ]
        }
        tl = cal.build(ticker="TEST", entity_id="test", sector_pack=sector_pack)
        dup_events = [e for e in tl.events if e.title == "Duplicate Event"]
        assert len(dup_events) == 1

    def test_full_build_with_multiple_sources(self):
        """Build with all source types producing events."""
        from aegis.core.portfolio.portfolio_integration import CatalystEvent

        sector_pack = {
            "catalyst_calendar": [
                {"title": "Industry Conference", "type": "other",
                 "date": (date.today() + timedelta(days=20)).isoformat()},
            ]
        }
        agent_cats = [
            CatalystEvent(
                catalyst_id="cat_edge",
                entity_id="meta",
                description="Edge decay: consensus catches up",
                catalyst_type="other",
                impact_if_positive="Edge preserved",
                impact_if_negative="Variant closes",
                source_agent="variant_analyst",
            ),
        ]

        @dataclass
        class MockInsights:
            guidance_items: list

        insights = MockInsights(guidance_items=[
            {"metric": "ARPU", "direction": "raised",
             "guidance_text": "ARPU guidance raised"},
        ])

        cal = CatalystCalendar()
        tl = cal.build(
            ticker="META", entity_id="meta",
            sector_pack=sector_pack,
            agent_catalysts=agent_cats,
            earnings_call_insights=insights,
        )

        # Should have SEC filings + sector + agent + earnings call events
        assert len(tl.events) >= 4
        sources = {e.source for e in tl.events}
        assert "sec_calendar" in sources
        assert "sector_pack" in sources
        assert "agent:variant_analyst" in sources
        assert "earnings_call" in sources


# ============================================================
# Dividend/Buyback Events Tests
# ============================================================

class TestDividendBuybackEvents:

    def test_ex_dividend_from_string_date(self):
        cal = CatalystCalendar()
        future_date = (date.today() + timedelta(days=10)).isoformat()
        events = cal._dividend_buyback_events("meta", {
            "ex_dividend_date": future_date,
            "dividend_yield": 0.005,
        })
        assert len(events) == 1
        assert events[0].event_type == "dividend"
        assert "0.50%" in events[0].description

    def test_no_dividend_data(self):
        cal = CatalystCalendar()
        events = cal._dividend_buyback_events("meta", {"current_price": 500})
        assert len(events) == 0

    def test_empty_market_data(self):
        cal = CatalystCalendar()
        events = cal._dividend_buyback_events("meta", None)
        assert len(events) == 0
