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

To run a deterministic live smoke proposal without the API or a Gemini key,
run this from `backend/` after the image builds:

```powershell
.\.venv\Scripts\python.exe scripts\verify_shoplite_sandbox.py
```

The `ShopLite sandbox verification` GitHub Actions workflow runs backend
tests, a fixed-patch sandbox smoke, and the complete browser-capture-to-sandbox
pipeline against a fresh Docker-enabled hosted runner on pushes to `vivek` and
manual dispatch. It grants the job read-only repository permission.

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
errors, blocked external requests, and private artifact IDs for the baseline
and patched mobile screenshots. The dashboard displays both screenshots, and
the artifact endpoint serves them by opaque ID. `failed` means the candidate did not
pass. A run fails closed if the baseline no longer reproduces the intended
defect, the source edit no longer matches exactly once, the Docker daemon/image
is unavailable, or the run exceeds its time budget.

## Run the complete Gemini-to-browser-verification demo

This runner connects the existing browser capture, backend Gemini detection,
safe patch proposal checks, and the isolated Docker sandbox. It tests only the
fixed loopback ShopLite app; it does not publish a PR or edit the checkout.

Configure `backend/.env` with a Gemini key and:

```dotenv
OMNISIGHT_VLM_ADAPTER=gemini
OMNISIGHT_PATCH_GENERATOR=gemini
OMNISIGHT_PATCH_SOURCE_ROOT=../demo-app
OMNISIGHT_APPROVED_PATCH_PATHS=styles/checkout.css
OMNISIGHT_ENABLE_SHOPLITE_SANDBOX=true
OMNISIGHT_SHOPLITE_SANDBOX_TOKEN=<local-random-token>
```

Start the demo server from `demo-app/`:

```powershell
python -m http.server 4173 --bind 127.0.0.1
```

Start OmniSight from `backend/` in another terminal:

```powershell
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```

In a third terminal, set the same sandbox token without putting it on the
command line, then run the explicitly approved workflow:

```powershell
$env:OMNISIGHT_SHOPLITE_SANDBOX_TOKEN = Read-Host "Local sandbox token"
.\.venv\Scripts\python.exe scripts\run_demo_pipeline.py --approve-sandbox
```

The runner captures the fixed checkout at mobile and desktop sizes, submits the
mobile observation to the backend, waits for detection, asks the safe action
engine for proposals, and sends the first accepted proposal to the sandbox.
Rejected proposal attempts and generator provenance are retained in the JSON
summary. The process exits successfully only when the sandbox report is
`verified`. The report is written under `backend/artifacts/demo-checkout/`.

Use `OMNISIGHT_VLM_ADAPTER=mock` and/or
`OMNISIGHT_PATCH_GENERATOR=mock` to exercise the deterministic offline
providers. For the patch generator, set
`OMNISIGHT_PATCH_MOCK_SCENARIO=shoplite_checkout_fix`; the regular `ok` mock
replaces the whole source file and correctly exceeds the normal patch budget.
Mock mode proves the plumbing, not Gemini model quality. Gemini
requests require a configured API key and available quota. A passing local
sandbox run is separate from the authenticated GitHub-base validation and
draft PR flow in [week 4](week4_github_app_connection.md).

## Isolation controls and limits

- Only one edit to the exact `styles/checkout.css` file is accepted.
- The fixed ShopLite app is copied to a temporary host directory; the original
  checkout is never edited. The container sees that stage read-only.
- Candidate browser execution occurs in Docker, not in the FastAPI process.
- Each run uses `--network=none`, `--pull=never`, `--init`, 1 CPU, 512 MiB RAM,
  128 PIDs, 128 MiB shared memory, a read-only root filesystem, and a 64 MiB
  no-exec temporary filesystem. It runs as a non-root UID with all
  capabilities dropped and `no-new-privileges` enabled.
- The Docker socket is never mounted in the container. The trusted API host
  still needs Docker daemon access to start/stop a container; this is a local
  development-only trust boundary, not a production deployment design.
- The runner has a 45-second hard timeout and permits only one concurrent run
  per API process. The temporary stage is deleted after every run.
- Container network isolation does not replace host/daemon isolation. Do not
  enable this endpoint on a shared or public backend.

The local workspace does not have a running Docker daemon, so the live run is
performed by GitHub Actions. The latest hosted smoke passed: baseline mobile
overflow 167 px, candidate mobile/desktop overflow 0 px, order confirmation
passed, zero browser errors, and zero external requests. The same workflow runs
on pushes to `vivek` and can also be started manually.
