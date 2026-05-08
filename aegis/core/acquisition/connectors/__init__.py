"""Data source connectors.

Available connectors:
- SECEDGARConnector: SEC EDGAR filings (Tier 1, free)
- SECAPIClient: Low-level SEC REST API wrapper
- XBRLParser: Parse XBRL company facts into structured data
- SECEntityRegistry: Ticker ↔ CIK lookup
- OpenBBConnector: Consensus, macro, peer data via OpenBB Platform (Tier 2)
- SECForm4Connector: SEC Form 4 insider trading data (Tier 1, free)
"""

from aegis.core.acquisition.connectors.edgar_connector import SECEDGARConnector
from aegis.core.acquisition.connectors.sec_api_client import SECAPIClient
from aegis.core.acquisition.connectors.sec_entity_registry import SECEntityRegistry
from aegis.core.acquisition.connectors.xbrl_parser import XBRLParser

__all__ = [
    "SECEDGARConnector",
    "SECAPIClient",
    "SECEntityRegistry",
    "XBRLParser",
]
