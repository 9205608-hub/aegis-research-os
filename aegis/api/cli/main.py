"""CLI Interface — Section 28.3.

Typer-based CLI for Aegis Research OS.
"""

from __future__ import annotations

import json
from typing import Optional

try:
    import typer
    app = typer.Typer(name="aegis", help="Aegis Research OS CLI")
except ImportError:
    # Typer not installed — provide a minimal fallback
    import argparse

    class _MinimalApp:
        def command(self, *a, **kw):
            def decorator(fn):
                return fn
            return decorator

        def __call__(self):
            print("Install typer for full CLI: pip install typer")

    app = _MinimalApp()


@app.command()
def research(
    entity: str,
    mode: str = "single_entity",
    theme: str = "",
):
    """Start a research run for an entity."""
    from aegis.core.planner import ResearchModeRouter, ResearchRequest
    from aegis.data_contracts.common import ResearchMode

    router = ResearchModeRouter()
    plan = router.route(ResearchRequest(
        research_mode=ResearchMode(mode),
        entity_ids=[e.strip() for e in entity.split(",")],
        theme=theme,
    ))
    print(f"Run ID: {plan.run_id}")
    print(f"Mode: {plan.research_mode.value}")
    print(f"Entities: {plan.entity_ids}")
    print(f"Phases: {[p.phase_name for p in plan.phases]}")


@app.command()
def metrics(entity: str):
    """Show registered metrics for an entity."""
    from aegis.core.truth.registry.metric_registry import MetricRegistry
    from aegis.core.truth.registry.seed_metrics import seed_core_metrics

    registry = MetricRegistry()
    seed_core_metrics(registry)
    all_metrics = registry.list_by_sector("all")
    print(f"Registered metrics ({len(all_metrics)}):")
    for m in all_metrics:
        print(f"  {m.metric_name}: {m.display_name} (v{m.formula_version})")


@app.command()
def health():
    """Check system health."""
    print("Aegis Research OS v0.1.0")
    print("Status: operational")

    # Check imports
    modules = [
        "aegis.core.agents",
        "aegis.core.critics",
        "aegis.core.decision_engine",
        "aegis.core.evals",
        "aegis.core.events",
        "aegis.core.macro",
        "aegis.core.market_expectations",
        "aegis.core.planner",
        "aegis.core.portfolio",
        "aegis.core.publish_gate",
        "aegis.core.thesis_manager",
        "aegis.core.truth.formulas.formula_engine",
    ]
    ok = 0
    for mod in modules:
        try:
            __import__(mod)
            ok += 1
        except ImportError as e:
            print(f"  FAIL: {mod} — {e}")
    print(f"Modules: {ok}/{len(modules)} loaded")


if __name__ == "__main__":
    if callable(app):
        app()
