"""
cli.py — Command-line interface for agent-core.

Commands:
    agent-core analyze    — Analyze a repo and print discovered roles
    agent-core validate   — Validate a swarm.yaml against the schema
    agent-core run        — Run a named workflow against the current repo
    agent-core init       — Write a starter swarm.yaml into the current directory
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich import print as rprint
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False
    rprint = print  # type: ignore[assignment]

console = Console() if _HAS_RICH else None  # type: ignore[assignment]

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s  %(name)s  %(message)s",
)
logger = logging.getLogger("agent_core.cli")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_analyze(args: argparse.Namespace) -> int:
    from agent_core.repo_analyzer import analyze

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"Error: path does not exist: {root}", file=sys.stderr)
        return 1

    print(f"Analyzing {root} ...")
    meta = analyze(root)

    if _HAS_RICH:
        table = Table(title="Repository analysis", show_header=True, header_style="bold")
        table.add_column("Property", style="cyan")
        table.add_column("Value")
        table.add_row("Languages", ", ".join(meta.primary_languages) or "—")
        table.add_row("Frameworks", ", ".join(meta.frameworks) or "—")
        table.add_row("Test frameworks", ", ".join(meta.test_frameworks) or "—")
        table.add_row("CI systems", ", ".join(meta.ci_systems) or "—")
        table.add_row("Has Docker", str(meta.has_docker))
        table.add_row("Has migrations", str(meta.has_migrations))
        table.add_row("Has OpenAPI spec", str(meta.has_openapi_spec))
        table.add_row("Recommended roles", "\n".join(meta.recommended_roles))
        if meta.custom_role_specs:
            table.add_row("Custom roles", "\n".join(s.name for s in meta.custom_role_specs))
        console.print(table)  # type: ignore[union-attr]
    else:
        print(f"Languages:        {meta.primary_languages}")
        print(f"Frameworks:       {meta.frameworks}")
        print(f"Test frameworks:  {meta.test_frameworks}")
        print(f"CI systems:       {meta.ci_systems}")
        print(f"Recommended roles: {meta.recommended_roles}")
        if meta.custom_role_specs:
            print(f"Custom roles:     {[s.name for s in meta.custom_role_specs]}")

    if args.output:
        out_path = Path(args.output)
        # Write a swarm.yaml pre-populated with discovered roles
        import yaml  # type: ignore[import-untyped]
        roles_data = []
        for role_name in meta.recommended_roles:
            spec = next((s for s in meta.custom_role_specs if s.name == role_name), None)
            if spec:
                roles_data.append(json.loads(spec.model_dump_json()))
        config = {
            "platform": "claude-code",
            "agents": roles_data,
            "token_budget_per_agent": 60000,
            "max_parallel_agents": 1,
            "quality_gate_strict": True,
            "output_dir": ".agent-swarm/outputs",
        }
        out_path.write_text(yaml.dump(config, sort_keys=False, default_flow_style=False))
        print(f"Wrote {out_path}")

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    import yaml  # type: ignore[import-untyped]
    from pydantic import ValidationError
    from agent_core.schemas import SwarmConfig

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: {config_path} not found", file=sys.stderr)
        return 1

    raw = yaml.safe_load(config_path.read_text())
    try:
        cfg = SwarmConfig(**raw)
        print(f"✓ {config_path} is valid")
        print(f"  platform: {cfg.platform}")
        print(f"  agents defined: {len(cfg.agents)}")
        print(f"  token budget: {cfg.token_budget_per_agent:,}")
        print(f"  parallel agents: {cfg.max_parallel_agents}")
        print(f"  strict quality gates: {cfg.quality_gate_strict}")
        return 0
    except ValidationError as exc:
        print(f"✗ Validation failed:\n{exc}", file=sys.stderr)
        return 1


def cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.root) / "swarm.yaml"
    if dest.exists() and not args.force:
        print(f"swarm.yaml already exists at {dest}. Use --force to overwrite.")
        return 1

    template = """\
# swarm.yaml — agent-core configuration
# Run `agent-core analyze --root . --output swarm.yaml` to auto-populate agents.

platform: claude-code   # claude-code | codex | gemini | generic

# Leave empty to auto-discover roles via `agent-core analyze`
agents: []

token_budget_per_agent: 60000
max_parallel_agents: 1
quality_gate_strict: true
output_dir: .agent-swarm/outputs
"""
    dest.write_text(template)
    print(f"Created {dest}")
    print("Next: run `agent-core analyze --root . --output swarm.yaml` to discover roles.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run a named workflow. Requires ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY."""
    import yaml  # type: ignore[import-untyped]
    from agent_core.schemas import SwarmConfig, Platform
    from agent_core.repo_analyzer import analyze

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: {config_path} not found. Run `agent-core init` first.", file=sys.stderr)
        return 1

    raw = yaml.safe_load(config_path.read_text())
    config = SwarmConfig(**raw)

    api_key = _resolve_api_key(config.platform)
    if not api_key:
        platform_env = {
            Platform.CLAUDE_CODE: "ANTHROPIC_API_KEY",
            Platform.CODEX: "OPENAI_API_KEY",
            Platform.GEMINI: "GEMINI_API_KEY",
        }.get(config.platform, "API_KEY")
        print(f"Error: {platform_env} environment variable not set.", file=sys.stderr)
        return 1

    root = Path(args.root).resolve()
    meta = analyze(root)

    if not config.agents:
        config = config.model_copy(update={"agents": meta.custom_role_specs})

    task = args.task
    if not task:
        task = input("Task description: ").strip()
        if not task:
            print("Error: task description is required.", file=sys.stderr)
            return 1

    print(f"Running workflow '{args.workflow}' for task: {task}")
    print(f"Platform: {config.platform} | Roles: {meta.recommended_roles}")

    # For the CLI, run with the discovered roles in sequential mode.
    # Production usage would use the Python API directly for more control.
    from agent_core.orchestrator import run_sequential

    async def _run() -> None:
        all_specs = {s.name: s for s in config.agents}
        workflow_roles = _workflow_role_sequence(args.workflow, list(all_specs.keys()))

        if not workflow_roles:
            print(f"No roles matched for workflow '{args.workflow}'. Available: {list(all_specs)}")
            return

        # Gather all source files for context (optimizer will rank and slice)
        source_files = list(root.rglob("*.py")) + list(root.rglob("*.ts")) + \
                       list(root.rglob("*.java")) + list(root.rglob("*.go"))
        source_files = [f for f in source_files if ".git" not in str(f)
                        and "node_modules" not in str(f) and "__pycache__" not in str(f)]

        chain = [(all_specs[r], source_files) for r in workflow_roles if r in all_specs]
        if not chain:
            print("No matching agent specs found in config for this workflow.")
            return

        results = await run_sequential(
            task_description=task,
            agent_chain=chain,
            config=config,
            api_key=api_key,
            repo_metadata=meta,
        )

        for result in results:
            print(f"\n{'='*60}")
            print(f"Agent: {result.role}  |  Status: {result.status}")
            print(f"Summary: {result.summary}")
            if result.diffs:
                print(f"Files changed: {[str(d.path) for d in result.diffs]}")
            if result.findings:
                for f in result.findings:
                    print(f"  [{f.severity}] {f.file}:{f.line or '?'} — {f.description}")
            if result.status.value == "escalated":
                print(f"ESCALATED: {result.escalation_reason}")
                print("Human decision required. Stopping.")
                return

    asyncio.run(_run())
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_api_key(platform: object) -> str:
    from agent_core.schemas import Platform
    env_map = {
        Platform.CLAUDE_CODE: "ANTHROPIC_API_KEY",
        Platform.CODEX: "OPENAI_API_KEY",
        Platform.GEMINI: "GEMINI_API_KEY",
        Platform.GENERIC: "ANTHROPIC_API_KEY",
    }
    return os.environ.get(env_map.get(platform, ""), "")  # type: ignore[arg-type]


def _workflow_role_sequence(workflow: str, available_roles: list[str]) -> list[str]:
    """Map a workflow name to an ordered list of roles, filtered to what's available."""
    sequences: dict[str, list[str]] = {
        "feature-dev":      ["architect", "implementer", "qa-engineer", "reviewer"],
        "bug-fix":          ["debugger", "reviewer"],
        "code-review":      ["reviewer"],
        "test-generation":  ["qa-engineer", "reviewer"],
    }
    sequence = sequences.get(workflow, [])
    return [r for r in sequence if r in available_roles]


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-core",
        description="Agent swarm configuration and orchestration tool",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # analyze
    p_analyze = sub.add_parser("analyze", help="Analyze a repository and discover agent roles")
    p_analyze.add_argument("--root", default=".", help="Repository root (default: current dir)")
    p_analyze.add_argument("--output", help="Write populated swarm.yaml to this path")

    # validate
    p_validate = sub.add_parser("validate", help="Validate a swarm.yaml file")
    p_validate.add_argument("--config", default="swarm.yaml", help="Path to swarm.yaml")

    # init
    p_init = sub.add_parser("init", help="Create a starter swarm.yaml in the current directory")
    p_init.add_argument("--root", default=".", help="Target directory")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing swarm.yaml")

    # run
    p_run = sub.add_parser("run", help="Run a named workflow against the current repo")
    p_run.add_argument(
        "workflow",
        choices=["feature-dev", "bug-fix", "code-review", "test-generation"],
        help="Workflow to execute",
    )
    p_run.add_argument("--task", help="Task description (prompted if omitted)")
    p_run.add_argument("--root", default=".", help="Repository root")
    p_run.add_argument("--config", default="swarm.yaml", help="Path to swarm.yaml")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    handlers = {
        "analyze":  cmd_analyze,
        "validate": cmd_validate,
        "init":     cmd_init,
        "run":      cmd_run,
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(handler(args))


if __name__ == "__main__":
    main()
