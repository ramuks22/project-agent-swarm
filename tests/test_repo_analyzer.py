"""test_repo_analyzer.py — Tests for repo_analyzer's dynamic role discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.repo_analyzer import analyze, load_role_templates
from agent_core.schemas import AgentRole


class TestAnalyze:
    def test_detects_python(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert "python" in meta.primary_languages

    def test_detects_playwright(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert "playwright" in meta.frameworks

    def test_detects_cucumber_feature_files(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert "cucumber" in meta.frameworks

    def test_detects_github_actions(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert "github-actions" in meta.ci_systems

    def test_detects_pytest(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert "pytest" in meta.test_frameworks

    def test_orchestrator_always_present(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert AgentRole.ORCHESTRATOR in meta.recommended_roles

    def test_playwright_triggers_test_automation_role(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert "test-automation-engineer" in meta.recommended_roles

    def test_test_automation_spec_has_playwright_responsibility(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        spec = next((s for s in meta.agent_specs if s.name == "test-automation-engineer"), None)
        assert spec is not None
        assert any("playwright" in r.lower() or "bdd" in r.lower() or "cucumber" in r.lower()
                   for r in spec.responsibilities)

    def test_implementer_present_for_source_repo(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert AgentRole.IMPLEMENTER in meta.recommended_roles

    def test_debugger_present_for_source_repo(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert AgentRole.DEBUGGER in meta.recommended_roles

    def test_empty_dir_gives_orchestrator_only(self, tmp_path: Path) -> None:
        meta = analyze(tmp_path)
        assert meta.recommended_roles == [AgentRole.ORCHESTRATOR]
        assert meta.primary_languages == []

    def test_nested_agent_swarm_is_ignored(self, tmp_path: Path) -> None:
        # Mocking the condition where tracking installs inside a sub-folder
        (tmp_path / ".agent-swarm").mkdir()
        (tmp_path / ".agent-swarm" / "package.json").write_text('{"name": "ignore me"}')
        (tmp_path / ".agent-swarm" / "hidden.go").write_text('package main')

        meta = analyze(tmp_path)
        # Verify the top level mock did not evaluate the framework leakage
        assert "javascript" not in meta.primary_languages
        assert "go" not in meta.primary_languages
        assert len(meta.primary_languages) == 0

    def test_module_map_excludes_hidden_dirs(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert ".git" not in meta.module_map

    def test_terraform_repo_triggers_infra_engineer(self, tmp_path: Path) -> None:
        (tmp_path / "infra").mkdir()
        (tmp_path / "infra" / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
        meta = analyze(tmp_path)
        assert "infra-engineer" in meta.recommended_roles
        spec = next((s for s in meta.agent_specs if s.name == "infra-engineer"), None)
        assert spec is not None
        assert any("terraform" in r.lower() for r in spec.responsibilities)

    def test_contributing_md_triggers_security_reviewer(self, fake_repo: Path) -> None:
        (fake_repo / "CONTRIBUTING.md").write_text(
            "## Review process\nAll PRs require a security review from the AppSec team.\n"
        )
        meta = analyze(fake_repo)
        assert "security-reviewer" in meta.recommended_roles

    def test_repo_metadata_fields_populated(self, fake_repo: Path) -> None:
        meta = analyze(fake_repo)
        assert meta.root == fake_repo
        assert isinstance(meta.has_docker, bool)
        assert isinstance(meta.has_migrations, bool)
        assert isinstance(meta.has_openapi_spec, bool)


class TestLoadRoleTemplates:
    def test_loads_markdown_files(self, tmp_path: Path) -> None:
        (tmp_path / "architect.md").write_text("# Architect\nDesign things.")
        (tmp_path / "implementer.md").write_text("# Implementer\nWrite code.")
        templates = load_role_templates(tmp_path)
        assert "architect" in templates
        assert "implementer" in templates
        assert "Design things." in templates["architect"]

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        templates = load_role_templates(tmp_path / "nonexistent")
        assert templates == {}

    def test_ignores_non_markdown_files(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("ignore me")
        (tmp_path / "architect.md").write_text("# Architect")
        templates = load_role_templates(tmp_path)
        assert "notes" not in templates
        assert "architect" in templates
