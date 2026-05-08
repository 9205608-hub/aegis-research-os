"""Sector Critic — Section 20.1.

Checks for:
- Missing sector-specific KPI analysis
- Ignoring sector cycle positioning
- Missing sector-specific accounting considerations
- Valuation without sector-appropriate method
"""

from __future__ import annotations

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult
from aegis.data_contracts.judgment_schema import JudgmentContract


class SectorCritic(CriticBase):
    """Reviews judgments for sector context consistency."""

    CRITIC_TYPE = "sector_critic"

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        issues: list[CriticIssue] = []
        ctx = context or {}
        sector_pack = ctx.get("sector_pack")

        if not sector_pack:
            # No sector pack — can't do sector-specific checks
            return CriticResult(
                critic_id=f"critic_sector_{id(self)}",
                critic_type=self.CRITIC_TYPE,
                issues=[],
                block_publish=False,
                overall_risk="low",
            )

        for j in judgments:
            issues.extend(self._check_sector_kpi_coverage(j, sector_pack))
            issues.extend(self._check_sector_context_applied(j, sector_pack))
            issues.extend(self._check_sector_accounting(j, sector_pack))

        return CriticResult(
            critic_id=f"critic_sector_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=self._any_block(issues),
            overall_risk=self._overall_risk(issues),
        )

    def _check_sector_kpi_coverage(
        self, j: JudgmentContract, sector_pack: dict
    ) -> list[CriticIssue]:
        """Check that critical sector KPIs are addressed in the analysis."""
        issues = []
        critical_kpis = [
            kpi for kpi in sector_pack.get("key_kpis", [])
            if isinstance(kpi, dict) and kpi.get("importance") == "critical"
        ]

        used_metrics = set(j.used_metric_ids)
        missing_critical = [
            kpi.get("display", kpi.get("metric"))
            for kpi in critical_kpis
            if kpi.get("metric") not in used_metrics
        ]

        if missing_critical:
            issues.append(self._make_issue(
                code="SECTOR_CRITICAL_KPI_MISSING",
                severity="warn",
                message=f"Critical sector KPIs not analyzed: {', '.join(missing_critical)}",
                judgment_ids=[j.judgment_id],
                action="Include critical sector KPIs in analysis or explain why not applicable",
            ))
        return issues

    def _check_sector_context_applied(
        self, j: JudgmentContract, sector_pack: dict
    ) -> list[CriticIssue]:
        """Check that sector context was injected."""
        issues = []
        expected_pack_id = sector_pack.get("sector_pack_id", "")

        if j.sector_context_applied != expected_pack_id:
            issues.append(self._make_issue(
                code="SECTOR_CONTEXT_NOT_APPLIED",
                severity="info",
                message=f"Judgment does not reference sector pack '{expected_pack_id}'",
                judgment_ids=[j.judgment_id],
                action="Ensure sector context agent ran before this judgment",
            ))
        return issues

    def _check_sector_accounting(
        self, j: JudgmentContract, sector_pack: dict
    ) -> list[CriticIssue]:
        """Check that sector-specific accounting issues are considered.

        BUG-Y43 (2026-05-06): the keyword extractor only knew English terms
        (sbc / subsid / vie / capitali / etc.) and the agent-text matcher
        used those English keys. Result: CN sector packs (sp_baijiu_cn_v1,
        sp_pharma_cn_v1, sp_banking_cn_v1, sp_new_energy_cn_v1) — whose
        notes are in Chinese with parenthetical English glosses — never
        produced any keys, and the check silently no-op'd. Now we extract
        BOTH English and Chinese terms from the note, and match against
        agent text in both alphabets.
        """
        issues = []
        acct_notes = sector_pack.get("accounting_considerations", [])
        if not acct_notes:
            return issues

        # Only check for accounting_analyst judgments
        if j.agent_name != "accounting_analyst":
            return issues

        all_text = " ".join(obs.text for obs in j.observations)
        all_text += " ".join(inf.text for inf in j.inferences)
        text_lc = all_text.lower()

        # English keywords map to their lower-case form
        EN_KEYWORDS = (
            "sbc", "subsid", "vie", "related party", "segment",
            "capitali", "depreciation", "revaluation", "lease",
            "revenue recognition", "deferred", "amortiz", "impair",
            "goodwill", "intangib",
        )
        # Chinese keywords for CN sector packs (CAS-specific concepts).
        ZH_KEYWORDS = (
            "合同负债", "消费税", "经销商", "压货", "直销", "基酒",
            "销售费用", "研发资本化", "集采", "政府补助", "政府补贴",
            "分部", "关联方", "可变利益实体", "折旧", "摊销",
            "递延", "减值", "商誉", "无形资产",
        )

        # Aggregate which keywords occur in the SECTOR PACK NOTES (not in
        # agent text yet). We only care about checking against agent text
        # for keywords that are actually in the sector's notes.
        notes_blob_lc = " ".join(acct_notes).lower()
        notes_blob = " ".join(acct_notes)
        en_hits = [kw for kw in EN_KEYWORDS if kw in notes_blob_lc]
        zh_hits = [kw for kw in ZH_KEYWORDS if kw in notes_blob]

        if not en_hits and not zh_hits:
            return issues  # Nothing to check against — no extractable concepts

        agent_addresses_some = (
            any(kw in text_lc for kw in en_hits)
            or any(kw in all_text for kw in zh_hits)
        )
        if not agent_addresses_some:
            issues.append(self._make_issue(
                code="SECTOR_ACCOUNTING_IGNORED",
                severity="info",
                message="Sector-specific accounting considerations not reflected in analysis",
                judgment_ids=[j.judgment_id],
                action=f"Consider sector accounting notes: {acct_notes[0][:100]}...",
            ))
        return issues
