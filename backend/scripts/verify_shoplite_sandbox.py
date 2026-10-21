"""Run the live fixed ShopLite sandbox check after its Docker image is built."""

from __future__ import annotations

import json
import sys

from app.config import Settings
from app.patch_schemas import PatchEdit, PatchFile, PatchProposal
from app.shoplite_sandbox import SandboxFailure, run_shoplite_sandbox


def main() -> int:
    proposal = PatchProposal(
        finding_id="shoplite-sandbox-smoke",
        rationale="Make the checkout action responsive to the available width.",
        generator_label="SMOKE_FIXTURE",
        files=[
            PatchFile(
                path="styles/checkout.css",
                edits=[
                    PatchEdit(old_text="width: 520px;", new_text="width: 100%;")
                ],
            )
        ],
    )
    settings = Settings(
        enable_shoplite_sandbox=True,
        shoplite_sandbox_token="explicit-cli-smoke-run",
        approved_patch_paths=["styles/checkout.css"],
        patch_allowed_extensions=[".css"],
    )
    try:
        report = run_shoplite_sandbox(
            proposal,
            proposal_id="shoplite-sandbox-smoke",
            settings=settings,
        )
    except SandboxFailure as exc:
        print(f"ShopLite sandbox smoke failed [{exc.code}]: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report.model_dump(mode="json"), indent=2))
    return 0 if report.status == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
