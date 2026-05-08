"""Chief Analyst Layer — LLM-driven research direction and thesis synthesis.

Four key roles:
1. ResearchDirector: Runs BEFORE agents to define research focus and hypothesis
2. ScenarioArchitect: Runs AFTER base DCF to construct narrative-driven scenarios
3. ThesisSynthesizer: Runs AFTER agents to synthesize all judgments into coherent thesis
4. ReportEditor: Runs AFTER decision to shape the final report narrative
"""

from aegis.core.chief_analyst.research_director import ResearchDirector, ResearchDirective
from aegis.core.chief_analyst.scenario_architect import ScenarioArchitect, ScenarioBlueprint
from aegis.core.chief_analyst.earnings_call_analyzer import EarningsCallAnalyzer, EarningsCallInsights
from aegis.core.chief_analyst.news_sentiment_analyzer import NewsSentimentAnalyzer, NewsSentimentInsights
from aegis.core.chief_analyst.thesis_synthesizer import ThesisSynthesizer, SynthesizedThesis
from aegis.core.chief_analyst.report_editor import ReportEditor, EditedReport

__all__ = [
    "ResearchDirector", "ResearchDirective",
    "ScenarioArchitect", "ScenarioBlueprint",
    "EarningsCallAnalyzer", "EarningsCallInsights",
    "NewsSentimentAnalyzer", "NewsSentimentInsights",
    "ThesisSynthesizer", "SynthesizedThesis",
    "ReportEditor", "EditedReport",
]
