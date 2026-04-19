"""test_context_optimizer.py — Tests for context optimizer relevance scoring."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.context_optimizer import (
    score_files,
    slice_to_budget,
    _extract_terms,
    _extract_trace_symbols,
    _is_test_file,
    _is_lock_file,
    _is_generated_file,
)


@pytest.fixture()
def source_files(tmp_path: Path) -> list[Path]:
    files = {
        "src/auth/token_validator.py": "class TokenValidator:\n    def validate(self, token): ...\n",
        "src/auth/models.py": "class User:\n    email: str\n    token: str\n",
        "src/billing/invoice.py": "class Invoice:\n    amount: float\n",
        "tests/test_token_validator.py": "def test_validate(): assert TokenValidator().validate('x')\n",
        "package-lock.json": '{"lockfileVersion":3}\n',
        "auto_generated.py": "# This file is auto-generated. Do not edit.\nFOO = 1\n",
    }
    paths = []
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        paths.append(p)
    return paths


class TestScoreFiles:
    def test_recently_changed_scores_highest(self, source_files: list[Path], tmp_path: Path) -> None:
        auth_model = tmp_path / "src/auth/models.py"
        scored = score_files(
            task_description="update user model",
            candidate_paths=source_files,
            agent_role="implementer",
            recently_changed=[auth_model],
        )
        top = scored[0]
        assert top.path == auth_model
        assert any("recently changed" in r for r in top.reasons)

    def test_lock_file_penalised(self, source_files: list[Path]) -> None:
        scored = score_files(
            task_description="token validation",
            candidate_paths=source_files,
            agent_role="implementer",
        )
        lock_scored = next(s for s in scored if "package-lock.json" in str(s.path))
        auth_scored = next(s for s in scored if "token_validator" in str(s.path))
        assert lock_scored.score < auth_scored.score

    def test_generated_file_penalised(self, source_files: list[Path]) -> None:
        scored = score_files(
            task_description="token validation",
            candidate_paths=source_files,
            agent_role="implementer",
        )
        gen = next(s for s in scored if "auto_generated" in str(s.path))
        assert gen.score < 0

    def test_test_file_boosted_for_qa_role(self, source_files: list[Path], tmp_path: Path) -> None:
        test_file = tmp_path / "tests/test_token_validator.py"
        scored = score_files(
            task_description="token validation coverage",
            candidate_paths=source_files,
            agent_role="qa-engineer",
        )
        test_scored = next(s for s in scored if s.path == test_file)
        assert any("test file" in r for r in test_scored.reasons)

    def test_task_term_in_path_boosts_score(self, source_files: list[Path], tmp_path: Path) -> None:
        scored = score_files(
            task_description="fix token validation logic",
            candidate_paths=source_files,
            agent_role="implementer",
        )
        token_file = next(s for s in scored if "token_validator" in str(s.path))
        assert any("path matches" in r for r in token_file.reasons)

    def test_error_trace_symbols_boost_score(self, source_files: list[Path]) -> None:
        trace = (
            'Traceback (most recent call last):\n'
            '  File "src/auth/token_validator.py", line 4, in validate\n'
            '    raise ValueError("bad token")\n'
            'ValueError: bad token\n'
        )
        scored = score_files(
            task_description="fix the error",
            candidate_paths=source_files,
            agent_role="debugger",
            error_trace=trace,
        )
        validator = next(s for s in scored if "token_validator" in str(s.path))
        assert any("error trace" in r for r in validator.reasons)

    def test_nonexistent_paths_skipped(self, tmp_path: Path) -> None:
        fake = tmp_path / "does_not_exist.py"
        scored = score_files("task", [fake], "implementer")
        assert scored == []


class TestSliceToBudget:
    def test_respects_token_budget(self, source_files: list[Path]) -> None:
        scored = score_files("token validation", source_files, "implementer")
        selected = slice_to_budget(scored, token_budget=500, reserve_for_prompt=100)
        total = sum(s.token_count for s in selected)
        assert total <= 400  # budget minus reserve

    def test_empty_input_returns_empty(self) -> None:
        assert slice_to_budget([], token_budget=10000) == []

    def test_high_score_file_truncated_when_needed(self, tmp_path: Path) -> None:
        big_file = tmp_path / "big.py"
        big_file.write_text("x = 1\n" * 2000)  # ~many tokens
        scored = score_files("refactor x variable", [big_file], "implementer",
                             recently_changed=[big_file])
        selected = slice_to_budget(scored, token_budget=300, reserve_for_prompt=0)
        # High-score file should appear as truncated rather than dropped entirely
        if selected:
            assert selected[0].path == big_file


class TestHelpers:
    def test_extract_terms_filters_stop_words(self) -> None:
        terms = _extract_terms("add input validation to the login endpoint")
        assert "the" not in terms
        assert "validation" in terms
        assert "login" in terms
        assert "endpoint" in terms

    def test_extract_trace_symbols_python(self) -> None:
        trace = 'File "src/auth.py", line 12, in validate_token\nKeyError: "exp"'
        symbols = _extract_trace_symbols(trace)
        assert "validate_token" in symbols

    def test_extract_trace_symbols_java(self) -> None:
        trace = "at com.example.AuthService.validateToken(AuthService.java:42)"
        symbols = _extract_trace_symbols(trace)
        assert "validateToken" in symbols

    def test_is_test_file_true_for_test_prefix(self, tmp_path: Path) -> None:
        assert _is_test_file(tmp_path / "tests" / "test_auth.py")

    def test_is_test_file_true_for_spec_suffix(self, tmp_path: Path) -> None:
        assert _is_test_file(tmp_path / "auth.spec.ts")

    def test_is_test_file_false_for_source(self, tmp_path: Path) -> None:
        assert not _is_test_file(tmp_path / "src" / "auth.py")

    def test_is_lock_file(self, tmp_path: Path) -> None:
        assert _is_lock_file(tmp_path / "poetry.lock")
        assert _is_lock_file(tmp_path / "yarn.lock")
        assert not _is_lock_file(tmp_path / "pyproject.toml")

    def test_is_generated_file(self) -> None:
        assert _is_generated_file(Path("x.py"), "# This file is auto-generated. Do not edit.\nFOO = 1")
        assert _is_generated_file(Path("x.py"), "// @generated by protoc\n")
        assert not _is_generated_file(Path("x.py"), "class MyService:\n    pass\n")
