"""
example_usage.py — How a host repository uses agent_core.

This file is for documentation purposes. It shows the complete integration
pattern: analyze repo → load config → run a task → consume structured output.

Install: pip install agent-core
Or as a submodule: git submodule add https://github.com/your-org/agent-swarm .agent-swarm
                   pip install -e .agent-swarm/
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import agent_core


async def main() -> None:
    repo_root = Path(".")

    # -----------------------------------------------------------------------
    # Step 1: Analyze the repo to discover appropriate agent roles.
    # This replaces hardcoded role lists — the roles emerge from the codebase.
    # -----------------------------------------------------------------------
    print("Analyzing repository...")
    metadata = agent_core.analyze(repo_root)
    print(f"Languages:  {metadata.primary_languages}")
    print(f"Frameworks: {metadata.frameworks}")
    print(f"Roles discovered: {metadata.recommended_roles}")
    print(f"Custom specs: {[s.name for s in metadata.custom_role_specs]}")

    # -----------------------------------------------------------------------
    # Step 2: Load swarm config.
    # If agents list is empty, the orchestrator uses the analyzer's specs.
    # -----------------------------------------------------------------------
    import yaml  # pyyaml
    with open("swarm.yaml") as f:
        raw = yaml.safe_load(f)

    config = agent_core.SwarmConfig(**raw)

    # If config has no explicit agents, use the discovered specs
    if not config.agents:
        config.agents = metadata.agent_specs
        print(f"Using {len(metadata.recommended_roles)} auto-discovered roles.")

    # Load the markdown templates for building system prompts
    template_dir = Path(".agent-swarm/agents")
    templates = agent_core.load_role_templates(template_dir)

    # -----------------------------------------------------------------------
    # Step 3: Find the specs for the roles you need.
    # Built-in roles are available from the package; custom roles come from metadata.
    # -----------------------------------------------------------------------
    all_specs = {s.name: s for s in config.agents}

    # For this example: architect → implementer → qa-engineer chain
    # Scope files to what's relevant for this task (not the whole repo)
    changed_files = [
        Path("src/auth/token_validator.py"),
        Path("src/auth/models.py"),
    ]

    architect_spec = all_specs.get("architect")
    implementer_spec = all_specs.get("implementer")
    qa_spec = all_specs.get("qa-engineer")

    if not (architect_spec and implementer_spec and qa_spec):
        print("Required roles not found in discovered specs. Check swarm.yaml.")
        return

    # -----------------------------------------------------------------------
    # Step 4: Run the chain.
    # State is passed explicitly — the package holds nothing between calls.
    # -----------------------------------------------------------------------
    api_key = os.environ["ANTHROPIC_API_KEY"]  # or OPENAI_API_KEY, GEMINI_API_KEY

    results = await agent_core.run_sequential(
        task_description=(
            "Add JWT expiry validation to the token validator. "
            "Tokens older than 24 hours should be rejected with a 401."
        ),
        agent_chain=[
            (architect_spec, changed_files),
            (implementer_spec, changed_files),
            (qa_spec, changed_files),
        ],
        config=config,
        api_key=api_key,
        repo_metadata=metadata,
    )

    # -----------------------------------------------------------------------
    # Step 5: Consume StructuredResult objects.
    # All outputs are typed — no string parsing in the host repo pipeline.
    # -----------------------------------------------------------------------
    for result in results:
        print(f"\n--- {result.role} ({result.status}) ---")
        print(f"Summary: {result.summary}")

        if result.diffs:
            print("Diffs:")
            for diff in result.diffs:
                print(f"  {diff.operation}: {diff.path}")

        if result.findings:
            print("Findings:")
            for f in result.findings:
                print(f"  [{f.severity}] {f.file}: {f.description}")

        if result.suggested_commands:
            print("Suggested commands (NOT auto-executed):")
            for cmd in result.suggested_commands:
                print(f"  $ {cmd}")

        if result.status == agent_core.TaskStatus.ESCALATED:
            print(f"ESCALATED: {result.escalation_reason}")
            print("Human decision required. Halting pipeline.")
            break


if __name__ == "__main__":
    asyncio.run(main())
