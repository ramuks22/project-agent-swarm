"""test_context_optimizer.py — Tests for context optimizer relevance scoring."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.context_optimizer import (
    ScoredFile,
    _extract_terms,
    _is_lock_file,
    get_eligible_candidates,
    pass_1_metadata_score,
    pass_2_content_refinement,
    slice_to_budget,
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
        "app/api/dashboard/route.ts": "function dashboard() { payment mode totals }",
        "lib/offline-sync.ts": "export function offline stock queue sales() {}",
        "package.json": '{"name": "test"}',
        ".env.example": "PORT=8080",
    }
    paths = []
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        paths.append(p)
    return paths


class TestPass1Score:
    def test_recently_changed_scores_highest(
        self, source_files: list[Path], tmp_path: Path
    ) -> None:
        auth_model = tmp_path / "src" / "auth" / "models.py"
        scored = pass_1_metadata_score(
            "update user model", source_files, "implementer", recently_changed=[auth_model]
        )
        assert scored[0].path == auth_model
        assert any("recently changed" in r for r in scored[0].reasons)

    def test_lock_file_penalised(self, source_files: list[Path]) -> None:
        scored = pass_1_metadata_score("token validation", source_files, "implementer")
        lock_scored = next(s for s in scored if "package-lock.json" in str(s.path))
        auth_scored = next((s for s in scored if "token_validator" in str(s.path)), None)
        assert auth_scored is not None
        assert lock_scored.score < auth_scored.score

    def test_conditional_config_bonus(self, source_files: list[Path], tmp_path: Path) -> None:
        # Task doesn't imply config -> penalized
        scored1 = pass_1_metadata_score("refactor auth logic", source_files, "implementer")
        pkg = next(s for s in scored1 if "package.json" in str(s.path))
        assert pkg.score < 0

        # Task implies config -> boosted
        scored2 = pass_1_metadata_score("fix docker package build", source_files, "implementer")
        pkg2 = next(s for s in scored2 if "package.json" in str(s.path))
        assert pkg2.score > 0

    def test_task_segment_match_boosts(self, source_files: list[Path], tmp_path: Path) -> None:
        scored = pass_1_metadata_score(
            "fix dashboard payment mode totals", source_files, "implementer"
        )
        dashboard = next(s for s in scored if "dashboard" in str(s.path))
        assert any("segments match task terms" in r for r in dashboard.reasons)


class TestPass2Score:
    def test_content_boosts_score(self, source_files: list[Path]) -> None:
        scored1 = pass_1_metadata_score(
            "offline stock queue sales role bug", source_files, "implementer"
        )
        offline_sync = next(s for s in scored1 if "offline-sync" in str(s.path))
        pre_score = offline_sync.score

        scored2 = pass_2_content_refinement(scored1, "offline stock queue sales role bug")
        offline_sync2 = next(s for s in scored2 if "offline-sync" in str(s.path))

        assert offline_sync2.score > pre_score
        assert offline_sync2.content_loaded is True


class TestBudgetPacker:
    def test_packer_continues_after_oversized(self, tmp_path: Path) -> None:
        p1 = tmp_path / "big.py"
        p1.write_text("x")
        sf1 = ScoredFile(path=p1, token_count=5000, score=20)
        p2 = tmp_path / "small.py"
        p2.write_text("y")
        sf2 = ScoredFile(path=p2, token_count=50, score=10)

        selected = slice_to_budget([sf1, sf2], token_budget=4100, reserve_for_prompt=4000)

        assert len(selected) == 1
        assert selected[0].path == p2

    def test_empty_budget_handling(self, tmp_path: Path) -> None:
        px = tmp_path / "x.py"
        px.write_text("x")
        sf1 = ScoredFile(path=px, token_count=50, score=50)
        selected = slice_to_budget([sf1], token_budget=10, reserve_for_prompt=10)
        assert len(selected) == 0

    def test_zero_scores_filtered_out(self, tmp_path: Path) -> None:
        px = tmp_path / "x.py"
        px.write_text("x")
        sf1 = ScoredFile(path=px, token_count=10, score=0)
        py = tmp_path / "y.py"
        py.write_text("y")
        sf2 = ScoredFile(path=py, token_count=10, score=-10)
        selected = slice_to_budget([sf1, sf2], token_budget=1000)
        assert len(selected) == 0


class TestRelevanceModes:
    def test_relevance_ignores_configs(self, source_files: list[Path]) -> None:
        scored = pass_1_metadata_score("dashboard payment mode totals", source_files, "implementer")
        scored = pass_2_content_refinement(scored, "dashboard payment mode totals")
        selected = slice_to_budget(scored, token_budget=8000)

        paths = [s.path.name for s in selected]
        assert "route.ts" in paths
        # package.json and .env.example should have score <= 0 so not selected at all
        assert "package.json" not in paths
        assert ".env.example" not in paths


class TestGetEligibleCandidates:
    def test_exclude_directories(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("x")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "x.js").write_text("x")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x")

        candidates = get_eligible_candidates(tmp_path)
        names = [p.name for p in candidates]
        assert "a.py" in names
        assert "config" not in names
        assert "x.js" not in names


class TestHelpers:
    def test_extract_terms_filters_stop_words(self) -> None:
        terms = _extract_terms("add input validation to the login endpoint")
        assert "the" not in terms
        assert "validation" in terms
        assert "login" in terms

    def test_is_lock_file(self) -> None:
        assert _is_lock_file(Path("poetry.lock"))
        assert not _is_lock_file(Path("pyproject.toml"))
