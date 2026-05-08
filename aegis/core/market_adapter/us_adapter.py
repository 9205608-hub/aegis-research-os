"""US Market Adapter — Section 6.1.

Handles SEC EDGAR filings, US GAAP, XBRL inline format.
"""

from aegis.data_contracts.common import AccountingStandard, MarketId

from .adapter_base import AdaptedData, MarketAdapter, MarketConfig


class USMarketAdapter(MarketAdapter):
    """Adapter for US market data (SEC/EDGAR)."""

    _CONFIG = MarketConfig(
        market_id=MarketId.US,
        regulatory_body="SEC",
        filing_format="XBRL_inline",
        accounting_standards=[AccountingStandard.US_GAAP],
        fiscal_year_convention="company_specific",
        primary_currency="USD",
        trading_calendar="NYSE",
        disclosure_languages=["en"],
        connector_ids=["edgar_connector"],
    )

    # Common XBRL concept mappings to canonical concept IDs
    CONCEPT_MAP: dict[str, str] = {
        # Income Statement
        "us-gaap:Revenues": "revenue",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
        "us-gaap:CostOfRevenue": "cost_of_revenue",
        "us-gaap:CostOfGoodsAndServicesSold": "cost_of_revenue",
        "us-gaap:GrossProfit": "gross_profit",
        "us-gaap:OperatingIncomeLoss": "operating_income",
        "us-gaap:OperatingExpenses": "operating_expenses",
        "us-gaap:NetIncomeLoss": "net_income",
        "us-gaap:EarningsPerShareDiluted": "eps_diluted",
        "us-gaap:EarningsPerShareBasic": "eps_basic",
        "us-gaap:IncomeTaxExpenseBenefit": "income_tax_expense",
        "us-gaap:NonoperatingIncomeExpense": "nonoperating_income",
        # R&D and SGA
        "us-gaap:ResearchAndDevelopmentExpense": "research_and_development",
        "us-gaap:SellingGeneralAndAdministrativeExpense": "selling_general_admin",
        # Depreciation & Amortization
        # Note: filings vary in which concepts they use. Alphabet (GOOG/GOOGL)
        # reports `us-gaap:Depreciation` and `us-gaap:AmortizationOfIntangible
        # Assets` separately rather than the combined concept — fact_bridge
        # derives `depreciation_amortization` from the components when the
        # combined field is missing.
        "us-gaap:DepreciationDepletionAndAmortization": "depreciation_amortization",
        "us-gaap:DepreciationAndAmortization": "depreciation_amortization",
        "us-gaap:Depreciation": "depreciation",
        "us-gaap:AmortizationOfIntangibleAssets": "amortization",
        "us-gaap:Amortization": "amortization",
        # Balance Sheet
        "us-gaap:Assets": "total_assets",
        "us-gaap:Liabilities": "total_liabilities",
        "us-gaap:StockholdersEquity": "shareholders_equity",
        "us-gaap:CashAndCashEquivalentsAtCarryingValue": "cash_and_equivalents",
        "us-gaap:ShortTermInvestments": "short_term_investments",
        "us-gaap:MarketableSecuritiesCurrent": "marketable_securities_current",
        "us-gaap:MarketableSecuritiesNoncurrent": "marketable_securities_noncurrent",
        "us-gaap:LongTermDebt": "long_term_debt",
        "us-gaap:LongTermDebtNoncurrent": "long_term_debt_noncurrent",
        "us-gaap:LongTermDebtCurrent": "long_term_debt_current",
        "us-gaap:CommercialPaper": "commercial_paper",
        "us-gaap:ShortTermBorrowings": "short_term_debt",
        "us-gaap:DebtInstrumentCarryingAmount": "total_debt_carrying",
        "us-gaap:AssetsCurrent": "current_assets",
        "us-gaap:LiabilitiesCurrent": "current_liabilities",
        "us-gaap:RetainedEarningsAccumulatedDeficit": "retained_earnings",
        "us-gaap:PropertyPlantAndEquipmentNet": "ppe_net",
        "us-gaap:InventoryNet": "inventory",
        "us-gaap:AccountsReceivableNetCurrent": "accounts_receivable",
        "us-gaap:AccountsPayableCurrent": "accounts_payable",
        # Cash Flow
        "us-gaap:NetCashProvidedByUsedInOperatingActivities": "cfo",
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment": "capex_ppe",
        "us-gaap:NetCashProvidedByUsedInInvestingActivities": "cfi",
        "us-gaap:NetCashProvidedByUsedInFinancingActivities": "cff",
        # Shareholder Returns
        "us-gaap:PaymentsForRepurchaseOfCommonStock": "share_buybacks",
        "us-gaap:StockRepurchasedAndRetiredDuringPeriodValue": "shares_repurchased_value",
        "us-gaap:StockRepurchasedAndRetiredDuringPeriodShares": "shares_repurchased_count",
        "us-gaap:PaymentsOfDividends": "dividends_paid",
        "us-gaap:CommonStockDividendsPerShareDeclared": "dividends_per_share",
        # SBC
        "us-gaap:ShareBasedCompensation": "sbc",
        "us-gaap:AllocatedShareBasedCompensationExpense": "sbc",
        # Shares
        "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding": "diluted_shares",
        "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic": "basic_shares",
        "us-gaap:CommonStockSharesOutstanding": "shares_outstanding",
        # Tax details
        "us-gaap:EffectiveIncomeTaxRateContinuingOperations": "effective_tax_rate",
    }

    @property
    def config(self) -> MarketConfig:
        return self._CONFIG

    def adapt_filing_data(self, raw_data: dict) -> tuple[dict, AdaptedData]:
        """Map XBRL concepts to canonical concept IDs."""
        adapted = {}
        notes = []

        for key, value in raw_data.items():
            canonical = self.CONCEPT_MAP.get(key)
            if canonical:
                adapted[canonical] = value
            else:
                # Keep unmapped data with original key, note it
                adapted[key] = value
                if key.startswith("us-gaap:"):
                    notes.append(f"Unmapped XBRL concept: {key}")

        metadata = AdaptedData(
            market_id=MarketId.US,
            original_format="XBRL_inline",
            currency="USD",
            accounting_standard=AccountingStandard.US_GAAP,
            adaptation_notes=notes,
        )
        return adapted, metadata

    def get_fiscal_year_end(self, entity_id: str) -> str:
        """US companies have company-specific fiscal year ends.

        In production, this queries Entity Master. Default is 12-31.
        """
        # TODO: Query entity master for actual FYE
        return "12-31"

    def get_default_accounting_standard(self) -> AccountingStandard:
        return AccountingStandard.US_GAAP

    def validate_market_data(self, data: dict) -> list[str]:
        errors = super().validate_market_data(data)
        # US-specific: check for accession number
        if "accession_no" in data and not data["accession_no"]:
            errors.append("US filing must have a valid accession number")
        return errors
