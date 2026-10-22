"""Verify server-side GitHub App access to one configured repository."""

from __future__ import annotations

import anyio
import json
import os
import sys

from app.config import get_settings
from app.github_app import GitHubAppClient, GitHubIntegrationFailure


async def _verify() -> int:
    owner = os.environ.get("OMNISIGHT_GITHUB_TARGET_OWNER", "").strip()
    repository = os.environ.get("OMNISIGHT_GITHUB_TARGET_REPOSITORY", "").strip()
    branch = os.environ.get("OMNISIGHT_GITHUB_TARGET_BRANCH", "").strip() or None
    if not owner or not repository:
        print(
            "Set OMNISIGHT_GITHUB_TARGET_OWNER and OMNISIGHT_GITHUB_TARGET_REPOSITORY.",
            file=sys.stderr,
        )
        return 2
    try:
        result = await GitHubAppClient(get_settings()).verify_repository(
            owner, repository, base_branch=branch
        )
    except GitHubIntegrationFailure as exc:
        print(f"GitHub App verification failed [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(_verify))
