"""
registry.py — Discovers and manages AgentSpecs from YAML/Markdown configurations.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from agent_core.schemas import AgentSpec, EscalationPolicy, QualityGate, ToolPermission

logger = logging.getLogger(__name__)

class AgentRegistry:
    """
    Loads agent configurations from a given directory (e.g., 'agents/').
    YAML files define the schemas.
    """
    def __init__(self, agents_dir: Path | str = "agents"):
        self.agents_dir = Path(agents_dir)
        self._registry: dict[str, AgentSpec] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self.agents_dir.exists():
            logger.warning("Agents directory %s not found. Registry empty.", self.agents_dir)
            return

        for ext in ("*.yaml", "*.yml"):
            for config_file in self.agents_dir.glob(ext):
                try:
                    with open(config_file, "r") as f:
                        data = yaml.safe_load(f)
                    
                    if not data or "name" not in data or "role" not in data:
                        logger.warning("Skipping invalid config %s", config_file)
                        continue
                    
                    # Convert raw dicts to Pydantic models for nested fields
                    quality_gates = [QualityGate(**qg) for qg in data.get("quality_gates", [])]
                    tools_allowed = [ToolPermission(**tp) for tp in data.get("tools_allowed", [])]
                    
                    escalation_data = data.get("escalation", {})
                    # C-02 fix: EscalationPolicy shouldn't be init with empty dict if schema strict
                    escalation = EscalationPolicy(**escalation_data) if escalation_data else EscalationPolicy()
                    
                    spec = AgentSpec(
                        name=data["name"],
                        role=data["role"],
                        description=data.get("description", ""),
                        responsibilities=data.get("responsibilities", ["Execute task"]),
                        quality_gates=quality_gates if quality_gates else [QualityGate(description="Standard validation")],
                        tools_allowed=tools_allowed,
                        out_of_scope=data.get("out_of_scope", []),
                        escalation=escalation,
                        output_json_schema=data.get("output_json_schema")
                    )
                    self._registry[spec.role] = spec
                    logger.debug("Loaded agent role: %s", spec.role)
                except Exception as e:
                    logger.error("Failed to parse %s: %s", config_file, e)

    def get(self, role: str) -> AgentSpec:
        """Get an agent by its role. Raises ValueError if not found."""
        if role not in self._registry:
            raise ValueError(f"Agent role '{role}' not found in registry at {self.agents_dir}")
        return self._registry[role]

    def all(self) -> list[AgentSpec]:
        """Return all registered agents."""
        return list(self._registry.values())

# Per-directory cache — keyed by resolved absolute path to prevent the stale
# singleton bug where a second call with a different path returns the first result.
_registry_cache: dict[str, AgentRegistry] = {}


def get_default_registry(agents_dir: str = "agents") -> AgentRegistry:
    resolved = str(Path(agents_dir).resolve())
    if resolved not in _registry_cache:
        _registry_cache[resolved] = AgentRegistry(resolved)
    return _registry_cache[resolved]
