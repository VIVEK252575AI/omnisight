"""Read-only retrieval of approved source files as patch context (W2.5).

The context is what a patch generator is allowed to see. It is built ONLY from
files that already pass the allow-list (:data:`app.patch_paths`), it is
size-capped, and it is read-only — nothing in this module writes, moves,
deletes or executes anything.

Symlink handling: a candidate is skipped unless both the link itself and its
resolved target stay inside the resolved source root, so an ``approved``
directory can never be used to read outside the tree.

When no source root is configured (the default — the ShopLite tree is not in
this repository yet) or the allow-list is empty, the context is empty and the
note explains why. The proposal is then rejected on path safety, never
silently accepted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import Settings
from app.patch_paths import is_approved, is_forbidden_for

if TYPE_CHECKING:  # pragma: no cover - type-checking only, keeps this module a
    from app.ai.contracts import Finding  # leaf (no runtime import of app.ai)

logger = logging.getLogger("omnisight")

# Hard ceiling on how many files a single proposal may be built from, so a
# misconfigured `styles/**` over a huge tree cannot flood memory.
MAX_CONTEXT_FILES = 20

_SELECTORS = re.compile(r"([#.])([A-Za-z_][\w-]*)")


@dataclass(frozen=True)
class SourceFile:
    """One approved file included in the context."""

    path: str
    text: str
    bytes: int
    truncated: bool = False


@dataclass(frozen=True)
class SourceContext:
    """Everything a generator may see, plus why it may be empty."""

    root: str | None = None
    files: list[SourceFile] = field(default_factory=list)
    matched_selectors: dict[str, list[str]] = field(default_factory=dict)
    truncated: bool = False
    note: str | None = None

    @property
    def empty(self) -> bool:
        return not self.files


def selector_tokens(target_candidate: str | None) -> list[str]:
    """Extract searchable bare tokens from a CSS selector like ``#cart.total``."""
    if not target_candidate:
        return []
    text = target_candidate.strip()
    if not text:
        return []
    tokens = [f"{prefix}{name}" for prefix, name in _SELECTORS.findall(text)]
    bare = text[1:] if text[0] in "#." else text
    if bare and re.fullmatch(r"[A-Za-z_][\w-]*", bare):
        tokens.append(bare)
    # Deterministic, de-duplicated, order preserved.
    return list(dict.fromkeys(t for t in tokens if t))


def _token_in_text(token: str, text: str) -> bool:
    """True when ``token`` appears as a whole token (not inside a longer word)."""
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _iter_candidates(root: Path):
    try:
        yield from sorted(p for p in root.rglob("*") if p.is_file())
    except OSError as exc:  # pragma: no cover - unreadable tree
        logger.warning("source tree walk failed: %s", exc)


def build_source_context(finding: Finding, settings: Settings) -> SourceContext:
    """Collect the approved, size-capped files relevant to ``finding``."""
    configured = (settings.patch_source_root or "").strip()
    root = Path(configured) if configured else None
    root_resolved: Path | None = None
    if root is not None and root.is_dir():
        try:
            root_resolved = root.resolve()
        except OSError:
            root_resolved = None
    else:
        root = None

    patterns = settings.approved_patch_paths
    if root_resolved is None:
        return SourceContext(
            root=configured or None,
            note="source_root_unavailable",
        )
    if not patterns:
        return SourceContext(
            root=str(root_resolved),
            note="approved_patch_paths_empty",
        )

    allowed_extensions = {ext.lower() for ext in settings.patch_allowed_extensions}
    budget = settings.patch_context_max_bytes
    files: list[SourceFile] = []
    used = 0
    truncated = False

    for path in _iter_candidates(root_resolved):
        if len(files) >= MAX_CONTEXT_FILES or used >= budget:
            truncated = True
            break
        if not _is_within(path, root_resolved):
            continue
        if path.is_symlink():
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not _is_within(resolved, root_resolved):
                continue
        try:
            rel = path.relative_to(root_resolved).as_posix()
        except ValueError:
            continue
        if not is_approved(rel, patterns):
            continue
        # Defence in depth: never feed secrets/VCS/lockfiles to a model even if
        # a broad allow-list pattern would have swept them up.
        if is_forbidden_for(rel, patterns):
            continue
        if path.suffix.lower() not in allowed_extensions:
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.warning("skipping unreadable source file %s: %s", rel, exc)
            continue

        remaining = budget - used
        if len(raw) > remaining:
            text = raw[:remaining].decode("utf-8", errors="replace")
            files.append(SourceFile(path=rel, text=text, bytes=remaining, truncated=True))
            used = budget
            truncated = True
            break
        files.append(
            SourceFile(
                path=rel,
                text=raw.decode("utf-8", errors="replace"),
                bytes=len(raw),
            )
        )
        used += len(raw)

    matched: dict[str, list[str]] = {}
    for token in selector_tokens(finding.target_candidate):
        hits = [f.path for f in files if _token_in_text(token, f.text)]
        if hits:
            matched[token] = hits

    note = None
    if not files:
        note = "no_approved_files_found"

    return SourceContext(
        root=str(root_resolved),
        files=files,
        matched_selectors=matched,
        truncated=truncated,
        note=note,
    )
