"""Path normalisation and allow-list enforcement for proposed patches.

Every path a generator (or extractor) produces is untrusted. Before a proposal
can be stored as ``proposed`` its paths must be:

1. **normalised** — a single forward-slash, repo-relative form. Absolute paths,
   Windows drive letters, UNC paths, ``..`` segments, NUL bytes and empty
   segments are rejected outright (:func:`normalize_relpath`);
2. **not forbidden** — ``.git``, ``.env*``, lockfiles, CI configuration and
   build configuration are rejected unless they are named *verbatim* (no glob
   characters) by an approved path — that is what "unless explicitly
   allowlisted" means here;
3. **an allowed extension** — ``.css``/``.scss``/``.jsx``/``.tsx`` by default
   (``.js``/``.ts`` only if listed in ``OMNISIGHT_PATCH_ALLOWED_EXTENSIONS``);
4. **allow-listed** — matched by ``OMNISIGHT_APPROVED_PATCH_PATHS``, whose
   default is EMPTY so everything is rejected until the ShopLite handoff.

Pattern matching uses :func:`fnmatch.fnmatchcase` semantics, where ``*`` also
crosses ``/``. Prefer directory-glob style (``styles/**``) and match the full
relative path.

This module is pure string logic: it never touches the filesystem. Symlink
escape is handled where files are actually read
(:func:`app.source_context.build_source_context`).
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Iterable, Sequence

from app.config import Settings
from app.patch_schemas import RejectionReason

_DRIVE_RE = re.compile(r"^[A-Za-z]:")

_LOCKFILES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pnpm-lock.yml",
        "bun.lockb",
        "composer.lock",
        "poetry.lock",
        "pipfile.lock",
        "cargo.lock",
        "gemfile.lock",
    }
)

_BUILD_FILES = frozenset(
    {
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "tsconfig.json",
        "jsconfig.json",
        "webpack.config.js",
        "vite.config.js",
        "vite.config.ts",
        "rollup.config.js",
        "babel.config.js",
        "babel.config.json",
        "postcss.config.js",
        "tailwind.config.js",
        "next.config.js",
        "next.config.mjs",
        "nuxt.config.js",
        ".eslintrc",
        ".eslintrc.js",
        ".eslintrc.json",
        ".prettierrc",
        ".prettierrc.json",
    }
)

_CI_DIRS = frozenset({".github", ".circleci", ".gitlab"})
_CI_FILES = frozenset(
    {
        ".gitlab-ci.yml",
        "jenkinsfile",
        "azure-pipelines.yml",
        ".travis.yml",
        "bitbucket-pipelines.yml",
    }
)


def normalize_relpath(raw: object) -> str | None:
    """Return a canonical repo-relative path, or None if the input is unsafe.

    Accepted input is only ever a *relative* POSIX-style path. Anything that
    could escape the source root (absolute, drive-qualified, UNC, ``..``) is
    rejected rather than repaired.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace("\\", "/")
    if not text or "\x00" in text:
        return None
    if _DRIVE_RE.match(text) or text.startswith("//") or text.startswith("/"):
        return None
    parts = text.split("/")
    if any(part == ".." for part in parts):
        return None
    cleaned = [part for part in parts if part not in (".", "")]
    if not cleaned:
        return None
    return "/".join(cleaned)


def _normalize_patterns(patterns: Iterable[str]) -> list[str]:
    out: list[str] = []
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        norm = pattern.strip().replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        if norm and norm != ".":
            out.append(norm)
    return out


def is_approved(relpath: str, patterns: Sequence[str]) -> bool:
    """True when ``relpath`` matches at least one allow-list pattern."""
    return any(fnmatchcase(relpath, pattern) for pattern in _normalize_patterns(patterns))


def _is_explicit_literal(relpath: str, patterns: Sequence[str]) -> bool:
    """True when a glob-free approved pattern names this exact file.

    This is the "unless explicitly allowlisted" escape: a lockfile or build
    config may only be patched when a pattern names that exact path with no
    wildcard characters.
    """
    for pattern in _normalize_patterns(patterns):
        if not any(ch in pattern for ch in "*?[") and pattern == relpath:
            return True
    return False


def is_explicitly_allowlisted(relpath: str, patterns: Sequence[str]) -> bool:
    """Public alias for the verbatim-allowlisting escape hatch."""
    return _is_explicit_literal(relpath, patterns)


def is_forbidden(relpath: str) -> bool:
    """True for VCS, env, lockfile, CI and build-config paths.

    Callers must still honour an explicit verbatim allow-list entry; see
    :func:`is_forbidden_for` / :func:`path_rejection`.
    """
    parts = relpath.split("/")
    basename = parts[-1]
    lowered = basename.lower()
    if ".git" in parts:
        return True
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    if lowered in _LOCKFILES:
        return True
    if lowered in _BUILD_FILES:
        return True
    if lowered in _CI_FILES:
        return True
    return any(part.lower() in _CI_DIRS for part in parts[:-1])


def is_forbidden_for(relpath: str, patterns: Sequence[str]) -> bool:
    """True when ``relpath`` is forbidden and not explicitly allowlisted."""
    return is_forbidden(relpath) and not _is_explicit_literal(relpath, patterns)


def path_rejection(relpath: str, settings: Settings) -> RejectionReason | None:
    """Return the reason ``relpath`` is not patchable, or None when it is.

    ``relpath`` must already be the output of :func:`normalize_relpath`.
    Checks run most-specific first so a lockfile reports ``forbidden_file``
    rather than the vaguer ``path_not_allowed``.
    """
    if is_forbidden_for(relpath, settings.approved_patch_paths):
        return RejectionReason.forbidden_file

    suffix = PurePosixPath(relpath).suffix.lower()
    allowed = {ext.lower() for ext in settings.patch_allowed_extensions}
    if suffix not in allowed:
        return RejectionReason.extension_not_allowed

    if not is_approved(relpath, settings.approved_patch_paths):
        return RejectionReason.path_not_allowed

    return None
