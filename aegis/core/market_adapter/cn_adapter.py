"""China Market Adapter — Section 6.1.

Handles A-share filings (cninfo), CAS accounting standard,
PDF structured format, calendar year fiscal year convention.
"""

from aegis.data_contracts.common import AccountingStandard, MarketId

from .adapter_base import AdaptedData, MarketAdapter, MarketConfig


class CNMarketAdapter(MarketAdapter):
    """Adapter for China A-share market data."""

    _CONFIG = MarketConfig(
        market_id=MarketId.CN,
        regulatory_body="CSRC",
        filing_format="PDF_structured",
        accounting_standards=[AccountingStandard.CAS],
        fiscal_year_convention="calendar_year",  # A股统一12月31日
        primary_currency="CNY",
        trading_calendar="SSE",
        disclosure_languages=["zh"],
        connector_ids=["cninfo_connector", "sse_connector", "szse_connector"],
        special_considerations={
            "related_party_transaction_risk": "high",
            "state_owned_enterprise_governance": "true",
            "vie_structure_for_offshore_listing": "true",
            "land_use_rights_not_freehold": "true",
            "government_subsidy_recognition": "common",
        },
    )

    # CAS (中国会计准则) concept mapping to canonical IDs
    CONCEPT_MAP: dict[str, str] = {
        # 利润表
        "营业收入": "revenue",
        "营业总收入": "revenue",
        "营业成本": "cost_of_revenue",
        "营业总成本": "total_operating_cost",
        "营业利润": "operating_income",
        "利润总额": "profit_before_tax",
        # AUDIT-A8: 所得税费用 was unmapped → all A-share DCFs silently used
        # the US 21% default tax rate. fact_bridge now derives
        # effective_tax_rate = income_tax_expense / profit_before_tax.
        "所得税费用": "income_tax_expense",
        "净利润": "net_income",
        "归属于母公司所有者的净利润": "net_income_to_parent",
        "基本每股收益": "eps_basic",
        "稀释每股收益": "eps_diluted",
        "毛利润": "gross_profit",
        # 资产负债表
        "资产总计": "total_assets",
        "负债合计": "total_liabilities",
        "所有者权益合计": "shareholders_equity",
        "归属于母公司所有者权益合计": "equity_to_parent",
        "货币资金": "cash_and_equivalents",
        "短期借款": "short_term_debt",
        "长期借款": "long_term_debt",
        "应付债券": "bonds_payable",
        # AUDIT-A6: 一年内到期的非流动负债 (current portion of long-term
        # borrowings/bonds — a SEPARATE line item, NOT included in 长期借款).
        # fact_bridge's total_debt fallback sums it for CN alongside 长期借款.
        "一年内到期的非流动负债": "long_term_debt_current",
        "流动资产合计": "current_assets",
        "流动负债合计": "current_liabilities",
        "少数股东权益": "minority_interest",
        # 现金流量表
        "经营活动产生的现金流量净额": "cfo",
        "投资活动产生的现金流量净额": "cfi",
        "筹资活动产生的现金流量净额": "cff",
        "购建固定资产、无形资产和其他长期资产支付的现金": "capex_ppe",
        # 折旧与摊销（来自现金流量表"补充资料"）
        "固定资产折旧": "depreciation",
        "无形资产摊销": "amortization_intangible",
        "长期待摊费用摊销": "amortization_lpe",
        "使用权资产摊销": "amortization_useright",
        # 特殊科目
        "政府补助": "government_subsidy",
        "研发费用": "r_and_d_expense",
        "开发支出": "capitalized_dev_cost",
        "使用权资产": "right_of_use_asset",
        "商誉": "goodwill",
        "股份支付费用": "sbc",
        # BUG-33: 应收账款标准化 — 优先用含票据口径，两者都映射到同一 canonical key
        "应收账款": "accounts_receivable",
        "应收票据及应收账款": "notes_and_accounts_receivable",
        "存货": "inventory",
        # AUDIT-A6-bonus: cninfo_connector's yfinance fallback emits these
        # CAS names (Reconciled Depreciation/Total Debt/EBITDA/Free Cash
        # Flow/Interest Expense) — without entries here they passed through
        # as raw Chinese keys and were silently dropped, degrading D&A to
        # the capex×0.5 proxy and understating total_debt.
        "折旧摊销": "depreciation_amortization",
        "有息负债合计": "total_debt",
        "EBITDA": "ebitda",
        "自由现金流": "free_cash_flow",
        "利息支出": "interest_expense",
    }

    # CAS-specific items that need special attention in cross-standard comparison
    CAS_SPECIAL_ITEMS = {
        "government_subsidy": "政府补助在CAS下常计入营业外收入或其他收益，需重分类",
        "capitalized_dev_cost": "CAS允许开发阶段资本化（类似IFRS），需与US GAAP比较时调整",
        "related_party_transactions": "A股关联交易风险较高，需特别审查",
        "land_use_rights": "中国土地使用权非永久产权，需作为无形资产摊销",
    }

    @property
    def config(self) -> MarketConfig:
        return self._CONFIG

    def adapt_filing_data(self, raw_data: dict) -> tuple[dict, AdaptedData]:
        """Map Chinese accounting concepts to canonical IDs."""
        adapted = {}
        notes = []

        for key, value in raw_data.items():
            canonical = self.CONCEPT_MAP.get(key)
            if canonical:
                adapted[canonical] = value
                # Flag special items for cross-standard review
                if canonical in self.CAS_SPECIAL_ITEMS:
                    notes.append(
                        f"CAS special item [{canonical}]: "
                        f"{self.CAS_SPECIAL_ITEMS[canonical]}"
                    )
            else:
                adapted[key] = value

        # Auto-flag government subsidy if present
        if "government_subsidy" in adapted and adapted["government_subsidy"]:
            notes.append(
                "Government subsidy detected. Requires reclassification "
                "for cross-standard comparison."
            )

        # Derive D&A aggregate from its components (akshare/eastmoney surfaces
        # 固定资产折旧/无形资产摊销/长期待摊费用摊销/使用权资产摊销 separately).
        # Downstream DCF engine needs a single `depreciation_and_amortization`
        # total to compute EBITDA and FCFF correctly.
        da_components = [
            adapted.get("depreciation"),
            adapted.get("amortization_intangible"),
            adapted.get("amortization_lpe"),
            adapted.get("amortization_useright"),
        ]
        da_values = [c for c in da_components if isinstance(c, (int, float))]
        if da_values:
            total_da = sum(da_values)
            # Canonical key used by orchestrator's DCF builder (line 2898, 3082)
            adapted["depreciation_amortization"] = total_da
            adapted["depreciation_and_amortization"] = total_da  # alias
            adapted["da"] = total_da  # alias

        metadata = AdaptedData(
            market_id=MarketId.CN,
            original_format="PDF_structured",
            currency="CNY",
            accounting_standard=AccountingStandard.CAS,
            fiscal_year_end="12-31",  # A股统一
            adaptation_notes=notes,
        )
        return adapted, metadata

    def get_fiscal_year_end(self, entity_id: str) -> str:
        """A-share companies uniformly use Dec 31 as fiscal year end."""
        return "12-31"

    def get_default_accounting_standard(self) -> AccountingStandard:
        return AccountingStandard.CAS

    def validate_market_data(self, data: dict) -> list[str]:
        errors = super().validate_market_data(data)

        # China-specific validations
        if "related_party_transactions" in data:
            rpt = data["related_party_transactions"]
            if isinstance(rpt, dict) and rpt.get("amount_ratio", 0) > 0.3:
                errors.append(
                    "WARNING: Related party transaction ratio > 30%. "
                    "Requires enhanced scrutiny per CAS 36."
                )

        return errors
