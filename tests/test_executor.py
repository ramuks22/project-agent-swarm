from __future__ import annotations

import subprocess
from pathlib import Path

from agent_core.executor import AutonomousExecutor
from agent_core.schemas import FileDiff, StructuredResult, TaskStatus


class TestAutonomousExecutor:
    async def test_apply_diff_success(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)  # noqa: S603,S607
        target = tmp_path / "hello.txt"
        target.write_text("old\n")

        diff = """--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-old
+new
"""
        result = StructuredResult(
            task_id="t1",
            role="implementer",
            status=TaskStatus.DONE,
            summary="Apply diff.",
            diffs=[
                FileDiff(
                    path=Path("hello.txt"),
                    operation="modify",
                    unified_diff=diff,
                    explanation="Update file content.",
                )
            ],
        )

        executor = AutonomousExecutor(tmp_path)
        outcome = await executor.execute(result)

        assert outcome.status == TaskStatus.DONE
        assert target.read_text() == "new\n"

    async def test_runs_allowlisted_pytest_command(self, tmp_path: Path) -> None:
        (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n")

        result = StructuredResult(
            task_id="t1",
            role="qa-engineer",
            status=TaskStatus.DONE,
            summary="Run tests.",
            suggested_commands=["pytest -q"],
        )

        executor = AutonomousExecutor(tmp_path)
        outcome = await executor.execute(result)

        assert outcome.status == TaskStatus.DONE
        assert outcome.command_results
        assert outcome.command_results[0].returncode == 0

    async def test_rejects_non_allowlisted_command(self, tmp_path: Path) -> None:
        result = StructuredResult(
            task_id="t1",
            role="qa-engineer",
            status=TaskStatus.DONE,
            summary="Bad command.",
            suggested_commands=["python -c \"print('hello')\""],
        )

        executor = AutonomousExecutor(tmp_path)
        outcome = await executor.execute(result)

        assert outcome.status == TaskStatus.ESCALATED
        assert outcome.failure_reason is not None
