"""Version-controlled prompt templates.

Prompts live as plain ``.txt`` files next to this module so every prompt has a
stable version identity (``visual-analysis-v1``, ...). The version string is
recorded on the request/result so old analyses stay reproducible.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

VISUAL_ANALYSIS_PROMPT_VERSION = "visual-analysis-v2"
STRUCTURED_OUTPUT_REPAIR_PROMPT_VERSION = "structured-output-repair-v1"
PATCH_PROPOSAL_PROMPT_VERSION = "patch-proposal-v1"


class PromptNotFoundError(RuntimeError):
    """Raised when a prompt template file is missing on disk."""


def load_prompt(name: str) -> str:
    """Return the text of the prompt template ``name`` (without extension)."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise PromptNotFoundError(f"invalid prompt name: {name!r}")
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.is_file():
        raise PromptNotFoundError(f"prompt template not found: {name}")
    return path.read_text(encoding="utf-8").strip()
