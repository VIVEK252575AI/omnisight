# Week 3 End-to-End Report — OmniSight

## Scope
Success path (clipped checkout button) and failure path exercised through the
FastAPI API. GitHub operations ran in dry-run/fake mode only; no real
repository was touched.

## Stage table

| Stage | Implementation | Result | Evidence |
|---|---|---|---|
| Scan creation | Real (FastAPI + PostgreSQL when configured) | OK | API transcript |
| Evidence collection | Fixture/mock (Playwright collector not wired in-process) | MOCK | fixture observation JSON |
| VLM detection | Mock adapter (default) | MOCK | findings response |
| Patch proposal | Mock generator (default) | MOCK | patch proposal record |
| Isolated apply | Real `SourcePatchApplier` (when repair enabled) | Real (gated) | workspace diff |
| Build/test validation | Config-driven commands (none configured by default) | not_configured | validation report |
| Visual verification | Mock VLM verifier (default) | MOCK | verification report |
| Delivery (branch/commit/PR) | FakeGitHubService (dry-run) | MOCK/DRY-RUN | dry-run PR payload |
| Scan state | `awaiting_review` after successful delivery | OK | scan response |

## Failure path
- Ineffective patch: build passes, defect remains → scan ends `escalated`
  after bounded attempts; rollbacks verified by hash; no PR payload created.
- Unapproved-path patch: rejected at proposal validation (`path_not_approved`).

## Limitations
- No real VLM, Playwright, Docker sandbox, or GitHub integration was
  exercised in this run; all AI/GitHub stages are mocked or fixture-based.
- Real end-to-end verification requires configured Gemini key, Playwright
  Chromium, Docker, and an explicitly authorized test repository.
