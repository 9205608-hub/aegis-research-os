"""Aegis Research OS — Core Data Contracts.

All data flowing through the system must conform to these schemas.
"""

from .atomic_fact_schema import AtomicAccountingFact
from .common import (
    AccountingStandard,
    ConfidenceBucket,
    Currency,
    Direction,
    EdgeDurability,
    EdgeType,
    MarketId,
    PeriodType,
    PublishingStatus,
    ResearchMode,
    Severity,
    SourceTier,
    StatementType,
    StrictModel,
)
from .comparison_matrix_schema import ComparisonDimension, ComparisonMatrix, RelativeValuation
from .consensus_snapshot_schema import ConsensusSnapshot
from .critic_result_schema import BiasDetectionResult, CriticIssue, CriticResult
from .edge_assessment_schema import EdgeAssessment
from .entity_relationship_schema import EntityRelationship, RevenueSignificance
from .entity_schema import EntityContract
from .event_schema import EventContract
from .evidence_packet_schema import EvidencePacket
from .judgment_schema import (
    CognitiveBiasSelfCheck,
    Counterargument,
    DisconfirmingTrigger,
    Inference,
    JudgmentContract,
    Observation,
)
from .macro_snapshot_schema import MacroSnapshot
from .metric_definition_schema import MetricDefinition
from .portfolio_signal_schema import CorrelationWarning, PortfolioSignal
from .postmortem_schema import PostMortem
from .run_manifest_schema import RunManifest
from .scenario_schema import (
    KeyAssumptionDisagreement,
    ScenarioDefinition,
    ScenarioMatrix,
    ScenarioOutput,
    SensitivityRanking,
)
from .thesis_schema import (
    EdgeClassification,
    KillCriterion,
    Monitorable,
    ThesisContract,
)
from .thesis_version_schema import ThesisFieldChange, ThesisVersionRecord

__all__ = [
    "AccountingStandard",
    "AtomicAccountingFact",
    "BiasDetectionResult",
    "CognitiveBiasSelfCheck",
    "ComparisonDimension",
    "ComparisonMatrix",
    "ConfidenceBucket",
    "ConsensusSnapshot",
    "CorrelationWarning",
    "Counterargument",
    "CriticIssue",
    "CriticResult",
    "Direction",
    "DisconfirmingTrigger",
    "EdgeAssessment",
    "EdgeClassification",
    "EdgeDurability",
    "EdgeType",
    "EntityContract",
    "EntityRelationship",
    "EventContract",
    "EvidencePacket",
    "Inference",
    "JudgmentContract",
    "KeyAssumptionDisagreement",
    "KillCriterion",
    "MacroSnapshot",
    "MarketId",
    "MetricDefinition",
    "Monitorable",
    "Observation",
    "PeriodType",
    "PortfolioSignal",
    "PostMortem",
    "PublishingStatus",
    "RelativeValuation",
    "ResearchMode",
    "RevenueSignificance",
    "RunManifest",
    "ScenarioDefinition",
    "ScenarioMatrix",
    "ScenarioOutput",
    "SensitivityRanking",
    "Severity",
    "SourceTier",
    "StatementType",
    "StrictModel",
    "ThesisContract",
    "ThesisFieldChange",
    "ThesisVersionRecord",
]
