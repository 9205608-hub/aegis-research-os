"""HTML report renderer v2 — Claude Design template.

Replaces the 2400-line Python HTML string assembler (html_report_legacy.py)
with a template-injection approach:

    1. Build REPORT dict from pipeline outputs
    2. Read web/report.html + web/report.jsx templates
    3. Inline the JSX with window.REPORT injected
    4. Return single self-contained HTML string

Signature is drop-in compatible with the legacy `generate_html_report`
so auto_research.py and replay_from_cache.py don't need to change.

Missing-data tolerance is intentional: every pipeline doesn't fill every
field, and we'd rather ship a report with a half-empty section than crash.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────
# Template paths
# ─────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_HTML = _PROJECT_ROOT / "web" / "report.html"
_TEMPLATE_JSX = _PROJECT_ROOT / "web" / "report.jsx"


# ─────────────────────────────────────────────────────────────────
# i18n helpers
# ─────────────────────────────────────────────────────────────────

_AGENT_ZH = {
    "management_analyst": "管理层分析师",
    "business_analyst": "业务分析师",
    "accounting_analyst": "会计分析师",
    "valuation_analyst": "估值分析师",
    "risk_analyst": "风险分析师",
    "variant_analyst": "变体分析师",
    "sector_context_agent": "宏观分析师",
    "macro_analyst": "宏观分析师",
    "research_director": "研究总监",
}

_AGENT_TOPIC_ZH = {
    "management_analyst": "资本配置与战略纪律",
    "business_analyst": "产能利用率与资产错配",
    "accounting_analyst": "财报质量与现金流真实性",
    "valuation_analyst": "DCF 与多组估值三角",
    "risk_analyst": "客户集中度与偿付能力",
    "variant_analyst": "市场共识与错价机制",
    "sector_context_agent": "行业周期与政策环境",
}

_AGENT_EN = {
    "management_analyst": "Management Analyst",
    "business_analyst": "Business Analyst",
    "accounting_analyst": "Accounting Analyst",
    "valuation_analyst": "Valuation Analyst",
    "risk_analyst": "Risk Analyst",
    "variant_analyst": "Variant Analyst",
    "sector_context_agent": "Sector Context Agent",
}

_AGENT_TOPIC_EN = {
    "management_analyst": "Capital allocation & strategy",
    "business_analyst": "Unit economics & segment mix",
    "accounting_analyst": "Earnings quality & cash flow",
    "valuation_analyst": "DCF & multiples triangulation",
    "risk_analyst": "Concentration & solvency",
    "variant_analyst": "Consensus gap decomposition",
    "sector_context_agent": "Macro & industry context",
}

_CRITIC_ZH = {
    "logic": "逻辑批评员",
    "accounting": "财务批评员",
    "evidence": "证据批评员",
    "sector": "行业批评员",
    "cognitive_bias": "认知偏差批评员",
    "macro_consistency": "宏观一致性批评员",
    "market": "市场批评员",
    "valuation": "估值批评员",
    "numeric_consistency": "数值一致性批评员",
    "narrative_fact": "叙述事实核查员",
    "llm_judge": "LLM 数值审核员",
}

_CRITIC_EN = {
    "logic": "Logic Critic",
    "accounting": "Accounting Critic",
    "evidence": "Evidence Critic",
    "sector": "Sector Critic",
    "cognitive_bias": "Cognitive Bias Critic",
    "macro_consistency": "Macro Consistency Critic",
    "market": "Market Critic",
    "valuation": "Valuation Critic",
    "numeric_consistency": "Numeric Consistency Critic",
    "narrative_fact": "Narrative Fact Critic",
    "llm_judge": "LLM Judge",
}

_CONF_ZH = {
    "very_low": "极低", "low": "低", "medium": "中", "high": "高", "very_high": "极高",
}

_STANCE_BY_IMPLIED_PCT = {
    # When (target / price - 1) is:
    # < -10%: bear; > +10%: bull; else neutral
}


# ─────────────────────────────────────────────────────────────────
# Safe accessor helpers
# ─────────────────────────────────────────────────────────────────

def _g(obj: Any, attr: str, default: Any = None) -> Any:
    """Get attribute from object or dict, tolerant of missing."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def _f(val: Any, default: float = 0.0) -> float:
    """Coerce to float, default on None/invalid."""
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _first_clause(text: str, soft_max: int = 160, hard_max: int = 220) -> str:
    """Extract a headline-appropriate first clause from a long paragraph.

    Preference order: split on the first sentence terminator (. ! ? 。！？)
    if that lands ≤ hard_max; otherwise cut at a clause break (— , ，)
    near soft_max; else word-boundary truncate with ellipsis.
    """
    s = (text or "").strip()
    if not s:
        return ""
    if len(s) <= soft_max:
        return s
    # Prefer a sentence break.
    for terminator in ("。", "！", "？", ". ", "! ", "? "):
        idx = s.find(terminator)
        if 30 <= idx <= hard_max:
            return s[: idx + len(terminator)].strip()
    # Fall back to a clause break near soft_max.
    for sep in ("—", "；", ";", "，", ", "):
        idx = s.rfind(sep, 0, hard_max)
        if idx >= 60:
            return s[:idx].strip()
    # Last resort: word-boundary cut with ellipsis.
    cut = s.rfind(" ", 0, soft_max)
    if cut < 60:
        cut = soft_max
    return s[:cut].rstrip(" ,;，；—") + "…"


# ─────────────────────────────────────────────────────────────────
# Currency / exchange detection
# ─────────────────────────────────────────────────────────────────

def _detect_market(entity_id: str | None, scenarios: dict | None,
                   meta_facts: dict | None = None) -> tuple[str, str, str]:
    """Return (currency_code, currency_symbol, market_tag).

    Priority order for currency:
      1. `meta_facts["__display"]["currency"]` — set by fact_bridge from
         the actual filing currency (Refactor 2).
      2. `scenarios["currency"]` — set by the orchestrator after DCF.
      3. Entity-id heuristic — A-share 6-digit numeric → CNY, else USD.

    `meta_facts` takes precedence so the renderer always agrees with the
    filing-level normalization regardless of caller wiring quirks.
    """
    currency = None
    if isinstance(meta_facts, dict):
        _disp = meta_facts.get("__display")
        if isinstance(_disp, dict):
            currency = _disp.get("currency")
        if not currency:
            currency = meta_facts.get("__currency")
    if not currency and isinstance(scenarios, dict):
        currency = scenarios.get("currency")

    if not currency:
        eid = str(entity_id or "").replace(".SS", "").replace(".SZ", "").strip()
        if len(eid) == 6 and eid.isdigit():
            currency = "CNY"
        else:
            currency = "USD"

    sym = {"USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£", "JPY": "¥", "HKD": "HK$"}.get(currency, "$")
    market = "CN" if currency == "CNY" else "US"
    return currency, sym, market


def _resolve_display_ctx(meta_facts: dict | None, currency_code: str) -> dict:
    """Return the {symbol, scale, unit, big_scale, big_unit} dict for display.

    Prefers the canonical block written by fact_bridge; falls back to a
    USD/CNY default table for caches predating Refactor 2 so old replays
    keep rendering. All renderer call sites should consume the resulting
    dict instead of branching on `is_zh` / `__currency` themselves.
    """
    if isinstance(meta_facts, dict):
        _disp = meta_facts.get("__display")
        if isinstance(_disp, dict) and _disp.get("symbol"):
            return dict(_disp)
    _DEFAULTS = {
        "CNY": {"symbol": "¥", "scale": 1e8, "unit": "亿",
                "big_scale": 1e12, "big_unit": "万亿"},
        "USD": {"symbol": "$", "scale": 1e9, "unit": "B",
                "big_scale": 1e12, "big_unit": "T"},
    }
    out = dict(_DEFAULTS.get(currency_code, _DEFAULTS["USD"]))
    out["currency"] = currency_code
    return out


def _detect_exchange(entity_id: str | None, currency: str) -> str:
    """Derive exchange code from ticker. A-share: SZ (0/3-prefix) or SH (6-prefix)."""
    eid = str(entity_id or "").upper().replace(".SS", "").replace(".SZ", "").strip()
    if currency == "CNY" and len(eid) == 6 and eid.isdigit():
        if eid.startswith("6"):
            return "SH"
        return "SZ"
    # Heuristic: most equity US tickers are listed on NASDAQ (default) or NYSE
    return "NASDAQ"


# ─────────────────────────────────────────────────────────────────
# Rating derivation
# ─────────────────────────────────────────────────────────────────

def _derive_rating(target: float, price: float, is_zh: bool) -> tuple[str, str]:
    """Map (target vs price) implied return to rating word + tone.

    Tone classes map to colors in report.jsx CSS:
        "buy"  → green
        "hold" → warn
        "avoid" → red (rendered as --up for CN 涨红 convention, but the report.html
                       shares palette with US convention, so kept semantic)
    """
    if price <= 0:
        return ("持有", "hold") if is_zh else ("Hold", "hold")
    implied = (target / price - 1) * 100
    if implied >= 20:
        return ("买入", "buy") if is_zh else ("Buy", "buy")
    if implied >= 5:
        return ("增持", "buy") if is_zh else ("Overweight", "buy")
    if implied <= -15:
        return ("回避", "avoid") if is_zh else ("Avoid", "avoid")
    if implied <= -5:
        return ("减持", "avoid") if is_zh else ("Reduce", "avoid")
    return ("持有", "hold") if is_zh else ("Hold", "hold")


def _derive_stance(implied_pct: float) -> str:
    """bear / bull / neutral from signed impact percent."""
    if implied_pct >= 10:
        return "bull"
    if implied_pct <= -10:
        return "bear"
    return "neutral"


# Keyword heuristics for extracting stance from an agent's first inference.
# Tuned from real AAPL/NVDA/TSLA/301358 agent outputs — not perfect, but
# catches clearly-directional claims. Falls back to neutral when mixed.
_BEAR_CUES_EN = (
    "overvalued", "overpay", "overprice", "fair value is not", "not robust",
    "significant risk", "concerns", "challenged", "deteriorat", "decline",
    "gap between", "below ", "shortfall", "weakness", "pressure", "headwind",
    "overstated", "red flag", "downside", "impair",
)
_BULL_CUES_EN = (
    "exceptional", "outstanding", "undervalued", "underpriced", "robust",
    "sound", "strong", "healthy", "durable", "resilient", "moat",
    "upside", "tailwind", "advantage", "leadership", "compound",
)
_BEAR_CUES_ZH = (
    "高估", "过度定价", "定价过高", "风险", "承压", "下行", "压制", "恶化",
    "弱化", "空间不足", "过剩", "断崖", "回落", "下滑", "减速", "缩水",
    "无法支撑", "偏差", "隐患", "不足",
)
_BULL_CUES_ZH = (
    "低估", "被低估", "坚挺", "出色", "优秀", "稳健", "韧性", "护城河",
    "上行", "顺风", "优势", "领先", "壁垒", "强劲", "修复", "改善",
)


_NEGATION_CUES_ZH = ("而非", "并非", "未达", "未到", "未能", "无法", "尚未",
                     "不是", "不属于", "不接近", "不构成", "不具备", "缺乏",
                     "远非", "并未", "尚不", "未必", "并不", "并无", "毫无",
                     "没有", "不存在", "难以", "免于", "免除")
_NEGATION_CUES_EN = ("not ", "no ", "without ", "lack of", "rather than",
                     "instead of", "absent", "unable to", "fails to",
                     "yet to", "far from")


def _stance_from_text(text: str, is_zh: bool) -> str:
    """Infer a single agent's stance from their first inference text.

    BUG-Y28 (2026-05-06): substring matching previously counted negated cues
    as positive hits. Cambricon v5 business_analyst thesis "市场地位更接近
    '窗口期红利'**而非**'可持续技术领先'" contains "领先" — a bull cue —
    that's part of a negated phrase. Old code returned `bull` despite the
    thesis being clearly bear-leaning. Now we scan ~12 chars before each
    cue match for a negation token; if found, the hit flips sides
    (bull negated → bear, bear negated → bull) instead of being counted
    naively.
    """
    if not text:
        return "neutral"
    t = text.lower() if not is_zh else text
    bear_cues = _BEAR_CUES_ZH if is_zh else _BEAR_CUES_EN
    bull_cues = _BULL_CUES_ZH if is_zh else _BULL_CUES_EN
    neg_cues = _NEGATION_CUES_ZH if is_zh else _NEGATION_CUES_EN

    def _count_with_negation(cues: tuple, opposite_cues: tuple) -> tuple[int, int]:
        """Returns (own_hits, flipped_hits_to_opposite_side).

        For each cue match, scan back up to 12 chars (Chinese) / 24 chars
        (English) for a negation token. If found, the hit "flips" — count
        toward the opposite side instead. Otherwise count own.
        """
        window = 12 if is_zh else 24
        own = 0
        flipped = 0
        # Find each cue position; check window before each occurrence.
        for c in cues:
            start = 0
            while True:
                idx = t.find(c, start)
                if idx < 0:
                    break
                # Look back `window` chars for any negation cue
                ctx_before = t[max(0, idx - window):idx]
                if any(n in ctx_before for n in neg_cues):
                    flipped += 1
                else:
                    own += 1
                start = idx + len(c)
        return own, flipped

    # bull cues that are negated count as bear; bear cues that are negated
    # count as bull. Compose the totals.
    bull_own, bull_flipped = _count_with_negation(bull_cues, bear_cues)
    bear_own, bear_flipped = _count_with_negation(bear_cues, bull_cues)
    bull_hits = bull_own + bear_flipped
    bear_hits = bear_own + bull_flipped

    if bear_hits > bull_hits:
        return "bear"
    if bull_hits > bear_hits:
        return "bull"
    return "neutral"


def _derive_score(judgment: Any) -> float:
    """Map judgment inference confidences to a 1-5 score. Rough heuristic."""
    infs = _g(judgment, "inferences", []) or []
    if not infs:
        return 3.0
    conf_map = {"very_low": 1.5, "low": 2.2, "medium": 3.0, "high": 3.8, "very_high": 4.5}
    scores = [conf_map.get(_g(i, "confidence", "medium"), 3.0) for i in infs]
    if not scores:
        return 3.0
    return round(sum(scores) / len(scores), 1)


# ─────────────────────────────────────────────────────────────────
# Sparkline (right-rail priceHistory)
# ─────────────────────────────────────────────────────────────────

_SPARKLINE_CACHE: dict[str, list[float]] = {}
_QUOTE_META_CACHE: dict[str, dict] = {}
_CSI300_CACHE: dict = {}  # {"dates": [...], "closes": [...]} — one-shot
_CN_HIST_CACHE: dict[str, Any] = {}  # ticker → DataFrame (1 year)


def _cn_prefix(code: str) -> str:
    """Map A-share 6-digit code to sina prefix (sh / sz)."""
    if code.startswith("6"):
        return "sh"
    return "sz"


def _fetch_cn_daily(code: str):
    """Fetch ~1 year of daily OHLCV for an A-share, preferring sina (stable)
    and falling back to eastmoney. Returns a pandas DataFrame normalized to
    columns: date / high / low / close / volume (volume in shares).

    Returns None on total failure. Cached per-process by code.
    """
    if code in _CN_HIST_CACHE:
        return _CN_HIST_CACHE[code]
    import time as _t
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")

    # ── Try sina first (stable, no push2 dependency) ──
    try:
        import akshare as ak
        from aegis.core.acquisition.connectors.akshare_connector import _no_proxy
        with _no_proxy():
            df = ak.stock_zh_a_daily(
                symbol=f"{_cn_prefix(code)}{code}",
                adjust="qfq",
                start_date=start, end_date=end,
            )
        if df is not None and not df.empty:
            out = df[["date", "high", "low", "close", "volume"]].copy()
            out["date"] = out["date"].astype(str)
            _CN_HIST_CACHE[code] = out
            return out
    except Exception:
        pass

    # ── Fallback: eastmoney (Chinese column names, volume in 手) ──
    try:
        import akshare as ak
        from aegis.core.acquisition.connectors.akshare_connector import _no_proxy
        for attempt in range(3):
            try:
                with _no_proxy():
                    df = ak.stock_zh_a_hist(
                        symbol=code, period="daily",
                        start_date=start, end_date=end, adjust="qfq",
                    )
                if df is not None and not df.empty:
                    out = df.rename(columns={
                        "日期": "date", "最高": "high", "最低": "low",
                        "收盘": "close", "成交量": "volume",
                    })[["date", "high", "low", "close", "volume"]].copy()
                    out["date"] = out["date"].astype(str)
                    # eastmoney volume is in 手 (100 shares)
                    out["volume"] = out["volume"] * 100
                    _CN_HIST_CACHE[code] = out
                    return out
            except Exception:
                if attempt < 2:
                    _t.sleep(0.8)
    except Exception:
        pass
    return None


def _csi300_series() -> dict:
    """Fetch the CSI 300 daily closes once per process. Empty dict on failure."""
    if _CSI300_CACHE:
        return _CSI300_CACHE
    try:
        import akshare as ak
        from aegis.core.acquisition.connectors.akshare_connector import _no_proxy
        with _no_proxy():
            df = ak.stock_zh_index_daily(symbol="sh000300")
        if df is None or df.empty:
            return {}
        _CSI300_CACHE["dates"] = [str(d) for d in df["date"].tolist()]
        _CSI300_CACHE["closes"] = [float(c) for c in df["close"].tolist()]
    except Exception:
        pass
    return _CSI300_CACHE


def _fetch_cn_div_yield(code: str) -> float | None:
    """Most recent annual cash-dividend yield for an A-share, via eastmoney.

    Returns the 股息率 value from the latest 实施分配 row, expressed as a
    percentage (e.g. 0.54 for 0.54%). None on failure or zero dividend.
    """
    try:
        import akshare as ak
        from aegis.core.acquisition.connectors.akshare_connector import _no_proxy
        import time as _t
        for attempt in range(2):
            try:
                with _no_proxy():
                    df = ak.stock_fhps_detail_em(symbol=code)
                if df is None or df.empty:
                    return None
                col = "现金分红-股息率"
                if col not in df.columns:
                    return None
                # Prefer 实施分配 (executed); fall back to latest row.
                status_col = "方案进度"
                if status_col in df.columns:
                    executed = df[df[status_col] == "实施分配"]
                    if not executed.empty:
                        df = executed
                # akshare returns the fraction (0.009 = 0.9%); convert to pct.
                raw = df[col].dropna()
                if raw.empty:
                    return None
                latest = float(raw.iloc[-1])
                if latest <= 0:
                    return None
                return latest * 100
            except Exception:
                if attempt == 0:
                    _t.sleep(0.8)
    except Exception:
        pass
    return None


def _compute_cn_beta(stock_df, date_col: str, close_col: str) -> float | None:
    """Beta against CSI 300 on the most recent 60 aligned trading days."""
    idx = _csi300_series()
    if not idx or len(idx.get("dates", [])) < 60:
        return None
    # Build index lookup: date → close
    idx_map = dict(zip(idx["dates"], idx["closes"]))
    # Align stock history with index by date; both are ISO strings.
    s_dates = [str(d) for d in stock_df[date_col].tolist()]
    s_closes = [float(c) for c in stock_df[close_col].tolist()]
    pairs = [(sc, idx_map[d]) for d, sc in zip(s_dates, s_closes)
             if d in idx_map]
    if len(pairs) < 60:
        return None
    pairs = pairs[-60:]
    # Daily returns
    sr = [pairs[i][0] / pairs[i - 1][0] - 1 for i in range(1, len(pairs))]
    ir = [pairs[i][1] / pairs[i - 1][1] - 1 for i in range(1, len(pairs))]
    if len(sr) < 30:
        return None
    mean_i = sum(ir) / len(ir)
    var_i = sum((x - mean_i) ** 2 for x in ir) / len(ir)
    if var_i <= 0:
        return None
    mean_s = sum(sr) / len(sr)
    cov_si = sum((sr[k] - mean_s) * (ir[k] - mean_i) for k in range(len(sr))) / len(sr)
    return round(cov_si / var_i, 2)


def _fetch_quote_meta(entity_id: str, market_tag: str) -> dict:
    """Fetch 52-week range + beta + dividend yield for the rail KVs.

    Returns a dict with keys: high52w, low52w, beta, div_yield (values may be
    None if missing). Empty dict on total failure.

    Respects `AEGIS_SKIP_SPARKLINE=1` (shares the same offline switch as the
    sparkline fetch — both hit yfinance).
    """
    if os.environ.get("AEGIS_SKIP_SPARKLINE") == "1":
        return {}
    eid = str(entity_id or "").strip()
    if not eid:
        return {}
    cache_key = f"{market_tag}:{eid}"
    if cache_key in _QUOTE_META_CACHE:
        return _QUOTE_META_CACHE[cache_key]
    meta: dict = {}
    try:
        if market_tag == "CN":
            code = eid.replace(".SS", "").replace(".SZ", "").strip()
            if len(code) == 6 and code.isdigit():
                df = _fetch_cn_daily(code)
                if df is not None and not df.empty:
                    recent = df.tail(252)
                    meta["high52w"] = float(recent["high"].max())
                    meta["low52w"] = float(recent["low"].min())
                    avg_vol = float(recent["volume"].mean() or 0)
                    if avg_vol > 0:
                        meta["avg_volume"] = avg_vol
                    meta["beta"] = _compute_cn_beta(recent, "date", "close")
                # Latest annual cash-dividend yield from eastmoney 分红配股
                meta["div_yield"] = _fetch_cn_div_yield(code)
        else:
            from aegis.core.acquisition.connectors.market_data_connector import MarketDataConnector
            yf_symbol = eid.split("_")[0].upper()
            snap = MarketDataConnector().get_snapshot(yf_symbol)
            meta = {
                "high52w": snap.fifty_two_week_high,
                "low52w": snap.fifty_two_week_low,
                "beta": snap.beta,
                "div_yield": snap.dividend_yield,
                "avg_volume": snap.average_volume,
            }
    except Exception:
        meta = {}
    _QUOTE_META_CACHE[cache_key] = meta
    return meta


def _fetch_sparkline(entity_id: str, market_tag: str, price_last: float) -> list[float]:
    """Fetch recent daily closes for the rail sparkline.

    Returns oldest-to-newest positive floats (last ~60 trading days). On any
    failure returns `[price_last]` (prior single-point behavior) so the UI still
    draws something. Results are cached in-process so replay_from_cache and
    repeated renders in the same session don't re-hit the network.

    Set `AEGIS_SKIP_SPARKLINE=1` to force the single-point fallback (useful for
    offline tests).
    """
    if os.environ.get("AEGIS_SKIP_SPARKLINE") == "1":
        return [price_last] if price_last else []
    fallback = [price_last] if price_last else []
    eid = str(entity_id or "").strip()
    if not eid:
        return fallback

    cache_key = f"{market_tag}:{eid}"
    if cache_key in _SPARKLINE_CACHE:
        return _SPARKLINE_CACHE[cache_key]

    closes: list[float] = []
    try:
        if market_tag == "CN":
            code = eid.replace(".SS", "").replace(".SZ", "").strip()
            if len(code) == 6 and code.isdigit():
                df = _fetch_cn_daily(code)
                if df is not None and not df.empty:
                    closes = [float(x) for x in df["close"].tolist()
                              if x is not None and float(x) > 0]
        else:
            from aegis.core.acquisition.connectors.market_data_connector import MarketDataConnector
            conn = MarketDataConnector()
            # Entity IDs sometimes carry a slugified-company suffix (e.g.
            # "meta_platforms") that yfinance does not recognize; take the
            # first segment before `_` as the ticker.
            yf_symbol = eid.split("_")[0].upper()
            points = conn.get_price_history(yf_symbol, period="3mo")
            closes = [float(p.close) for p in points
                      if getattr(p, "close", 0) and float(p.close) > 0]
    except Exception:
        closes = []

    closes = closes[-60:]
    result = closes if len(closes) >= 2 else fallback
    _SPARKLINE_CACHE[cache_key] = result
    return result


# ─────────────────────────────────────────────────────────────────
# Macro section builder
# ─────────────────────────────────────────────────────────────────

def _build_macro_block(macro_snapshot: Any, is_zh: bool) -> dict | None:
    """Render a macro context section from a MacroSnapshot (FRED-backed).

    Returns None when no snapshot or no usable fields. The caller passes
    through to `REPORT.macro`; the template auto-hides on null.
    """
    if macro_snapshot is None:
        return None

    ffr = getattr(macro_snapshot, "fed_funds_rate", None)
    y10 = getattr(macro_snapshot, "us_10y_yield", None)
    cpi = getattr(macro_snapshot, "cpi_yoy", None)
    unemp = getattr(macro_snapshot, "unemployment_rate", None)
    vix = getattr(macro_snapshot, "vix", None)
    pmi = getattr(macro_snapshot, "pmi_manufacturing", None)
    slope = getattr(macro_snapshot, "yield_curve_slope_2s10s", None)
    dxy = getattr(macro_snapshot, "usd_dxy", None)
    cycle = getattr(macro_snapshot, "cycle_phase_estimate", None)

    def _pct(x: float | None) -> str | None:
        return f"{x * 100:.2f}%" if x is not None else None

    # KPI table — skip any field that's None
    kpis: list[dict] = []
    pairs_en = [
        ("Fed Funds Rate", _pct(ffr)),
        ("US 10Y Yield", _pct(y10)),
        ("CPI YoY", _pct(cpi)),
        ("Unemployment", _pct(unemp)),
        ("VIX", f"{vix:.1f}" if vix else None),
        ("Manufacturing PMI", f"{pmi:.1f}" if pmi else None),
        ("2s10s Spread", f"{slope:.0f} bps" if slope is not None else None),
        ("US Dollar Index", f"{dxy:.1f}" if dxy else None),
    ]
    pairs_zh = [
        ("联邦基金利率", _pct(ffr)),
        ("10 年期美债", _pct(y10)),
        ("CPI 同比", _pct(cpi)),
        ("失业率", _pct(unemp)),
        ("VIX", f"{vix:.1f}" if vix else None),
        ("制造业 PMI", f"{pmi:.1f}" if pmi else None),
        ("2s10s 利差", f"{slope:.0f} bps" if slope is not None else None),
        ("美元指数", f"{dxy:.1f}" if dxy else None),
    ]
    pairs = pairs_zh if is_zh else pairs_en
    for label, value in pairs:
        if value is not None:
            kpis.append({"label": label, "value": value})

    # Narrative paragraphs — purely descriptive, no forward speculation
    paragraphs: list[str] = []
    if ffr is not None and cpi is not None:
        real_rate = (ffr - cpi) * 100
        if is_zh:
            p1 = (f"当前联邦基金利率 <strong>{ffr*100:.2f}%</strong>，"
                  f"CPI 同比 <strong>{cpi*100:.2f}%</strong>，"
                  f"真实利率约 <strong>{real_rate:+.2f}%</strong>")
            if y10 is not None:
                p1 += f"。10 年期美债收益率 <strong>{y10*100:.2f}%</strong>"
                if slope is not None:
                    p1 += f"，2s10s 利差 <strong>{slope:.0f} bps</strong>"
            p1 += "。"
        else:
            p1 = (f"The Fed Funds Rate is <strong>{ffr*100:.2f}%</strong> against "
                  f"<strong>{cpi*100:.2f}%</strong> CPI YoY, implying a real rate of "
                  f"<strong>{real_rate:+.2f}%</strong>")
            if y10 is not None:
                p1 += f". The US 10Y yield sits at <strong>{y10*100:.2f}%</strong>"
                if slope is not None:
                    p1 += f" with a 2s10s spread of <strong>{slope:.0f} bps</strong>"
            p1 += "."
        paragraphs.append(p1)

    if vix is not None or pmi is not None:
        parts: list[str] = []
        if vix is not None:
            if is_zh:
                vix_desc = "低波动" if vix < 15 else ("正常" if vix < 22 else "高波动")
                parts.append(f"VIX <strong>{vix:.1f}</strong>（{vix_desc}）")
            else:
                vix_desc = "low-vol regime" if vix < 15 else ("normal vol" if vix < 22 else "stressed")
                parts.append(f"volatility sits in a <strong>{vix_desc}</strong> ({vix:.1f} VIX)")
        if pmi is not None:
            if is_zh:
                pmi_desc = "收缩" if pmi < 50 else ("边际扩张" if pmi < 52 else "扩张")
                parts.append(f"制造业 PMI <strong>{pmi:.1f}</strong>（{pmi_desc}）")
            else:
                pmi_desc = "contracting" if pmi < 50 else ("marginally expanding" if pmi < 52 else "expanding")
                parts.append(f"manufacturing is <strong>{pmi_desc}</strong> ({pmi:.1f} PMI)")
        if parts:
            sep = "；" if is_zh else "; "
            paragraphs.append(sep.join(parts) + ("。" if is_zh else "."))

    if not paragraphs and not kpis:
        return None

    subtitle = None
    if cycle:
        cycle_zh = {
            "early_expansion": "扩张初期",
            "mid_expansion": "扩张中期",
            "late_expansion": "扩张末期",
            "mid_cycle": "周期中段",
            "mid-cycle": "周期中段",
            "slowdown": "放缓期",
            "recession": "衰退期",
            "early_recovery": "复苏初期",
            "recovery": "复苏期",
        }.get(cycle.lower(), cycle)
        cycle_en = cycle.replace("_", " ")
        # Avoid "mid-cycle cycle" stutter — the name may already say cycle.
        if is_zh:
            subtitle = f"{cycle_zh} · 周期" if "周期" not in cycle_zh else cycle_zh
        else:
            subtitle = cycle_en if "cycle" in cycle_en.lower() else f"{cycle_en} cycle"

    return {
        "title": "宏观环境" if is_zh else "Macro Context",
        "subtitle": subtitle,
        "paragraphs": paragraphs,
        "kpis": kpis,
        "kpisTitle": "关键宏观变量" if is_zh else "Key macro variables",
        "shares": [],
    }


# ─────────────────────────────────────────────────────────────────
# Main builder
# ─────────────────────────────────────────────────────────────────

def build_report_dict(
    *,
    decision: Any = None,
    computed_metrics: dict | None = None,
    market_data: dict | None = None,
    agent_judgments: list | None = None,
    critic_results: list | None = None,
    meta_facts: dict | None = None,
    dcf_projections: list | None = None,
    dcf_output: Any = None,
    sensitivity_table: dict | None = None,
    sensitivity_rankings: list | None = None,
    entity_name: str | None = None,
    entity_id: str | None = None,
    # Display-time decomposition (Refactor 1, 2026-05-04). The orchestrator
    # splits A-share risk-warning prefixes (ST / *ST) from the company name
    # at name-resolution time. Renderer consumes these directly instead of
    # re-stripping in the display layer. Falls back to entity_name when not
    # provided (e.g. legacy callers).
    entity_name_clean: str | None = None,
    risk_warning_prefix: str | None = None,
    segment_detail: dict | None = None,
    edited_report: Any = None,
    synthesized_thesis: Any = None,
    catalyst_timeline: Any = None,
    scenarios: dict | None = None,
    scenario_probabilities: dict | None = None,
    run_id: str | None = None,
    period: str | None = None,
    pipeline_duration: str | None = None,
    model_name: str | None = None,
    open_questions: list | None = None,
    macro_snapshot: Any = None,
    **_unused,
) -> dict:
    """Map the pipeline parameter bag to the REPORT schema consumed by report.jsx.

    Every field has a safe fallback — missing input produces an empty/placeholder
    section rather than a crash.
    """
    computed_metrics = computed_metrics or {}
    market_data = market_data or {}
    meta_facts = meta_facts or {}
    scenarios = scenarios or {}
    agent_judgments = agent_judgments or []
    critic_results = critic_results or []
    dcf_projections = dcf_projections or []

    # Derive currency / market
    eid = entity_id or _g(decision, "entity_id", "")
    currency_code, curr_sym, market_tag = _detect_market(eid, scenarios, meta_facts)
    exchange = _detect_exchange(eid, currency_code)
    is_zh = currency_code == "CNY"
    # Refactor 2 (2026-05-04): single source of truth for display formatting.
    # All sites that previously branched on `is_zh` for "1e8 亿 vs 1e9 B"
    # now consume `display_ctx` directly. Symbol stays in `curr_sym` for
    # backwards-compatible call sites that already use it.
    display_ctx = _resolve_display_ctx(meta_facts, currency_code)
    curr_sym = display_ctx["symbol"]

    # Price
    price_last = _f(market_data.get("current_price"))
    # Intraday delta isn't reliably cached — heuristic 0 if not provided
    price_change = _f(market_data.get("day_change"), 0.0)
    price_change_pct = _f(market_data.get("day_change_pct"), 0.0)

    # Scenarios: bear/base/bull + probabilities + narratives
    sc = scenarios
    prob_weighted = _f(sc.get("probability_weighted_value"))
    base_value = _f(sc.get("base_value"))
    # Target = probability-weighted value (primary), fallback to base value
    target = prob_weighted if prob_weighted > 0 else base_value

    # DCF-meaningful guard. Refactor 4 (2026-05-04): prefer the engine-
    # set flag from DCFOutput; the engine has the most accurate view
    # (e.g. enterprise_value < 0 even when per_share rounds positive).
    # Fall back to the local heuristic for callers that don't pass
    # dcf_output (legacy / partial replays).
    _engine_meaningful = _g(dcf_output, "is_meaningful", None) if dcf_output is not None else None
    _ebitda = _f(meta_facts.get("ebitda"))
    _opincome = _f(meta_facts.get("operating_income"))
    if _engine_meaningful is not None:
        _dcf_meaningful = bool(_engine_meaningful) and (_ebitda > 0 or _opincome > 0)
    else:
        _dcf_meaningful = (base_value > 0) and (_ebitda > 0 or _opincome > 0)

    # Book value per share — useful asset-floor anchor when DCF is n/m.
    _equity = _f(meta_facts.get("total_equity"))
    _shares = _f(meta_facts.get("shares_outstanding")) or _f(market_data.get("shares_outstanding"))
    book_per_share = (_equity / _shares) if (_equity > 0 and _shares > 0) else 0.0

    # BUG-Y22 (2026-05-06): rating used to call `_derive_rating(target, price)`
    # whenever DCF was meaningful, completely ignoring publishing_status. So
    # a BLOCKED Cambricon report (signal=no_signal, sizing=no_position) still
    # rendered as "买入" with target ¥4475.77 just because (target/price-1)
    # > 20%. That contradicts the publish gate's verdict and seriously
    # misleads readers who skim the rating block. Now the publish-gate
    # outcome takes precedence: blocked / needs_review → "暂不评级" with
    # neutral tone, and we still surface the DCF-implied return as context
    # below so the analytical signal isn't lost.
    _ps = (_g(decision, "publishing_status", "") or "").lower()
    _rating_downgraded = False
    if _ps == "blocked":
        rating_word = "暂不评级" if is_zh else "Not Rated"
        rating_tone = "hold"  # neutral colour — neither buy nor avoid
    elif _ps == "needs_review":
        rating_word = "审核中" if is_zh else "Under Review"
        rating_tone = "hold"
    elif _ps == "downgraded":
        # AUDIT-C2 (2026-07): the decision engine's third status — unresolved
        # cross-agent conflicts downgrade the report instead of blocking it
        # (decision_engine/engine.py). The Y22 fix only covered blocked /
        # needs_review, so downgraded fell through and rendered as a clean
        # "买入" with zero caveat. Keep the derived rating (the engine chose
        # to downgrade, not withhold) but flag it so the rating block reads
        # "评级已降级 · 存在未解决分歧" instead of "概率加权".
        _rating_downgraded = True
        if _dcf_meaningful:
            rating_word, rating_tone = _derive_rating(target, price_last, is_zh)
        elif book_per_share > 0 and price_last > 0:
            rating_word, rating_tone = _derive_rating(book_per_share, price_last, is_zh)
        else:
            rating_word, rating_tone = ("持有", "hold") if is_zh else ("Hold", "hold")
    elif not _dcf_meaningful:
        # When DCF is n/m, use book value per share as the implied-return
        # anchor (book is a hard asset floor for distressed equities).
        # If book is also missing/negative, fall back to "持有" since the
        # publish-status branches above already handle blocked/needs_review.
        if book_per_share > 0 and price_last > 0:
            rating_word, rating_tone = _derive_rating(book_per_share, price_last, is_zh)
        else:
            rating_word, rating_tone = ("持有", "hold") if is_zh else ("Hold", "hold")
    else:
        rating_word, rating_tone = _derive_rating(target, price_last, is_zh)

    confidence_raw = _g(decision, "confidence_bucket", "medium")
    # Title-case the English version so it matches the visual weight of
    # other verdict labels (Buy / Hold / Avoid, Medium-High risk etc.)
    _CONF_EN = {
        "very_low": "Very Low", "low": "Low", "medium": "Medium",
        "high": "High", "very_high": "Very High",
    }
    confidence_display = _CONF_ZH.get(confidence_raw, confidence_raw) if is_zh \
        else _CONF_EN.get(confidence_raw, confidence_raw.replace("_", " ").title())

    # Ticker mark: first character of company name
    company_name = entity_name or (str(eid).replace("_", " ").title() if eid else "Unknown")
    # The orchestrator pre-splits A-share risk-warning prefixes (ST / *ST)
    # at name-resolution time so the renderer no longer needs to know about
    # exchange convention. Fall back to in-place split for legacy callers
    # that didn't pass the cleaned form.
    if not entity_name_clean:
        # Inline normalization for backwards compatibility (legacy callers
        # / older replay caches that pre-date Refactor 1).
        from aegis.core.orchestrator.auto_research import normalize_entity_display
        entity_name_clean, _legacy_prefix = normalize_entity_display(company_name)
        if not risk_warning_prefix:
            risk_warning_prefix = _legacy_prefix
    display_name = entity_name_clean or company_name
    eid_clean = str(eid or "").replace(".SS", "").replace(".SZ", "").upper()
    if is_zh and display_name:
        ticker_mark = display_name[:1]
    elif eid_clean:
        # Ticker mark: use the ticker itself (first 1–2 chars) rather than the
        # company name initial — avoids collisions like "A" for both Apple and
        # Alphabet, "M" for Meta and McDonald's, etc. A-share 6-digit codes
        # fall through to display_name[:1] above when is_zh is true.
        ticker_mark = eid_clean[:2] if len(eid_clean) >= 4 else eid_clean[:1]
    else:
        ticker_mark = display_name[:1] if display_name else "·"

    # Sector
    sector_raw = meta_facts.get("sector") or meta_facts.get("industry") or ""
    if not sector_raw and is_zh:
        sector_raw = "—"
    elif not sector_raw:
        sector_raw = "—"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Period label
    period_raw = period or _g(decision, "period", None) or "—"
    if is_zh and period_raw and period_raw.startswith("FY"):
        period_display = f"{period_raw} 年报"
    else:
        period_display = period_raw

    # ── Headline & lede (prefer edited_report, else synthesized_thesis) ──
    headline = _g(edited_report, "headline")
    lede = _g(edited_report, "lede") or _g(edited_report, "opening_paragraph")

    if not headline:
        # Fallback to synthesized core_thesis. It renders as the section
        # h2, so it needs to read like a title — one punchy clause. A full
        # paragraph (200+ chars) drowns the section head; aim for a
        # headline length.
        raw = _g(synthesized_thesis, "core_thesis", "") if synthesized_thesis else ""
        headline = _first_clause(raw, soft_max=100, hard_max=140)
    if not headline:
        # BUG-Y34 (2026-05-06): smoke / rule-based / no-llm modes don't run
        # the Editor or Synthesizer, so headline used to render empty —
        # report card looked broken. Build a deterministic title from
        # entity + DCF gap so the page header is still informative.
        try:
            implied = (target / price_last - 1) * 100 if (target and price_last) else None
        except Exception:
            implied = None
        company = entity_name or eid or ""
        if implied is not None:
            verdict = "上行空间" if implied > 0 else ("下行空间" if implied < 0 else "公允区间")
            if is_zh:
                # AUDIT (2026-07): mirror the English branch's abs() — a signed
                # negative here rendered "-23.8%下行空间", a double negative
                # that reads like upside. Direction lives in `verdict` alone.
                headline = f"{company}：DCF 基准隐含 {abs(implied):.1f}% {verdict}（rule-based 摘要）"
            else:
                direction = "upside" if implied > 0 else ("downside" if implied < 0 else "fairly valued")
                headline = f"{company}: rule-based DCF implies {abs(implied):.1f}% {direction}"
        else:
            headline = (
                f"{company}：rule-based 摘要（无 LLM 评论）" if is_zh
                else f"{company}: rule-based summary (no LLM commentary)"
            )
    if not lede:
        lede = _g(synthesized_thesis, "core_thesis", "") if synthesized_thesis else ""

    # Executive paragraphs: prefer synthesized_thesis
    exec_paragraphs = []
    if synthesized_thesis:
        for field in ("market_implied_story", "key_assumption_disagreement"):
            txt = _g(synthesized_thesis, field, "")
            if txt:
                exec_paragraphs.append(txt)
    if not exec_paragraphs and lede:
        exec_paragraphs = [lede]

    # Core callout
    # When DCF is n/m (loss-making, negative base), suppress the price-target
    # framing — a negative per-share value is not interpretable as a price
    # and "-131% 估值回归空间" is mathematically nonsensical (the floor is
    # -100%). Switch to qualitative language anchored on book value and
    # surface the underlying issue (持续亏损 / EBITDA<0).
    if not _dcf_meaningful:
        _why = []
        if base_value <= 0:
            _why.append("DCF 基准为负" if is_zh else "negative DCF base")
        if _ebitda <= 0:
            _why.append("EBITDA ≤ 0" if is_zh else "EBITDA ≤ 0")
        if _opincome <= 0:
            _why.append("营业利润为负" if is_zh else "negative operating income")
        _why_str = "、".join(_why) if is_zh else ", ".join(_why)
        if is_zh:
            _bv_clause = (
                f"账面每股净资产 {curr_sym}{book_per_share:.2f}，"
                if book_per_share > 0 else ""
            )
            core_callout = (
                f"<strong>核心判断：</strong>"
                f"{_bv_clause}"
                f"现价 {curr_sym}{price_last:.2f}；"
                f"DCF 在持续亏损情形下不具参考价值（{_why_str}），"
                f"建议参照同业可比、资产价值或重组期权框架评估。"
            )
        else:
            _bv_clause = (
                f"Book value/share {curr_sym}{book_per_share:.2f}; "
                if book_per_share > 0 else ""
            )
            core_callout = (
                f"<strong>Core judgment: </strong>"
                f"{_bv_clause}"
                f"spot {curr_sym}{price_last:.2f}. "
                f"DCF is not meaningful for a loss-making company "
                f"({_why_str}); use peer-multiple, asset-based, or "
                f"restructuring-option frameworks instead."
            )
    elif is_zh:
        core_callout = (
            f"<strong>核心判断：</strong>"
            f"基准 DCF 公允价值 {curr_sym}{base_value:.2f}；"
            f"概率加权 {curr_sym}{target:.2f}，"
            f"较现价 {curr_sym}{price_last:.2f} 存在 <strong>"
            f"{((target / price_last - 1) * 100 if price_last > 0 else 0):+.1f}%</strong> 估值回归空间。"
        )
    else:
        core_callout = (
            f"<strong>Core judgment: </strong>"
            f"Base-case DCF {curr_sym}{base_value:.2f}; "
            f"probability-weighted {curr_sym}{target:.2f}, "
            f"implying <strong>{((target / price_last - 1) * 100 if price_last > 0 else 0):+.1f}%</strong> "
            f"vs spot {curr_sym}{price_last:.2f}."
        )

    # ── Quick stats ──
    # Market cap in 亿/B units
    mkt_cap = _f(market_data.get("market_cap"))
    revenue = _f(meta_facts.get("revenue"))
    net_income = _f(meta_facts.get("net_income"))
    ebitda = _f(meta_facts.get("ebitda")) or _f(
        _f(meta_facts.get("operating_income")) + _f(meta_facts.get("depreciation_amortization"))
    )
    fcf_simple = _f(computed_metrics.get("fcf_simple"))
    op_margin = _f(computed_metrics.get("operating_margin")) * 100
    eps_basic = _f(meta_facts.get("eps_basic"))
    cfo_ni = _f(computed_metrics.get("cfo_to_net_income"))
    # Refactor 3 (2026-05-04): consume the canonical orchestrator keys.
    # Previously the renderer read non-existent `ev_ebitda` / `pe_ttm`
    # keys (orchestrator emits `ev_to_ebitda` / `pe_ratio_ttm`) and fell
    # back to local computation `price_last / eps_basic`, which BYPASSED
    # the upstream n/m guard and re-introduced "−44.3×" displays for
    # loss-making issuers. Read the canonical keys directly; if absent,
    # the orchestrator decided the ratio was not meaningful — surface
    # zero (renderer's `if val:` check then skips the row).
    ev_ebitda = _f(computed_metrics.get("ev_to_ebitda"))
    pe_ttm = _f(computed_metrics.get("pe_ratio_ttm") or computed_metrics.get("pe_ratio"))

    def _unit(v: float) -> str:
        """Format big numbers via the canonical display context.

        Refactor 2: scale / unit come from fact_bridge's __display block
        instead of being re-derived from `is_zh`. Output keeps the legacy
        spacing convention: zh uses " 亿" (space + suffix), en/USD uses
        "B" (no space) — preserving downstream snapshot compatibility.
        """
        sym = display_ctx["symbol"]
        big_scale = display_ctx.get("big_scale", 1e12)
        big_unit = display_ctx.get("big_unit", "T")
        scale = display_ctx.get("scale", 1e9)
        unit = display_ctx.get("unit", "B")
        # zh-style suffixes (亿 / 万亿 / 億 / 兆) read better with a space;
        # en-style (B / T) does not. Heuristic: ASCII suffix → no space.
        spacer = "" if unit.isascii() and big_unit.isascii() else " "
        if abs(v) >= big_scale:
            return f"{sym}{v / big_scale:,.2f}{spacer}{big_unit}"
        return f"{sym}{v / scale:,.1f}{spacer}{unit}"

    quick = []
    if mkt_cap:
        quick.append({"lbl": "市值" if is_zh else "Market Cap", "val": _unit(mkt_cap),
                      "sub": "" if not revenue else (f"市销率 {mkt_cap / revenue:.1f}×" if is_zh else f"P/S {mkt_cap / revenue:.1f}×")})
    # Refactor 3 (2026-05-04): the orchestrator now omits the metric key
    # entirely when the ratio is meaningless (P/E with negative earnings,
    # EV/EBITDA with EBITDA ≤ 0). Renderers just check for presence —
    # no n/m branching, no string-prefix conditionals. If the key isn't
    # there, skip the row.
    if pe_ttm:
        quick.append({"lbl": "市盈率 (TTM)" if is_zh else "P/E (TTM)", "val": f"{pe_ttm:.1f}×",
                      "sub": "" if not eps_basic else (f"EPS {eps_basic:.2f}" if not is_zh else f"每股收益 {eps_basic:.2f}")})
    if ev_ebitda:
        quick.append({"lbl": "EV / EBITDA", "val": f"{ev_ebitda:.1f}×", "sub": ""})
    if op_margin:
        quick.append({"lbl": "营业利润率" if is_zh else "Op. Margin", "val": f"{op_margin:.1f}%", "sub": ""})
    if fcf_simple:
        quick.append({"lbl": "自由现金流" if is_zh else "Free Cash Flow", "val": _unit(fcf_simple),
                      "sub": f"CFO / NI = {cfo_ni:.2f}" if cfo_ni else ""})
    else:
        # NVDA-like case: capex missing from cache → FCF can't be derived.
        # Fall back to CFO with a label that's honest about the gap.
        _cfo = _f(meta_facts.get("cfo")) or _f(meta_facts.get("cash_from_operations"))
        if _cfo:
            quick.append({"lbl": "经营现金流" if is_zh else "Operating Cash Flow", "val": _unit(_cfo),
                          "sub": f"CFO / NI = {cfo_ni:.2f}" if cfo_ni else (
                              "CFO 口径，capex 数据缺失" if is_zh else "CFO only; capex unavailable")})

    # ── Scenario cells ──
    scen_cells = []
    for key, label_zh, label_en in [
        ("bear", "悲观情景", "Bear Case"),
        ("base", "基准情景", "Base Case"),
        ("bull", "乐观情景", "Bull Case"),
    ]:
        px = _f(sc.get(f"{key}_value"))
        prob = _f(sc.get(f"{key}_probability"))
        narr = sc.get(f"{key}_narrative", "")
        # AUDIT-A10: when the orchestrator mechanically rewrote this
        # scenario value (0.5×/2× envelope clamp or inversion guard), it
        # sets `{key}_clamped` + the raw pre-clamp value. Disclose it —
        # BUG-Y37 only logged to stderr, so readers saw a bespoke narrative
        # paired with a number that was actually a clamp artifact.
        clamped = bool(sc.get(f"{key}_clamped"))
        raw_val = _f(sc.get(f"{key}_raw_value")) if clamped else 0.0
        footnote = ""
        if clamped:
            raw_txt = f"{curr_sym}{raw_val:.2f}" if raw_val else ""
            if is_zh:
                footnote = ("⚠ 该情景值超出合理区间已保守夹逼"
                            + (f"（模型原始输出 {raw_txt}）" if raw_txt else "（引擎保守修正）"))
            else:
                footnote = ("⚠ Value clamped to a conservative bound"
                            + (f" (raw model output {raw_txt})" if raw_txt else " (engine correction)"))
        cell = {
            "key": key,
            "tag": label_zh if is_zh else label_en,
            "prob": prob,
            "px": round(px, 2),
            # Append the disclosure to the narrative so the current JSX
            # template renders it without needing a new field; `clamped` /
            # `footnote` / `rawPx` stay structured for future templates.
            "narrative": (f"{narr}\n{footnote}".strip() if footnote else narr),
            "clamped": clamped,
        }
        if clamped:
            cell["footnote"] = footnote
            cell["rawPx"] = round(raw_val, 2) if raw_val else None
        scen_cells.append(cell)

    # ── Thesis ──
    st = synthesized_thesis
    thesis = {
        "core": _g(st, "core_thesis", "") or "",
        "variant": _g(st, "my_variant", "") or "",
        "whyNow": _g(st, "why_now", "") or "",
        "marketStory": _g(st, "market_implied_story", "") or "",
        "divergence": _g(st, "key_assumption_disagreement", "") or "",
        "counter": _g(st, "counter_thesis", "") or "",
    }

    # ── Agents ──
    agents_out = []
    for j in agent_judgments:
        agent_raw = _g(j, "agent_name", "agent")
        role = _AGENT_ZH.get(agent_raw, agent_raw.replace("_", " ").title()) if is_zh \
            else _AGENT_EN.get(agent_raw, agent_raw.replace("_", " ").title())
        topic = _AGENT_TOPIC_ZH.get(agent_raw, "") if is_zh else _AGENT_TOPIC_EN.get(agent_raw, "")

        # Thesis: first inference text (truncate to keep card tight)
        infs = _g(j, "inferences", []) or []
        thesis_text = _g(infs[0], "text", "") if infs else ""
        if not thesis_text:
            obs = _g(j, "observations", []) or []
            thesis_text = _g(obs[0], "text", "") if obs else ""

        # Pros: top 2 observations
        obs_all = _g(j, "observations", []) or []
        pros = [_g(o, "text", "")[:150] for o in obs_all[:2] if _g(o, "text", "")]
        # Cons: counterarguments
        counters = _g(j, "counterarguments", []) or []
        cons = [_g(c, "text", "")[:150] for c in counters[:2] if _g(c, "text", "")]

        # Refactor 5 (2026-05-04): consume the orchestrator-stamped flag
        # `AgentOutput.is_llm_fallback` instead of fishing for "[规则模板
        # 兜底]" prefixes in the rendered text. Falls back to string
        # detection only for legacy caches predating the refactor (so
        # replay still works on snapshots written before this change).
        _LEGACY_MOCK_TAGS = ("[规则模板兜底", "[rule-based fallback", "[规则模板", "rule-based fallback]")
        is_mock_fallback = bool(_g(j, "is_llm_fallback", False))
        if not is_mock_fallback:
            # Legacy cache fallback path
            is_mock_fallback = any(_t in (thesis_text or "") for _t in _LEGACY_MOCK_TAGS) or \
                (pros and any(any(_t in p for _t in _LEGACY_MOCK_TAGS) for p in pros))

        if is_mock_fallback:
            stance = "neutral"
            score = 0.0
        else:
            # Stance: per-agent heuristic from their first inference text. The
            # previous logic used the global (target / price - 1) gap for every
            # agent — making 7 agents all read "bear" when the stock looked
            # overvalued, even if the accounting agent said earnings quality
            # was sound. Falls back to global gap when text signal is weak.
            stance = _stance_from_text(thesis_text, is_zh)
            if stance == "neutral":
                implied = (target / price_last - 1) * 100 if (price_last > 0 and target is not None) else 0
                if abs(implied) >= 30:
                    # Severe gap: default to bias direction if per-agent text was
                    # unclear, so we don't show all-neutral for an extreme call.
                    stance = _derive_stance(implied)
            # Sector context agent is usually neutral
            if agent_raw == "sector_context_agent":
                stance = "neutral"

            score = _derive_score(j)

        # BUG-Y29 (2026-05-06): expose narrative_supplement (deep-mode
        # ~1500-2800 chars of free-form analysis the LLM writes BEYOND the
        # structured Judgment) so readers can drill into the agent's full
        # reasoning. Was being silently dropped before — orchestrator now
        # attaches it as runtime attribute to each judgment.
        narrative_text = _g(j, "narrative_supplement", "") or ""
        agents_out.append({
            "role": role,
            "name": topic or (role + " 结论" if is_zh else role + " View"),
            "stance": stance,
            "score": score,
            "thesis": thesis_text,
            "pros": pros,
            "cons": cons,
            "narrative": narrative_text,
            "fallback": is_mock_fallback,
        })

    # ── Critics ──
    critics_out = []
    for cr in critic_results:
        ct = _g(cr, "critic_type", "unknown")
        ct_key = ct[:-7] if ct.endswith("_critic") else ct
        name = _CRITIC_ZH.get(ct_key, ct_key.replace("_", " ").title() + "批评员") if is_zh \
            else _CRITIC_EN.get(ct_key, ct_key.replace("_", " ").title() + " Critic")
        issues = len(_g(cr, "issues", []) or [])
        critics_out.append({"name": name, "issues": issues})

    # ── Sensitivity matrix ──
    sens_dict = None
    if sensitivity_table and isinstance(sensitivity_table, dict):
        var1 = sensitivity_table.get("var1_values") or []
        var2 = sensitivity_table.get("var2_values") or []
        matrix = sensitivity_table.get("matrix") or []
        # var1/var2 are usually decimals like 0.09 for 9% — convert to percents for display
        rows = [round(v * 100 if v < 1 else v, 1) for v in var1]
        cols = [round(v * 100 if v < 1 else v, 1) for v in var2]
        sens_dict = {
            "paragraphs": [
                ("下表为每个单因子 ±1σ 冲击对基准每股价值的弹性。" if is_zh
                 else "Elasticity of base per-share value under ±1σ single-factor shocks.")
            ],
            "rows": rows,
            "cols": cols,
            # Infeasible cells (wacc − tg gap too small) arrive as None —
            # keep None so the JSX layer renders "n/m" instead of a number.
            "matrix": [[None if v is None else round(v, 2) for v in row] for row in matrix],
            "baseValue": _f(_g(dcf_output, "per_share_value")) or _f(sc.get("base_value")),
            # Color semantics follow market convention (US: green=up/red=down,
            # CN: red=up/green=down). Heat-map cell coloring is wired to
            # --up/--down which swap per market — so this legend text must
            # swap too.
            "footnote": (
                "红色表示高于基准（更乐观），绿色反之。" if is_zh
                else "Green = above base (more upside), red = below."
            ),
        }

    # ── Driver sensitivity (bar list) ──
    # The sensitivity engine emits assumption keys in several naming
    # conventions across versions (legacy short forms + current full
    # snake_case). Map both.
    driver_sens = []
    _DRIVER_ZH = {
        "wacc": "加权资本成本 WACC",
        "operating_margin": "营业利润率",
        "capex_rate": "资本开支率",
        "capex_to_revenue": "资本开支 / 营收",
        "terminal_growth": "永续增长率",
        "terminal_growth_rate": "永续增长率",
        "revenue_growth": "收入增速",
        "tax_rate": "有效税率",
        "effective_tax_rate": "有效税率",
        "sbc_to_revenue": "股权激励 / 营收",
        "buyback_yield_annual": "回购收益率",
        "depreciation_to_revenue": "折旧 / 营收",
        "dep_to_revenue": "折旧 / 营收",
    }
    _DRIVER_EN = {
        "wacc": "WACC",
        "operating_margin": "Operating Margin",
        "capex_rate": "Capex Rate",
        "capex_to_revenue": "Capex / Revenue",
        "terminal_growth": "Terminal Growth",
        "terminal_growth_rate": "Terminal Growth",
        "revenue_growth": "Revenue Growth",
        "tax_rate": "Effective Tax Rate",
        "effective_tax_rate": "Effective Tax Rate",
        "sbc_to_revenue": "SBC / Revenue",
        "buyback_yield_annual": "Buyback Yield",
        "depreciation_to_revenue": "D&A / Revenue",
        "dep_to_revenue": "D&A / Revenue",
    }
    for r in (sensitivity_rankings or []):
        if not isinstance(r, dict):
            continue
        key = r.get("assumption", "")
        label = _DRIVER_ZH.get(key, key) if is_zh else _DRIVER_EN.get(key, key)
        delta = _f(r.get("signed_impact_pct")) * 100  # fraction → pct
        # Skip drivers with no measurable impact — they clutter the chart
        # with empty rows ($0 shock, 0.0 pct).
        if abs(delta) < 0.05:
            continue
        shock = round(_f(r.get("shocked_per_share")))
        driver_sens.append({"k": label, "delta": round(delta, 1), "shock": shock})

    # ── DCF projections → array form expected by report.jsx ──
    # Capex sign convention varies by source (yfinance / akshare emit a
    # negative cash-outflow value; some connectors give positive magnitude).
    # The FCFF column in the DCF table is labeled "Capex" and, by DCF
    # convention, is shown as a positive magnitude (the formula then
    # subtracts it). Normalize to abs() so AAPL and 301358 display the
    # same way.
    dcf_rows = []
    div = display_ctx.get("scale", 1e9)
    for p in dcf_projections:
        getter = (lambda k: p.get(k)) if isinstance(p, dict) else (lambda k: _g(p, k))
        y = getter("year")
        rev = _f(getter("revenue")) / div
        ebit = _f(getter("operating_income")) / div
        da = _f(getter("depreciation")) / div
        nopat = _f(getter("nopat")) / div
        capex = abs(_f(getter("capex"))) / div
        dnwc = _f(getter("change_in_nwc")) / div
        fcff = _f(getter("fcff")) / div
        pv = _f(getter("pv_fcff")) / div
        dcf_rows.append([y, rev, ebit, da, nopat, capex, dnwc, fcff, pv])

    # Long-form unit suffix for axis labels and footnotes — keep zh in
    # full Chinese, fall back to "{currency} billions" elsewhere.
    if is_zh:
        unit_suffix = f"{display_ctx.get('unit', '亿')}元人民币"
    else:
        # AUDIT (2026-07): display_ctx["unit"] is the abbreviation ("B"),
        # not a word — the old f"...{unit}s".lower() rendered "usd bs" in
        # every US report (NVDA demo, DCF footnote + revenue chart title).
        # Map abbreviation → word; unknown units pass through unchanged.
        _UNIT_WORDS = {"B": "billions", "T": "trillions", "M": "millions"}
        _unit_raw = str(display_ctx.get("unit", "B"))
        unit_suffix = f"{currency_code} {_UNIT_WORDS.get(_unit_raw.upper(), _unit_raw)}"

    dcf_summary = {}
    if dcf_output:
        div = display_ctx.get("scale", 1e9)
        eq_raw = _f(_g(dcf_output, "equity_value"))
        ps_raw = _f(_g(dcf_output, "per_share_value"))
        # Back-derive the denominator actually used to compute per_share_value
        # (equity / per_share). dcf_output.future_shares is net of projected
        # buybacks and would make the bridge math appear off by ~20%.
        implied_shares = (eq_raw / ps_raw) if ps_raw else _f(_g(dcf_output, "future_shares"))
        dcf_summary = {
            "pvCashflows": round(_f(_g(dcf_output, "pv_fcff_sum")) / div, 1),
            "pvTerminal": round(_f(_g(dcf_output, "pv_terminal_value")) / div, 1),
            "ev": round(_f(_g(dcf_output, "enterprise_value")) / div, 1),
            "netDebt": round((_f(_g(dcf_output, "enterprise_value")) - eq_raw) / div, 1),
            "equity": round(eq_raw / div, 1),
            "shares": round(implied_shares / div, 2),
            "perShare": round(ps_raw, 2),
        }
    else:
        # Fallback: synthesize from scenarios if dcf_output is missing
        base_val = _f(sc.get("base_value"))
        dcf_summary = {
            "pvCashflows": 0, "pvTerminal": 0, "ev": 0, "netDebt": 0,
            "equity": 0, "shares": 0, "perShare": base_val,
        }

    # WACC/g must come from the actual DCF input, not the visual midpoint of
    # the sensitivity table. The table range is intentionally flexible; using
    # its midpoint can mislabel the model assumptions in generated reports.
    dcf_assumptions = sc.get("dcf_assumptions") if isinstance(sc, dict) else {}
    wacc_base = 9.0
    g_base = 2.5
    if isinstance(dcf_assumptions, dict):
        wacc_raw = dcf_assumptions.get("wacc")
        g_raw = dcf_assumptions.get("terminal_growth_rate")
        if isinstance(wacc_raw, (int, float)):
            wacc_base = round(wacc_raw * 100 if wacc_raw < 1 else wacc_raw, 1)
        if isinstance(g_raw, (int, float)):
            g_base = round(g_raw * 100 if g_raw < 1 else g_raw, 1)
    elif sens_dict and sens_dict["rows"] and sens_dict["cols"]:
        wacc_base = sens_dict["rows"][len(sens_dict["rows"]) // 2]
        g_base = sens_dict["cols"][len(sens_dict["cols"]) // 2]

    dcf_block = {
        "title": "十年现金流桥接与企业价值拆解" if is_zh else "10-year FCFF bridge & EV decomposition",
        "subtitle": "DCF 引擎 · FCFF 两阶段模型" if is_zh else "DCF Engine · Two-stage FCFF",
        "unit": unit_suffix,
        "waccBase": wacc_base,
        "gBase": g_base,
        "sharesBase": dcf_summary.get("shares"),
        "paragraphHtml": (
            f"FCFF 两阶段模型：Y1–Y{len(dcf_rows) or 10} 明细期 + 永续期（g = {g_base}%, WACC = {wacc_base}%）。"
            if is_zh
            else f"Two-stage FCFF: Y1–Y{len(dcf_rows) or 10} explicit + terminal (g = {g_base}%, WACC = {wacc_base}%)."
        ),
        "projection": dcf_rows,
        "summary": dcf_summary,
    }
    # AUDIT-A10: disclose the BUG-Y23 30× cumulative-growth cap. Without
    # this, a hyper-growth name's assumption table showed revenue growth
    # suddenly dropping to ~terminal (e.g. 3.5% at Y6) with zero
    # explanation — readers couldn't tell engine intervention from a model
    # view. Flag written by `_build_dcf_input` into meta_facts.
    if isinstance(meta_facts, dict) and meta_facts.get("__growth_path_capped"):
        _cap_yr = meta_facts.get("__growth_path_capped_year")
        _yr_txt = f"Y{_cap_yr}" if _cap_yr else ""
        cap_note = (
            f"⚠ 增长路径自 {_yr_txt} 起触及累计 30× 营收上限，已保守收敛至永续增速"
            f"（引擎干预，非模型观点）。"
            if is_zh else
            f"⚠ Growth path capped from {_yr_txt} at the cumulative 30× revenue "
            f"bound and converged to terminal growth (engine intervention, not a model view)."
        )
        dcf_block["paragraphHtml"] += (" " + cap_note)
        dcf_block["growthPathCapped"] = True
        dcf_block["growthPathCappedYear"] = _cap_yr

    # ── Financials section ──
    # Revenue history: meta_facts["__historical_revenue"] is {year_int: revenue_raw}
    # for up to 5 years (populated by both SEC fetcher and akshare connector).
    # Fall back to the single current-year bar only if the series is missing.
    rev_history = []
    div = display_ctx.get("scale", 1e9)
    hist_raw = (meta_facts or {}).get("__historical_revenue") if isinstance(meta_facts, dict) else None
    if isinstance(hist_raw, dict) and hist_raw:
        for year in sorted(hist_raw.keys()):
            v = _f(hist_raw[year])
            if v <= 0:
                continue
            rev_history.append({"y": str(year), "v": round(v / div, 1)})
    if not rev_history and revenue:
        current_year = period_raw[2:] if (period_raw and period_raw.startswith("FY")) else ""
        rev_history = [{"y": current_year or "—", "v": round(revenue / div, 1)}]

    financial_kpis = []

    def _kpi(label_zh: str, label_en: str, val_str: str, tone: str | None = None, total: bool = False):
        financial_kpis.append({"label": label_zh if is_zh else label_en, "value": val_str,
                               **({"tone": tone} if tone else {}),
                               **({"total": total} if total else {})})

    if revenue:
        _kpi("营收", "Revenue", _unit(revenue))
    if net_income:
        _kpi("净利润", "Net Income", _unit(net_income))
    if ebitda:
        _kpi("EBITDA", "EBITDA", _unit(ebitda))
    cfo = _f(meta_facts.get("cfo")) or _f(meta_facts.get("cash_from_operations"))
    if cfo:
        _kpi("经营现金流", "CFO", _unit(cfo), tone="down" if cfo < 0 else "up")
    if fcf_simple:
        _kpi("自由现金流", "FCF", _unit(fcf_simple), tone="down" if fcf_simple < 0 else "up")

    gm = _f(computed_metrics.get("gross_margin")) * 100
    if gm:
        _kpi("毛利率", "Gross Margin", f"{gm:.1f}%", total=True)
    if op_margin:
        _kpi("营业利润率", "Op. Margin", f"{op_margin:.1f}%")
    roic = _f(computed_metrics.get("roic")) * 100
    if roic:
        _kpi("ROIC", "ROIC", f"{roic:.1f}%")
    roe = _f(computed_metrics.get("roe")) * 100
    if roe:
        _kpi("ROE", "ROE", f"{roe:.1f}%")
    # Refactor 3: orchestrator omits non-meaningful ratios — just render
    # if present, no n/m guard needed.
    if pe_ttm:
        _kpi("市盈率 (静态)", "P/E (static)", f"{pe_ttm:.1f}×")
    if ev_ebitda:
        _kpi("EV / EBITDA", "EV / EBITDA", f"{ev_ebitda:.1f}×")

    financials_block = {
        "title": "利润表 vs 现金流表" if is_zh else "Income statement vs cash flow",
        "subtitle": "会计分析师 · 业务分析师" if is_zh else "Accounting Analyst · Business Analyst",
        "paragraphs": [],
        "revTitle": ("年度营收 · " + unit_suffix) if is_zh else f"Annual revenue · {unit_suffix}",
        "revHistory": rev_history,
        "revHighlightYear": rev_history[-1]["y"] if rev_history else "",
        "revFootnote": "",
        "kpisTitle": f"{period_raw} 关键指标" if is_zh else f"{period_raw} Key Metrics",
        "kpis": financial_kpis,
        "calloutHtml": (
            f"<strong>会计分析师提示 · </strong>CFO / NI = <span class='mono'>{cfo_ni:.2f}</span>。"
            if is_zh and cfo_ni
            else ""
        ),
    }

    # ── Conclusion ──
    conclusion_paragraphs = []
    if st:
        if _g(st, "core_thesis"):
            conclusion_paragraphs.append(_g(st, "core_thesis"))
        if _g(st, "counter_thesis"):
            conclusion_paragraphs.append(
                ("反向论点：" if is_zh else "Counter-thesis: ") + _g(st, "counter_thesis")
            )
        if _g(st, "what_would_change_my_mind"):
            conclusion_paragraphs.append(
                ("何种证据会改变判断：" if is_zh else "What would change my mind: ")
                + _g(st, "what_would_change_my_mind")
            )

    conclusion_block = {
        "title": ("等待关键催化剂验证" if is_zh else "Awaiting catalyst verification"),
        "subtitle": "首席分析师 · 终稿" if is_zh else "Chief Analyst · Final",
        "paragraphs": conclusion_paragraphs,
        "catalystsTitle": "未来 6 个月催化剂" if is_zh else "Catalysts · Next 6 months",
    }

    # ── Catalysts ──
    # Keep only events dated today or later. The pipeline often includes
    # historical earnings prints in its timeline for context, but the
    # report section is titled "upcoming catalysts" — past events make
    # it read like we can't tell the difference.
    catalysts_out = []
    if catalyst_timeline:
        from datetime import date
        today = date.today()
        events = _g(catalyst_timeline, "events", []) or []
        for ev in events:
            title = _g(ev, "title", "") or ""
            # A-share issuers don't file with the SEC — drop legacy timeline
            # entries that called out "SEC 10-Q / 10-K Due" regardless of market.
            if is_zh and title.startswith("SEC "):
                continue
            date_obj = _g(ev, "expected_date")
            if date_obj and hasattr(date_obj, "year"):
                ev_date = date_obj.date() if hasattr(date_obj, "date") and not isinstance(date_obj, date) else date_obj
                if hasattr(ev_date, "__class__") and ev_date.__class__.__name__ == "date" and ev_date < today:
                    continue
            date_str = date_obj.strftime("%Y-%m-%d") if date_obj and hasattr(date_obj, "strftime") else str(date_obj or "")
            impact_mag = _g(ev, "impact_magnitude", "medium")
            impact_display = {"high": "高", "medium": "中", "low": "低"}.get(impact_mag, impact_mag) if is_zh else impact_mag.title()
            note = _g(ev, "description", "") or ""
            # Strip "EPS: nan (surprise: +nan%)" noise from future earnings
            # rows. Use word-boundary patterns so legit words like "maintains"
            # or "dominant" aren't affected.
            if re.search(r"\bnan\b|\+?nan%", note, re.IGNORECASE):
                note = ""
            catalysts_out.append({
                "date": date_str,
                "title": title,
                "impact": impact_display,
                "note": note,
            })
            if len(catalysts_out) >= 8:
                break

    # ── Macro section ──
    # Filled from the FRED-sourced MacroSnapshot when present (US path only —
    # FRED is US-only). When macro_snapshot is None the template auto-hides.
    macro_block = _build_macro_block(macro_snapshot, is_zh)

    # ── Rail ──
    def _short_question(q: Any) -> str:
        raw = q.get("question", "") if isinstance(q, dict) else str(q or "")
        raw = raw.strip()
        if len(raw) <= 80:
            return raw
        # Cut at nearest word boundary ≤80 chars with ellipsis.
        cut = raw.rfind(" ", 0, 80)
        if cut < 40:
            cut = 80
        return raw[:cut].rstrip(" ,;，；") + "…"

    # Fill rail.marketKvs with key capital-structure + valuation anchors
    # available from meta_facts. Skip any field we don't have — better to
    # omit than show "—".
    mkvs = []
    cash = _f(meta_facts.get("cash_and_equivalents"))
    total_debt = _f(meta_facts.get("total_debt"))
    net_debt = _f(meta_facts.get("net_debt"))
    if mkt_cap:
        mkvs.append({"k": "市值" if is_zh else "Market Cap", "v": _unit(mkt_cap)})
    if revenue:
        mkvs.append({"k": "营收 (TTM)" if is_zh else "Revenue (TTM)", "v": _unit(revenue)})
    if cash:
        mkvs.append({"k": "现金及等价物" if is_zh else "Cash & equiv.", "v": _unit(cash)})
    if total_debt:
        mkvs.append({"k": "有息负债" if is_zh else "Total Debt", "v": _unit(total_debt)})
    if net_debt:
        tone = "down" if net_debt > 0 else "up"  # net debt > 0 = leverage burden
        mkvs.append({"k": "净负债" if is_zh else "Net Debt", "v": _unit(net_debt), "tone": tone})
    if ev_ebitda:
        # Refactor 3: orchestrator gates this metric upstream.
        mkvs.append({"k": "EV / EBITDA", "v": f"{ev_ebitda:.1f}×"})

    # ── 52w range / beta / div yield from live snapshot (US path only) ──
    quote_meta = _fetch_quote_meta(eid, market_tag)
    high52 = quote_meta.get("high52w")
    low52 = quote_meta.get("low52w")
    if high52 and low52 and high52 > low52:
        # Position-within-range tone: cold (>80% of range = down tone hint),
        # warm (<20% of range = up tone hint), else neutral.
        rng = high52 - low52
        pos = (price_last - low52) / rng if price_last and rng > 0 else 0.5
        tone = "down" if pos >= 0.80 else ("up" if pos <= 0.20 else "")
        if is_zh:
            label = "52 周区间"
            val = f"¥{low52:.2f}–¥{high52:.2f}"
        else:
            label = "52w Range"
            val = f"${low52:.2f}–${high52:.2f}"
        mkvs.append({"k": label, "v": val, "tone": tone} if tone else {"k": label, "v": val})
    beta = quote_meta.get("beta")
    if beta is not None and beta > 0:
        mkvs.append({"k": "Beta", "v": f"{beta:.2f}"})
    div_yield = quote_meta.get("div_yield")
    if div_yield is not None and div_yield >= 0.05:
        # yfinance's `dividendYield` is already in percent form in recent
        # versions (e.g. 0.38 = 0.38% for AAPL). Skip tokens below 0.05%
        # which are effectively zero-yield stocks.
        mkvs.append({
            "k": "股息率" if is_zh else "Div Yield",
            "v": f"{div_yield:.2f}%",
        })
    avg_vol = quote_meta.get("avg_volume")
    if avg_vol and avg_vol > 0:
        if is_zh:
            # Market-native units: ≥1亿 → "X.X亿", ≥1万 → "X.X万"
            if avg_vol >= 100_000_000:
                vol_s = f"{avg_vol/100_000_000:.1f}亿"
            elif avg_vol >= 10_000:
                vol_s = f"{avg_vol/10_000:.1f}万"
            else:
                vol_s = f"{int(avg_vol):,}"
        else:
            if avg_vol >= 1_000_000_000:
                vol_s = f"{avg_vol/1_000_000_000:.1f}B"
            elif avg_vol >= 1_000_000:
                vol_s = f"{avg_vol/1_000_000:.1f}M"
            else:
                vol_s = f"{int(avg_vol):,}"
        mkvs.append({
            "k": "日均成交量" if is_zh else "Avg Volume",
            "v": vol_s,
        })

    rail_block = {
        "priceHistory": _fetch_sparkline(eid, market_tag, price_last),
        "marketKvs": mkvs,
        "openQuestions": [_short_question(q) for q in (open_questions or [])[:3]],
        "biasStatus": _g(decision, "bias_check_status", "—"),
    }

    # ── Stale banner ──
    # AUDIT (2026-07): the gap used to be (cur_year - fy_year) * 12 — whole-
    # year granularity that ignored the current month AND the real fiscal
    # period end, so a 15.6-month-old FY2024 report claimed "距今约 24 个月",
    # and non-calendar fiscal years (NVDA ends late Jan) got a hardcoded
    # 12-31 period end in the copy. Prefer a real period-end date if the
    # pipeline surfaced one in meta_facts; otherwise approximate with the
    # calendar FY end (fy_year-12) at month granularity.
    stale_banner_text = None
    if period_raw and period_raw.startswith("FY"):
        try:
            fy_year = int(period_raw[2:])
            _now = datetime.now()
            pe_year, pe_month = fy_year, 12
            period_end_label = f"{fy_year}-12-31"
            _pe_raw = None
            if isinstance(meta_facts, dict):
                # No connector writes a canonical period-end key yet; probe
                # the plausible names tolerantly so this picks it up as soon
                # as upstream starts propagating one.
                for _k in ("__fiscal_period_end", "fiscal_period_end",
                           "__period_end", "period_end", "report_date"):
                    if meta_facts.get(_k):
                        _pe_raw = str(meta_facts[_k])
                        break
            if _pe_raw:
                _m = re.match(r"(\d{4})-(\d{2})-(\d{2})", _pe_raw)
                if _m:
                    pe_year, pe_month = int(_m.group(1)), int(_m.group(2))
                    period_end_label = _m.group(0)
            gap_months = (_now.year * 12 + _now.month) - (pe_year * 12 + pe_month)
            if gap_months >= 15:
                stale_banner_text = (
                    f"最新可得财报为 {period_raw}（截至 {period_end_label}），距今约 {gap_months} 个月；"
                    f"本分析的价格动态基于实时行情，但财务基数落后约 {gap_months // 12} 个财年。"
                ) if is_zh else (
                    f"Latest available filing: {period_raw} (period end {period_end_label}), "
                    f"~{gap_months} months stale; real-time quote valid, financials lag."
                )
        except ValueError:
            pass

    # ── Assemble ──
    report = {
        # `company` is the canonical company name (cleaned of exchange risk
        # warnings like ST / *ST). Templates that want to call out the risk
        # warning consume `riskWarning` separately as a badge.
        "company": display_name,
        "companyFullName": company_name,
        "riskWarning": risk_warning_prefix or None,
        "code": str(eid or "").replace(".SS", "").replace(".SZ", ""),
        "exchange": exchange,
        "sector": sector_raw,
        "reportDate": now,
        "runId": run_id or _g(decision, "run_id", ""),
        "period": period_display,
        "tickerMark": ticker_mark,
        "confidence": confidence_display,
        "bias": _g(decision, "bias_check_status", "—"),
        "pipelineDuration": pipeline_duration or "—",
        "model": model_name or "—",
        "staleBanner": stale_banner_text,

        "price": {
            "last": round(price_last, 2),
            "change": round(price_change, 2),
            "changePct": round(price_change_pct, 2),
            "currency": curr_sym,
            "asOf": now,
            "market": market_tag,
        },
        # Single source of truth for the front-end formatting layer. Callers
        # that need to render a number consume this instead of branching on
        # `price.currency` / `is_zh`.
        "display": {
            "currency": display_ctx.get("currency", currency_code),
            "symbol": display_ctx["symbol"],
            "scale": display_ctx.get("scale", 1e9),
            "unit": display_ctx.get("unit", "B"),
            "bigScale": display_ctx.get("big_scale", 1e12),
            "bigUnit": display_ctx.get("big_unit", "T"),
        },

        "rating": {
            "word": rating_word,
            "tone": rating_tone,
            # When DCF is not meaningful, expose `null` for target instead of
            # a negative number that the front-end would render as a price.
            # `weighted` flips to a qualitative label so any UI showing
            # "概率加权 ¥-0.83" instead reads "DCF n/m".
            "target": (round(target, 2) if _dcf_meaningful else None),
            # AUDIT-C2: downgraded reports keep their rating but the target
            # label becomes an explicit caveat (rendered by Verdict in
            # report.jsx via `rating.weighted`).
            "weighted": (
                ("评级已降级 · 存在未解决分歧" if is_zh else "Downgraded · unresolved conflicts")
                if _rating_downgraded else
                ("概率加权" if is_zh else "Probability-weighted")
                if _dcf_meaningful else
                ("DCF 不适用 · 见同业/资产框架" if is_zh else "DCF n/m · see peer/asset framing")
            ),
            "downgraded": _rating_downgraded,
            "timeHorizon": "12 个月" if is_zh else "12 months",
            "riskLevel": "中高" if is_zh else "Medium-High",
            "dcfMeaningful": _dcf_meaningful,
            "bookValuePerShare": round(book_per_share, 2) if book_per_share > 0 else None,
        },

        "headline": headline,
        "lede": lede,
        "executiveParagraphs": exec_paragraphs,
        "coreCalloutHtml": core_callout,

        "quick": quick,
        "scenarios": scen_cells,
        # Short pull-quote under the three scenarios: one punchy clause from
        # the variant view. Falls back to None if no variant text.
        # Pullquote: one punchy clause. Use tight hard_max (120) so a long
        # single-sentence paragraph still gets chopped at the first comma/
        # semicolon instead of returning the whole thing.
        "valuationPullquote": (
            {
                "text": _first_clause(thesis["variant"], soft_max=100, hard_max=120),
                "attrib": ("变体分析师 · 估值缺口分解" if is_zh else "Variant Analyst · Consensus gap decomposition"),
            } if thesis.get("variant") else None
        ),

        "macro": macro_block,
        "financials": financials_block,
        "dcf": dcf_block,

        "agents": agents_out,
        "critics": critics_out,
        "thesis": thesis,

        "sensitivity": sens_dict,
        "driverSensitivity": driver_sens,

        "conclusion": conclusion_block,
        "catalysts": catalysts_out,

        "rail": rail_block,
    }
    return report


# ─────────────────────────────────────────────────────────────────
# Renderer: inject REPORT dict into the shared template
# ─────────────────────────────────────────────────────────────────

# Matches the <script type="text/babel" src="report.jsx"></script> line
# we replace during inlining. Kept tolerant of whitespace changes.
_JSX_SCRIPT_RE = re.compile(
    r'<script\s+type="text/babel"\s+src="report\.jsx"></script>', re.IGNORECASE,
)


# BUG-Y39 (2026-05-06): Python's json.dumps by default emits `Infinity`
# / `-Infinity` / `NaN` for non-finite floats, which are NOT valid JSON
# and break the browser's JSON.parse → entire React tree fails to
# render. We already fixed one source (DCF implied_exit_multiple → None
# in Y7) but defense-in-depth: walk the dict and replace any float NaN
# / inf with None before serialization. allow_nan=False as belt+braces
# would raise on hit which would also be visible — keep defense-only.
# (Hoisted to module level 2026-07 so the unit suite can exercise it
# directly — AUDIT-E3.)
def _sanitize_floats(o):
    import math as _math
    if isinstance(o, float):
        return None if (_math.isinf(o) or _math.isnan(o)) else o
    if isinstance(o, dict):
        return {k: _sanitize_floats(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_sanitize_floats(v) for v in o]
    if isinstance(o, tuple):
        return tuple(_sanitize_floats(v) for v in o)
    return o


def render_report_html(report_dict: dict,
                       *, template_html: Path | None = None,
                       template_jsx: Path | None = None) -> str:
    """Inject REPORT dict + inline the JSX into the shared HTML template.

    Returns a single self-contained HTML string that opens offline.
    """
    html_path = template_html or _TEMPLATE_HTML
    jsx_path = template_jsx or _TEMPLATE_JSX

    html_tpl = html_path.read_text(encoding="utf-8")
    jsx_body = jsx_path.read_text(encoding="utf-8")

    # BUG-31 (2026-04-23): replace the hardcoded `<title>Aegis 投研 — 湖南裕能
    # (301358.SZ)</title>` with the actual entity. The template still ships
    # the seed-data title because every prior demo only opened the file with
    # JS-driven content swap; the static <title> never got templated.
    company = report_dict.get("company") or report_dict.get("code") or ""
    code = report_dict.get("code") or ""
    exchange = report_dict.get("exchange") or ""
    if exchange and code and not exchange.endswith(("Z", "S")):
        exchange = ""  # only show known suffixes
    suffix = f" ({code}.{exchange})" if exchange and code else (f" ({code})" if code else "")
    new_title = f"<title>Aegis 投研 — {company}{suffix}</title>"
    html_tpl = re.sub(r"<title>[^<]*</title>", new_title, html_tpl, count=1)

    # Serialize REPORT — ensure_ascii=False to keep Chinese, default=str for
    # dates. Non-finite floats are nulled by _sanitize_floats (BUG-Y39).
    _safe_dict = _sanitize_floats(report_dict)
    report_json = json.dumps(_safe_dict, ensure_ascii=False, default=str, allow_nan=False)
    # AUDIT (2026-07): the JSON is inlined into a <script> block, where the
    # HTML parser terminates on the first literal "</script>" — any LLM text
    # containing that substring (or a "<!--" that swallows a later
    # "<script>") white-screens the whole report. Escape "/" after "<" as
    # the JSON-equivalent "\/", and the "!" of "<!--" as a JSON unicode
    # escape (u+0021); both round-trip to the original characters through
    # JSON.parse, so legit data is unaffected.
    report_json = report_json.replace("</", "<\\/").replace("<!--", "<\\u0021--")

    inline_block = (
        f'<script>window.REPORT = {report_json};</script>\n'
        f'<script type="text/babel" data-presets="react">\n{jsx_body}\n</script>'
    )

    # Use a lambda as the replacement so re.sub does NOT interpret backslash
    # escapes like \n, \t in the replacement string — those sequences appear
    # in the JSON-escaped string values and must round-trip untouched.
    match = _JSX_SCRIPT_RE.search(html_tpl)
    if match:
        return html_tpl[:match.start()] + inline_block + html_tpl[match.end():]

    # Fallback: inject before </body>
    return html_tpl.replace("</body>", inline_block + "\n</body>", 1)


# ─────────────────────────────────────────────────────────────────
# Public entry — drop-in replacement for legacy generate_html_report
# ─────────────────────────────────────────────────────────────────

def generate_html_report(
    decision: Any,
    computed_metrics: dict[str, float],
    market_data: dict[str, float],
    agent_judgments: list[Any],
    critic_results: list[Any],
    meta_facts: dict[str, Any] | None = None,
    dcf_projections: list[dict] | None = None,
    sensitivity_table: dict | None = None,
    sensitivity_rankings: list[dict] | None = None,
    segment_projections: dict[str, list[dict]] | None = None,
    entity_name: str | None = None,
    segment_detail: dict[str, Any] | None = None,
    consensus_estimates: list[Any] | None = None,
    earnings_history: list[Any] | None = None,
    peer_fundamentals: list[Any] | None = None,
    price_target_consensus: dict[str, Any] | None = None,
    edited_report: Any | None = None,
    research_directive: Any | None = None,
    synthesized_thesis: Any | None = None,
    earnings_call_insights: Any | None = None,
    historical_valuation: dict | None = None,
    catalyst_timeline: Any | None = None,
    insider_summary: Any | None = None,
    news_sentiment_insights: Any | None = None,
    scenarios: dict | None = None,
    # ── v2 extensions (optional; callers can pass to fill richer fields) ──
    period: str | None = None,
    dcf_output: Any = None,
    pipeline_duration: str | None = None,
    model_name: str | None = None,
    macro_snapshot: Any = None,
    entity_name_clean: str | None = None,
    risk_warning_prefix: str | None = None,
) -> str:
    """Drop-in signature-compatible replacement for the legacy HTML renderer.

    Unused args (insider_summary, news_sentiment_insights, peer_fundamentals,
    earnings_call_insights, consensus_estimates, earnings_history) are
    accepted but ignored — the new design doesn't render them. They'll be
    added as optional sections in a follow-up if needed.
    """
    # Extract entity_id
    entity_id = str(getattr(decision, "entity_id", "") or "")

    # Extract open questions from decision
    open_q = getattr(decision, "open_questions", None) or []

    # Resolve period: explicit kwarg > decision attr > scenarios dict
    if period is None:
        period = getattr(decision, "period", None) or (scenarios or {}).get("period")

    # Resolve dcf_output: explicit kwarg > decision attr
    if dcf_output is None:
        dcf_output = getattr(decision, "dcf_output", None)

    report_dict = build_report_dict(
        decision=decision,
        computed_metrics=computed_metrics,
        market_data=market_data,
        agent_judgments=agent_judgments,
        critic_results=critic_results,
        meta_facts=meta_facts,
        dcf_projections=dcf_projections,
        dcf_output=dcf_output,
        sensitivity_table=sensitivity_table,
        sensitivity_rankings=sensitivity_rankings,
        entity_name=entity_name,
        entity_id=entity_id,
        entity_name_clean=entity_name_clean,
        risk_warning_prefix=risk_warning_prefix,
        segment_detail=segment_detail,
        edited_report=edited_report,
        synthesized_thesis=synthesized_thesis,
        catalyst_timeline=catalyst_timeline,
        scenarios=scenarios,
        run_id=getattr(decision, "run_id", None),
        period=period,
        open_questions=open_q,
        pipeline_duration=pipeline_duration,
        model_name=model_name,
        macro_snapshot=macro_snapshot,
    )

    return render_report_html(report_dict)
