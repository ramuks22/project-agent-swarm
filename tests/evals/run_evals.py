"""
run_evals.py — Executes Golden Dataset evaluations against the Swarm Engine.
"""

import argparse
import asyncio
import json
from pathlib import Path

from agent_core.drivers.base import BaseAgentDriver
from agent_core.orchestrator import run_sequential
from agent_core.registry import get_default_registry
from agent_core.schemas import Platform, SwarmConfig

EVAL_DATA_PATH = Path(__file__).parent / "golden_tasks.json"


class MockDriver(BaseAgentDriver):
    """A cheap driver that fakes success instantly to verify wiring."""

    def __init__(self, spec, **kwargs):
        super().__init__(spec, "mock_key", **kwargs)

    def _build_messages(self, context):
        return []

    async def _call_api(self, messages, context):
        return "{}"

    def _parse_response(self, raw, context):
        # Return a valid StructuredResult — note FileFinding was renamed ReviewFinding
        from agent_core.schemas import ReviewFinding, StructuredResult

        return StructuredResult(
            task_id=context.task_id,
            role=self.spec.role,
            status="done",
            summary=f"Mocked output for {self.spec.name}",
            findings=[ReviewFinding(file="test.py", severity="nit", description="Mock finding")],
            suggested_commands=[],
            payload={},
        )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--invoke-real", action="store_true", help="Use real configured model instead of MockDriver"
    )
    parser.add_argument(
        "--platform", type=str, default="gemini", help="Platform to use if --invoke-real is set"
    )
    args = parser.parse_args()

    with open(EVAL_DATA_PATH) as f:
        tasks = json.load(f)

    # Derive agents dir from repo root (2 levels up from tests/evals/)
    repo_root = Path(__file__).resolve().parent.parent.parent
    registry = get_default_registry(agents_dir=str(repo_root / "agents"))
    print(f"Running {len(tasks)} evaluations...")

    # Patch registry for MockDriver if needed
    if not args.invoke_real:
        import agent_core.orchestrator

        def mock_get_driver(platform, spec, api_key, **kwargs):
            return MockDriver(spec, **kwargs)

        agent_core.orchestrator._get_driver = mock_get_driver
        print("Using MockDriver for cost-free pipeline validation.")
    else:
        print(f"Using REAL {args.platform} models for key scenario validation.")

    platform_map = {
        "gemini": Platform.GEMINI,
        "claude": Platform.CLAUDE_CODE,
        "openai": Platform.OPENAI,
    }
    platform_enum = platform_map.get(args.platform, Platform.GENERIC)

    for idx, eval_task in enumerate(tasks):
        print(f"\n--- Eval {idx + 1}/{len(tasks)}: {eval_task['id']} ---")
        try:
            agent = registry.get(eval_task["expected_role"])
            config = SwarmConfig(platform=platform_enum, agents=[agent])

            results = await run_sequential(
                task_description=eval_task["description"],
                agent_chain=[(agent, [])],
                config=config,
                api_key="mock" if not args.invoke_real else "real_key",
            )

            if results and results[-1].role == eval_task["expected_role"]:
                print(f"✅ PASSED: Expected role {eval_task['expected_role']} was executed.")
            else:
                print("❌ FAILED: Unexpected execution path.")

        except ValueError as e:  # Agent not found in registry
            # If the user hasn't created the yaml files for the test roles yet, mock it
            print(f"⚠️ SKIPPED (Registry error): {e}")


if __name__ == "__main__":
    asyncio.run(main())
