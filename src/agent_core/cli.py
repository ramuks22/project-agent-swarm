"""
cli.py — Command-line interface for agent-core.

Commands:
    agent-core analyze    — Analyze a repo and print discovered roles
    agent-core validate   — Validate a swarm.yaml against the schema
    agent-core auto       — Run the autonomous coordinator flow against the current repo
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
from typing import Any

try:
    from rich.console import Console
    from rich.table import Table

    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

console = Console() if _HAS_RICH else None

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

    if console is not None:
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
        if meta.agent_specs:
            table.add_row(
                "Full Agent Specs", "\n".join(f"{s.name} ({s.role})" for s in meta.agent_specs)
            )
        console.print(table)
    else:
        print(f"Languages:        {meta.primary_languages}")
        print(f"Frameworks:       {meta.frameworks}")
        print(f"Test frameworks:  {meta.test_frameworks}")
        print(f"CI systems:       {meta.ci_systems}")
        print(f"Recommended roles: {meta.recommended_roles}")
        if meta.agent_specs:
            print(f"Full Agent Specs:  {[s.name for s in meta.agent_specs]}")

    if args.output:
        out_path = Path(args.output)
        # Write a swarm.yaml pre-populated with discovered roles
        import yaml

        roles_data = []
        for spec in meta.agent_specs:
            roles_data.append(json.loads(spec.model_dump_json()))
        config = {
            "platform": "claude-code",
            "agents": roles_data,
            "token_budget_per_agent": 60000,
            "max_parallel_agents": 1,
            "quality_gate_strict": True,
            "output_dir": ".swarm/outputs",
        }
        out_path.write_text(yaml.dump(config, sort_keys=False, default_flow_style=False))
        _ensure_gitignore(args.root, ".swarm/")
        print(f"Wrote {out_path}")

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    import yaml
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
output_dir: .swarm/outputs
"""
    dest.write_text(template)
    _ensure_gitignore(args.root, ".swarm/")
    print(f"Created {dest}")
    print("Next: run `agent-core analyze --root . --output swarm.yaml` to discover roles.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run a named workflow. Requires ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY."""
    import yaml

    from agent_core.repo_analyzer import analyze
    from agent_core.schemas import Platform, SwarmConfig

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
        config = config.model_copy(update={"agents": meta.agent_specs})

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
        all_specs = {str(s.role).lower(): s for s in config.agents}
        workflow_roles = _workflow_role_sequence(args.workflow, list(all_specs.keys()))

        if not workflow_roles:
            print(f"No roles matched for workflow '{args.workflow}'. Available: {list(all_specs)}")
            return

        # Gather all source files for context (optimizer will rank and slice)
        from agent_core.context_optimizer import get_eligible_candidates

        source_files = get_eligible_candidates(root)

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
            print(f"\n{'=' * 60}")
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


def _print_autonomous_summary(state: object) -> None:
    from agent_core.schemas import SwarmRunState

    run_state = state if isinstance(state, SwarmRunState) else SwarmRunState.model_validate(state)
    print(f"\nTask:   {run_state.task_id}")
    print(f"Flow:   {run_state.plan.flow}")
    print(f"Phase:  {run_state.current_phase}")
    print(f"Status: {run_state.status}")
    if run_state.pending_gate:
        print(f"Pending gate: {run_state.pending_gate.gate_type}")
    if run_state.completion_summary:
        print(f"Summary: {run_state.completion_summary}")
    if run_state.escalation_reason:
        print(f"Escalation: {run_state.escalation_reason}")


def cmd_auto(args: argparse.Namespace) -> int:
    """Run the autonomous coordinator flow against the current repo."""
    import yaml

    from agent_core.orchestrator import resume_autonomous, run_autonomous
    from agent_core.repo_analyzer import analyze
    from agent_core.schemas import (
        ApprovalMode,
        GateDecision,
        GateType,
        Platform,
        SwarmConfig,
        TaskStatus,
    )

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
        config = config.model_copy(update={"agents": meta.agent_specs})

    task = args.task
    if not task:
        task = input("Task description: ").strip()
        if not task:
            print("Error: task description is required.", file=sys.stderr)
            return 1

    approval_mode = ApprovalMode(args.approval_mode)
    interactive = sys.stdin.isatty()
    if approval_mode == ApprovalMode.MAJOR_GATES and not interactive:
        print(
            "Error: --approval-mode major_gates requires an interactive TTY.",
            file=sys.stderr,
        )
        return 1

    print(f"Running autonomous flow for task: {task}")
    print(f"Platform: {config.platform} | Roles: {meta.recommended_roles}")

    async def _run() -> int:
        state = await run_autonomous(
            task_description=task,
            config=config,
            api_key=api_key,
            repo_root=root,
            repo_metadata=meta,
            approval_mode=approval_mode,
            execute=args.execute,
        )

        while state.status == TaskStatus.PENDING and state.pending_gate:
            if not interactive:
                _print_autonomous_summary(state)
                print(
                    "Autonomous run is waiting on a gate and cannot continue without interactive input.",
                    file=sys.stderr,
                )
                return 1
            _print_autonomous_summary(state)
            if state.pending_gate.gate_type == GateType.CLARIFICATION_REQUIRED:
                for question in state.plan.clarification_questions:
                    print(f"- {question.prompt}")
                response = input("Clarification: ").strip()
                decision = GateDecision.APPROVE if response else GateDecision.REJECT
                comments = response or "Clarification was not provided."
            else:
                response = input(f"Approve gate {state.pending_gate.gate_type}? [y/N]: ").strip().lower()
                decision = GateDecision.APPROVE if response in {"y", "yes"} else GateDecision.REJECT
                comments = ""
                if decision == GateDecision.REJECT:
                    comments = input("Reason: ").strip()

            state = await resume_autonomous(
                task_id=state.task_id,
                config=config,
                api_key=api_key,
                repo_root=root,
                decision=decision,
                comments=comments,
            )

        _print_autonomous_summary(state)
        return 0 if state.status == TaskStatus.DONE else 1

    return asyncio.run(_run())


def cmd_optimizer_verify(args: argparse.Namespace) -> int:
    from agent_core.context_optimizer import (
        _is_config_file,
        get_eligible_candidates,
        pass_1_metadata_score,
        pass_2_content_refinement,
        slice_to_budget,
    )

    task_desc = args.task
    budget = args.budget
    root = Path(args.root).resolve()
    if not root.exists():
        print(f"Error: path does not exist: {root}", file=sys.stderr)
        return 1

    print(f"Running optimizer verification for task: '{task_desc}' (budget: {budget})\\n")

    candidates = get_eligible_candidates(root)
    print(f"Candidates found (after directory exclusions): {len(candidates)}")

    scored_pass_1 = pass_1_metadata_score(
        task_description=task_desc, candidate_paths=candidates, agent_role="", recently_changed=None
    )

    print(f"Pass-1 files logically scored: {len(scored_pass_1)}")

    scored_pass_2 = pass_2_content_refinement(
        candidates=scored_pass_1,
        task_description=task_desc,
        error_trace=None,
        max_reads=25,
        preview_bytes=8192,
    )

    previews_read = sum(1 for sf in scored_pass_2 if sf.content_loaded)
    print(f"Pass-2 bounded previews read: {previews_read}")

    zeros = sum(1 for sf in scored_pass_2 if sf.score <= 0)
    print(f"Zero-scored files cleanly filtered: {zeros}")

    selected = slice_to_budget(scored_pass_2, token_budget=budget, reserve_for_prompt=0)

    selected_count = len(selected)
    artifacts = sum(1 for sf in selected if sf.score < 0)
    configs = sum(1 for sf in selected if _is_config_file(sf.path))
    truncated_count = sum(1 for sf in selected if sf.truncated)
    total_estimated = sum(sf.token_count for sf in selected)

    print("\\n--- Selection Summary ---")
    print(f"Total Selected: {selected_count}")
    print(f"Truncated Files: {truncated_count}")
    print(f"Config Files Included: {configs}")
    print(f"Artifact Files Included: {artifacts} (Target: 0)")
    print(
        f"Total Token Selection Estimate: {total_estimated} / {budget} ({budget - total_estimated} remaining)"
    )

    print("\\n--- Top Selected Candidates ---")
    if console is not None:
        table = Table(title="Optimizer Top Selections", show_header=True, header_style="bold")
        table.add_column("Score", justify="right", style="green")
        table.add_column("Tokens", justify="right", style="cyan")
        table.add_column("Path")
        for sf in selected:
            state = "[Trunc]" if sf.truncated else ""
            table.add_row(
                f"{sf.score}", f"{state} {sf.token_count}", str(sf.path.relative_to(root))
            )
        console.print(table)
    else:
        for sf in selected:
            state = "[Trunc]" if sf.truncated else ""
            print(
                f"[{sf.score:^5}] {state:<7} {sf.token_count:>5} tokens | {sf.path.relative_to(root)}"
            )

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_api_key(platform: Any) -> str:
    from agent_core.schemas import Platform

    env_map = {
        Platform.CLAUDE_CODE: "ANTHROPIC_API_KEY",
        Platform.CODEX: "OPENAI_API_KEY",
        Platform.OPENAI: "OPENAI_API_KEY",
        Platform.GEMINI: "GEMINI_API_KEY",
        Platform.GENERIC: "ANTHROPIC_API_KEY",
    }
    if not isinstance(platform, Platform):
        return ""

    resolved = os.environ.get(env_map.get(platform, ""), "")
    if resolved:
        return resolved

    if platform == Platform.GEMINI:
        legacy = os.environ.get("GOOGLE_API_KEY", "")
        if legacy:
            logger.warning("GOOGLE_API_KEY is deprecated. Prefer GEMINI_API_KEY.")
        return legacy

    return ""


def _ensure_gitignore(root: str | Path, pattern: str) -> None:
    """Ensure the given pattern is in the host repo's .gitignore."""
    gitignore_path = Path(root) / ".gitignore"
    if not gitignore_path.exists():
        return

    content = gitignore_path.read_text(errors="ignore")
    if pattern not in content:
        with open(gitignore_path, "a") as f:
            if not content.endswith("\n") and content:
                f.write("\n")
            f.write(f"\n# Agent Swarm state\n{pattern}\n")
        print(f"Added {pattern} to {gitignore_path}")


def _workflow_role_sequence(workflow: str, available_roles: list[str]) -> list[str]:
    """Map a workflow name to an ordered list of roles, filtered to what's available."""
    sequences: dict[str, list[str]] = {
        "feature-dev": ["architect", "implementer", "qa-engineer", "reviewer"],
        "bug-fix": ["debugger", "reviewer"],
        "code-review": ["reviewer"],
        "test-generation": ["qa-engineer", "reviewer"],
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

    # auto
    p_auto = sub.add_parser("auto", help="Run the autonomous coordinator flow")
    p_auto.add_argument("--task", help="Task description (prompted if omitted)")
    p_auto.add_argument("--root", default=".", help="Repository root")
    p_auto.add_argument("--config", default="swarm.yaml", help="Path to swarm.yaml")
    p_auto.add_argument(
        "--approval-mode",
        default="major_gates",
        choices=["major_gates", "none"],
        help="Whether to pause for human approval at major gates",
    )
    p_auto.add_argument(
        "--execute",
        action="store_true",
        help="Enable the optional execution layer to apply diffs and run allowlisted commands",
    )

    # optimizer-verify
    p_opt = sub.add_parser("optimizer-verify", help="Debug and verify the 2-pass context optimizer")
    p_opt.add_argument("task", help="Task description string")
    p_opt.add_argument(
        "budget", nargs="?", type=int, default=8000, help="Token budget testing limit"
    )
    p_opt.add_argument("--root", default=".", help="Repository root")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    handlers = {
        "analyze": cmd_analyze,
        "validate": cmd_validate,
        "init": cmd_init,
        "run": cmd_run,
        "auto": cmd_auto,
        "optimizer-verify": cmd_optimizer_verify,
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(handler(args))


if __name__ == "__main__":
    main()
