# Week 1 Write-Up — Backend / FastAPI (W1.2 – W1.5)

- **Owner**: Backend / FastAPI Developer
- **Branch**: `lakshy` (`VIVEK252575AI/omnisight`)
- **Window**: 2026-10-12 → 2026-10-13
- **Commits**: `066bd33` (foundation) → `bfc4c56` (W1.2–W1.4) → `8a3b1ab` (W1.5 audit)
- **Nature**: implementation + verification. Nothing in this document is claimed
  without a run behind it; anything not exercised is labelled as such.

---

## 1. Scope and outcome

- **W1.2 — API scaffold**: FastAPI app factory, configuration, CORS, health,
  router split, in-memory stores, global error handling.
- **W1.3 — Simulated CI/CD webhook receiver**: bounded, idempotent event intake
  with optional HMAC signature verification and a host allow-list.
- **W1.4 — Evidence contract (`observation/v1`)**: validated observation intake,
  safe artifact resolution, artifact serving by opaque id.
- **W1.5 — Integration & testing audit**: 74-test suite plus a real-server HTTP
  walk-through in both open and signed configurations; written report
  (`docs/week1_integration_audit.md`).

Outcome: **the Week 1 boundary works end to end against the local application** —
*simulated CI event → scan created → evidence registered → artifact retrievable*.
Decision recorded: **READY WITH CONDITIONS** for Week 2 (three conditions, none
requiring rework of Week 1 code).

---

## 2. Features built

### 2.1 Application foundation

- **`create_app()` factory** (`app/main.py`) so tests and the ASGI server build the
  same app; `app` is the import-time instance for `uvicorn app.main:app`.
- **Configuration is 12-factor and secret-safe** (`app/config.py`): all settings
  come from `OMNISIGHT_*` env vars (optional `.env`), cached via `lru_cache`, and
  secrets are never logged or returned.
- **CORS uses an explicit allow-list**, never a wildcard, parsed from a
  comma-separated env value (default `localhost:5173`).
- **`GET /health`** returns service name, status and version for liveness probes.
- **One error envelope for every non-2xx response**
  (`{"error": {"code", "message", "details"}}`) via four handlers: `DomainError`,
  `RequestValidationError`, `StarletteHTTPException`, and a catch-all `Exception`.
- **Stack traces never reach the client**: unhandled exceptions are logged
  server-side and returned as a generic `INTERNAL_ERROR`.
- **Stable `ErrorCode` enum** (generic + domain codes such as `SCAN_NOT_FOUND`,
  `ILLEGAL_TRANSITION`, `TERMINAL_STATE`, `INVALID_EVIDENCE`, `ARTIFACT_INVALID`,
  `EVIDENCE_STATE_CONFLICT`).
- **Startup warning when the webhook secret is unset**, so an unauthenticated
  receiver is never silently exposed.
- **Feature flags default to `False`** for capabilities that do not exist yet
  (Playwright worker, VLM, patch engine, GitHub delivery) — the API never
  advertises what it cannot do.

### 2.2 Scan API and lifecycle state machine

- **Endpoints**: `GET /api/scans`, `POST /api/scans` (201), `GET /api/scans/{id}`,
  `POST /api/scans/{id}/transition`.
- **Request validation** (`app/schemas.py`):
  - `target_url` must be a valid `HttpUrl`; length capped at 2048 chars
    (`URL_TOO_LONG`, 422) on top of schema validation.
  - `viewports` 1–4 entries, each `width` 240–3840 and `height` 240–2160;
    default 390×844.
  - `routes` max 10, must start with `/` and contain no whitespace — absolute or
    external routes are rejected with field-level `details`.
- **19-state lifecycle** with an explicit `LEGAL_TRANSITIONS` map: any edge not in
  the map is rejected (409 `ILLEGAL_TRANSITION`, with the allowed next states in
  the message).
- **Terminal states** (`ready_for_review`, `failed`, `cancelled`) are checked
  first and return `TERMINAL_STATE` (409) — they can never transition again.
- **Safety invariant**: `validation_failed` and `verification_failed` have no edge
  to `patch_verified`, so a failed build/test can never surface as a verified
  repair; a dedicated pure test asserts the map stays well-formed.
- **Store is an interface, not a global dict**: thread-safe in-memory `ScanStore`
  with `create/get/list/replace`, plus `compare_and_transition()` as a
  single-writer guard so two concurrent triggers cannot start the same work twice.
  Swappable for SQLite later without touching routers.

### 2.3 Simulated CI/CD webhook receiver

- **Three closed event types**: `build.started`, `build.succeeded`, `build.failed`.
- **Explicitly simulation-only**: `source` is pinned to the literal `"simulated"`,
  so no payload can masquerade as a real GitHub Actions/Jenkins integration;
  `extra="forbid"` rejects unknown fields.
- **`build.succeeded` requires `target_url`** and creates a scan stub in `created`
  state **only** — no browser, model or GitHub call is triggered.
- **`build.started` / `build.failed` are stored without a scan.**
- **Idempotent delivery**: `event_id` is claimed under the store lock *before* the
  scan stub is created; only the winning call creates a scan, duplicates return
  **200** with `status="duplicate"` and the original `scan_id`. A failure while
  creating the scan stores nothing, so no half-written record exists and a retry
  can claim the id again.
- **Optional HMAC-SHA256 verification**: `X-OmniSight-Signature`, accepting raw
  hex or a `sha256=` prefix, compared with `hmac.compare_digest`; missing/invalid
  → 401 `UNAUTHORIZED`. With no secret configured, verification is skipped (and the
  startup warning fires).
- **Bounded input**: body limited to 64 KiB using both `Content-Length` and actual
  length → 413 `PAYLOAD_TOO_LARGE`; metadata capped at 50 keys / 4096 bytes.
- **Optional target-host allow-list** for SSRF control → 403 `FORBIDDEN`.
- **Queryability**: paginated list with `?event_type=` filter (limit 1–200),
  single-event fetch → 404 `CI_EVENT_NOT_FOUND`.

### 2.4 Evidence contract — `observation/v1`

- **`Observation` is a frozen, versioned contract** (`schema_version` literal
  `"observation/v1"`) covering: `url`, `route`, `state`, `viewport`, `screenshot`,
  `elements[]`, `console_errors[]`, `network_errors[]`, `captured_at`.
- **Bounded on every collection**: 2000 elements, 500 console errors, 500 network
  errors, text ≤ 2000 chars, console message ≤ 4000; `computed_style` capped at
  40 properties / 100-char keys / 500-char values; `box.w`/`box.h` must be ≥ 0.
- **Unknown fields are tolerated** (`extra="ignore"`) so the collector can evolve,
  while `schema_version` gates breaking changes; the fixture's `_fixture` label is
  dropped rather than echoed.
- **Intake**: `POST /api/v1/scans/{id}/evidence` accepts evidence only while the
  scan is `created`; success returns 201 and moves the scan to
  `evidence_collection`. A second submission or a wrong-state scan → 409
  `EVIDENCE_STATE_CONFLICT`.
- **Failure is traceable, never silent**: invalid evidence (schema or artifact)
  marks the scan `failed` with a recorded reason in `error`.
- **Artifact safety** (`app/artifacts.py`) treats every collector path as hostile:
  - rejects absolute paths, drive/anchor paths, `..` segments and null bytes;
  - resolves the path and requires it to stay inside the artifact root, which also
    catches **symlink escapes** (`Path.resolve()` runs before containment);
  - allows only `.png/.jpg/.jpeg/.json`; screenshots must be PNG/JPEG;
  - requires an existing regular file within size limits (5 MiB image / 1 MiB JSON);
  - **checks PNG/JPEG magic bytes** — a `.png` extension is not proof of image data.
- **Serving**: `GET /api/v1/artifacts/{artifact_id}` returns the bytes with the
  correct media type, looked up by an opaque UUID, so a client can never request
  an arbitrary filesystem path; unknown id → 404 `ARTIFACT_NOT_FOUND`.
- **Storage**: thread-safe `EvidenceStore` grouped by scan, holding validated paths
  only; observation content is stored as inert text (never rendered or executed).

---

## 3. Fixes and hardening applied during the week

- **Unified the error surface** — early FastAPI defaults returned three different
  shapes (validation array, `{"detail": ...}`, plain 500). All non-2xx paths now
  produce the same envelope with a stable code and field-level `details`.
- **Separated "terminal" from "illegal"** — ending a finished scan is a different
  failure than a bad edge, so it returns `TERMINAL_STATE` instead of a generic
  conflict; this keeps client handling deterministic.
- **Closed an idempotency race** — the original record-then-create order could
  create two scans for one `event_id` under concurrent delivery. Claiming the id
  under the lock before the side effect makes exactly one scan possible and
  guarantees no partial record on failure (covered by an injected-failure test).
- **Made evidence rejection change state** — an invalid observation used to leave a
  scan stuck in `created` with no explanation; it now transitions to `failed` with
  the reason recorded, so a retry/cleanup path is obvious.
- **Added magic-byte validation** — extension checks alone let a text file named
  `.png` through; the resolver now verifies the PNG/JPEG signature.
- **Hardened path containment** — resolving before the `relative_to` check closes
  the symlink-escape hole that a plain string prefix check leaves open.
- **Removed wildcard CORS** in favour of an explicit, env-driven allow-list.
- **Hardened signature verification** — constant-time comparison, support for the
  common `sha256=` prefix, and a clear 401 for missing headers.
- **Enforced body limits twice** — a declared `Content-Length` is checked before
  reading and the real length after, so a lying header cannot bypass the cap.
- **Tightened route input** — routes must be relative and whitespace-free; the
  failure is reported per field rather than as a bare 422.

Findings that were **deliberately not changed** (documented, out of scope or
needing approval, per the audit): missing 403/500 in the webhook's OpenAPI
`responses` (D-2), mixed `/api` vs `/api/v1` versioning (D-3), and the README's
separate artifact-file wording vs the single `observation.json` reality (D-4).

---

## 4. Blockers and open items

- **D-1 (P1) — Real evidence handoff unverified.** No Playwright collector exists
  in the repo, so no genuine collector output has ever been accepted; only the
  labelled fixture. The contract is validated, the producer/consumer handshake is
  not. Owner: Playwright teammate.
- **VLM provider/model not chosen.** Week 2 must stay adapter/config-driven; no
  provider, model name, key or endpoint may be hardcoded.
- **D-3 — Endpoint versioning undecided** (`/api/scans` vs `/api/v1/...`); must be
  settled before the React dashboard integrates.
- **No real CI integration.** The receiver is a simulation; a real
  GitHub Actions/Jenkins payload is explicitly out of scope and rejected
  (`source` is pinned).
- **Persistence is in-memory by design.** Scans, events and evidence are lost on
  restart; SQLite is deferred until the lifecycle gains long-running execution.
- **Webhook is unauthenticated by default.** Correct for local simulation, but the
  secret must be set outside dev.
- **Windows symlink test skipped** — `os.symlink` needs privileges the runner does
  not have; the containment logic itself is still exercised by traversal tests.
- **Third-party deprecation warning** (`starlette`/`anyio` `BlockingPortal`) — not
  our code, no functional impact.
- **GitHub credentials** are needed before Week 3 (PyGithub delivery).

---

## 5. What I learned

- **Contract-first pays off immediately.** Freezing `observation/v1` turned an
  ambiguous "send me a screenshot" into a testable interface, and made the missing
  Playwright link obvious instead of hiding inside a vague integration.
- **An explicit state machine is cheaper than scattered `if` checks.** One
  transition map plus a terminal-state set produced correct 409s and a provable
  "failure can never become success" invariant.
- **Browser and model output must be treated as hostile input.** Path traversal,
  symlink escape, fake image extensions and oversized bodies are all realistic;
  validation has to be fail-closed and layered.
- **Idempotency is an ordering problem, not a dictionary lookup.** Claiming the key
  before doing the side effect is what actually prevents double work.
- **Honest labelling is part of the engineering.** "Tested against the local
  application" vs "Not implemented" prevented a green test run from being read as
  a finished integration.
- **Negative tests carry most of the value.** The suite's 401/403/409/413/422 cases
  are what proved the guards actually work.

## 6. Experience

- The **real-server walk-through** (uvicorn on a real port, driven over HTTP) caught
  confidence that `TestClient` alone cannot give — it exercised startup config,
  middleware and real serialization exactly as a caller would.
- **Running the audit as a separate slice** was worth it: it produced a prioritized
  defect list (P0 none, one P1, three P2) instead of an untriaged pile of notes.
- **Scope discipline was the hard part.** Tempting additions (real CI, background
  execution, versioning cleanup) were logged rather than smuggled in, which kept
  the Week 1 boundary reviewable.
- **Friction**: Windows/PowerShell tooling (`rg` unavailable), path-separator edge
  cases in artifact tests, and the symlink test skip. None blocked delivery.
- **Documentation drift is real** — the README described a file set the backend does
  not consume; comparing live OpenAPI behaviour against the doc was what surfaced it.

## 7. Understanding

- Week 1 is the **trust boundary** of the product: the Playwright collector and the
  CI system are outside the backend, and the backend owns validated state, not
  browser facts.
- The pipeline is **Detect → Fix → Verify → Deliver**, and Week 1 built the rails
  for it: a scan record with a guarded lifecycle, an evidence contract that later
  stages can consume, and a trigger path from CI.
- **The backend, not the model or browser, is the source of truth**: findings and
  observations are untrusted inputs that get parsed into validated schemas before
  anything else touches them.
- **Replaceable boundaries beat early integrations**: config-selected adapters and
  swappable store interfaces mean the provider decision and the database decision
  can both be deferred without rework.
- **Propose-only is a deliberate safety stance**: later stages may generate patches,
  but nothing is written to disk until a validated, allow-listed path exists.

---

## 8. Verification evidence

| Check | Result |
|---|---|
| `pytest tests\ -q` (run twice) | **74 passed**, 1 third-party warning, 0.79s / 0.76s |
| Week 1 tests by file | `test_api.py` 15, `test_errors.py` 16, `test_evidence.py` 19 (20 collected), `test_webhooks.py` 23 |
| Real-server walk-through, no secret | 12 steps, **0 failures** (signature steps skipped) |
| Real-server walk-through, signed | all steps pass + bad/missing signature → 401, **0 failures** |
| Startup behaviour | unauthenticated warning / "signature verification ENABLED" as configured |

## 9. Status labels (verbatim)

| Capability | Status |
|---|---|
| Scan API + lifecycle state machine | Tested against the local application |
| Simulated CI webhook receiver (incl. HMAC signature mode) | Tested against the local application |
| Evidence registration + artifact serving/safety | Tested against the local application |
| Week 1 boundary (CI event → scan → evidence) | Tested against the local application |
| Real Playwright collector → backend handoff | Not implemented |
| Real CI (GitHub Actions/Jenkins) → backend | Not implemented |
| VLM / patch engine / PyGithub / dashboard | Not implemented (out of Week 1 scope) |
