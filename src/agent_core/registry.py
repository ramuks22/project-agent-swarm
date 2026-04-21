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

# Internal location of standard role definitions
_BUILTIN_AGENTS_DIR = Path(__file__).parent / "agents"

class AgentRegistry:
    """
    Loads agent configurations from a given directory (e.g., 'agents/').
    YAML files define the schemas.
    """
    def __init__(self, agents_dir: Path | str | None = None):
        self.agents_dir = Path(agents_dir) if agents_dir else None
        self._registry: dict[str, AgentSpec] = {}
        
        # 1. Load factory defaults (built-in)
        self._load_from_dir(_BUILTIN_AGENTS_DIR)
        
        # 2. Load user overrides (local agents/ folder), if provided
        if self.agents_dir:
            self._load_from_dir(self.agents_dir)

    def _load_from_dir(self, directory: Path) -> None:
        if not directory.exists():
            return

        for ext in ("*.yaml", "*.yml"):
            for config_file in directory.glob(ext):
                try:
                    with open(config_file, "r") as f:
                        data = yaml.safe_load(f)
                    
                    if not data or "name" not in data or "role" not in data:
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
                    # Overwrites built-ins if filenames/roles match (Law of Precedence)
                    self._registry[str(spec.role).lower()] = spec
                    logger.debug("Loaded agent role: %s from %s", spec.role, directory)
                except Exception as e:
                    logger.error("Failed to parse %s: %s", config_file, e)

    def get(self, role: str) -> AgentSpec:
        """Get an agent by its role identifier. Raises ValueError if not found."""
        key = str(role).lower()
        if key not in self._registry:
            raise ValueError(f"Agent role '{role}' not found in registry (local: {self.agents_dir})")
        return self._registry[key]

    def all(self) -> list[AgentSpec]:
        """Return all registered agents."""
        return list(self._registry.values())

# Per-directory cache — keyed by resolved absolute path to prevent the stale
# singleton bug where a second call with a different path returns the first result.
_registry_cache: dict[str, AgentRegistry] = {}


def get_default_registry(agents_dir: str = "agents") -> AgentRegistry:
    """
    Returns an AgentRegistry that prioritizes the provided directory 
    over the built-in package agents.
    """
    path_obj = Path(agents_dir)
    resolved = str(path_obj.resolve()) if path_obj.exists() else "BUILTIN_ONLY"
    
    if resolved not in _registry_cache:
        # If the requested directory doesn't exist, we still want built-ins
        actual_dir = path_obj if path_obj.exists() else None
        _registry_cache[resolved] = AgentRegistry(actual_dir)
    return _registry_cache[resolved]
