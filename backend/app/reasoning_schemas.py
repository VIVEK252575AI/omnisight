"""Structured, review-only LLM reasoning contracts."""
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

class ReasoningPriority(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_ids: list[str] = Field(min_length=1, max_length=20)
    priority: Literal["critical", "high", "medium", "low"]
    score: float = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=1200)

class FindingGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    finding_ids: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=1000)

class RootCauseHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_ids: list[str] = Field(min_length=1, max_length=20)
    hypothesis: str = Field(min_length=1, max_length=1500)
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)
    source_inspection_required: bool = True

class ReasoningResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scan_id: str
    summary: str = Field(min_length=1, max_length=2500)
    priorities: list[ReasoningPriority] = Field(default_factory=list, max_length=30)
    related_findings: list[FindingGroup] = Field(default_factory=list, max_length=30)
    observed_facts: list[str] = Field(default_factory=list, max_length=30)
    root_cause_hypotheses: list[RootCauseHypothesis] = Field(default_factory=list, max_length=30)
    unknowns: list[str] = Field(default_factory=list, max_length=30)
    repair_plan: list[str] = Field(default_factory=list, max_length=20)
    verification_checks: list[str] = Field(default_factory=list, max_length=20)
    provider: str = "Gemini"
    model: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
