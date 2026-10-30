# Week 3 Write-Up — Self-Healing Loop & GitHub Delivery (W3.1 – W3.8)

- **Owner**: Backend / FastAPI Developer
- **Branch**: `lakshy` (`VIVEK252575AI/omnisight`)
- **Nature**: implementation + verification. Every capability is labelled with the
  project's status vocabulary; nothing is claimed without a run behind it.

---

## 1. Scope and outcome

- **W3.1 — Self-healing loop**: apply a validated patch in an isolated workspace,
  run build/tests, re-screenshot, VLM-verifies the original defect is gone.
- **W3.2 — Docker sandbox validation**: ShopLite-only container build/test with
  timeouts, output caps, argument-list subprocess (no shell interpolation).
- **W3.3 — Verification stage**: build success alone is not proof; deterministic
  browser evidence (bbox vs viewport, visibility, overflow) must agree, and a
  deterministic fail overrides a VLM "fixed".
- **W3.4 — Orchestration**: bounded retry (max 3 attempts), rollback to baseline
  hash after every failure, escalation on exhaustion.
- **W3.5 — GitHub delivery service**: PyGithub branch/commit/PR preparation behind
  a `GitHubService` interface with a fake for tests.
- **W3.6 — Branch, commit and PR workflow after validation gates**:
  `DeliveryService.deliver(scan_id, finding_id)` gated on explicit conditions,
  persisted PR metadata, scan state `awaiting_review`, dry-run default.
- **W3.7 — GitHub error handling**: typed domain errors, partial-state
  recording, safe retry/resume.
- **W3.8 — End-to-end integration test**: success path reaches
  `awaiting_review` with a verified attempt and dry-run PR payload; failure path
  ends `escalated`, baseline hashes restored, no PR payload.

Outcome: **the Detect → Fix → Verify → Deliver pipeline runs end to end against
the local application with mocks** — stored evidence produces validated findings,
a finding produces a reviewable proposal, the patch is applied in an isolated
workspace, built and re-verified, and a draft PR payload is prepared for human
review. No PR is ever merged by the backend.

**What Week 3 did not build**: real VLM integration (provider not finalised), real
GitHub push/PR (dry-run/fake only unless authorised), React dashboard approval
flow (Week 4), image cropping/chunking (Week 4).

---

## 2. Features built

### 2.1 Self-healing loop and orchestration

- **Isolated workspace manager** (`app/workspace_manager.py`): every patch is
  applied in a worktree, never in the FastAPI server's own source tree and never
  on the default branch.
- **Patch applier** (`SourcePatchApplier`): only validated, structured patch
  content; approved paths normalised, no `..`, no absolute paths, no symlink
  escapes; never `.git`, `.env`, lockfiles, CI/build config unless allowlisted;
  size limits enforced.
- **Workspace validator** (`WorkspaceValidator`): runs build/test commands from
  configuration (never from model output) with timeouts, output caps, argument-list
  subprocess calls (no shell string interpolation).
- **Source patch verifier** (`SourcePatchVerifier`): re-screenshots after the fix
  and re-runs the VLM; verification is `verified_fixed` / `still_defective` /
  `inconclusive` / `insufficient_evidence`.
- **Bounded retry** (`run_repair` / `_repair_one`): max 3 attempts per finding
  (configurable); every failed attempt is rolled back to the recorded baseline
  hash and logged; exhaustion escalates the scan.
- **Scan state machine**: `LEGAL_TRANSITIONS` updated for `repairing`,
  `validation_passed`, `validation_failed`, `patch_verified`,
  `verification_failed`, `ready_for_review`, `escalated`, `awaiting_review`;
  terminal states (`ready_for_review`, `awaiting_review`, `failed`, `cancelled`,
  `escalated`) cannot transition further.
- **Crash recovery** (`recover_orphaned_scans`): scans left in `repairing` after a
  restart are settled to retryable `detected` or `escalated`.

### 2.2 Docker sandbox validation

- **ShopLite-only sandbox** (`app/shoplite_sandbox.py`): fixed image, build/test
  commands from config, token-gated, timeout-capped.
- **Validation report** (`ValidationReport`): build/test results, exit codes,
  durations, logs stored as references (not in DB columns for large blobs).
- **Failure is never reported as success**: a failed build or failed visual
  verification can never surface as `validation_passed` / `patch_verified`.

### 2.3 Verification stage

- **Deterministic evidence overrides the VLM**: bounding box vs viewport,
  visibility, overflow are computed from Playwright geometry; a deterministic
  fail overrides a VLM "fixed".
- **Four outcomes**: `verified_fixed`, `still_defective`, `inconclusive`,
  `insufficient_evidence`.
- **A successful build alone does not prove the visual defect is fixed** — the
  re-screenshot and geometry check are mandatory.

### 2.4 GitHub delivery (W3.5 – W3.7)

- **`GitHubService` interface** (`app/github_delivery.py`): `FakeGitHubService`
  (records calls, returns deterministic payloads) and `PyGithubGitHubService`
  (real client, lazy, token redacted).
- **Token redaction** (`redact_github_token`): ghp_/github_pat_/gho_/ghu_/ghs_/ghr_
  prefixes; sentinel-key tests.
- **Branch-name safety** (`GITHUB_BRANCH_PATTERN`): no leading `/`, no `..`, no
  `//`, length ≤ 250.
- **`DeliveryService`** (`app/delivery.py`): gated delivery, PR body builder,
  idempotency (existing branch/PR/PR record reused), dry-run payload.
- **PR metadata** (`PRRecord`): number, url, branch, head_sha, created_at, dry_run,
  status/sub_state, partial_hint, payload.
- **Sub-states**: `branch_created` → `committed` → `pr_opened` → `delivered`;
  failure after `branch_created`/`committed` → `delivery_partial` with hint.
- **Error mapping** (`map_github_error`): 8 typed codes with HTTP status,
  retryable flag, retry_after_seconds; no secret leakage.
- **Endpoints**: `POST /api/v1/scans/{id}/deliver`,
  `GET /api/v1/scans/{id}/pull-request`.
- **Scan state**: `awaiting_review` added to `ScanStatus` and `LEGAL_TRANSITIONS`.

### 2.5 End-to-end test (W3.8)

- **Skippable E2E smoke** (`tests/test_e2e_week3.py`) gated on
  `OMNISIGHT_E2E=1`.
- **Stage table** in `docs/week3_e2e_report.md` with honest real/mock/fixture labels.
- Success path: reaches `awaiting_review` with verified attempt + dry-run PR payload.
- Failure path: ends `escalated`, baseline hashes restored, no PR payload.
- Unapproved-path patch: rejected at proposal validation.

---

## 3. Tests

- **Delivery gates**: each gate failing individually blocks delivery with a named
  unmet gate (`test_delivery_gates.py`).
- **Success path**: fake service; payload content assertions (files, message, PR
  body fields).
- **Idempotent second call**: returns existing PR without re-running gates.
- **Dry-run**: records payload without service calls.
- **Escalated/failed scan blocked**.
- **GitHub error mapping**: each error code at each step (branch/commit/PR).
- **Partial state persistence and resume**.
- **Rate-limit retry-after propagation**.
- **Redaction sentinel test**: no token in any captured output.
- **No merge capability**: test scans the service for merge/auto-merge/deploy calls.
- Existing suite baseline preserved: 557+ passed; 5 pre-existing failures
  (PyGithub SDK missing, contradictory redaction assertion, undefined `status`
  in one test) unchanged.

---

## 4. Fixes and reconciliations

- **Redaction bug fixed** (`github_delivery.py`): token segment bounded to next
  whitespace so a trailing space no longer skips redaction.
- **`_token_present_in_settings` threshold** raised to ≥20 chars so short/repr
  strings are not treated as genuine tokens.
- **Branch pattern** gained a leading-`/` rejection.
- **In-memory scan store fallback** (`storage.py`): `get_store` returns a
  process-wide singleton when `DATABASE_URL` is unset, so the API runs without
  PostgreSQL.
- **Detection/evidence/patch stores** gained the same singleton-in-memory fallback.
- **Playwright capture router** registered in `main.py` (was missing).
- **CORS** extended to allow port 5174 (Vite dev server).
- **Playwright capture** switched from async to `asyncio.create_subprocess_exec`
  (Windows event-loop compatibility), then to a subprocess script; uses
  `domcontentloaded` + optional `networkidle` with 45s timeout for slow sites.
- **Evidence record** added to capture so detection has stored evidence.
- **Scan transition** after capture: `created → evidence_collection`.
- **`awaiting_review`** added to `ScanStatus` and `LEGAL_TRANSITIONS`.
- **Supabase Storage** service (`supabase_storage.py`) with httpx REST client and
  a local test double.

---

## 5. What I learned

- **Dry-run-first is the only safe default for delivery.** Preparing the exact
  payload without touching GitHub keeps the pipeline demonstrable and reviewable.
- **Partial state must be recorded, not just raised.** A branch created but PR
  failed is recoverable only if the sub-state is persisted.
- **Idempotency is a property of the whole flow**, not just the PR call —
  existing branch, existing commit, existing PR record all need detection.
- **Gate lists should be explicit and named.** A 409 with `unmet_gates[]` is
  far more debuggable than a generic 400.
- **Windows async subprocess is a footgun.** `SelectorEventLoop` doesn't support
  subprocess; using `asyncio.create_subprocess_exec` (proactor) or a separate
  process avoids `NotImplementedError`.
- **In-memory singletons are necessary for demo mode.** FastAPI dependencies that
  return a new store per request break cross-request state; a module-level
  singleton fixes it.
- **CORS allow-lists must track dev-server ports.** Adding 5174 was a one-line
  fix that unblocked the frontend.
- **Empty evidence is a real failure mode.** Detection needs a stored observation;
  the Playwright capture must register one, not just save a screenshot file.

## 6. Experience

- **The biggest friction was environment, not code.** Missing PyGithub SDK,
  Windows event-loop issues, port conflicts and CORS each cost more time than
  the feature logic.
- **The end-to-end demo revealed integration gaps.** Capture saved a screenshot
  but never an evidence record; detect then failed with `no_evidence`. Fixing
  this made the whole pipeline runnable.
- **Testing against real sites exposed timeout assumptions.** `networkidle` never
  resolves on some pages; switching to `domcontentloaded` + best-effort
  `networkidle` made capture robust.
- **The W3.8 E2E report forced honesty.** Every stage labelled real/mock/fixture
  prevented overclaiming; only the mock adapter and fake GitHub ran.
- **Small, named fixes beat refactors.** Each 502/404/422 had a specific cause
  (missing router, missing evidence, wrong field name, CORS) and a minimal fix.

## 7. Understanding

- Week 3 covers **Fix → Verify → Deliver**: a validated patch is applied in an
  isolated workspace, built and re-verified, then prepared as a draft PR for
  human review.
- **Verification is two-layered**: a passing build is necessary but not sufficient;
  deterministic browser geometry must confirm the original defect is gone, and a
  deterministic fail overrides a VLM "fixed".
- **Delivery is human-gated.** The backend prepares a branch, commit and PR but
  never merges, auto-merges or deploys. `awaiting_review` is the terminal state.
- **Failure is a first-class outcome.** Bounded retries, rollback to baseline
  hashes, escalation on exhaustion, and `delivery_partial` on mid-flight failure
  ensure nothing is silently lost.
- **Dry-run is the safe default.** Exact branch/commit/PR payloads are produced
  without calling GitHub, so the pipeline is demonstrable without credentials.
- **The pipeline is only as good as its integration.** Each layer (capture,
  evidence, detection, proposal, apply, validate, verify, deliver) must hand off
  correctly; the W3.8 E2E exposed every gap.

---

## 8. Verification evidence

| Check | Result |
|---|---|
| End-to-end flow (example.com) | scan → capture → detect (accepted) → findings=1, status=detected |
| End-to-end flow (emptrackai.online) | capture complete, detect accepted, findings=1, status=detected |
| End-to-end flow (mrittikanaturals.co.in) | capture complete, detect accepted, findings=1, status=detected |
| Delivery gates | each gate failure blocks with named unmet gate |
| Dry-run | payload recorded, no service calls |
| Idempotent delivery | second call returns existing PR |
| No merge capability | asserted via test |
| Redaction sentinel | no token in any captured output |
| Default offline suite | passes with API key unset |

## 9. Status labels (verbatim)

| Capability | Status |
|---|---|
| Self-healing loop (apply/re-screenshot/verify) | Tested against the local application (mocks) |
| Docker sandbox validation | Implemented; not tested (Docker unavailable) |
| Verification stage (deterministic + VLM) | Tested with mocks |
| Bounded retry / rollback / escalation | Tested with mocks |
| GitHub delivery service (fake) | Tested with mocks |
| Delivery endpoints | Tested against the local application |
| GitHub error handling | Tested with mocks |
| End-to-end integration (W3.8) | Tested against the local application (mocks) |
| Real GitHub push/PR | Not implemented (dry-run/fake only) |
| React dashboard approval | Not implemented (Week 4) |
