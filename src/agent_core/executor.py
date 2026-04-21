"""
executor.py — Optional workspace execution layer for autonomous runs.

The orchestrator remains a coordinator. This module applies diffs and runs
allowlisted verification commands when `execute=true` is explicitly enabled.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import tempfile
from pathlib import Path

from agent_core.schemas import (
    ExecutorCommandResult,
    ExecutorOutcome,
    RepoMetadata,
    StructuredResult,
    TaskStatus,
)
from agent_core.security.tool_sandbox import is_command_safe

logger = logging.getLogger(__name__)

_ALLOWED_COMMAND_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("pytest",),
    ("python", "-m", "pytest"),
    ("uv", "run", "pytest"),
    ("ruff", "check"),
    ("uv", "run", "ruff", "check"),
    ("mypy",),
    ("uv", "run", "mypy"),
    ("python", "-m", "unittest"),
    ("npm", "test"),
    ("npm", "run", "test"),
    ("go", "test"),
    ("cargo", "test"),
    ("terraform", "validate"),
    ("docker", "compose", "config"),
)


class AutonomousExecutor:
    """Applies diffs and runs verification commands in the repo root."""

    def __init__(self, repo_root: Path, repo_metadata: RepoMetadata | None = None) -> None:
        self.repo_root = repo_root
        self.repo_metadata = repo_metadata

    async def execute(
        self,
        result: StructuredResult,
        *,
        default_commands: list[str] | None = None,
    ) -> ExecutorOutcome:
        outcome = ExecutorOutcome(status=TaskStatus.DONE)

        if result.diffs:
            apply_result = await self._apply_diffs(result)
            if apply_result.status == TaskStatus.ESCALATED:
                return apply_result
            outcome.applied_paths.extend(apply_result.applied_paths)

        commands = result.suggested_commands or default_commands or []
        for command in commands:
            safe, reason = is_command_safe(command)
            if not safe:
                return ExecutorOutcome(
                    status=TaskStatus.ESCALATED,
                    applied_paths=outcome.applied_paths,
                    command_results=outcome.command_results,
                    failure_reason=reason,
                )

            if not self._is_allowlisted(command):
                return ExecutorOutcome(
                    status=TaskStatus.ESCALATED,
                    applied_paths=outcome.applied_paths,
                    command_results=outcome.command_results,
                    failure_reason=f"Command is not allowlisted for autonomous execution: {command}",
                )

            command_result = await self._run_command(command)
            outcome.command_results.append(command_result)
            if command_result.returncode != 0:
                return ExecutorOutcome(
                    status=TaskStatus.ESCALATED,
                    applied_paths=outcome.applied_paths,
                    command_results=outcome.command_results,
                    failure_reason=f"Command failed: {command}",
                )

        return outcome

    def default_commands_for_verification(self) -> list[str]:
        if not self.repo_metadata:
            return []

        if "pytest" in self.repo_metadata.test_frameworks:
            return ["pytest -q"]
        if "playwright-test" in self.repo_metadata.test_frameworks:
            return ["npm test"]
        if "terraform" in self.repo_metadata.frameworks:
            return ["terraform validate"]
        return []

    async def _apply_diffs(self, result: StructuredResult) -> ExecutorOutcome:
        patch_chunks = [diff.unified_diff for diff in result.diffs if diff.unified_diff.strip()]
        if not patch_chunks:
            return ExecutorOutcome(
                status=TaskStatus.DONE,
                applied_paths=[str(diff.path) for diff in result.diffs],
            )

        patch_text = "\n".join(patch_chunks)
        patch_file = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
                handle.write(patch_text)
                patch_file = Path(handle.name)

            proc = await asyncio.create_subprocess_exec(
                "git",
                "apply",
                "--whitespace=nowarn",
                "--reject",
                "--unsafe-paths",
                str(patch_file),
                cwd=str(self.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        finally:
            if patch_file and patch_file.exists():
                patch_file.unlink()

        if proc.returncode != 0:
            return ExecutorOutcome(
                status=TaskStatus.ESCALATED,
                failure_reason=(
                    "Failed to apply generated patch: "
                    f"{stderr.decode('utf-8', errors='ignore').strip() or stdout.decode('utf-8', errors='ignore').strip()}"
                ),
            )

        return ExecutorOutcome(
            status=TaskStatus.DONE,
            applied_paths=[str(diff.path) for diff in result.diffs],
        )

    async def _run_command(self, command: str) -> ExecutorCommandResult:
        argv = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout, stderr = await proc.communicate()
        returncode = proc.returncode
        if returncode is None:
            raise RuntimeError(f"Command finished without a return code: {command}")
        return ExecutorCommandResult(
            command=command,
            returncode=returncode,
            stdout=stdout.decode("utf-8", errors="ignore"),
            stderr=stderr.decode("utf-8", errors="ignore"),
        )

    def _is_allowlisted(self, command: str) -> bool:
        try:
            argv = tuple(shlex.split(command))
        except ValueError:
            logger.warning("Rejecting unparsable executor command: %s", command)
            return False

        return any(argv[: len(prefix)] == prefix for prefix in _ALLOWED_COMMAND_PREFIXES)
