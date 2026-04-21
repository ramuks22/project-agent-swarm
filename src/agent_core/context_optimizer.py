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

import os
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
    token_count: int
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    content: str | None = None
    content_loaded: bool = False
    truncated: bool = False


def get_eligible_candidates(root_dir: Path) -> list[Path]:
    """Efficiently yield non-excluded file paths."""
    excludes = {
        ".git",
        "node_modules",
        ".next",
        ".playwright-cli",
        "coverage",
        "dist",
        "build",
        "__pycache__",
        ".agent-swarm",
    }
    candidates = []
    for root, dirs, files in os.walk(root_dir):
        # Prune excluded directories in-place so os.walk doesn't descend into them
        dirs[:] = [d for d in dirs if d not in excludes]
        for file in files:
            candidates.append(Path(root) / file)
    return candidates


def pass_1_metadata_score(
    task_description: str,
    candidate_paths: list[Path],
    agent_role: str,
    recently_changed: list[Path] | None = None,
) -> list[ScoredFile]:
    """Pass 1: Score files based purely on metadata without reading contents."""
    recently_changed_set = set(recently_changed or [])
    task_terms = _extract_terms(task_description)

    config_task_terms = {"config", "build", "dependency", "package", "docker", "setup", "install"}
    task_implies_config = any(term in config_task_terms for term in task_terms)

    scored: list[ScoredFile] = []
    for path in candidate_paths:
        try:
            st = path.stat()
            # Estimate token count without reading
            est_tokens = max(1, int(st.st_size / _CHARS_PER_TOKEN))
        except OSError:
            continue

        sf = ScoredFile(
            path=path,
            token_count=est_tokens,
        )

        if path in recently_changed_set:
            sf.score += 50
            sf.reasons.append("recently changed")

        path_parts = [p.lower() for p in path.parts]
        path_str = str(path).lower()

        segment_matches = [t for t in task_terms if t in path_parts]
        if segment_matches:
            sf.score += 40
            sf.reasons.append(f"path segments match task terms: {segment_matches}")

        substring_matches = [t for t in task_terms if t in path_str and t not in segment_matches]
        if substring_matches:
            sf.score += 20
            sf.reasons.append(f"path substring matches task terms: {substring_matches}")

        if agent_role in ("qa-engineer", "debugger") and _is_test_file(path):
            sf.score += 15
            sf.reasons.append("test file + qa/debug role")

        # Schema detection (filename only in pass 1)
        name = path.name.lower()
        is_schema = (
            "schema" in name
            or "model" in name
            or "entity" in name
            or "interface" in name
            or "types" in name
            or name.endswith(".prisma")
        )
        if agent_role == "architect" and is_schema:
            sf.score += 15
            sf.reasons.append("schema/model file + architect role")

        if _is_config_file(path):
            if task_implies_config:
                sf.score += 10
                sf.reasons.append("config file (task implies config)")
            else:
                sf.score -= 5
                sf.reasons.append("config file (penalised, task does not imply config)")

        if _is_lock_file(path):
            sf.score -= 20
            sf.reasons.append("lock file (penalised)")

        scored.append(sf)

    return sorted(scored, key=lambda f: f.score, reverse=True)


def pass_2_content_refinement(
    candidates: list[ScoredFile],
    task_description: str,
    error_trace: str | None = None,
    max_reads: int = 25,
    preview_bytes: int = 8192,
) -> list[ScoredFile]:
    """Pass 2: Refine the top N candidates by previewing content chunks."""
    task_terms = _extract_terms(task_description)
    trace_symbols = _extract_trace_symbols(error_trace or "")

    for sf in candidates[:max_reads]:
        try:
            with sf.path.open("r", encoding="utf-8", errors="ignore") as f:
                content_preview = f.read(preview_bytes)

            sf.content = content_preview
            sf.content_loaded = True

            st_size = sf.path.stat().st_size
            if st_size <= preview_bytes:
                sf.token_count = _count_tokens(content_preview)

            content_lower = content_preview.lower()

            content_matches = [t for t in task_terms if t in content_lower]
            if content_matches:
                sf.score += min(20, len(content_matches) * 4)
                sf.reasons.append(f"content matches task terms: {content_matches[:3]}")

            if trace_symbols:
                matched_symbols = [s for s in trace_symbols if s in content_preview]
                if matched_symbols:
                    sf.score += min(10, len(matched_symbols) * 3)
                    sf.reasons.append(f"error trace symbols found: {matched_symbols[:2]}")

            if _is_generated_file(sf.path, content_preview):
                sf.score -= 30
                sf.reasons.append("auto-generated (penalised)")

        except OSError:
            pass

    return sorted(candidates, key=lambda f: f.score, reverse=True)


def slice_to_budget(
    scored_files: list[ScoredFile],
    token_budget: int,
    reserve_for_prompt: int = 4000,
) -> list[ScoredFile]:
    """Select the highest-scoring files sequentially fitting within the token budget."""
    available = token_budget - reserve_for_prompt
    selected: list[ScoredFile] = []
    used = 0

    for sf in scored_files:
        if sf.score <= 0:
            continue

        if used + sf.token_count <= available:
            if not sf.content_loaded:
                try:
                    sf.content = sf.path.read_text(errors="ignore")
                    sf.token_count = _count_tokens(sf.content)
                    sf.content_loaded = True
                except OSError:
                    continue

            if used + sf.token_count <= available:
                selected.append(sf)
                used += sf.token_count
            elif sf.score >= 40 and available - used > 300:
                remaining = available - used
                truncated_content = _truncate_to_tokens(sf.content or "", remaining - 50)
                sf.content = truncated_content + "\\n\\n[... truncated to fit token budget ...]"
                sf.token_count = remaining
                sf.reasons.append("truncated")
                sf.truncated = True
                selected.append(sf)
                used += remaining
        else:
            if sf.score >= 40 and available - used > 300:
                if not sf.content_loaded:
                    try:
                        sf.content = sf.path.read_text(errors="ignore")
                        sf.content_loaded = True
                    except OSError:
                        continue
                remaining = available - used
                truncated_content = _truncate_to_tokens(sf.content or "", remaining - 50)
                sf.content = truncated_content + "\\n\\n[... truncated to fit token budget ...]"
                sf.token_count = remaining
                sf.reasons.append("truncated")
                sf.truncated = True
                selected.append(sf)
                used += remaining

    return selected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_terms(text: str) -> list[str]:
    """Extract meaningful terms from a task description for matching."""
    # Remove stop words, keep identifiers and domain terms
    stop = {
        "the",
        "a",
        "an",
        "is",
        "in",
        "to",
        "for",
        "of",
        "and",
        "or",
        "with",
        "that",
        "this",
        "it",
        "be",
        "by",
        "as",
        "at",
        "on",
    }
    words = re.findall(r"[a-z][a-z0-9_]{2,}", text.lower())
    return [w for w in words if w not in stop]


def _extract_trace_symbols(trace: str) -> list[str]:
    """Extract function names, class names, and file names from a stack trace."""
    symbols: list[str] = []
    # Python-style: File "path/file.py", line N, in function_name
    symbols += re.findall(r"in (\w+)", trace)
    # Java-style: at com.example.ClassName.methodName
    symbols += re.findall(r"at [\w.]+\.(\w+)\(", trace)
    # Generic: identifiers that look like symbols (CamelCase or snake_case)
    symbols += re.findall(r"\b([A-Z][a-zA-Z0-9]{2,}|[a-z][a-z0-9]{2,}_[a-z][a-z0-9_]+)\b", trace)
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
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "tsconfig.json",
        "pom.xml",
        "build.gradle",
        "cargo.toml",
        "go.mod",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        ".env.example",
        "makefile",
    }


def _is_lock_file(path: Path) -> bool:
    return path.name.lower() in {
        "poetry.lock",
        "package-lock.json",
        "yarn.lock",
        "pipfile.lock",
        "cargo.lock",
        "composer.lock",
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
            return str(_ENCODING.decode(tokens[:max_tokens]))
        except Exception:
            pass
    # Fallback: truncate by character count
    char_limit = int(max_tokens * _CHARS_PER_TOKEN)
    return text[:char_limit]
