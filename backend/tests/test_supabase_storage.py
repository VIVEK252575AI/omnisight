"""Supabase storage tests (local test double only)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.config import Settings
from app.supabase_storage import LocalStorage


def test_local_storage_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        s = LocalStorage(Path(d))
        s.upload("b", "x/y.txt", b"hello", "text/plain")
        assert s.signed_url("b", "x/y.txt").startswith("file://")
        s.delete("b", "x/y.txt")
