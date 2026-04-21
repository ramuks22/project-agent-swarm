"""
tool_sandbox.py — Security verification for agent-suggested shell commands.
"""

import logging
import re

logger = logging.getLogger(__name__)

# List of explicitly forbidden binary patterns in suggested commands
BLOCKLISTED_COMMANDS = [
    r"(?i)\brm\s+-(?:r|f|rf|fr)\b",
    r"(?i)\bmkfs\b",
    r"(?i)\bdd\s+if=",
    r"(?i)\bshutdown\b",
    r"(?i)\breboot\b",
    r"(?i)\bchmod\s+777\b",
]


def is_command_safe(command: str) -> tuple[bool, str]:
    """
    Checks if a shell command is safe to show to a user for validation.
    Returns (is_safe, reason).
    """
    for pattern in BLOCKLISTED_COMMANDS:
        if re.search(pattern, command):
            return False, f"Potentially destructive command detected: {pattern}"

    # Check for chained commands or redirects that might hide payload
    if ";" in command or "&&" in command or "|" in command:
        logger.debug("Command contains chaining, tagging for close manual review.")

    return True, "Safe to review"
