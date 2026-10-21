"""Versioned AI prompt loading."""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(name: str) -> str:
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("invalid prompt name")
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.is_file():
        raise RuntimeError(f"prompt template not found: {name}")
    return path.read_text(encoding="utf-8").strip()
