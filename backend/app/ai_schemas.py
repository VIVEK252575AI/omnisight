"""Contracts for review-only Gemini patch suggestions."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas import Finding

PATCHABLE_SUFFIXES = frozenset({".css", ".scss", ".jsx", ".tsx"})


class SourceFileContext(BaseModel):
    """One caller-supplied, bounded source excerpt; never written by this API."""

    model_config = {"extra": "forbid"}

    path: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1, max_length=8000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if "\\" in value or value.startswith("/"):
            raise ValueError("path must be a relative POSIX path")
        path = PurePosixPath(value)
        if any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("path must not contain traversal components")
        if path.suffix.lower() not in PATCHABLE_SUFFIXES:
            raise ValueError("only CSS, SCSS, JSX, and TSX files are supported")
        return path.as_posix()


class PatchSuggestionRequest(BaseModel):
    """A validated finding plus only the source excerpts Gemini may reference."""

    model_config = {"extra": "forbid"}

    finding: Finding
    source_files: list[SourceFileContext] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def paths_are_unique(self):
        paths = [source.path for source in self.source_files]
        if len(paths) != len(set(paths)):
            raise ValueError("source file paths must be unique")
        return self


class SuggestedEdit(BaseModel):
    model_config = {"extra": "forbid"}

    old_text: str = Field(min_length=1, max_length=8000)
    new_text: str = Field(min_length=1, max_length=8000)


class SuggestedFilePatch(BaseModel):
    model_config = {"extra": "forbid"}

    path: str = Field(min_length=1, max_length=240)
    edits: list[SuggestedEdit] = Field(min_length=1, max_length=5)


class GeminiPatchPayload(BaseModel):
    """Strictly parsed provider response before source-context validation."""

    model_config = {"extra": "forbid"}

    schema_version: Literal[1]
    finding_id: str = Field(min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=1000)
    generator_label: str = Field(min_length=1, max_length=100)
    files: list[SuggestedFilePatch] = Field(max_length=5)


class PatchSuggestionResponse(BaseModel):
    """Human-reviewable output; never means the patch has been applied."""

    finding_id: str
    provider: Literal["gemini"] = "gemini"
    model: str
    rationale: str
    files: list[SuggestedFilePatch]
    status: Literal["proposed", "no_safe_patch"] = "proposed"
