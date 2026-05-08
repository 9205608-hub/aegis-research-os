"""Critics — Section 20."""

from aegis.core.critics.base import CriticBase
from aegis.core.critics.logic_critic.critic import LogicCritic
from aegis.core.critics.accounting_critic.critic import AccountingCritic
from aegis.core.critics.evidence_critic.critic import EvidenceCritic
from aegis.core.critics.sector_critic.critic import SectorCritic
from aegis.core.critics.cognitive_bias_critic.critic import CognitiveBiasCritic
from aegis.core.critics.macro_consistency_critic.critic import MacroConsistencyCritic
from aegis.core.critics.market_critic.critic import MarketCritic
from aegis.core.critics.cross_entity_critic.critic import CrossEntityCritic
from aegis.core.critics.numeric_consistency_critic.critic import NumericConsistencyCritic
from aegis.core.critics.narrative_fact_critic.critic import NarrativeFactCritic
from aegis.core.critics.llm_judge_critic.critic import LLMJudgeCritic

__all__ = [
    "CriticBase",
    "LogicCritic",
    "AccountingCritic",
    "EvidenceCritic",
    "SectorCritic",
    "CognitiveBiasCritic",
    "MacroConsistencyCritic",
    "MarketCritic",
    "CrossEntityCritic",
    "NumericConsistencyCritic",
    "NarrativeFactCritic",
    "LLMJudgeCritic",
]
