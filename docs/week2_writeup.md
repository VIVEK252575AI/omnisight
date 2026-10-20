# Week 2 Write-Up — VLM Detection & Safe Action Engine (W2.1 – W2.7)

- **Owner**: Backend / FastAPI Developer
- **Branch**: `lakshy` (`VIVEK252575AI/omnisight`)
- **Window**: 2026-10-16 → 2026-10-18
- **Commits**: `6276312` (gateway) → `bc915b1` (W2.2) → `e577c83` (W2.3) →
  `fbdc6a4` (W2.4 + W2.5) → `0e131c4` (W2.6 + W2.7)
- **Nature**: implementation + verification. Every capability below is labelled
  with the project's status vocabulary; nothing is claimed without a run behind it.

---

## 1. Scope and outcome

- **AI Gateway boundary** (`6276312`) — the validated path from a model call to a
  parsed record, with one bounded repair re-ask.
- **W2.2 — Replaceable VLM adapter boundary**: provider-neutral `VisionAnalyzer`
  protocol, deterministic offline MOCK, teammate adapter shell, config-selected
  factory that fails fast.
- **W2.3 — Structured visual-finding schema + parser**: the single canonical
  Week 2 finding vocabulary and a parser that never raises.
- **W2.4 — VLM detection workflow**: evidence → findings inside the running API,
  with crash-safe state handling and auditable failure reasons.
- **W2.5 — Safe action engine / patch proposals**: finding → reviewable proposal
  record, nothing written to disk.
- **W2.6 — Code-response extraction & rejection**: isolate patch payload from
  prose, validate paths/applicability/payload, reject with closed-set reasons.
- **W2.7 — Test matrix**: manifest-driven corpus of VLM and patch responses,
  integration failure rows, provenance integrity checks, mutation sanity check.

Outcome: **the Detect → Fix half of the pipeline runs end to end against the local
application with mocks** — stored evidence produces validated findings, a finding
produces a reviewable proposal, and every failure path settles with a
machine-readable reason instead of hanging or silently succeeding.

**What Week 2 did not build**: no real VLM integration (provider not chosen), no
patch apply/validation/verification, no GitHub delivery, no dashboard. Those are
Week 3/4 slices.

---

## 2. Features built

### 2.1 AI Gateway boundary

- **One validated path to a model** (`app/ai/gateway.py`): call the adapter →
  parse raw text into `VlmResponse` → optionally re-ask once.
- **Two entry points for two caller needs**:
  - `analyze_visual()` — strict: returns validated output or raises `AiError`.
  - `run()` — auditable: **never raises** for AI failures; returns a
    `GatewayResult` that keeps the raw text alongside the parsed result or the
    failure reason.
- **Bounded repair**: `1 + OMNISIGHT_AI_MAX_REPAIR_ATTEMPTS` (default 1 extra)
  attempts, only for repairable parse failures (`empty`, `not_json`,
  `schema_invalid`, `unknown_category`). `contradictory` is never retried;
  transport failures are never retried.
- **Provenance on every call**: `provider_label`, `is_mock`, `model_label`,
  `prompt_version`, `schema_version`, `attempts`, `duration_ms`, `finish_reason`,
  `warnings`.
- **Normalised error taxonomy** (`app/ai/errors.py`): configuration, auth,
  rate-limited, unavailable, timeout, empty, not-JSON, schema-invalid,
  evidence-invalid, insufficient-context, scope-exceeded, refused, budget,
  cancelled — mapped to typed adapter exceptions.
- **Repair is observable**: a successful repair adds a warning
  (`"repaired after N failed attempt(s)"`), so a repaired answer is never
  presented as a first-try success.

### 2.2 Replaceable VLM adapter boundary (W2.2)

- **`VisionAnalyzer` protocol** (`app/ai/adapters/base.py`) — the only shape
  exchanged across the model boundary: resolve screenshot ids, enforce the image
  budget, call the provider under a timeout, return `RawVisionResult`.
- **The adapter must not parse, execute, persist or interpret model output** —
  that belongs to the gateway. This keeps provider-specific code small and
  untrusted text out of storage.
- **Images cross the boundary by artifact id, never by path or inline bytes**;
  the adapter resolves ids server-side through the evidence store.
- **Payload budget enforced before the call** (`enforce_image_budget`): count and
  total bytes, so an oversized payload never leaves the process.
- **Wall-clock timeout helper** (`call_with_timeout`) using a single-worker
  executor; a timeout becomes `VisionTimeoutError`.
- **Redacted logging**: provider, request id, repair flag, screenshot count,
  measurement count, category allow-list — never image bytes, prompts,
  credentials or model output.
- **Deterministic offline MOCK** (`MockVisionAnalyzer`, labelled `MOCK`):
  `clipped_button`, `healthy_page`, `needs_inspection`, `malformed_text`,
  `malformed_schema`, `empty`, `timeout`, `provider_error`.
- **Teammate adapter shell**: wraps an injected callable, applies the same
  timeout and budget, labels results `TEAMMATE`; fails fast if no callable was
  injected rather than silently falling back.
- **Config-selected factory** (`OMNISIGHT_VLM_ADAPTER`): `mock` default,
  `teammate` opt-in, unknown value → startup failure.

### 2.3 Canonical finding schema and parser (W2.3)

- **One finding vocabulary** (`app/ai/contracts.py`) — replaces the earlier
  LLD-only category set; MOCK, gateway and tests were reconciled onto it:
  - categories (closed): `clipped_element`, `overlapping_elements`,
    `low_contrast_text`, `responsive_layout_failure`,
    `hidden_or_unreachable_element`, `needs_inspection`;
  - outcomes: `defects_found`, `no_defects`, `needs_inspection`;
  - severity: `critical`, `high`, `medium`, `low`, `informational`;
  - confidence optional in `[0, 1]`;
  - `schema_version = 1`.
- **Finding record fields**: `finding_id`, `category`, `target_candidate`,
  `target_unverified`, `observation`, `evidence`, `potential_causes`, `severity`,
  `confidence`, `region{x,y,width,height}`, `status`.
- **Backend-assigned fields are never trusted from the model**:
  `finding_id`, `status` and `target_unverified` are declared only so a model
  that sends them does not trip `extra="forbid"` — their values are ignored and
  recomputed.
- **Parser never raises** (`parse_vlm_output`): every failure becomes a
  `ParseResult(status="rejected", reason=...)` with one of `empty`, `not_json`,
  `schema_invalid`, `unknown_category`, `contradictory`.
- **Conservative JSON extraction**: whole text, or exactly one tagged
  ```` ```json ```` block. No `eval`/`exec`; prose, multiple blocks or malformed
  JSON are rejected.
- **Semantic checks**: closed category set (unknown rejected, never coerced),
  confidence bounds, severity enum, `no_defects` with findings (and
  `defects_found` without findings) rejected as contradictory.
- **Target verification**: a `target_candidate` absent from the supplied DOM
  candidates is kept but flagged `target_unverified=true` — not silently
  dropped, and not silently trusted.
- **Duplicate merge**: findings sharing `(category, target_candidate)` are merged,
  preserving order and unioning causes.
- **Sanitisation**: control characters stripped and free text capped
  (observation/evidence 2000, summary 4000, target 500, causes 50×1000) before a
  record exists.
- **Prompt versioning**: `visual-analysis-v2.txt` /
  `VISUAL_ANALYSIS_PROMPT_VERSION = "visual-analysis-v2"`; v1 retained for
  reproducibility.

### 2.4 VLM detection workflow (W2.4)

- **Endpoints**:
  - `POST /api/v1/scans/{scan_id}/detect` → **202** schedules one run;
  - `GET /api/v1/scans/{scan_id}/findings` → validated findings + latest run;
  - `GET /api/v1/findings/{finding_id}` → one finding with evidence refs.
- **Idempotent trigger**: while a scan is `detecting` or already `detected`, a
  re-post returns **202** with `accepted=false` and starts nothing. Only
  `evidence_collection`, `detection_failed` and `needs_inspection` can start a
  run — exactly the set `LEGAL_TRANSITIONS` allows to reach `detecting`.
- **Atomic single-writer entry**: `ScanStore.compare_and_transition` moves the
  scan into `detecting`; if another request won the race, the loser re-reads and
  reports the in-flight run instead of starting a duplicate.
- **Crash safety**: the scan is moved to `detecting` *before* the background task
  is scheduled; `run_guarded()` converts unexpected exceptions into
  `detection_failed` (`reason_code="interrupted"`) and a `finally` block settles
  the scan, so **no path can leave a scan stuck in `detecting`**.
- **Evidence selection**: the stored observation whose viewport width matches the
  scan's first requested viewport; otherwise the first record.
- **Candidate bounding**: DOM candidates capped at
  `OMNISIGHT_DETECTION_MAX_CANDIDATES` (50), ranked interactive (+2),
  off-viewport (+2), invisible (+1), capture order as tie-break, duplicate
  selectors dropped.
- **Deterministic context**: `viewport`, `box.<selector>` and
  `in_viewport.<selector>` measurements are derived from the collector's
  observation, not from the model.
- **Task shape**: one `VisionRequest` with prompt/schema versions, page context,
  screenshot refs, measurements, the full category allow-list, and
  `max_findings` from settings.
- **Outcome mapping**: `needs_inspection` → scan `needs_inspection`; otherwise
  `detected` (including a clean `no_defects` page).
- **Persisted provenance** (`FindingRecord`): `adapter_label`, `is_mock`, route,
  viewport, `evidence_ids`, `screenshot_artifact_ids`.
- **Run record** (`DetectionRun`): adapter label, mock flag, model label,
  prompt/schema versions, attempts, duration, outcome, reason code/message,
  findings count, warnings, timestamps.
- **Failure reasons recorded, not swallowed**: `no_evidence`, `not_json`,
  `schema_invalid`, `empty`, `contradictory`, `timeout`,
  `provider_unavailable`, `auth_error`, `rate_limited`,
  `configuration_error`, `budget_exceeded`, `capability_unsupported`,
  `cancelled`, `interrupted`, `unknown`.
- **A failed re-run does not destroy earlier audit data**: `findings_count`
  counts only what this run validated; previously validated findings stay in
  place.
- **Raw model text policy**: in memory only, UTF-8-capped at
  `OMNISIGHT_DETECTION_MAX_RAW_OUTPUT_BYTES` (8192), cleared at the start of every
  run, and returned only when `OMNISIGHT_DETECTION_EXPOSE_RAW_OUTPUT=true` **and**
  the caller passes `?include_raw=true` (never by the list endpoint).
- **Mock provenance is explicit**: `adapter_label="MOCK"` wherever a mock
  produced the record, so mocked output can never look like real inference.

### 2.5 Safe action engine — patch proposals (W2.5)

- **Endpoints**:
  - `POST /api/v1/findings/{finding_id}/patch-proposals` → **201** with a
    `proposed` **or** `rejected` record;
  - `GET /api/v1/findings/{finding_id}/patch-proposals` → every attempt, oldest
    first, plus the finding's current status.
- **A rejection is a 201, not a transport error**: it is a stored, reviewable
  outcome with reason codes. Generator/extractor failure is never reported as
  success.
- **Guards**: 404 `FINDING_NOT_FOUND`; 409 `FINDING_NOT_PATCHABLE` when the
  finding is not `detected` (e.g. already has a `proposed` patch).
- **Read-only source context** (`app/source_context.py`):
  - only allow-listed files, extension-filtered and forbidden-file-excluded;
  - capped at 20 files and `OMNISIGHT_PATCH_CONTEXT_MAX_BYTES` (4096);
  - symlinks skipped unless the link and its resolved target both stay inside the
    resolved source root;
  - selector tokens from `target_candidate` are matched to files and reported;
  - an empty context carries an explicit reason (`source_root_unavailable`,
    `approved_patch_paths_empty`, `no_approved_files_found`) — it never silently
    becomes "no context, proceed anyway".
- **Replaceable generator boundary** (`PatchGenerator`): returns raw text only;
  config-selected (`mock` default, `teammate` fails fast until integrated);
  wall-clock timeout via the shared helper.
- **10 deterministic MOCK generator scenarios**: `ok`, `timeout`, `error`,
  `invalid_json`, `empty`, `prose_with_fence`, `two_plain_fences`,
  `two_tagged_fences`, `prose_only`, `truncated`.
- **Atomic finding claim**: only a fully validated proposal flips the finding
  `detected → patch_proposed` via `compare_and_set_finding_status`; two
  concurrent proposals cannot both win. A rejected attempt leaves the status
  untouched, so a corrected attempt is allowed.
- **Attempt history**: every attempt is numbered and stored, so retries are
  observable rather than overwriting the previous answer.
- **API returns a summary only**: paths, edit counts, payload bytes, rationale,
  generator label, mock flag, reasons. The full editable payload stays
  server-side for a later apply step.

### 2.6 Code-response extraction and rejection (W2.6)

- **Required generator format** (`patch-proposal-v1.txt`): one JSON object, or
  exactly one fenced block tagged `omnisight-patch`. Untagged fences are never
  treated as patches; prose is discarded and counted
  (`discarded_prose_chars`).
- **Extraction order**:
  1. whole BOM-stripped text parses as one JSON object → use it;
  2. else exactly one `omnisight-patch` fence → use its body;
  3. else `ambiguous_multiple_patches` (several tagged blocks), `truncated`
     (output looks cut off), or `no_patch_found`.
- **Edge cases handled**: UTF-8 BOM, CRLF, curly ("smart") quotes with one
  best-effort recovery pass, unterminated/nested fences, prose containing
  code-like text, and oversized output capped **before** parsing (`too_large`).
- **Validation pipeline** (`SourcePatchExtractor`):
  - schema — payload → `PatchProposal`, and `finding_id` must match the target;
  - paths — normalise, forbidden-file, extension, allow-list;
  - applicability — read the target file **read-only**; `old_text` must occur
    exactly once (`edit_not_applicable` / `edit_ambiguous`);
  - payload — structural checks on the **inserted** `new_text` only.
- **Newline normalisation on both sides** so a CRLF file and an LF edit still
  match exactly.
- **Heuristic structural checks** (`patch_code_checks.py`) — no CSS/JS parser is
  shipped, so these are deliberately conservative and deep syntax is deferred to
  the W3.3 sandbox build:
  - delimiter balance `{}`/`()`/`[]` outside string literals, enforced only when
    the inserted text opens a structure (so `height: auto;` is allowed);
  - best-effort JSX tag balance (only when a closing tag exists; `<>`/`</>`
    fragments handled; `<` must be followed by a letter or `/`, so `a < b` is
    never read as a tag);
  - CSS forbidden: external `@import url(`, `expression(`, `javascript:`;
  - JS/TS/JSX forbidden: `eval(`, `Function(`, `child_process`, `fs` string
    literals, `XMLHttpRequest`, `<script`, `document.write`,
    `dangerouslySetInnerHTML`, `fetch(`/`import(` to an absolute URL.
- **`ExtractionResult{status, proposal, reasons, discarded_prose_chars,
  message}`** — `rejected` is a normal outcome, never raised.
- **No interpreter ever touches model text**: no `eval`, `exec`, `subprocess`,
  `node`; the fence regex is lazy and linear over bounded input; on uncertainty
  the answer is rejected.

### 2.7 Test matrix (W2.7)

- **Two manifest-driven corpora** under `backend/tests/fixtures/`:
  - `vlm_responses/` — 12 raw VLM outputs → `parse_vlm_output`;
  - `ai_patch_responses/` — 7 raw generator outputs → `SourcePatchExtractor`,
    each with its source tree and allow-list, and a `__FINDING_ID__`
    placeholder substituted at test time.
- **Manifest is the single source of truth**, loaded at import: a missing or
  corrupt manifest fails collection loudly rather than silently shrinking the
  suite.
- **30 matrix tests**:
  - 12 VLM parsing (3 valid shapes, JSON-in-prose, 5 rejection classes, empty);
  - 7 patch extraction (2 accepted, 5 rejected with distinct reasons);
  - 7 integration rows through `POST /detect` (all MOCK);
  - 4 manifest integrity/coverage tests (files exist, no orphans, provenance
    labelled, coverage counts).
- **Integration rows assert the real API contract**: each drives a scan to
  `detection_failed` and checks the expected `reason_code` — `timeout`,
  `provider_unavailable`, `rate_limited` (injected analyser), `interrupted`
  (injected crash), `budget_exceeded` (1-byte image budget), `not_json`,
  `schema_invalid`.
- **Provenance is enforced**: every fixture is `kind: fixture` (0 real);
  a `real` entry must record where it was captured.
- **Reuse over duplication**: matrix tests import the existing detection and
  patch helpers (`_detect`, `_evidence_ready`, `_findings`, `_scan_status`,
  `_finding`, `_make_tree`) rather than re-implementing setup.
- **Mutation sanity check** performed and reverted:
  1. wrong `expected_reason` on `non_json_prose` →
     `test_vlm_parsing_fixture[non_json_prose]` fails
     (`assert 'not_json' == 'schema_invalid'`);
  2. deleting `empty_body.txt` → both the orphan check and that fixture case
     fail.

---

## 3. Fixes, reconciliations and hardening

### 3.1 Contract reconciliations (spec → code)

- **One finding vocabulary** — the earlier LLD-only category set was replaced by
  the canonical W2.3 set, and the MOCK adapter, gateway and tests were reconciled
  onto it instead of keeping parallel vocabularies.
- **One rejection vocabulary** — the W2.6 reason names were normalised to a
  single closed `RejectionReason` superset:
  - `path_not_allowed` → `path_not_approved`;
  - `extension_not_allowed` → `unsupported_file_type`;
  - `invalid_proposal` → `schema_invalid`;
  - `extractor_not_implemented` dropped (the real extractor now exists).
- **One `too_large` code** shared by the extraction input cap and the edit-payload
  budget, reported by whichever layer sees the oversized input — instead of two
  near-duplicate codes.
- **Schema modules split deliberately**: `detection_schemas.py` and
  `patch_schemas.py` embed the canonical `Finding` without creating a circular
  import through `app/schemas.py`.
- **README corrected** to match the code: the real `VlmResponse` shape, the closed
  category set, backend-assigned fields, the "one bounded repair re-ask" wording,
  the exact `observation/v1` contract, and the actual `backend/app` tree.

### 3.2 Correctness fixes

- **Backend-assigned fields ignored, not rejected** — `finding_id`, `status` and
  `target_unverified` are accepted syntactically then overwritten, so a model
  cannot forge identity or claim a finding was already proposed.
- **Newline normalisation added to source context** (`_decode`) — without it, an
  `old_text` captured from a CRLF file could never match the extractor's
  universal-newline read, producing false `edit_not_applicable` rejections.
- **`old_text` is never judged as new code** — forbidden-construct checks apply
  only to `new_text`, because `old_text` is pre-existing code the patch did not
  author.
- **Structural checks only fire when they can be trusted** — bracket balance only
  when the text opens a structure, JSX only when a closing tag exists, and `<`
  only counts as a tag when followed by a letter or `/`; this removed false
  positives on CSS fragments and comparisons like `a < b`.
- **`old_text` matching is exact and singular** — zero occurrences and multiple
  occurrences are distinct codes (`edit_not_applicable` vs `edit_ambiguous`), so
  an ambiguous edit can never be applied at "the first" spot.
- **Raw output cleared at the start of each run** — a new failed run cannot
  surface the previous run's text.
- **Per-request analyzer construction** — the detection router builds its adapter
  against the request's evidence store instead of `app.state.vision_analyzer`,
  which resolves against the process-wide store and is wrong under test
  dependency overrides.
- **Crash settlement** — `run_guarded` + `finally` + `_settle` guarantee the scan
  leaves `detecting` even if the task raises before any normal path runs.
- **Lost-race handling** — if the scan moved while a run was in flight, findings
  and the run are not persisted; the state machine stays authoritative.
- **Byte-accurate caps** — raw output and patch payloads are capped in UTF-8
  bytes, not characters.
- **`too_large` checked before parsing** — a giant response is rejected before
  the fence regex or JSON parser ever sees it.
- **Path-rejection ordering** — most-specific check first, so a lockfile reports
  `forbidden_file`, not the vaguer `path_not_approved`.
- **All rejection reasons accumulated** (deduplicated, deterministic order), so a
  proposal with an unsafe path *and* an oversized payload reports both instead of
  hiding the second problem.
- **Integration-row expectations fixed** — the first W2.7 attempt asserted
  `reason_code == scenario`, which is false for `adapter_exception→interrupted`
  and `oversized_image→budget_exceeded`; replaced with an explicit
  `(label, expected_reason)` table.

### 3.3 Safety hardening

- **Fail-closed allow-list**: `OMNISIGHT_APPROVED_PATCH_PATHS` defaults to empty,
  so with no ShopLite handoff **every** proposal is rejected on
  `path_not_approved` rather than accepted by default.
- **Forbidden-file defence in depth**: `.git`, `.env*`, lockfiles, CI config and
  build config are rejected even if a broad allow-list pattern would have swept
  them up; only a glob-free pattern naming the exact file overrides this.
- **Symlink escape checks in source context** — a link is skipped unless both the
  link and its resolved target stay inside the resolved root.
- **No execution surface**: model/generator text is only ever parsed; no
  `eval`/`exec`/`subprocess`/`node`/import, and raw text is never written to
  disk (`OMNISIGHT_AI_ALLOW_RAW_OUTPUT_STORAGE` stays `false`).
- **Mock leakage prevention**: `adapter_label`/`generator_label` and `is_mock`
  are carried on every record and response, so a mocked record is always
  identifiable as mocked.
- **Secrets stay out**: adapters log only redacted metadata; error types carry no
  provider response bodies, secrets or raw model output.

---

## 4. Blockers and open items

- **VLM provider/model not finalised** — everything is adapter/config-driven, but
  no real inference has ever run; the fixture corpus is **0 real / 12 fixture**
  and all integration rows are MOCK. Real-response tests are pending the provider
  decision.
- **ShopLite source tree missing** — `OMNISIGHT_PATCH_SOURCE_ROOT` and
  `OMNISIGHT_APPROVED_PATCH_PATHS` default to empty, so applicability and
  acceptance are only exercised against injected temporary trees; a real
  proposal is rejected on `path_not_approved` until the handoff.
- **No apply / sandbox validation / verification** — approving and applying a
  patch, rerunning detection and reaching `patch_verified` is a later slice.
- **No CSS/JS parser** — deep syntax validation is deliberately deferred to the
  W3.3 sandbox build; current checks are heuristics and can only reject obvious
  constructs, not prove correctness.
- **`rate_limited` has no MOCK scenario** — the W2.7 matrix injects
  `_RateLimitedAnalyzer`; the mock adapter itself cannot produce it.
- **Teammate adapters are shells** — `teammate` for both VLM and patch generator
  fails fast until the teammate modules inject a callable.
- **`OMNISIGHT_ENABLE_VLM_DETECTION` does not gate the endpoints yet** — the
  workflow is exercised directly so it can be verified before the flag has a
  consumer.
- **No queue or retry budget** — detection runs once per request; a provider
  outage is recorded, not retried with backoff.
- **Persistence is in-memory** — runs, findings and proposals are lost on
  restart, same as the Week 1 stores.
- **Windows symlink test skipped** — `os.symlink` needs privileges the runner does
  not have.

---

## 5. What I learned

- **Trust is a pipeline, not a check.** Raw text goes through extraction →
  schema → semantics → provenance → storage; each layer can only *narrow* what is
  accepted. That structure made every failure class nameable.
- **A closed vocabulary is worth more than a flexible one.** Rejecting an unknown
  category (`unknown_category`) instead of coercing it removed a whole class of
  downstream ambiguity — and made "unknown" itself a reportable outcome.
- **Bounded repair beats unbounded retry.** One re-ask fixes shape errors cheaply
  without hiding a broken provider behind retries; transport failures are
  explicitly never repaired.
- **"Never raises" is a design property, not a `try`.** `parse_vlm_output` and the
  gateway's `run()` return auditable results, so a rejected answer can still be
  recorded instead of vanishing into an exception handler.
- **Provenance is part of correctness.** Carrying `adapter_label`/`is_mock` on
  every record is what makes "this was mocked" a verifiable fact rather than a
  footnote.
- **Exact-match editing is a newline problem.** Normalising CRLF/CR to LF on both
  the file and the edit was the difference between a working applicability check
  and a flood of false rejections.
- **Heuristics must know when to stay silent.** Only checking bracket balance when
  a structure opens, and only treating `<` as a tag when a letter follows it,
  prevented the checker from rejecting valid code.
- **Fail-closed defaults are a feature.** An empty allow-list that rejects
  everything is safer and easier to reason about than a permissive default that
  "works" in dev.
- **Mutation testing turns a green suite into evidence.** Deliberately corrupting
  one expectation and deleting one fixture proved the matrix actually fails when
  it should.
- **Corpus-driven tests scale better than copy-pasted cases.** Adding a file plus
  a manifest entry adds a test; the orphan check stops the corpus from rotting.

## 6. Experience

- **Writing five design docs alongside the code** forced the naming
  reconciliations (`path_not_approved`, `unsupported_file_type`,
  `schema_invalid`) to happen once, in the open, instead of drifting between
  modules.
- **The two-boundary split (adapter vs gateway) paid off immediately**: the MOCK
  adapter stayed trivial, the teammate shell stayed a shell, and all parsing logic
  lived in one testable place.
- **The W2.7 corpus was the most valuable debugging tool.** Building raw
  responses first made the parser's edge cases concrete (BOM, smart quotes,
  truncated JSON, prose around a fence) instead of hypothetical.
- **Reusing helpers across test modules** kept the matrix readable and meant a
  harness fix improved every layer at once.
- **The one real bug in W2.7 was in the test, not the product** — assuming the
  scenario label equals the reason code. Fixing it with an explicit mapping table
  made the intent obvious.
- **Speed stayed good**: the whole suite runs in ~4s, so the full matrix was
  cheap to run repeatedly (three consecutive runs: 4.01s / 4.25s / 4.00s, no
  flakiness).
- **Friction**: no real model and no ShopLite tree meant every "success" had to be
  labelled as mock/fixture; the Windows symlink skip remains an environment gap.
- **Scope discipline**: the temptation to add a parser dependency or an apply step
  was resisted and logged as W3 work, which kept this slice reviewable.

## 7. Understanding

- Week 2 covers the **Detect → Fix** half of Detect → Fix → Verify → Deliver:
  evidence becomes findings, and a finding becomes a proposal. Nothing is
  verified or delivered yet.
- **The backend owns truth; the model owns suggestions.** A model can describe a
  defect, but only the backend assigns ids, status, verification flags and
  severity handling — and only after schema and semantic checks.
- **Two safety funnels, same shape**:
  - VLM: raw text → JSON extraction → schema → closed categories →
    contradictions → duplicates → sanitisation → record;
  - patch: raw text → payload isolation → schema → path safety → applicability →
    payload structure → budgets → record.
- **Reason codes are the observability layer.** Because every rejection has a
  stable code produced by exactly one layer, a failure can be triaged without
  reading logs or guessing which stage gave up.
- **Propose-only is a deliberate boundary.** The engine can generate and validate
  a change, but nothing touches disk until a later slice owns sandboxing and
  verification — so a bad model answer can never damage a repository.
- **Adapters are replaceable by construction.** Provider-neutral contracts,
  config selection and fail-fast factories mean the provider decision can land
  later without reworking the workflow.
- **State machines and compare-and-set are the concurrency story.** Detection and
  proposal both use atomic claims, so duplicate triggers and racing proposals
  lose cleanly instead of double-running.

---

## 8. Verification evidence

| Check | Result |
|---|---|
| Weekly slice commits | `6276312`, `bc915b1`, `e577c83`, `fbdc6a4`, `0e131c4` |
| Week 2 tests collected | **216** across 7 files |
| Full suite | **289 passed, 1 skipped** (290 collected), ~4.0s |
| Three consecutive runs | 4.01s / 4.25s / 4.00s — no flakiness or order dependence |
| Week 2 test files | `test_vlm_adapter.py` 20, `test_ai_gateway.py` 15, `test_findings.py` 23, `test_detection.py` 42, `test_action_engine.py` 41, `test_patch_extraction.py` 45, `test_week2_matrix.py` 30 |
| Corpus | VLM **0 real / 12 fixture**; patch **0 real / 7 fixture**; integration **7 MOCK** |
| Mutation A | wrong `expected_reason` → `test_vlm_parsing_fixture[non_json_prose]` fails |
| Mutation B | deleted `empty_body.txt` → orphan check + `[empty_body]` both fail |
| Endpoint surface | 16 operations / 15 paths in the live OpenAPI (5 added in Week 2) |

## 9. Status labels (verbatim)

| Capability | Status |
|---|---|
| Structured finding schema + parser (W2.3) | Tested with mocks |
| VLM adapter boundary (W2.2) | Tested with mocks |
| VLM detection workflow (W2.4) | Tested with mocks / against the local application |
| Safe action engine + proposals (W2.5) | Tested with mocks |
| Code-response extraction/rejection (W2.6) | Tested with mocks |
| Week 2 test matrix (W2.7) | Tested with mocks (0 real fixtures) |
| Real VLM / real model responses | Not implemented (provider not finalised) |
| Real ShopLite source tree / patch apply | Not implemented (handoff pending, no apply step) |
| Sandbox build / patch verification | Not implemented (Week 3) |
| PyGithub delivery / dashboard | Not implemented (Week 3/4) |
