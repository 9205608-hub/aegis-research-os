# Aegis Research OS — NVDA Report Postmortem
> Date: 2026-04-13 | Report: NVDA FY2024

## Summary
First NVDA research report contained **6 critical data errors**, primarily caused by the system not handling stock splits and international currency differences. This document records root causes and fixes for each issue to guide future iteration.

---

## Issue #1: Stock Split (CRITICAL)
**Symptom**: P/E=9.2x, Market Cap=$274B, EV/EBITDA=8.0x — all ~10x off reality.

**Root Cause**: NVDA did a 10:1 stock split in June 2024. XBRL data from FY2024 (ended Jan 2024) reports pre-split `diluted_shares` (~2.46B). User passes `--price 110` (post-split). System calculates `market_cap = 110 × 2.46B = $274B` instead of `110 × 24.6B = $2.7T`.

**Why It Happened**: The system was designed with the assumption that XBRL share counts and user-provided prices are in the same "split era." No stock split detection or adjustment existed.

**Fix**: Always fetch live `shares_outstanding` from yfinance before calculating market metrics. If live shares differ from XBRL by >2x, log a split detection warning. Update `meta_facts["diluted_shares"]` so DCF engine also uses correct share count.

**Files Changed**: `aegis/core/orchestrator/auto_research.py` (lines 334+)

**Lesson**: Any system that mixes historical filing data with real-time market prices MUST reconcile the share count basis. This should be a standard validation step.

---

## Issue #2: Peer Currency Mismatch (CRITICAL)
**Symptom**: TSM revenue shows $3,809.1B (actually TWD, ~40x overstated in USD terms).

**Root Cause**: `openbb_connector._fetch_single_peer()` reads `totalRevenue` from yfinance without checking `financialCurrency`. TSM reports in TWD, ASML in EUR, etc.

**Why It Happened**: The peer fetching code was written for US-listed companies reporting in USD. International peers were added later without currency handling.

**Fix**: Read `financialCurrency` from yfinance `info`, apply FX conversion to absolute values (revenue, net_income). Ratios (margins, PE, EV/EBITDA) are currency-independent and need no conversion.

**Files Changed**: `aegis/core/acquisition/connectors/openbb_connector.py` (_fetch_single_peer)

**Lesson**: Any data pipeline that compares companies across countries must normalize currency at the source.

---

## Issue #3: Stale Data Period
**Symptom**: Report uses FY2024 ($60.9B revenue) while FY2025 ($130.5B) is already available. Consensus shows FY_Current at $369B — a 6x gap.

**Root Cause**: `--period FY2024` was passed explicitly. The system defaults to the period specified without checking if newer data exists.

**Status**: Not fixed in this round — requires design decision on whether to auto-detect latest available period vs. respecting user input. Recommend adding a warning when consensus estimates are >2x the historical revenue.

**Lesson**: Add a data freshness check — if consensus FY_Current is >50% above the period's revenue, warn the user.

---

## Issue #4: Peer ROIC = 0.0%
**Symptom**: All peers show ROIC=0.0% except the target company.

**Root Cause**: `PeerFundamentals.roic` defaults to `0` (not `None`). yfinance doesn't provide ROIC directly. The value is never populated but displayed as 0.0%.

**Fix**: Changed default to `None`. HTML renderer now shows "—" for None values.

**Files Changed**: `openbb_connector.py` (dataclass), `html_report.py` (peer table rendering)

**Lesson**: Use `None` for missing data, `0` for actual zeros. Display logic must distinguish between the two.

---

## Issue #5: Sensitivity Table Values
**Symptom**: Sensitivity table shows $17,980 base value and $280,710 shocked value — nonsensical at per-share level.

**Root Cause**: Cascading from Issue #1. DCF total equity value divided by pre-split shares (2.46B) produces inflated per-share values.

**Fix**: Resolved by fixing share count in Issue #1.

---

## Issue #6: Mock Agent Template Text
**Symptom**: Valuation Analyst counterargument: "Key counter-thesis: alternative interpretation of the data exists" — obviously a placeholder.

**Root Cause**: When LLM call fails (JSON parse error), the system falls back to `mock_client.py` which has hardcoded META-specific text. The generic fallback produces vacuous placeholder text.

**Fix**: 
1. Improved generic fallback text to be role-appropriate but clearly labeled as "[rule-based fallback]"
2. Removed META-specific hardcoded numbers from valuation_analyst mock

**Future Work**: Mock client should extract actual metrics from the context parameter to produce data-grounded fallback judgments.

**Files Changed**: `aegis/core/llm/mock_client.py`

---

## Issue #7: Segment Naming
**Symptom**: "Oemand Other", "Us", "Tw" instead of "OEM & Other", "United States", "Taiwan"

**Root Cause**: `_format_segment_name()` falls back to `.title()` for unrecognized segments. `title()` converts "us" → "Us", "oem and other" → "Oemand Other" (because the XBRL member was concatenated without spaces).

**Fix**: Added extensive `_SEGMENT_NAME_MAP` entries for geographic codes and NVDA-specific segments. Improved fallback to fix common uppercase abbreviations.

**Files Changed**: `aegis/core/reports/html_report.py`

**Lesson**: XBRL member names are not human-readable. The mapping table needs to be comprehensive and extensible.

---

## Previously Fixed Issues (Same Session)

| Issue | Fix |
|-------|-----|
| `html_report.py` — `{{}}` in f-string causing `unhashable type: 'dict'` | Changed to `dict()` |
| `auto_research.py` — `agent_macro` UnboundLocalError | Pass `None` instead |
| `research_director.py` / `thesis_synthesizer.py` — `:.1f` on string | Added `float()` cast |
| `research_director.py` / `thesis_synthesizer.py` — scenarios dict format | Added `isinstance` filter |
| `judgment_schema.py` — `source_ids` min_length=1 too strict | Changed to `default_factory=list` |
| `glm_client.py` — JSON parse failures | Added multi-stage repair logic |

---

## Architecture Lessons

1. **Data provenance matters**: When mixing data from different sources (XBRL, yfinance, user input), always validate that units, currency, and share basis are consistent.
2. **Fail gracefully, label clearly**: When an LLM agent fails, the fallback should be labeled as such in the report so readers know which sections have reduced quality.
3. **Defensive formatting**: Never apply numeric format codes (`.1f`, `.1%`) without ensuring the value is actually numeric. Always cast or guard.
4. **Null vs Zero**: Use `None` for missing data throughout the pipeline. `0` should only mean "the value is actually zero."
