"""
prompt_guard.py — Lightweight pre-processor to detect prompt injection risks.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Basic heuristics for detecting injection attempts
INJECTION_PATTERNS = [
    r"(?i)ignore (?:all )?previous instructions",
    r"(?i)disregard (?:all )?previous instructions",
    r"(?i)you are now (?:a|an) (?!architect|implementer|qa-engineer|reviewer|debugger)",
    r"(?i)system (?:is )?off",
    r"(?i)new role:",
    r"(?i)bypass (?:all )?filters",
    r"(?i)dan mode",
]


def scan_for_injection(text: str) -> bool:
    """
    Scans a string for common prompt injection patterns.
    Returns True if an injection attempt is suspected.
    """
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text):
            logger.warning("Potential prompt injection attempt detected: %s", pattern)
            return True
    return False


def protect_prompt(text: str) -> str:
    """
    Applies defensive wrappers to a user-provided prompt.
    """
    # Simple defense-in-depth: sandwiching the prompt in clear markers
    protected = f"--- BEGIN USER TASK DESCRIPTION ---\n{text}\n--- END USER TASK DESCRIPTION ---"
    return protected
