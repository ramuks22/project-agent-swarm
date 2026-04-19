"""
context_optimizer.py — Scores and ranks files by relevance to a task,
then slices them to fit within the token budget.

Problem this solves:
    A naive file inclusion strategy (alphabetical, or all changed files)
    wastes the token budget on peripheral files and starves the agent of
    the most critical context. This module ranks files so the most relevant
    ones survive the slice even when the budget is tight.

Design:
    Scoring is additive: each signal adds to a file's score.
    Higher score = higher priority in the budget allocation.
    No external ML — purely lexical and structural heuristics.
    Fast enough to run synchronously before each agent invocation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tiktoken as _tiktoken
    _TIKTOKEN_AVAILABLE = True
except ImportError:
    _TIKTOKEN_AVAILABLE = False

_ENCODING = None

# Characters-per-token ratio for fallback (cl100k_base averages ~3.5 chars/token)
_CHARS_PER_TOKEN = 3.5


def _count_tokens(text: str) -> int:
    """Count tokens using tiktoken if available, otherwise estimate via char ratio."""
    global _ENCODING
    if _TIKTOKEN_AVAILABLE:
        try:
            if _ENCODING is None:
                _ENCODING = _tiktoken.get_encoding("cl100k_base")
            return len(_ENCODING.encode(text, disallowed_special=()))
        except Exception:
            pass  # network unavailable or other error — fall through to estimator
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


# ---------------------------------------------------------------------------
# Scoring signals
# ---------------------------------------------------------------------------


@dataclass
class ScoredFile:
    path: Path
    content: str
    token_count: int
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def score_files(
    task_description: str,
    candidate_paths: list[Path],
    agent_role: str,
    recently_changed: list[Path] | None = None,
    error_trace: str | None = None,
) -> list[ScoredFile]:
    """
    Score a list of candidate files for relevance to the task and agent role.
    Returns files sorted by score descending (most relevant first).

    Signals applied (additive):
        +50  file is in recently_changed list
        +30  filename/path contains a term from the task description
        +20  file content contains a term from the task description
        +15  file is a test file and role is qa-engineer or debugger
        +15  file is an interface/schema/model file and role is architect
        +10  file is a config file (pyproject, package.json, etc.)
        +10  a symbol from the error_trace is referenced in this file
        -20  file is a lock file (poetry.lock, package-lock.json, etc.)
        -30  file is a build artifact or auto-generated file
    """
    recently_changed_set = set(recently_changed or [])
    task_terms = _extract_terms(task_description)
    trace_symbols = _extract_trace_symbols(error_trace or "")

    scored: list[ScoredFile] = []
    for path in candidate_paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            continue

        sf = ScoredFile(
            path=path,
            content=content,
            token_count=_count_tokens(content),
        )

        # --- Recency signal
        if path in recently_changed_set:
            sf.score += 50
            sf.reasons.append("recently changed")

        # --- Task term in path
        path_str = str(path).lower()
        matching_terms = [t for t in task_terms if t in path_str]
        if matching_terms:
            sf.score += 30
            sf.reasons.append(f"path matches task terms: {matching_terms}")

        # --- Task term in content
        content_lower = content.lower()
        content_matches = [t for t in task_terms if t in content_lower]
        if content_matches:
            sf.score += min(20, len(content_matches) * 4)
            sf.reasons.append(f"content matches task terms: {content_matches[:3]}")

        # --- Role-specific signals
        is_test = _is_test_file(path)
        is_schema = _is_schema_or_model(path, content)
        is_config = _is_config_file(path)

        if agent_role in ("qa-engineer", "debugger") and is_test:
            sf.score += 15
            sf.reasons.append("test file + qa/debug role")

        if agent_role == "architect" and is_schema:
            sf.score += 15
            sf.reasons.append("schema/model file + architect role")

        if is_config:
            sf.score += 10
            sf.reasons.append("config file")

        # --- Error trace symbols
        if trace_symbols:
            matched_symbols = [s for s in trace_symbols if s in content]
            if matched_symbols:
                sf.score += min(10, len(matched_symbols) * 3)
                sf.reasons.append(f"error trace symbols found: {matched_symbols[:2]}")

        # --- Penalty signals
        if _is_lock_file(path):
            sf.score -= 20
            sf.reasons.append("lock file (penalised)")

        if _is_generated_file(path, content):
            sf.score -= 30
            sf.reasons.append("auto-generated (penalised)")

        scored.append(sf)

    return sorted(scored, key=lambda f: f.score, reverse=True)


def slice_to_budget(
    scored_files: list[ScoredFile],
    token_budget: int,
    reserve_for_prompt: int = 4000,
) -> list[ScoredFile]:
    """
    Select the highest-scoring files that fit within the token budget.
    reserve_for_prompt tokens are held back for the system prompt and task text.
    """
    available = token_budget - reserve_for_prompt
    selected: list[ScoredFile] = []
    used = 0

    for sf in scored_files:
        if used + sf.token_count <= available:
            selected.append(sf)
            used += sf.token_count
        else:
            # Try to include a truncated version for very high-scoring files
            if sf.score >= 40 and available - used > 200:
                remaining = available - used
                truncated_content = _truncate_to_tokens(sf.content, remaining - 50)
                selected.append(ScoredFile(
                    path=sf.path,
                    content=truncated_content + "\n\n[... truncated to fit token budget ...]",
                    token_count=remaining,
                    score=sf.score,
                    reasons=sf.reasons + ["truncated"],
                ))
                used = available
            break

    return selected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_terms(text: str) -> list[str]:
    """Extract meaningful terms from a task description for matching."""
    # Remove stop words, keep identifiers and domain terms
    stop = {"the", "a", "an", "is", "in", "to", "for", "of", "and", "or",
             "with", "that", "this", "it", "be", "by", "as", "at", "on"}
    words = re.findall(r"[a-z][a-z0-9_]{2,}", text.lower())
    return [w for w in words if w not in stop]


def _extract_trace_symbols(trace: str) -> list[str]:
    """Extract function names, class names, and file names from a stack trace."""
    symbols: list[str] = []
    # Python-style: File "path/file.py", line N, in function_name
    symbols += re.findall(r'in (\w+)', trace)
    # Java-style: at com.example.ClassName.methodName
    symbols += re.findall(r'at [\w.]+\.(\w+)\(', trace)
    # Generic: identifiers that look like symbols (CamelCase or snake_case)
    symbols += re.findall(r'\b([A-Z][a-zA-Z0-9]{2,}|[a-z][a-z0-9]{2,}_[a-z][a-z0-9_]+)\b', trace)
    return list(dict.fromkeys(symbols))  # deduplicate, preserve order


def _is_test_file(path: Path) -> bool:
    name = path.name.lower()
    parts = [p.lower() for p in path.parts]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".spec.ts")
        or name.endswith(".test.ts")
        or name.endswith(".test.js")
        or name.endswith("_spec.rb")
        or "test" in parts
        or "tests" in parts
        or "spec" in parts
        or "__tests__" in parts
    )


def _is_schema_or_model(path: Path, content: str) -> bool:
    name = path.name.lower()
    return (
        "schema" in name
        or "model" in name
        or "entity" in name
        or "interface" in name
        or "types" in name
        or name.endswith(".prisma")
        or "class " in content[:500]  # quick scan of file head
        or "interface " in content[:500]
    )


def _is_config_file(path: Path) -> bool:
    name = path.name.lower()
    return name in {
        "pyproject.toml", "setup.py", "setup.cfg",
        "package.json", "tsconfig.json",
        "pom.xml", "build.gradle",
        "cargo.toml", "go.mod",
        "dockerfile", "docker-compose.yml", "docker-compose.yaml",
        ".env.example", "makefile",
    }


def _is_lock_file(path: Path) -> bool:
    return path.name.lower() in {
        "poetry.lock", "package-lock.json", "yarn.lock",
        "pipfile.lock", "cargo.lock", "composer.lock",
        "gemfile.lock",
    }


def _is_generated_file(path: Path, content: str) -> bool:
    generated_headers = [
        "this file is auto-generated",
        "do not edit",
        "generated by",
        "code generated",
        "@generated",
        "autogenerated",
    ]
    head = content[:300].lower()
    return any(h in head for h in generated_headers)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to approximately max_tokens tokens."""
    if _TIKTOKEN_AVAILABLE and _ENCODING is not None:
        try:
            tokens = _ENCODING.encode(text, disallowed_special=())
            if len(tokens) <= max_tokens:
                return text
            return _ENCODING.decode(tokens[:max_tokens])
        except Exception:
            pass
    # Fallback: truncate by character count
    char_limit = int(max_tokens * _CHARS_PER_TOKEN)
    return text[:char_limit]
