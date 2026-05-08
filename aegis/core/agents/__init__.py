"""Specialist Agents — Section 19."""

from aegis.core.agents.base import AgentBase, AgentInput, AgentOutput
from aegis.core.agents.accounting_analyst.agent import AccountingAnalyst
from aegis.core.agents.business_analyst.agent import BusinessAnalyst
from aegis.core.agents.management_analyst.agent import ManagementAnalyst
from aegis.core.agents.sector_context_agent.agent import SectorContextAgent
from aegis.core.agents.valuation_analyst.agent import ValuationAnalyst
from aegis.core.agents.variant_analyst.agent import VariantAnalyst
from aegis.core.agents.risk_analyst.agent import RiskAnalyst
from aegis.core.agents.llm_agent_base import LLMAgentBase
from aegis.core.agents.llm_agents import (
    LLMAccountingAnalyst,
    LLMBusinessAnalyst,
    LLMSectorContextAgent,
    LLMManagementAnalyst,
    LLMValuationAnalyst,
    LLMVariantAnalyst,
    LLMRiskAnalyst,
)

__all__ = [
    "AgentBase",
    "AgentInput",
    "AgentOutput",
    # Rule-based agents
    "AccountingAnalyst",
    "BusinessAnalyst",
    "ManagementAnalyst",
    "SectorContextAgent",
    "ValuationAnalyst",
    "VariantAnalyst",
    "RiskAnalyst",
    # LLM-powered agents
    "LLMAgentBase",
    "LLMAccountingAnalyst",
    "LLMBusinessAnalyst",
    "LLMSectorContextAgent",
    "LLMManagementAnalyst",
    "LLMValuationAnalyst",
    "LLMVariantAnalyst",
    "LLMRiskAnalyst",
]
