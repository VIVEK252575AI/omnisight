"""Request and response schemas for local-owner project setup."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    target_url: str = Field(min_length=1, max_length=2048)
    environment_kind: Literal["development", "staging", "other"] = "staging"

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("target URL contains an invalid port") from None
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("target URL must use http or https")
        if not parsed.hostname:
            raise ValueError("target URL must include a host")
        if parsed.username or parsed.password:
            raise ValueError("target URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("target URL must not contain a query string or fragment")
        host = parsed.hostname.lower()
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc += f":{port}"
        path = parsed.path.rstrip("/")
        return parsed._replace(scheme=parsed.scheme.lower(), netloc=netloc, path=path).geturl()


class TargetSummary(BaseModel):
    id: str
    name: str
    base_url: str
    environment_kind: str
    authorization_status: str
    version: int
    created_at: datetime


class ProjectSummary(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    targets: list[TargetSummary]
    repository_status: Literal["not_connected"] = "not_connected"
