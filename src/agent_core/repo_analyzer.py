"""
repo_analyzer.py — Introspects the host repository to determine what agent
roles are actually needed, what tools should be available, and what quality
constraints make sense for this specific codebase.

Design principle: No agent roles are hardcoded. The markdown files in agents/
are TEMPLATES and EXAMPLES. This module discovers what the repo actually
needs and generates AgentSpec instances accordingly. The orchestrator reads
these specs and routes tasks — it never decides roles based on a fixed list.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from agent_core.schemas import (
    AgentRole,
    AgentSpec,
    EscalationPolicy,
    QualityGate,
    RepoMetadata,
    ToolPermission,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language + framework detection patterns
# ---------------------------------------------------------------------------

LANG_MARKERS: dict[str, list[str]] = {
    "python":     ["*.py", "pyproject.toml", "setup.py", "requirements*.txt", "Pipfile"],
    "typescript": ["*.ts", "*.tsx", "tsconfig.json"],
    "javascript": ["*.js", "*.jsx", "package.json"],
    "java":       ["*.java", "pom.xml", "build.gradle", "*.gradle.kts"],
    "go":         ["*.go", "go.mod"],
    "rust":       ["*.rs", "Cargo.toml"],
    "ruby":       ["*.rb", "Gemfile"],
    "csharp":     ["*.cs", "*.csproj", "*.sln"],
}

FRAMEWORK_MARKERS: dict[str, list[str]] = {
    "react":      ["package.json:react", "*.tsx", "*.jsx"],
    "nextjs":     ["next.config.*"],
    "django":     ["manage.py", "django"],
    "fastapi":    ["fastapi"],
    "spring":     ["spring-boot", "SpringApplication"],
    "playwright": ["playwright.config.*", "@playwright/test"],
    "cucumber":   ["*.feature", "cucumber"],
    "terraform":  ["*.tf", "terraform"],
    "kubernetes": ["*.yaml:kind: Deployment", "helm"],
}

TEST_FRAMEWORK_MARKERS: dict[str, list[str]] = {
    "pytest":   ["pytest", "conftest.py"],
    "jest":     ["jest.config.*", "\"jest\""],
    "junit":    ["@Test", "junit"],
    "rspec":    ["_spec.rb", "RSpec"],
    "vitest":   ["vitest.config.*"],
    "mocha":    ["mocha"],
    "playwright-test": ["@playwright/test", "test.spec.ts"],
}

CI_MARKERS: dict[str, str] = {
    "github-actions": ".github/workflows",
    "gitlab-ci":      ".gitlab-ci.yml",
    "circleci":       ".circleci/config.yml",
    "jenkins":        "Jenkinsfile",
    "azure-devops":   "azure-pipelines.yml",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze(repo_root: Path) -> RepoMetadata:
    """
    Introspect a repository and return its RepoMetadata, including
    a list of recommended agent roles and their generated AgentSpecs.

    This is the only place where repo-specific knowledge enters the system.
    """
    logger.info("Analyzing repository at %s", repo_root)

    langs = _detect_languages(repo_root)
    frameworks = _detect_frameworks(repo_root)
    test_fws = _detect_test_frameworks(repo_root)
    ci = _detect_ci(repo_root)
    module_map = _build_module_map(repo_root, langs)

    has_docker = any((repo_root / f).exists() for f in ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"])
    has_migrations = _has_migrations(repo_root)
    has_openapi = _has_openapi(repo_root)

    roles, custom_specs = _determine_roles(
        langs=langs,
        frameworks=frameworks,
        test_fws=test_fws,
        ci=ci,
        has_docker=has_docker,
        has_migrations=has_migrations,
        has_openapi=has_openapi,
        repo_root=repo_root,
    )

    logger.info("Discovered roles: %s", roles)
    return RepoMetadata(
        root=repo_root,
        primary_languages=langs,
        frameworks=frameworks,
        test_frameworks=test_fws,
        ci_systems=ci,
        has_docker=has_docker,
        has_migrations=has_migrations,
        has_openapi_spec=has_openapi,
        module_map=module_map,
        recommended_roles=roles,
        custom_role_specs=custom_specs,
    )


def load_role_templates(template_dir: Path) -> dict[str, str]:
    """
    Load the markdown role templates from agents/ directory.
    These are used as the base prose for system prompts.
    Returns a dict of role_name -> markdown_text.
    """
    templates: dict[str, str] = {}
    if not template_dir.exists():
        return templates
    for md_file in template_dir.glob("*.md"):
        templates[md_file.stem] = md_file.read_text()
    return templates


# ---------------------------------------------------------------------------
# Role determination — this is where roles are assigned, not hardcoded
# ---------------------------------------------------------------------------


def _determine_roles(
    langs: list[str],
    frameworks: list[str],
    test_fws: list[str],
    ci: list[str],
    has_docker: bool,
    has_migrations: bool,
    has_openapi: bool,
    repo_root: Path,
) -> tuple[list[str], list[AgentSpec]]:
    """
    Determine which agent roles are appropriate for this repo.

    Logic:
    - Every repo gets an orchestrator.
    - A repo with source code gets an implementer.
    - A repo with source code and no tests gets a qa-engineer.
    - A repo with an API surface (openapi, REST framework) gets an architect.
    - A repo with CI gets a reviewer.
    - Any repo with existing tests or a debuggable history gets a debugger.
    - Repos with Terraform/Kubernetes get an infra-engineer role (custom).
    - Repos with Playwright/Cucumber get a test-automation role (custom).
    - Additional roles may be discovered from CONTRIBUTING.md or .github/CODEOWNERS.
    """
    roles: list[str] = [AgentRole.ORCHESTRATOR]
    specs: list[AgentSpec] = []

    has_source = bool(langs)
    has_tests = bool(test_fws)
    has_api = has_openapi or any(f in frameworks for f in ["fastapi", "django", "spring", "express"])
    has_ci = bool(ci)

    if has_source:
        roles.append(AgentRole.IMPLEMENTER)
        roles.append(AgentRole.DEBUGGER)

    if has_api or len(langs) > 1 or has_migrations:
        roles.append(AgentRole.ARCHITECT)

    if has_tests or has_source:
        roles.append(AgentRole.QA_ENGINEER)

    if has_ci or has_source:
        roles.append(AgentRole.REVIEWER)

    # --- Custom role: infra-engineer (Terraform / Kubernetes)
    if "terraform" in frameworks or "kubernetes" in frameworks:
        roles.append("infra-engineer")
        specs.append(_build_infra_engineer_spec(frameworks))

    # --- Custom role: test-automation (Playwright / Cucumber / Selenium)
    playwright_or_bdd = any(f in frameworks for f in ["playwright", "cucumber", "selenium"])
    if playwright_or_bdd:
        roles.append("test-automation-engineer")
        specs.append(_build_test_automation_spec(test_fws, frameworks))

    # --- Read CONTRIBUTING.md and .github/CODEOWNERS for additional signals
    extra = _parse_contributing_doc(repo_root)
    for role_name, role_spec in extra.items():
        if role_name not in roles:
            roles.append(role_name)
            specs.append(role_spec)

    return roles, specs


# ---------------------------------------------------------------------------
# Custom spec builders (examples of dynamic generation)
# ---------------------------------------------------------------------------


def _build_infra_engineer_spec(frameworks: list[str]) -> AgentSpec:
    tools = ["terraform"] if "terraform" in frameworks else []
    tools += ["kubectl"] if "kubernetes" in frameworks else []

    return AgentSpec(
        name="infra-engineer",
        role="infra-engineer",
        description=(
            "Invoke for infrastructure-as-code tasks: Terraform plans, "
            "Kubernetes manifests, Helm charts, cloud resource definitions. "
            "Discovered because this repo contains infrastructure code."
        ),
        responsibilities=[
            "Review and author Terraform modules and plans",
            "Write and validate Kubernetes manifests and Helm values",
            "Identify drift between desired and actual infrastructure state",
            "Enforce least-privilege IAM and network policies",
            "Flag destructive resource changes before they are applied",
        ],
        quality_gates=[
            QualityGate(description="terraform validate passes (if Terraform is present)"),
            QualityGate(description="No wildcard IAM permissions introduced"),
            QualityGate(description="All resource names follow existing naming conventions"),
            QualityGate(description="Destructive changes are explicitly flagged in the summary"),
        ],
        tools_allowed=[ToolPermission(name=t) for t in ["Read", "Write", "Bash", "Glob"]],
        out_of_scope=[
            "Application code changes",
            "CI/CD pipeline configuration (separate concern)",
            "Applying infrastructure changes directly — suggest commands only",
        ],
        escalation=EscalationPolicy(max_retries=1),
    )


def _build_test_automation_spec(test_fws: list[str], frameworks: list[str]) -> AgentSpec:
    fw_list = ", ".join(f for f in test_fws + frameworks if f in {"playwright", "cucumber", "selenium", "cypress"})

    return AgentSpec(
        name="test-automation-engineer",
        role="test-automation-engineer",
        description=(
            f"Invoke for end-to-end and browser automation test work using {fw_list}. "
            "Discovered because this repo contains E2E test infrastructure."
        ),
        responsibilities=[
            f"Write and maintain {fw_list} test suites",
            "Author BDD feature files (Gherkin) that map to step definitions",
            "Implement page object models for UI test stability",
            "Diagnose flaky tests and make them deterministic",
            "Integrate with CI pipelines for test execution",
        ],
        quality_gates=[
            QualityGate(description="Tests follow the page object model pattern used in this repo"),
            QualityGate(description="No hard-coded wait times — use explicit waits"),
            QualityGate(description="BDD scenarios written in business language, not technical steps"),
            QualityGate(description="Tests pass in headless mode"),
        ],
        tools_allowed=[ToolPermission(name=t) for t in ["Read", "Write", "Edit", "Bash", "Glob"]],
        out_of_scope=[
            "Unit or integration tests (route to qa-engineer)",
            "Application code changes to make tests pass (route to implementer)",
        ],
        escalation=EscalationPolicy(max_retries=2),
    )


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _detect_languages(root: Path) -> list[str]:
    found: list[str] = []
    for lang, patterns in LANG_MARKERS.items():
        for pattern in patterns:
            if "." in pattern:
                if list(root.rglob(pattern)):
                    found.append(lang)
                    break
            elif (root / pattern).exists():
                found.append(lang)
                break
    return found


def _detect_frameworks(root: Path) -> list[str]:
    found: list[str] = []
    for fw, markers in FRAMEWORK_MARKERS.items():
        for marker in markers:
            if ":" in marker:
                filename, keyword = marker.split(":", 1)
                for f in root.rglob(filename):
                    try:
                        if keyword in f.read_text(errors="ignore"):
                            found.append(fw)
                            break
                    except OSError:
                        continue
            elif "*" in marker:
                if list(root.rglob(marker)):
                    found.append(fw)
            elif (root / marker).exists():
                found.append(fw)
            if fw in found:
                break
    return list(dict.fromkeys(found))  # preserve order, deduplicate


def _detect_test_frameworks(root: Path) -> list[str]:
    found: list[str] = []
    # Search in config and package files (not just .json)
    config_patterns = ["*.json", "*.toml", "*.cfg", "*.ini"]
    for fw, markers in TEST_FRAMEWORK_MARKERS.items():
        for marker in markers:
            if "*" in marker:
                if list(root.rglob(marker)):
                    found.append(fw)
                    break
            else:
                for pattern in config_patterns:
                    if fw in found:
                        break
                    for cfg in root.rglob(pattern):
                        try:
                            if marker in cfg.read_text(errors="ignore"):
                                found.append(fw)
                                break
                        except OSError:
                            continue
                if fw in found:
                    break
    return list(dict.fromkeys(found))


def _detect_ci(root: Path) -> list[str]:
    return [name for name, path in CI_MARKERS.items() if (root / path).exists()]


def _has_migrations(root: Path) -> bool:
    return bool(
        list(root.rglob("migrations/*.py"))
        or list(root.rglob("db/migrate/*.rb"))
        or (root / "flyway.conf").exists()
        or list(root.rglob("V*.sql"))
    )


def _has_openapi(root: Path) -> bool:
    for pattern in ["openapi.yaml", "openapi.json", "swagger.yaml", "swagger.json"]:
        if list(root.rglob(pattern)):
            return True
    return False


def _build_module_map(root: Path, langs: list[str]) -> dict[str, list[str]]:
    """Map top-level source directories to their file paths."""
    module_map: dict[str, list[str]] = {}
    exclude = {".git", "node_modules", "__pycache__", ".venv", "venv", ".agent-swarm"}
    for child in root.iterdir():
        if child.is_dir() and child.name not in exclude:
            files = [str(f.relative_to(root)) for f in child.rglob("*") if f.is_file()]
            if files:
                module_map[child.name] = files
    return module_map


def _parse_contributing_doc(root: Path) -> dict[str, AgentSpec]:
    """
    Read CONTRIBUTING.md or .github/CODEOWNERS for custom role signals.
    Returns a dict of role_name -> AgentSpec for any discovered additional roles.

    Example: a CONTRIBUTING.md that mentions 'security reviewer' or 'release manager'
    triggers discovery of a corresponding agent role.
    """
    custom: dict[str, AgentSpec] = {}
    patterns = [
        root / "CONTRIBUTING.md",
        root / ".github" / "CONTRIBUTING.md",
        root / "docs" / "CONTRIBUTING.md",
    ]

    for doc in patterns:
        if not doc.exists():
            continue
        text = doc.read_text(errors="ignore").lower()

        if re.search(r"security\s+review|sec\s+review|appsec", text):
            custom["security-reviewer"] = AgentSpec(
                name="security-reviewer",
                role="security-reviewer",
                description=(
                    "Discovered from CONTRIBUTING.md. Invoke for security-focused "
                    "code review: auth flows, input validation, secret handling, "
                    "dependency vulnerabilities, OWASP Top 10 surface."
                ),
                responsibilities=[
                    "Review authentication and authorization implementations",
                    "Identify injection vulnerabilities (SQL, command, template)",
                    "Check secret and credential handling",
                    "Validate input sanitisation and output encoding",
                    "Flag vulnerable dependency versions",
                ],
                quality_gates=[
                    QualityGate(description="OWASP Top 10 surface explicitly evaluated"),
                    QualityGate(description="All auth paths reviewed"),
                    QualityGate(description="No secrets in code or logs"),
                ],
                tools_allowed=[ToolPermission(name=t) for t in ["Read", "Glob", "Grep", "WebSearch"]],
                out_of_scope=["Making fixes — findings only"],
                escalation=EscalationPolicy(max_retries=1),
            )
        break  # Only read the first CONTRIBUTING.md found

    return custom
