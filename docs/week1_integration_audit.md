# Week 1 Integration & Testing Audit (W1.5)

- **Task**: W1.5 — Week 1 Integration and Testing Audit (VERIFICATION)
- **Owner**: Backend / FastAPI Developer (me). Collaborator: Playwright owner.
- **Repository**: `VIVEK252575AI/omnisight` — branch `lakshy`
- **Commit audited (HEAD)**: `4f3bce1` (working tree clean at audit start)
- **Date**: 2026-10-13
- **Nature**: verification only — no production code was changed. Added: one test
  script (`backend/scripts/week1_audit_walkthrough.py`) and this report.

> **Truthfulness scope.** Everything below was executed in this session. Where a
> capability was *not* exercised, it is explicitly labelled. No metric, status
> code, or integration is claimed without a run to back it.

---

## 1. Preconditions — result

| Check | Command | Result |
|---|---|---|
| Correct working dir | `git rev-parse --show-toplevel` | `E:/Infotact project/omnisight` ✅ |
| Correct branch | `git branch --show-current` | `lakshy` ✅ (not switched) |
| Tracked remote | `git status -sb` | `## lakshy...origin/lakshy` ✅ |
| Pre-existing uncommitted changes | `git status --short` | **none** (clean) ✅ |

**Instruction files found**: `README.md`, `AGENTS.md`, `backend/requirements.txt`,
`backend/.env.example`.
**Not present**: `CLAUDE.md`, `CONTRIBUTING.md`, `backend/pyproject.toml`, `docs/`.

`README.md` is a 911-line design document; it does **not** specify endpoint-level
contracts or HTTP status codes. `AGENTS.md` carries the owner's git rule and
backend conventions.

---

## 2. Repository reconnaissance

### Week 1 code that exists (backend)

- `app/main.py` — `create_app()` factory, CORS, global error handlers, startup
  warning when the webhook secret is unset.
- `app/routers/scans.py` — scan CRUD + lifecycle transition.
- `app/routers/ci_webhooks.py` — simulated CI/CD receiver.
- `app/routers/evidence.py` — evidence registration + artifact serving.
- `app/event_storage.py` (`CIEventStore`/`InMemoryCIEventStore`/`get_event_store`),
  `app/evidence_storage.py` (`EvidenceStore`), `app/storage.py` (scan store).
- `app/artifacts.py` — safe artifact resolver (rejects absolute / `..` / symlink
  escape / wrong extension / non-image bytes / missing / oversize).
- `app/schemas.py`, `app/errors.py`, `app/ids.py`, `app/config.py`.

### Endpoints present (from live OpenAPI)

| Method | Path | Declared responses |
|---|---|---|
| GET | `/health` | 200 |
| GET | `/api/scans` | 200 |
| POST | `/api/scans` | 201, 422 |
| GET | `/api/scans/{scan_id}` | 200, 404, 422 |
| POST | `/api/scans/{scan_id}/transition` | 200, 404, 409, 422 |
| POST | `/api/v1/webhooks/ci` | 200, 202, 401, 413, 422 |
| GET | `/api/v1/webhooks/events` | 200, 422 |
| GET | `/api/v1/webhooks/events/{event_id}` | 200, 404, 422 |
| POST | `/api/v1/scans/{scan_id}/evidence` | 201, 400, 404, 409, 413, 422 |
| GET | `/api/v1/scans/{scan_id}/evidence` | 200, 404, 422 |
| GET | `/api/v1/artifacts/{artifact_id}` | 200, 404, 422 |

OpenAPI version `3.1.0`, app `0.1.0`.

### Consumers / callers

- The React dashboard (teammate, Week 4) is the eventual consumer of the scan,
  evidence, and webhook-event endpoints. No dashboard code exists in this repo
  yet.
- `scripts/send_simulated_event.py` is a manual caller of the CI webhook.
- `tests/conftest.py` provides fixtures used by all Week 1 tests.

### Tests that exist for Week 1 (73 `def test_*`, **74** collected — one parametrized)

`test_api.py` (15): health; scan create/roundtrip/list/404; viewport & route
validation; illegal/terminal transitions; lifecycle happy path; "verified" never
reported early; state-machine edges well-formed.

`test_errors.py` (16): error-envelope shape for unknown route, missing field,
invalid URL, malformed JSON, empty body, length boundary, oversized body; domain
errors (scan-not-found, illegal transition); internal error hides traceback; CORS
preflight allowed/disallowed; settings defaults & comma-separated parsing.

`test_evidence.py` (19→20): mobile/desktop observation stored + scan transitions;
artifact served with correct content type; **fixture consumed byte-for-byte**;
404 unknown scan; 400 for missing/traversal/absolute/wrong-extension/non-image;
422 for missing box/negative box/unknown schema version; 413 oversize; 409
duplicate/wrong-state; 404 unknown artifact.

`test_webhooks.py` (23): succeeded→scan, started/failed without scan; duplicate
single-scan; 422 for missing target_url/unknown type/bad scheme/non-simulated
source/extra field/malformed JSON; 413 oversize; valid & `sha256=`-prefixed
signature accepted; invalid/missing signature 401; tampered body rejected;
signature ignored when no secret; list/filter; pagination; unknown event 404;
**disallowed host 403 / allowed host accepted**; scan-failure 500 with no partial
scan.

---

## 3. Full test suite

Command (run twice, from `backend/`):

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

| Run | Result | Duration |
|---|---|---|
| 1 | **74 passed**, 1 warning | 0.79s |
| 2 | **74 passed**, 1 warning | 0.76s |

- Warning: `starlette/testclient.py:40` — `anyio.abc.BlockingPortal` deprecation.
  Third-party, not our code, no functional impact.
- **No flakiness observed** across the two runs.

---

## 4. Integration walk-through (real server, real TCP)

Script: `backend/scripts/week1_audit_walkthrough.py`. It boots the **real** FastAPI
app via uvicorn on a real port and drives it over HTTP with `urllib`. Run twice.

```powershell
.\.venv\Scripts\python.exe scripts\week1_audit_walkthrough.py --port 8011
.\.venv\Scripts\python.exe scripts\week1_audit_walkthrough.py --port 8012 --server-secret audit-secret
```

### Run 1 — no webhook secret (`OMNISIGHT_CI_WEBHOOK_SECRET` unset)

| Step | Expected | Observed | Result |
|---|---|---|---|
| health | 200 | 200 | PASS |
| ci `build.succeeded` (simulated) | 202 | 202 | PASS |
| ci duplicate delivery | 200 (`status=duplicate`) | 200 | PASS |
| events list | 200 | 200 | PASS |
| fetch scan (status) | 200 `created` | 200 `created` | PASS |
| submit evidence (fixture) | 201 | 201 | PASS |
| evidence list | 200 | 200 | PASS |
| scan after evidence | `evidence_collection` | `evidence_collection` | PASS |
| fetch artifact bytes | 200 | 200 | PASS |
| second evidence on same scan | 409 `EVIDENCE_STATE_CONFLICT` | 409 | PASS |
| traversal screenshot path | 400 `ARTIFACT_INVALID` | 400 | PASS |
| signature checks | SKIP (no secret) | SKIP | — |

Summary: **PASS, 0 failures**. Startup logged the expected unauthenticated
warning (simulation/dev only).

### Run 2 — signature enforced (`OMNISIGHT_CI_WEBHOOK_SECRET=audit-secret`)

All of Run 1's steps passed again (signed event accepted with 202), **plus**:

| Step | Expected | Observed | Result |
|---|---|---|---|
| ci bad signature | 401 | 401 | PASS |
| ci missing signature | 401 | 401 | PASS |

Summary: **PASS, 0 failures**. Startup logged "signature verification ENABLED".

The full Week 1 boundary — *simulated CI event → scan created → evidence
registered → artifact retrievable* — is therefore **verified against the local
application over HTTP**, for both the open and the signed configuration.

---

## 5. OpenAPI vs documented contract — discrepancies

The README documents the `observation.json` **shape** (lines 310–336) and the
browser-artifact file set (lines 294–302); it does not document endpoints/status
codes. Comparing the live OpenAPI to observed behaviour:

1. **Undocumented response codes — `POST /api/v1/webhooks/ci`.** Behaviour and
   tests produce **403** (`FORBIDDEN`, host allow-list) and **500** (scan-creation
   failure), but the OpenAPI `responses` list only `200, 202, 401, 413, 422`.
   Documentation gap only (code is correct and tested).
2. **Mixed API versioning.** Scan routes are unversioned (`/api/scans`) while CI
   and evidence routes are versioned (`/api/v1/...`). Functional today, but an
   inconsistent public surface for the Week 4 dashboard and a future breaking
   risk. Reported, not changed (G: no endpoint changes).
3. **Artifact-file set gap (info).** README lists `dom_snapshot.json`,
   `console_errors.log`, `network_trace.json` as separate artifacts; the backend
   consumes a single `observation.json` (console/network evidence embedded) plus
   the referenced screenshot. Not a defect — align wording/contract when the
   Playwright owner finalizes output.

---

## 6. Real Playwright collector — status

**Not present.** `git grep -i playwright` matches only: the `enable_playwright_worker`
config flag (default `False`), explanatory comments, `.env.example`, and fixture
metadata (`collector` field). There is **no collector script** and **no real
ShopLite** target in the repo.

Per the prompt's failure clause, the walk-through used the **labelled fixture**
(`tests/fixtures/observation_fixture.json`, marked `source: fixture`) and the real
Playwright → backend handoff is recorded as an **open item**.

---

## 7. Defect list (prioritized)

**P0 (blocking):** none.

**P1 (fix before Week 2 depends on it):**
- **D-1 — Real evidence handoff unverified.** No Playwright collector output has
  ever been accepted by the evidence endpoint; only fixtures. The Observation
  contract is validated (including byte-for-byte fixture consumption), but the
  real producer/consumer handshake is untested. Owner: Playwright teammate.

**P2 (documentation / consistency):**
- **D-2 — Incomplete OpenAPI responses** for `POST /api/v1/webhooks/ci` (missing
  403, 500). Fix by documenting the responses (a production-code change; needs
  approval).
- **D-3 — Mixed API versioning** (`/api/scans` vs `/api/v1/...`). Decide and
  standardize before the dashboard integrates.
- **D-4 — README artifact-file wording** vs single-`observation.json` reality.

**Non-defect observations (out of scope, logged only):**
- Persistence is in-memory → all scans/events/evidence are lost on restart.
- The webhook receiver is **simulation only** and unauthenticated by default.
- The `starlette`/`anyio` deprecation warning.

---

## 8. Status labels per capability

| Capability | Status |
|---|---|
| Scan API + lifecycle state machine | **Tested against the local application** |
| Simulated CI webhook receiver (incl. HMAC signature mode) | **Tested against the local application** |
| Evidence registration + artifact serving/safety | **Tested against the local application** |
| Week 1 boundary (CI event → scan → evidence) | **Tested against the local application** (real uvicorn over HTTP; CI events are synthetic) |
| Real Playwright collector → backend handoff | **Not implemented** (collector absent) |
| Real CI (GitHub Actions/Jenkins) → backend | **Not implemented** (simulation only) |
| VLM / patch engine / PyGithub / dashboard | **Not implemented** (out of Week 1 scope) |

---

## 9. Week 2 readiness decision

### **READY WITH CONDITIONS**

The Week 1 backend boundary is green and reproducible: 74/74 tests pass twice, and
a real-server HTTP walk-through passes all positive and negative cases in both the
open and signed configurations. Week 2 (VLM + Action Engine) can start against the
frozen evidence contract **provided**:

1. The Playwright teammate ships a collector whose output validates against the
   `Observation` (`observation/v1`) contract — the only unverified link (D-1).
2. The team picks a real VLM provider/model via configuration **before** Week 2
   coding; no provider may be hardcoded (none is chosen today).
3. The endpoint-versioning decision (D-3) is made before the React dashboard
   consumes the API.

None of these require rework of Week 1 backend code.

---

## 10. Work needed from teammates

- **Playwright owner**: produce the real collector + a controlled ShopLite mock,
  emitting `observation/v1`; confirm artifact layout matches `app/artifacts.py`
  expectations. This closes D-1.
- **VLM owner**: agree the finding contract (`type`, `target`, `evidence`,
  `severity`, `confidence` per README) and choose a provider via env only.
- **React dashboard owner**: agree on the public API surface; resolve D-3
  (versioning) before integration.
- **GitHub test credentials**: needed before Week 3 (PyGithub). Not required now.
