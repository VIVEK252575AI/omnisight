# Week 3 — Controlled ShopLite Docker sandbox

This slice verifies one human-approved patch against the repository's fixed
`demo-app` checkout. It does not apply changes to the repository, create a PR,
or accept an arbitrary repository, URL, command, Docker image, or filesystem
path.

## One-time local setup

Build the pinned Playwright image from the repository root (the image build
needs network access to download its base image):

```powershell
docker build --tag omnisight-shoplite-sandbox:1 --file demo-app/sandbox/Dockerfile demo-app/sandbox
```

Configure `backend/.env` for the existing proposal flow and this opt-in demo:

```dotenv
OMNISIGHT_PATCH_SOURCE_ROOT=../demo-app
OMNISIGHT_APPROVED_PATCH_PATHS=styles/checkout.css
OMNISIGHT_ENABLE_SHOPLITE_SANDBOX=true
OMNISIGHT_SHOPLITE_SANDBOX_TOKEN=<long-random-local-secret>
```

The backend should be bound to loopback for local development. The new endpoint
also rejects non-loopback clients and requires the approval token in a header.
Keep this API local; this milestone does not add general user authentication.

## Verify a proposal

First create a finding and a valid patch proposal through the existing
`/api/v1/findings/{finding_id}/patch-proposals` workflow. Review the proposal,
then explicitly approve the demo-only run:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/v1/demo/shoplite/patch-proposals/<proposal-id>/validate `
  -H "Content-Type: application/json" `
  -H "X-OmniSight-Demo-Approval: <long-random-local-secret>" `
  -d '{"approval":"approve-demo-only"}'
```

A successful response has `status: verified` and reports the baseline mobile
overflow, patched mobile/desktop overflow, checkout confirmation, browser
errors, and blocked external requests. `failed` means the candidate did not
pass. A run fails closed if the baseline no longer reproduces the intended
defect, the source edit no longer matches exactly once, the Docker daemon/image
is unavailable, or the run exceeds its time budget.

## Isolation controls and limits

- Only one edit to the exact `styles/checkout.css` file is accepted.
- The fixed ShopLite app is copied to a temporary host directory; the original
  checkout is never edited. The container sees that stage read-only.
- Candidate browser execution occurs in Docker, not in the FastAPI process.
- Each run uses `--network=none`, `--pull=never`, 1 CPU, 512 MiB RAM, 128 PIDs,
  a read-only root filesystem, a 64 MiB no-exec temporary filesystem, a
  non-root UID, dropped capabilities, and `no-new-privileges`.
- The Docker socket is never mounted in the container. The trusted API host
  still needs Docker daemon access to start/stop a container; this is a local
  development-only trust boundary, not a production deployment design.
- The runner has a 45-second hard timeout and permits only one concurrent run
  per API process. The temporary stage is deleted after every run.
- Container network isolation does not replace host/daemon isolation. Do not
  enable this endpoint on a shared or public backend.

The current configured machine did not have a running Docker daemon when this
slice was implemented. The Python-side policy, temporary staging, endpoint
gates, and Docker command are covered by tests; run the one-time setup above on
a machine with Docker Desktop/Linux Engine to verify the actual container.
