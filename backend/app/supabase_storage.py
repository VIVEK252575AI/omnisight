"""Supabase Storage artifact service (REST, service-role key, server-side only)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol

import httpx

from app.config import Settings


class ArtifactStorage(Protocol):
    def upload(self, bucket: str, object_path: str, data: bytes, content_type: str) -> str: ...
    def signed_url(self, bucket: str, object_path: str, expires_in: int = 3600) -> str: ...
    def delete(self, bucket: str, object_path: str) -> None: ...


class SupabaseStorage:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base = (settings.supabase_url or "").rstrip("/")
        self._key = (settings.supabase_service_role_key or "").strip()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key}", "apikey": self._key}

    def upload(self, bucket: str, object_path: str, data: bytes, content_type: str) -> str:
        url = f"{self._base}/storage/v1/object/{bucket}/{object_path}"
        r = httpx.post(url, headers={**self._headers(), "Content-Type": content_type}, content=data, timeout=60)
        r.raise_for_status()
        return object_path

    def signed_url(self, bucket: str, object_path: str, expires_in: int = 3600) -> str:
        url = f"{self._base}/storage/v1/object/sign/{bucket}/{object_path}"
        r = httpx.post(url, headers=self._headers(), json={"expiresIn": expires_in}, timeout=30)
        r.raise_for_status()
        signed = r.json().get("signedURL") or r.json().get("signedUrl")
        return f"{self._base}{signed}" if signed and signed.startswith("/") else signed

    def delete(self, bucket: str, object_path: str) -> None:
        url = f"{self._base}/storage/v1/object/{bucket}"
        r = httpx.delete(url, headers=self._headers(), json={"prefixes": [object_path]}, timeout=30)
        r.raise_for_status()


class LocalStorage:
    """Test-double storage backed by the local filesystem."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def upload(self, bucket: str, object_path: str, data: bytes, content_type: str) -> str:
        target = self._root / bucket / object_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return object_path

    def signed_url(self, bucket: str, object_path: str, expires_in: int = 3600) -> str:
        return f"file://{(self._root / bucket / object_path).resolve()}"

    def delete(self, bucket: str, object_path: str) -> None:
        target = self._root / bucket / object_path
        if target.exists():
            target.unlink()


import os
...
def build_storage(settings: Settings) -> ArtifactStorage:
    url = settings.supabase_url or os.getenv("SUPABASE_URL")
    key = settings.supabase_service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if url and key:
        settings.supabase_url = url
        settings.supabase_service_role_key = key
        return SupabaseStorage(settings)
    return LocalStorage(Path(settings.artifacts_dir))
