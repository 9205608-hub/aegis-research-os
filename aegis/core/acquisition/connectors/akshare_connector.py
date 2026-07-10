"""AkShare A-share data connector — primary data source for China stocks.

Why akshare over yfinance for A-shares:
- **Freshness**: akshare hits eastmoney directly, which updates the same day
  a company files. yfinance typically lags 1-2 weeks behind Chinese filings.
- **Coverage**: full 3-statement history (利润表/资产负债表/现金流量表), ~7 years.
- **Precision**: returns raw CAS-line-item numbers, not re-aggregated.
- **Metadata**: real listing date, total vs float shares, industry classification.

Proxy handling: Chinese data source hosts (eastmoney.com, sina.com.cn) must
bypass outbound proxies that route through non-CN egress. This connector
temporarily unsets HTTP(S)_PROXY before akshare calls and restores them after.
"""

from __future__ import annotations

import logging
import os
import ssl
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


_CN_BYPASS_HOSTS = (
    "eastmoney.com,*.eastmoney.com,push2.eastmoney.com,"
    "82.push2.eastmoney.com,push2delay.eastmoney.com,"
    "emweb.securities.eastmoney.com,datacenter-web.eastmoney.com,"
    "sina.com.cn,*.sina.com.cn,finance.sina.com.cn,"
    "vip.stock.finance.sina.com.cn,money.finance.sina.com.cn"
)


@contextmanager
def _no_proxy():
    """Temporarily disable proxy env vars and set NO_PROXY for CN hosts.

    Chinese endpoints (eastmoney, sina) must be reached directly; routing
    them through an international proxy (Clash / Shadowsocks) either fails
    or returns cached/stale data.

    We both:
    1. Pop HTTP_PROXY / HTTPS_PROXY / ALL_PROXY (so `requests` sees no proxy)
    2. Set NO_PROXY positively (defensive, in case requests picks up a
       sessions-level proxy from urllib3 connection pool)
    """
    saved: dict[str, str] = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                "ALL_PROXY", "all_proxy"):
        if key in os.environ:
            saved[key] = os.environ.pop(key)
    saved_no_proxy = os.environ.get("NO_PROXY"), os.environ.get("no_proxy")
    os.environ["NO_PROXY"] = _CN_BYPASS_HOSTS
    os.environ["no_proxy"] = _CN_BYPASS_HOSTS
    # Also bypass SSL verification (China CA chain quirks with some middleboxes)
    saved_ssl = ssl._create_default_https_context
    ssl._create_default_https_context = ssl._create_unverified_context
    try:
        yield
    finally:
        for key, val in saved.items():
            os.environ[key] = val
        # Restore NO_PROXY
        if saved_no_proxy[0] is not None:
            os.environ["NO_PROXY"] = saved_no_proxy[0]
        else:
            os.environ.pop("NO_PROXY", None)
        if saved_no_proxy[1] is not None:
            os.environ["no_proxy"] = saved_no_proxy[1]
        else:
            os.environ.pop("no_proxy", None)
        ssl._create_default_https_context = saved_ssl


@dataclass
class AkShareFinancials:
    """Parsed A-share financial data from akshare/eastmoney."""
    stock_code: str
    company_name: str
    fiscal_year: int           # Latest annual report fiscal year
    report_date: str           # "2024-12-31"
    facts: dict[str, float] = field(default_factory=dict)         # CAS concept → value
    historical: dict[int, dict[str, float]] = field(default_factory=dict)  # year → facts
    market_data: dict[str, float] = field(default_factory=dict)   # price, cap, shares
    company_info: dict[str, Any] = field(default_factory=dict)    # name, industry, listing_date


# Field mapping: eastmoney column name → CAS concept name
# These are the canonical Chinese accounting standards concept IDs that
# CNMarketAdapter expects.
_INCOME_MAP = {
    "TOTAL_OPERATE_INCOME": "营业收入",
    "OPERATE_COST": "营业成本",
    "TOTAL_OPERATE_COST": "营业总成本",
    "RESEARCH_EXPENSE": "研发费用",
    "SALE_EXPENSE": "销售费用",
    "MANAGE_EXPENSE": "管理费用",
    "FINANCE_EXPENSE": "财务费用",
    "OPERATE_PROFIT": "营业利润",
    "TOTAL_PROFIT": "利润总额",
    "INCOME_TAX": "所得税费用",
    "NETPROFIT": "净利润",
    "PARENT_NETPROFIT": "归属于母公司所有者的净利润",
    "BASIC_EPS": "基本每股收益",
    "DILUTED_EPS": "稀释每股收益",
}

_BALANCE_MAP = {
    "MONETARYFUNDS": "货币资金",
    "ACCOUNTS_RECE": "应收账款",
    "NOTE_ACCOUNTS_RECE": "应收票据及应收账款",
    "INVENTORY": "存货",
    "TOTAL_CURRENT_ASSETS": "流动资产合计",
    "FIXED_ASSET": "固定资产",
    "CIP": "在建工程",
    "INTANGIBLE_ASSET": "无形资产",
    "GOODWILL": "商誉",
    "TOTAL_NONCURRENT_ASSETS": "非流动资产合计",
    "TOTAL_ASSETS": "资产总计",
    "SHORT_LOAN": "短期借款",
    "ACCOUNTS_PAYABLE": "应付账款",
    # AUDIT-A6: current portion of long-term borrowings/bonds — without this
    # the total_debt fallback misses reclassified debt (万科A 2024: ¥146B).
    "NONCURRENT_LIAB_1YEAR": "一年内到期的非流动负债",
    "TOTAL_CURRENT_LIAB": "流动负债合计",
    "LONG_LOAN": "长期借款",
    "BOND_PAYABLE": "应付债券",
    "TOTAL_NONCURRENT_LIAB": "非流动负债合计",
    "TOTAL_LIABILITIES": "负债合计",
    "TOTAL_PARENT_EQUITY": "归属于母公司所有者权益合计",
    "MINORITY_EQUITY": "少数股东权益",
    "TOTAL_EQUITY": "所有者权益合计",
}

_CASHFLOW_MAP = {
    "NETCASH_OPERATE": "经营活动产生的现金流量净额",
    "NETCASH_INVEST": "投资活动产生的现金流量净额",
    "NETCASH_FINANCE": "筹资活动产生的现金流量净额",
    # NOTE: eastmoney reports capex as positive (cash outflow magnitude).
    # yfinance/EDGAR convention is negative (signed cash flow). Downstream
    # DCF engine expects the yfinance convention, so we negate this field
    # at parse time in _do_fetch() below (search for SIGN_FLIP_CAPEX).
    "CONSTRUCT_LONG_ASSET": "购建固定资产、无形资产和其他长期资产支付的现金",
    # D&A components — critical for EBITDA / FCFF computation.
    # eastmoney uses FA_IR_DEPR, not DEPRECIATION_FIXED_ASSETS.
    "FA_IR_DEPR": "固定资产折旧",          # 固定资产、投资性房地产折旧
    "IA_AMORTIZE": "无形资产摊销",
    "LPE_AMORTIZE": "长期待摊费用摊销",
    "USERIGHT_ASSET_AMORTIZE": "使用权资产摊销",
    "FINANCE_EXPENSE_CF": "财务费用",
    "BEGIN_CASH_EQUIVALENTS": "期初现金余额",
    "END_CASH_EQUIVALENTS": "期末现金余额",
}


def _safe_float(val: Any) -> float | None:
    """Coerce to float, returning None for any non-finite or invalid value.

    BUG-Y40 (2026-05-06): previously rejected only ``None`` / ``""`` / the
    literal string ``"nan"``. Python ``float('inf')`` (and the strings
    ``'inf'`` / ``'-inf'``) coerced cleanly through and propagated to the
    DCF engine + JSON serializers — emitting ``Infinity`` in the HTML
    REPORT JSON would break browser ``JSON.parse``. Reject all non-finite
    values at the parse boundary.
    """
    if val is None or val == "":
        return None
    s = str(val).lower().strip()
    if s in ("nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    # Belt-and-braces: even after the string filter above, an actual
    # `float('inf')` Python object still slips through unless we check
    # `isfinite`. NaN is also caught here.
    import math as _math
    if not _math.isfinite(f):
        return None
    return f


class AkShareConnector:
    """Primary A-share data connector. Tries akshare (eastmoney) first."""

    def __init__(self) -> None:
        self._ak = None

    def _ensure(self) -> bool:
        if self._ak is None:
            try:
                import akshare as ak
                self._ak = ak
                return True
            except ImportError:
                logger.warning("akshare not installed — run: pip install akshare")
                return False
        return True

    @staticmethod
    def _normalize_symbol(stock_code: str) -> str:
        """301358 → SZ301358, 600519 → SH600519."""
        clean = stock_code.replace(".SZ", "").replace(".SS", "").strip()
        if clean.startswith("6"):
            return f"SH{clean}"
        if clean.startswith(("0", "3")):
            return f"SZ{clean}"
        return clean

    @staticmethod
    def _fetch_industry_f10(stock_code: str) -> dict[str, str] | None:
        """Industry lookup via the eastmoney datacenter F10 API.

        push2.eastmoney.com (stock_individual_info_em) is flaky behind the
        CN proxy bypass, but datacenter.eastmoney.com answers reliably
        (verified live 2026-07, ~0.1s). EM2016 is eastmoney's 3-level
        industry string ("基础化工-化学制品-其他化学制品") — same taxonomy
        family as push2's 行业 field, so the orchestrator's substring
        keyword matching keeps working unchanged. Falls back to the CSRC
        classification when EM2016 is absent. Returns None on any failure.
        """
        clean = stock_code.replace(".SZ", "").replace(".SS", "").strip()
        secucode = f"{clean}.{'SH' if clean.startswith('6') else 'SZ'}"
        params = {
            "reportName": "RPT_F10_BASIC_ORGINFO",
            "columns": "SECUCODE,SECURITY_NAME_ABBR,EM2016,INDUSTRYCSRC1",
            "filter": f'(SECUCODE="{secucode}")',
            "pageSize": "1",
        }
        try:
            import requests
            with _no_proxy():
                resp = requests.get(
                    "https://datacenter.eastmoney.com/securities/api/data/v1/get",
                    params=params, timeout=8,
                )
            rows = ((resp.json() or {}).get("result") or {}).get("data") or []
            if not rows:
                return None
            row = rows[0]
            industry = str(row.get("EM2016") or "").strip()
            csrc = str(row.get("INDUSTRYCSRC1") or "").strip()
            if not industry:
                industry = csrc
            if not industry:
                return None
            return {
                "industry": industry,
                "industry_csrc": csrc,
                "name": str(row.get("SECURITY_NAME_ABBR") or "").strip(),
            }
        except Exception as e:
            logger.debug(f"datacenter F10 industry lookup failed: {e}")
            return None

    def fetch(self, stock_code: str) -> AkShareFinancials | None:
        """Fetch full financials + real-time quote + company info.

        Returns None on any failure — caller should fall back to yfinance.
        """
        if not self._ensure():
            return None

        clean = stock_code.replace(".SZ", "").replace(".SS", "").strip()
        symbol = self._normalize_symbol(stock_code)

        # BUG-30 (2026-04-23): akshare's eastmoney endpoints occasionally
        # return RemoteDisconnected / Connection aborted on first touch,
        # especially when the Clash proxy has just renegotiated the
        # .eastmoney.com bypass rule. Retry 2× with short backoff before
        # surrendering to the eastmoney connector fallback.
        import time as _time
        last_err: Exception | None = None
        for attempt, delay in enumerate([0, 1.0, 3.0]):
            if delay:
                _time.sleep(delay)
            try:
                with _no_proxy():
                    return self._do_fetch(clean, symbol)
            except Exception as e:
                last_err = e
                err_name = type(e).__name__
                # Only retry on transient network errors; fast-fail on
                # logic errors ("empty profit sheet" etc).
                transient = any(s in err_name for s in (
                    "ConnectionError", "RemoteDisconnected", "Timeout",
                    "ProtocolError", "ReadError", "ChunkedEncodingError",
                ))
                if attempt < 2 and transient:
                    logger.warning(
                        f"akshare transient error for {stock_code} (attempt "
                        f"{attempt + 1}/3): {err_name}: {str(e)[:160]} — retrying"
                    )
                    continue
                break
        logger.warning(
            f"akshare fetch failed for {stock_code}: "
            f"{type(last_err).__name__}: {str(last_err)[:200]}"
        )
        return None

    @staticmethod
    def _retry_transient(label: str, fn, attempts: int = 3, base_delay: float = 0.8):
        """Run a single akshare endpoint with per-call transient retry.

        BUG-30 (2026-05-05): the outer retry in `fetch()` re-runs all three
        statement endpoints from scratch when any one fails — a 3x cost when
        only the second call hit a transient `RemoteDisconnected`. This
        helper isolates per-call retries so a flaky `balance` doesn't force
        re-pulling `profit` + `cashflow`.
        """
        import time as _time
        last: Exception | None = None
        for i in range(attempts):
            if i > 0:
                _time.sleep(base_delay * (2 ** (i - 1)))  # 0.8s, 1.6s
            try:
                return fn()
            except Exception as e:
                err_name = type(e).__name__
                transient = any(s in err_name for s in (
                    "ConnectionError", "RemoteDisconnected", "Timeout",
                    "ProtocolError", "ReadError", "ChunkedEncodingError",
                ))
                last = e
                if not transient or i == attempts - 1:
                    raise
                logger.debug(
                    f"akshare.{label} transient {err_name} "
                    f"(attempt {i + 1}/{attempts}), retrying"
                )
        raise last  # pragma: no cover — loop always raises or returns

    def _do_fetch(self, stock_code: str, symbol: str) -> AkShareFinancials:
        ak = self._ak

        # Financial statements (yearly) — each call gets independent retry so
        # a transient blip on `balance` doesn't drop `profit`'s good response.
        profit = self._retry_transient(
            "profit", lambda: ak.stock_profit_sheet_by_yearly_em(symbol=symbol),
        )
        balance = self._retry_transient(
            "balance", lambda: ak.stock_balance_sheet_by_yearly_em(symbol=symbol),
        )
        cashflow = self._retry_transient(
            "cashflow", lambda: ak.stock_cash_flow_sheet_by_yearly_em(symbol=symbol),
        )

        if profit is None or profit.empty:
            raise RuntimeError("empty profit sheet")

        # Parse latest row + historical
        def parse_df(df, field_map):
            """Convert dataframe to {year: {cas_concept: value}} dict."""
            out: dict[int, dict[str, float]] = {}
            if df is None or df.empty:
                return out
            for _, row in df.iterrows():
                rd = row.get("REPORT_DATE")
                if rd is None:
                    continue
                try:
                    year = int(str(rd)[:4])
                except (ValueError, TypeError):
                    continue
                facts: dict[str, float] = {}
                for col, cas_name in field_map.items():
                    if col in df.columns:
                        v = _safe_float(row.get(col))
                        if v is not None:
                            # Last-write-wins if two columns map to same CAS concept
                            facts[cas_name] = v
                out[year] = facts
            return out

        income_by_year = parse_df(profit, _INCOME_MAP)
        balance_by_year = parse_df(balance, _BALANCE_MAP)
        cashflow_by_year = parse_df(cashflow, _CASHFLOW_MAP)

        # SIGN_FLIP_CAPEX: eastmoney stores capex as positive (magnitude of
        # cash outflow). The DCF engine and historical-data consumers downstream
        # expect the yfinance/EDGAR convention where capex is a signed cash
        # flow (negative = outflow). Flip sign here so the rest of the pipeline
        # sees a consistent negative value.
        for year, facts in cashflow_by_year.items():
            capex_key = "购建固定资产、无形资产和其他长期资产支付的现金"
            if capex_key in facts and facts[capex_key] > 0:
                facts[capex_key] = -facts[capex_key]

        # Merge per year
        all_years = sorted(
            set(income_by_year) | set(balance_by_year) | set(cashflow_by_year),
            reverse=True,
        )
        if not all_years:
            raise RuntimeError("no fiscal years parsed")

        merged_by_year: dict[int, dict[str, float]] = {}
        for year in all_years:
            merged: dict[str, float] = {}
            merged.update(income_by_year.get(year, {}))
            merged.update(balance_by_year.get(year, {}))
            merged.update(cashflow_by_year.get(year, {}))
            if merged:
                merged_by_year[year] = merged

        latest_year = max(merged_by_year.keys())
        latest_facts = merged_by_year[latest_year]

        # Company info + real-time quote — tries multiple akshare endpoints.
        # This environment (China-based behind Clash Verge) can reach the
        # emweb.securities.eastmoney.com financial-statement host reliably but
        # often cannot reach push2.eastmoney.com (used by stock_individual_info_em
        # and stock_zh_a_spot_em). Falls back gracefully; yfinance downstream
        # can still supply price via market_data_connector.
        company_name: str | None = None
        market_data: dict[str, float] = {}
        company_info: dict[str, Any] = {}

        # Method 1: stock_individual_info_em (fastest, gives shares + listing date + industry)
        info_df = None
        for attempt in range(2):
            try:
                info_df = ak.stock_individual_info_em(symbol=stock_code)
                if info_df is not None and not info_df.empty:
                    break
            except Exception:
                if attempt == 0:
                    import time as _t
                    _t.sleep(0.5)

        if info_df is not None and not info_df.empty:
            info_dict = dict(zip(info_df["item"], info_df["value"]))
            company_name = str(info_dict.get("股票简称", "")).strip() or None
            market_data["current_price"] = _safe_float(info_dict.get("最新")) or 0.0
            market_data["total_shares"] = _safe_float(info_dict.get("总股本")) or 0.0
            market_data["float_shares"] = _safe_float(info_dict.get("流通股")) or 0.0
            market_data["market_cap"] = _safe_float(info_dict.get("总市值")) or 0.0
            market_data["float_market_cap"] = _safe_float(info_dict.get("流通市值")) or 0.0
            company_info["industry"] = str(info_dict.get("行业", ""))
            company_info["listing_date"] = str(info_dict.get("上市时间", ""))
            company_info["name"] = company_name
        else:
            logger.debug(f"akshare individual_info unavailable for {stock_code}, "
                         f"trying bid_ask fallback for price only")
            # Method 2: stock_bid_ask_em — level-1 quote fallback (different host)
            got_price = False
            try:
                q = ak.stock_bid_ask_em(symbol=stock_code)
                if q is not None and not q.empty:
                    qd = dict(zip(q["item"], q["value"]))
                    price = _safe_float(qd.get("最新"))
                    if price:
                        market_data["current_price"] = price
                        got_price = True
                        logger.info(f"akshare: got price via bid_ask fallback (¥{price:.2f})")
            except Exception as e:
                logger.debug(f"akshare bid_ask fallback also failed: {e}")

            # Method 3: tencent/sina level-1 quote helper (hosts reachable
            # even when push2.eastmoney.com is down). Replaces the old
            # stock_zh_a_spot() full-market crawl, which was dead code
            # (AUDIT 2026-07): sina's 代码 column carries "sz002669"-style
            # prefixes so the bare-code match never hit, the frame has no
            # 总股本/总市值 columns to read, and the ~80-page crawl risks a
            # sina IP ban on repeat runs.
            if not got_price:
                try:
                    from .tencent_sina_quote import fetch_cn_quote
                    q3 = fetch_cn_quote(stock_code)
                    if q3 is not None and q3.current_price:
                        market_data["current_price"] = q3.current_price
                        logger.info(f"akshare: got price via tencent/sina "
                                    f"quote (¥{q3.current_price:.2f})")
                        if q3.shares_outstanding and not market_data.get("total_shares"):
                            market_data["total_shares"] = q3.shares_outstanding
                        if q3.market_cap and not market_data.get("market_cap"):
                            market_data["market_cap"] = q3.market_cap
                        if q3.name and not company_name:
                            company_name = q3.name
                            company_info.setdefault("name", q3.name)
                except Exception as e:
                    logger.debug(f"tencent/sina quote fallback also failed: {e}")

        # Method 1.5 (AUDIT 2026-07, BUG-Y18 root-cause fix): when push2 is
        # unreachable — or answered without a 行业 field — fetch the industry
        # from the eastmoney datacenter F10 API, a different host that stays
        # reachable behind the CN proxy bypass. Without this, any A-share
        # missing from the orchestrator's name-fragment whitelist collapses
        # to the General sector pack (Cambricon v2: 25× DCF miscalibration).
        if not company_info.get("industry"):
            f10 = self._fetch_industry_f10(stock_code)
            if f10:
                company_info["industry"] = f10["industry"]
                if f10.get("industry_csrc"):
                    company_info.setdefault("industry_csrc", f10["industry_csrc"])
                if not company_name and f10.get("name"):
                    company_name = f10["name"]
                    company_info.setdefault("name", company_name)
                logger.info(f"akshare: industry via datacenter F10 fallback: "
                            f"{company_info['industry']}")

        # Derived: shares_outstanding for DCF (use total shares, not float)
        if market_data.get("total_shares"):
            market_data["shares_outstanding"] = market_data["total_shares"]

        logger.info(
            f"akshare: {stock_code} ({company_name}) FY{latest_year}, "
            f"{len(latest_facts)} facts, price=¥{market_data.get('current_price', 0):.2f}"
        )

        return AkShareFinancials(
            stock_code=stock_code,
            company_name=company_name or "",
            fiscal_year=latest_year,
            report_date=f"{latest_year}-12-31",
            facts=latest_facts,
            historical=merged_by_year,
            market_data=market_data,
            company_info=company_info,
        )
