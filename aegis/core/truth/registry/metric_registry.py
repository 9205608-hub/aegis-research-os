"""Metric Registry — the governance system for all metric definitions.

Not a dictionary, but a versioned, governed registry.
All metrics must be registered here before entering the system.
"""

from aegis.data_contracts import MetricDefinition


class MetricRegistryError(Exception):
    """Raised when metric registry operations fail."""


class MetricRegistry:
    """In-memory metric registry with governance enforcement.

    Rules:
    - All metrics must have unique definition_id.
    - Deprecated definitions must retain supersession chain.
    - Report layer can only reference publishable=True definitions.
    - Cross-standard usage must have cross_standard_notes.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, MetricDefinition] = {}

    def register(self, definition: MetricDefinition) -> None:
        """Register a new metric definition."""
        if definition.definition_id in self._definitions:
            existing = self._definitions[definition.definition_id]
            if existing.formula_version >= definition.formula_version:
                raise MetricRegistryError(
                    f"Definition '{definition.definition_id}' already registered "
                    f"with formula_version={existing.formula_version}. "
                    f"New version must be higher."
                )
        if definition.supersedes and definition.supersedes not in self._definitions:
            raise MetricRegistryError(
                f"Superseded definition '{definition.supersedes}' not found in registry."
            )
        self._definitions[definition.definition_id] = definition

    def get(self, definition_id: str) -> MetricDefinition:
        """Get a metric definition by ID."""
        if definition_id not in self._definitions:
            raise MetricRegistryError(
                f"Definition '{definition_id}' not found. "
                f"All metrics must be registered before use."
            )
        return self._definitions[definition_id]

    def get_publishable(self, definition_id: str) -> MetricDefinition:
        """Get a metric definition, ensuring it is publishable."""
        defn = self.get(definition_id)
        if not defn.publishable:
            raise MetricRegistryError(
                f"Definition '{definition_id}' is not publishable (status={defn.definition_status})."
            )
        return defn

    def list_all(self) -> list[MetricDefinition]:
        """List all registered definitions."""
        return list(self._definitions.values())

    def list_by_sector(self, sector: str) -> list[MetricDefinition]:
        """List definitions applicable to a given sector."""
        return [
            d for d in self._definitions.values()
            if not d.sector_applicability or sector in d.sector_applicability
        ]

    @property
    def version(self) -> str:
        """Registry version based on count and max formula version."""
        if not self._definitions:
            return "metric_v0_empty"
        max_fv = max(d.formula_version for d in self._definitions.values())
        return f"metric_v{len(self._definitions)}_{max_fv}"
