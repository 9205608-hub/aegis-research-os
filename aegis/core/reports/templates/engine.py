"""Template Engine — Jinja2-based report generation.

Supports multiple output formats: HTML, Markdown, plain text.
Templates are stored as .j2 files in the templates directory.

Usage:
    engine = TemplateEngine()
    html = engine.render("investment_report", format="html", context={...})
    md = engine.render("investment_report", format="markdown", context={...})
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape


class TemplateEngine:
    """Multi-format report template engine.

    Templates are loaded from the templates directory.
    Each template can have format-specific variants:
        investment_report.html.j2
        investment_report.md.j2
        comparison_report.html.j2
    """

    def __init__(self, template_dir: Path | str | None = None) -> None:
        self._template_dir = Path(template_dir) if template_dir else Path(__file__).parent
        self._env = Environment(
            loader=FileSystemLoader(str(self._template_dir)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # Register custom filters
        self._env.filters["format_number"] = _format_number
        self._env.filters["format_pct"] = _format_pct
        self._env.filters["format_currency"] = _format_currency
        self._env.filters["format_multiple"] = _format_multiple

    def render(
        self,
        template_name: str,
        format: str = "html",
        context: dict[str, Any] | None = None,
    ) -> str:
        """Render a template with the given context.

        Args:
            template_name: Base template name (e.g., "investment_report")
            format: Output format ("html", "markdown", "text")
            context: Template variables

        Returns:
            Rendered string
        """
        # Map format names to file extensions
        ext_map = {"html": "html", "markdown": "md", "md": "md", "text": "txt"}
        ext = ext_map.get(format, format)
        filename = f"{template_name}.{ext}.j2"
        template = self._env.get_template(filename)

        ctx = context or {}
        ctx["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        return template.render(**ctx)

    def render_investment_report(
        self,
        decision: Any,
        computed_metrics: dict[str, float],
        market_data: dict[str, float],
        agent_judgments: list[Any],
        critic_results: list[Any],
        meta_facts: dict[str, Any] | None = None,
        dcf_projections: list[dict] | None = None,
        sensitivity_rankings: list[dict] | None = None,
        format: str = "html",
    ) -> str:
        """Render an investment report with standard context extraction.

        Convenience method that extracts context from the pipeline objects.
        """
        entity = getattr(decision, "entity_id", "Unknown")
        run_id = getattr(decision, "run_id", "")

        # Scenarios
        bear = getattr(decision, "bear_case_value", 0) or 0
        base = getattr(decision, "base_case_value", 0) or 0
        bull = getattr(decision, "bull_case_value", 0) or 0
        price = market_data.get("current_price", 0)

        # Edge assessment
        edge = getattr(decision, "edge_assessment", None)
        edge_dict = {}
        if edge:
            edge_dict = {
                "type": str(getattr(edge, "primary_edge_type", "N/A")),
                "source": getattr(edge, "edge_source", ""),
                "durability": str(getattr(edge, "edge_durability", "")),
                "why_market_wrong": getattr(edge, "why_market_is_wrong", ""),
                "decay_trigger": getattr(edge, "edge_decay_trigger", ""),
            }

        # Agents
        agents = []
        for j in agent_judgments:
            agents.append({
                "name": j.agent_name,
                "observations": len(j.observations),
                "inferences": len(j.inferences),
                "counterarguments": len(j.counterarguments),
                "top_inferences": [inf.text[:200] for inf in j.inferences[:3]],
            })

        # Critics
        critics = []
        for cr in critic_results:
            critics.append({
                "type": cr.critic_type,
                "issues": len(cr.issues),
                "block": cr.block_publish,
                "risk": cr.overall_risk,
                "issue_details": [
                    {"code": i.issue_code, "severity": str(i.severity), "message": i.message[:150]}
                    for i in cr.issues
                ],
            })

        context = {
            "entity": entity,
            "run_id": run_id,
            "status": getattr(decision, "publishing_status", "draft"),
            "confidence": getattr(decision, "confidence_bucket", "medium"),
            "bias_status": getattr(decision, "bias_check_status", "unknown"),
            "bear": bear,
            "base": base,
            "bull": bull,
            "price": price,
            "edge": edge_dict,
            "metrics": computed_metrics,
            "facts": meta_facts or {},
            "agents": agents,
            "critics": critics,
            "dcf_projections": dcf_projections or [],
            "sensitivity_rankings": sensitivity_rankings or [],
            "kill_criteria": getattr(decision, "kill_criteria", []),
            "monitorables": getattr(decision, "monitorables", []),
            "core_thesis": getattr(decision, "core_thesis", ""),
            "variant": getattr(decision, "variant", ""),
        }

        return self.render("investment_report", format=format, context=context)

    def render_comparison_report(
        self,
        comparison_matrix: Any,
        per_entity_metrics: dict[str, dict[str, float]],
        per_entity_dcf: dict[str, float] | None = None,
        format: str = "html",
    ) -> str:
        """Render a multi-entity comparison report."""
        dims = []
        for d in getattr(comparison_matrix, "dimensions", []):
            dims.append({
                "name": d.dimension,
                "rankings": d.rankings,
                "values": d.values,
            })

        context = {
            "theme": getattr(comparison_matrix, "theme", ""),
            "entity_ids": getattr(comparison_matrix, "entity_ids", []),
            "dimensions": dims,
            "top_picks": getattr(comparison_matrix, "top_picks", []),
            "rationale": getattr(comparison_matrix, "top_pick_rationale", ""),
            "per_entity_metrics": per_entity_metrics,
            "per_entity_dcf": per_entity_dcf or {},
            "relative_valuation": {
                "metric": getattr(comparison_matrix.relative_valuation, "metric", ""),
                "values": getattr(comparison_matrix.relative_valuation, "values", {}),
                "median": getattr(comparison_matrix.relative_valuation, "sector_median", 0),
            } if hasattr(comparison_matrix, "relative_valuation") else {},
            "risks": getattr(comparison_matrix, "cross_entity_risks", []),
        }

        return self.render("comparison_report", format=format, context=context)

    def list_templates(self) -> list[str]:
        """List all available templates."""
        return [
            p.stem.rsplit(".", 1)[0]  # Remove format extension
            for p in self._template_dir.glob("*.j2")
        ]


def _format_number(
    value: float | int | None,
    decimals: int = 1,
    symbol: str = "$",
) -> str:
    """Format a number with billions/millions suffix.

    TODO-Y4 (2026-05-06): default `$` is preserved for back-compat, but the
    helper now accepts an explicit currency symbol so A-share callers can
    pass `¥`. The single source of truth for which symbol to use is
    `aegis.core._display.resolve_display(facts).symbol`.
    """
    if value is None:
        return "N/A"
    abs_val = abs(value)
    if abs_val >= 1e12:
        return f"{symbol}{value/1e12:.{decimals}f}T"
    if abs_val >= 1e9:
        return f"{symbol}{value/1e9:.{decimals}f}B"
    if abs_val >= 1e6:
        return f"{symbol}{value/1e6:.{decimals}f}M"
    if abs_val >= 1e3:
        return f"{symbol}{value/1e3:.{decimals}f}K"
    return f"{symbol}{value:.{decimals}f}"


def _format_pct(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}%}"


def _format_currency(
    value: float | None,
    decimals: int = 0,
    symbol: str = "$",
) -> str:
    if value is None:
        return "N/A"
    return f"{symbol}{value:,.{decimals}f}"


def _format_multiple(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}x"
