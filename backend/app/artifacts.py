"""Safe resolution of evidence artifacts referenced by observations.

All artifact paths supplied by a collector are treated as hostile input. A
path is only accepted when it is:

- relative (no absolute paths, no drive/anchor),
- free of ``..`` segments and null bytes,
- resolved *within* the configured artifact root (symlink escapes are caught
  because :meth:`Path.resolve` follows links before the containment check),
- of an allowed extension (``.png`` / ``.jpg`` / ``.jpeg`` / ``.json``),
- an existing regular file within the size limit, and
- for images, actually carrying PNG/JPEG magic bytes (extension is not proof).
"""

from pathlib import Path

from fastapi import status

from app.config import Settings
from app.errors import DomainError, ErrorCode

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".json"})
IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg"})

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".json": "application/json",
}


def artifact_root(settings: Settings) -> Path:
    """Return the resolved artifact root directory."""
    return Path(settings.artifacts_dir).resolve()


def _reject(message: str) -> DomainError:
    return DomainError(
        ErrorCode.ARTIFACT_INVALID,
        message,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _has_image_signature(path: Path) -> bool:
    with path.open("rb") as fh:
        head = fh.read(8)
    return head.startswith(_PNG_SIGNATURE) or head.startswith(_JPEG_SIGNATURE)


def resolve_artifact(
    relative: str,
    settings: Settings,
    *,
    expect_image: bool = False,
) -> Path:
    """Resolve ``relative`` against the artifact root, or raise a 400 error."""
    if not isinstance(relative, str) or not relative.strip():
        raise _reject("artifact path is empty")
    if "\x00" in relative:
        raise _reject("artifact path contains a null byte")

    rel = Path(relative)
    if rel.is_absolute() or rel.drive or rel.anchor:
        raise _reject("absolute artifact paths are not allowed")
    if any(part == ".." for part in rel.parts):
        raise _reject("artifact path must not contain '..'")

    root = artifact_root(settings)
    resolved = (root / rel).resolve()

    try:
        resolved.relative_to(root)
    except ValueError:
        raise _reject("artifact path escapes the artifact root") from None

    ext = resolved.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise _reject(f"unsupported artifact extension {ext!r}")
    if expect_image and ext not in IMAGE_EXTENSIONS:
        raise _reject("screenshot must be a png or jpeg image")

    if not resolved.is_file():
        raise _reject(f"artifact not found: {relative}")

    limit = (
        settings.artifact_max_image_bytes
        if ext in IMAGE_EXTENSIONS
        else settings.artifact_max_json_bytes
    )
    if resolved.stat().st_size > limit:
        raise _reject(f"artifact exceeds the {limit} byte limit")

    if ext in IMAGE_EXTENSIONS and not _has_image_signature(resolved):
        raise _reject("file does not contain valid png/jpeg image data")

    return resolved


def media_type_for(path: Path) -> str:
    """Return the media type for a resolved artifact path."""
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
