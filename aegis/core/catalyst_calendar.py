"""Catalyst Calendar — aggregates time-bound events from multiple sources.

A catalyst is any identifiable, time-bound event that could close the variant
gap between our view and the market's view. The calendar provides a unified
timeline for:
  1. Earnings dates (yfinance)
  2. SEC filing deadlines (10-K/10-Q due dates)
  3. Sector-specific events (from sector pack YAML)
  4. Agent-derived catalysts (from variant/risk analyst inferences)
  5. Product launches, regulatory milestones (from earnings call insights)

Usage:
    calendar = CatalystCalendar()
    events = calendar.build(
        ticker="META", entity_id="meta_platforms",
        earnings_history=[...], sector_pack={...},
        agent_catalysts=[...], earnings_call_insights=...
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalendarEvent:
    """A single event in the catalyst calendar timeline."""

    event_id: str
    entity_id: str
    title: str
    description: str
    expected_date: date | None = None
    date_confidence: str = "low"  # "low", "medium", "high"
    event_type: str = "other"
    # "earnings", "filing", "product_launch", "regulatory",
    # "macro", "management", "dividend", "buyback", "other"
    impact_direction: str = "unknown"  # "positive", "negative", "uncertain", "unknown"
    impact_magnitude: str = "medium"  # "low", "medium", "high"
    source: str = ""  # "yfinance", "sec_calendar", "sector_pack", "agent", "earnings_call"

    @property
    def days_until(self) -> int | None:
        if self.expected_date is None:
            return None
        return (self.expected_date - date.today()).days

    @property
    def is_upcoming(self) -> bool:
        d = self.days_until
        return d is not None and d >= 0

    @property
    def urgency(self) -> str:
        d = self.days_until
        if d is None:
            return "undated"
        if d <= 7:
            return "imminent"
        if d <= 30:
            return "near_term"
        if d <= 90:
            return "medium_term"
        return "long_term"


@dataclass
class CatalystTimeline:
    """Complete catalyst timeline for an entity."""

    entity_id: str
    ticker: str
    events: list[CalendarEvent]
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def upcoming(self) -> list[CalendarEvent]:
        """Events in the future, sorted by date."""
        result = [e for e in self.events if e.is_upcoming]
        return sorted(result, key=lambda e: e.expected_date or date.max)

    @property
    def next_catalyst(self) -> CalendarEvent | None:
        up = self.upcoming
        return up[0] if up else None

    @property
    def next_earnings(self) -> CalendarEvent | None:
        for e in self.upcoming:
            if e.event_type == "earnings":
                return e
        return None

    def events_within_days(self, days: int) -> list[CalendarEvent]:
        cutoff = date.today() + timedelta(days=days)
        return [e for e in self.upcoming if e.expected_date and e.expected_date <= cutoff]

    def to_dict(self) -> dict[str, Any]:
        """Serialize for agent_macro / report injection."""
        events = []
        for e in self.upcoming[:15]:
            events.append({
                "title": e.title,
                "date": e.expected_date.isoformat() if e.expected_date else None,
                "days_until": e.days_until,
                "type": e.event_type,
                "urgency": e.urgency,
                "impact_direction": e.impact_direction,
                "description": e.description,
                "source": e.source,
            })
        next_cat = self.next_catalyst
        next_earn = self.next_earnings
        return {
            "event_count": len(self.events),
            "upcoming_count": len(self.upcoming),
            "next_catalyst": {
                "title": next_cat.title,
                "date": next_cat.expected_date.isoformat() if next_cat and next_cat.expected_date else None,
                "days_until": next_cat.days_until,
                "type": next_cat.event_type,
            } if next_cat else None,
            "next_earnings": {
                "date": next_earn.expected_date.isoformat() if next_earn and next_earn.expected_date else None,
                "days_until": next_earn.days_until,
            } if next_earn else None,
            "events_30d": len(self.events_within_days(30)),
            "events_90d": len(self.events_within_days(90)),
            "timeline": events,
        }


class CatalystCalendar:
    """Builds a unified catalyst timeline from multiple data sources."""

    def build(
        self,
        ticker: str,
        entity_id: str,
        *,
        earnings_history: list[Any] | None = None,
        sector_pack: dict[str, Any] | None = None,
        agent_catalysts: list[Any] | None = None,
        earnings_call_insights: Any | None = None,
        market_data: dict[str, Any] | None = None,
    ) -> CatalystTimeline:
        """Build a complete catalyst timeline for an entity."""
        events: list[CalendarEvent] = []

        events.extend(self._earnings_events(ticker, entity_id, earnings_history))
        events.extend(self._sec_filing_events(ticker, entity_id))
        events.extend(self._cn_filing_events(ticker, entity_id))
        events.extend(self._sector_pack_events(entity_id, sector_pack))
        events.extend(self._agent_catalyst_events(entity_id, agent_catalysts))
        events.extend(self._earnings_call_events(entity_id, earnings_call_insights))
        events.extend(self._dividend_buyback_events(entity_id, market_data))

        # Deduplicate by title + date
        seen: set[str] = set()
        unique: list[CalendarEvent] = []
        for e in events:
            key = f"{e.title}_{e.expected_date}"
            if key not in seen:
                seen.add(key)
                unique.append(e)

        return CatalystTimeline(
            entity_id=entity_id,
            ticker=ticker,
            events=unique,
        )

    def _earnings_events(
        self, ticker: str, entity_id: str, earnings_history: list | None,
    ) -> list[CalendarEvent]:
        """Extract upcoming earnings dates from yfinance."""
        events: list[CalendarEvent] = []

        # A-share tickers (.SZ/.SS OR bare 6-digit like 600089 / 301358)
        # have no yfinance earnings_dates coverage; querying them produces
        # noisy "symbol may be delisted" warnings that mislead the reader
        # into thinking the company is actually delisted.
        # BUG-29 (2026-04-23): added the bare-6-digit path — previously
        # only .SZ/.SS suffixed tickers were skipped, so passing 600089
        # (as the auto pipeline does) still produced the warning.
        if ticker.endswith((".SZ", ".SS")) or (ticker.isdigit() and len(ticker) == 6):
            return events

        try:
            import yfinance as yf
            stock = yf.Ticker(ticker)
            cal = stock.earnings_dates
            if cal is not None and not cal.empty:
                for idx, row in cal.iterrows():
                    try:
                        event_date = idx.date() if hasattr(idx, 'date') else None
                        if event_date is None:
                            continue
                        eps_est = row.get("EPS Estimate")
                        reported = row.get("Reported EPS")

                        if reported is not None:
                            # Past earnings — include for context
                            surprise = ""
                            if eps_est is not None and eps_est != 0:
                                s_pct = (float(reported) - float(eps_est)) / abs(float(eps_est))
                                surprise = f" (surprise: {s_pct:+.1%})"
                            events.append(CalendarEvent(
                                event_id=f"earn_past_{entity_id}_{event_date}",
                                entity_id=entity_id,
                                title=f"Earnings Report",
                                description=f"EPS: {reported}{surprise}",
                                expected_date=event_date,
                                date_confidence="high",
                                event_type="earnings",
                                impact_direction="unknown",
                                source="yfinance",
                            ))
                        else:
                            # Future earnings
                            est_str = f" (EPS est: ${eps_est:.2f})" if eps_est else ""
                            events.append(CalendarEvent(
                                event_id=f"earn_future_{entity_id}_{event_date}",
                                entity_id=entity_id,
                                title=f"Earnings Report (Upcoming)",
                                description=f"Scheduled earnings release{est_str}",
                                expected_date=event_date,
                                date_confidence="high",
                                event_type="earnings",
                                impact_direction="uncertain",
                                impact_magnitude="high",
                                source="yfinance",
                            ))
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"Earnings date fetch failed for {ticker}: {e}")

        return events

    def _sec_filing_events(
        self, ticker: str, entity_id: str,
    ) -> list[CalendarEvent]:
        """Estimate SEC filing deadlines based on fiscal year end.

        Large accelerated filers: 10-K due 60 days, 10-Q due 40 days after period end.
        Only applies to US-listed tickers — A-share issuers file with the
        CSRC on a different cadence and shouldn't be tagged "SEC 10-Q Due".
        """
        events: list[CalendarEvent] = []
        # A-share tickers (.SZ/.SS or bare 6-digit numeric) don't file with the SEC.
        eid = str(entity_id or "").replace(".SS", "").replace(".SZ", "").strip()
        if ticker.endswith((".SZ", ".SS")) or (len(eid) == 6 and eid.isdigit()):
            return events
        today = date.today()

        # Estimate quarterly filing dates for current year
        # Most tech companies have Dec 31 fiscal year end
        quarter_ends = [
            (date(today.year, 3, 31), "Q1"),
            (date(today.year, 6, 30), "Q2"),
            (date(today.year, 9, 30), "Q3"),
            (date(today.year, 12, 31), "FY"),
        ]

        for period_end, label in quarter_ends:
            if label == "FY":
                due_delta = timedelta(days=60)  # 10-K
                filing_type = "10-K Annual Report"
            else:
                due_delta = timedelta(days=40)  # 10-Q
                filing_type = f"10-Q {label}"

            due_date = period_end + due_delta
            if due_date >= today - timedelta(days=30):  # Include recent + future
                events.append(CalendarEvent(
                    event_id=f"sec_{entity_id}_{label}_{today.year}",
                    entity_id=entity_id,
                    title=f"SEC {filing_type} Due",
                    description=f"{filing_type} filing deadline (period ending {period_end.isoformat()})",
                    expected_date=due_date,
                    date_confidence="medium",
                    event_type="filing",
                    impact_direction="unknown",
                    impact_magnitude="low",
                    source="sec_calendar",
                ))

        return events

    def _cn_filing_events(
        self, ticker: str, entity_id: str,
    ) -> list[CalendarEvent]:
        """A-share CSRC filing deadlines. Unlike SEC, the CSRC has firm
        calendar-date deadlines that every issuer shares:

          Q1 季报 (Jan-Mar) → due Apr 30
          半年报 (Jan-Jun) → due Aug 31
          Q3 三季报 (Jul-Sep) → due Oct 31
          年报 (Jan-Dec) → due Apr 30 of following year

        Generates events for the current year's upcoming deadlines only.
        """
        events: list[CalendarEvent] = []
        eid = str(entity_id or "").replace(".SS", "").replace(".SZ", "").strip()
        is_a_share = ticker.endswith((".SZ", ".SS")) or (len(eid) == 6 and eid.isdigit())
        if not is_a_share:
            return events
        today = date.today()
        # (title_en, title_zh, due_date, period_end)
        candidates = [
            ("Q1 Report Due", "一季报披露截止",
             date(today.year, 4, 30), date(today.year, 3, 31)),
            ("Interim Report Due", "半年报披露截止",
             date(today.year, 8, 31), date(today.year, 6, 30)),
            ("Q3 Report Due", "三季报披露截止",
             date(today.year, 10, 31), date(today.year, 9, 30)),
            ("Annual Report Due", "年报披露截止",
             date(today.year + 1, 4, 30), date(today.year, 12, 31)),
        ]
        for title_en, title_zh, due, period_end in candidates:
            # Include upcoming + recent (past 30 days) to match SEC path behavior
            if due < today - timedelta(days=30):
                continue
            events.append(CalendarEvent(
                event_id=f"csrc_{entity_id}_{title_en.split()[0].lower()}_{due.year}",
                entity_id=entity_id,
                title=title_zh,
                description=f"报告期 {period_end.isoformat()}，证监会披露截止日",
                expected_date=due,
                date_confidence="high",
                event_type="filing",
                impact_direction="unknown",
                impact_magnitude="medium",
                source="csrc_calendar",
            ))
        return events

    def _sector_pack_events(
        self, entity_id: str, sector_pack: dict | None,
    ) -> list[CalendarEvent]:
        """Extract sector-specific events from sector pack YAML."""
        if not sector_pack:
            return []

        events: list[CalendarEvent] = []
        sp_events = sector_pack.get("catalyst_calendar", [])
        for evt in sp_events:
            if not isinstance(evt, dict):
                continue
            expected = evt.get("date")
            if expected and isinstance(expected, str):
                try:
                    expected = date.fromisoformat(expected)
                except ValueError:
                    expected = None

            events.append(CalendarEvent(
                event_id=f"sp_{entity_id}_{evt.get('title', '')[:20]}",
                entity_id=entity_id,
                title=evt.get("title", "Sector Event"),
                description=evt.get("description", ""),
                expected_date=expected,
                date_confidence=evt.get("date_confidence", "low"),
                event_type=evt.get("type", "other"),
                impact_direction=evt.get("impact_direction", "uncertain"),
                source="sector_pack",
            ))

        return events

    def _agent_catalyst_events(
        self, entity_id: str, agent_catalysts: list | None,
    ) -> list[CalendarEvent]:
        """Convert agent-extracted CatalystEvent objects to calendar events."""
        if not agent_catalysts:
            return []

        events: list[CalendarEvent] = []
        for cat in agent_catalysts:
            if hasattr(cat, "catalyst_id"):
                # It's a CatalystEvent from portfolio_integration
                events.append(CalendarEvent(
                    event_id=cat.catalyst_id,
                    entity_id=entity_id,
                    title=cat.description[:80],
                    description=f"Positive: {cat.impact_if_positive}\n"
                                f"Negative: {cat.impact_if_negative}",
                    expected_date=cat.expected_date if hasattr(cat, "expected_date") else None,
                    date_confidence=getattr(cat, "date_confidence", "low"),
                    event_type=getattr(cat, "catalyst_type", "other"),
                    impact_direction="uncertain",
                    source=f"agent:{getattr(cat, 'source_agent', 'unknown')}",
                ))
            elif isinstance(cat, dict):
                events.append(CalendarEvent(
                    event_id=f"agent_{entity_id}_{len(events)}",
                    entity_id=entity_id,
                    title=cat.get("description", "Agent catalyst")[:80],
                    description=cat.get("description", ""),
                    expected_date=None,
                    event_type=cat.get("catalyst_type", "other"),
                    impact_direction="uncertain",
                    source=f"agent:{cat.get('source_agent', 'unknown')}",
                ))

        return events

    def _earnings_call_events(
        self, entity_id: str, earnings_call_insights: Any | None,
    ) -> list[CalendarEvent]:
        """Extract catalyst-like events from earnings call analysis."""
        if not earnings_call_insights:
            return []

        events: list[CalendarEvent] = []
        guidance = getattr(earnings_call_insights, "guidance_items", [])
        for g in guidance:
            if not isinstance(g, dict):
                continue
            direction = g.get("direction", "")
            metric = g.get("metric", "")
            text = g.get("guidance_text", "")
            if direction in ("raised", "new"):
                impact = "positive"
            elif direction in ("lowered", "withdrawn"):
                impact = "negative"
            else:
                impact = "uncertain"

            events.append(CalendarEvent(
                event_id=f"ec_guidance_{entity_id}_{metric}",
                entity_id=entity_id,
                title=f"Guidance: {metric} ({direction})",
                description=text[:200],
                expected_date=None,
                event_type="management",
                impact_direction=impact,
                impact_magnitude="medium" if direction in ("raised", "lowered") else "low",
                source="earnings_call",
            ))

        return events

    def _dividend_buyback_events(
        self, entity_id: str, market_data: dict | None,
    ) -> list[CalendarEvent]:
        """Add dividend and buyback events if available from market data."""
        if not market_data:
            return []

        events: list[CalendarEvent] = []

        ex_div_date = market_data.get("ex_dividend_date")
        if ex_div_date:
            try:
                if isinstance(ex_div_date, str):
                    ex_date = date.fromisoformat(ex_div_date[:10])
                elif isinstance(ex_div_date, (int, float)):
                    ex_date = datetime.fromtimestamp(ex_div_date, tz=timezone.utc).date()
                else:
                    ex_date = None

                if ex_date:
                    events.append(CalendarEvent(
                        event_id=f"div_{entity_id}_{ex_date}",
                        entity_id=entity_id,
                        title="Ex-Dividend Date",
                        description=f"Ex-dividend date. "
                                    f"Yield: {market_data.get('dividend_yield', 0):.2%}"
                            if market_data.get("dividend_yield") else "Ex-dividend date",
                        expected_date=ex_date,
                        date_confidence="high",
                        event_type="dividend",
                        impact_direction="positive",
                        impact_magnitude="low",
                        source="yfinance",
                    ))
            except (ValueError, TypeError):
                pass

        return events
