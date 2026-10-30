# Week 3 Write-Up — OmniSight

## Scope
Self-healing loop (apply fix, re-screenshot, VLM verifies) + PyGithub branch/commit/PR workflow.

## Implemented
- Delivery service (`backend/app/delivery.py`): `DeliveryService.deliver(scan_id, finding_id)` gated on attempt verified, validation passed, verification policy, allowlisted paths, isolated workspace, scan not cancelled/escalated, GitHub configured (or dry-run).
- PR body builder with finding summary, category, target, severity/confidence, validation results, verification result (MOCK/real label), before/after artifacts, attempt count, AI-generated warning.
- Persisted PR metadata (`PRRecord`: number, url, branch, head_sha, created_at, dry_run, status/sub_state) and scan transition to `awaiting_review`.
- Dry-run default: exact branch/commit/PR payloads without calling GitHub.
- Endpoints: `POST /api/v1/scans/{id}/deliver`, `GET /api/v1/scans/{id}/pull-request`.
- GitHub error handling (W3.7): typed error codes (github_not_configured/auth_failed/permission_denied/rate_limited/not_found/conflict/unavailable/unknown), partial-state recording (`delivery_partial`), retry resume via existing branch/PR detection, retryable + retry_after_seconds.
- Tests: gate failures, success path, duplicate delivery, dry-run, escalated/failed scan blocked.

## Safety
- No merge/auto-merge/deploy code path. Asserted via tests.
- Failure after branch creation recorded as `delivery_partial` with next-action hint.
- Secrets redacted everywhere (sentinel test).

## Status labels
- Delivery service & endpoints: Tested against local application (import + collection).
- GitHub error mapping: Implemented; mocked only (no real PyGithub).
- E2E: Mocked/fixture (skipped by default; requires OMNISIGHT_E2E=1).

## Known limitations
- PyGithub SDK not installed; real client paths unverified.
- PR records are process-local in-memory.
- No real VLM/Playwright/Docker exercised in E2E.

## Contract changes
- New endpoints: POST/GET /api/v1/scans/{id}/deliver|pull-request.
- New ScanStatus: `awaiting_review`.
- New ErrorCode values: github_*, delivery_*.
- New schema: `PRRecord`.

## Work needed from teammates
- Playwright in-process collector; real Gemini key; Docker sandbox; authorized test repo for real GitHub delivery.
```
echo written