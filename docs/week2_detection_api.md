# Week 2 — VLM Detection API (W2.4)

How evidence becomes validated findings inside the running FastAPI application.
Implemented in `backend/app/routers/detection.py`,
`backend/app/detection_service.py`, `backend/app/detection_schemas.py` and
`backend/app/detection_storage.py`.

## Endpoints

| Method | Path | Success | Failures |
| --- | --- | --- | --- |
| `POST` | `/api/v1/scans/{scan_id}/detect` | `202` — schedules one detection run | `404 SCAN_NOT_FOUND`, `409 DETECTION_STATE_CONFLICT` |
| `GET` | `/api/v1/scans/{scan_id}/findings` | `200` — validated findings + latest run | `404 SCAN_NOT_FOUND` |
| `GET` | `/api/v1/findings/{finding_id}` | `200` — one finding with evidence refs | `404 FINDING_NOT_FOUND` |

`POST /detect` is **idempotent**: while a scan is `detecting` or already
`detected`, it returns `202` with `"accepted": false` and starts nothing.
Only `evidence_collection`, `detection_failed` and `needs_inspection` can
start a run — the same set `LEGAL_TRANSITIONS` allows to reach `detecting`.

## State machine additions

```
evidence_collection ──► detecting ──► detected ──► analysis
        ▲                  │  ▲
        │                  │  └── detection_failed ──► (retry / analysis / failed)
        │                  └──► needs_inspection ──► (retry / analysis / failed)
        └── (retry from detection_failed / needs_inspection)
```

Entering `detecting` is a **single-writer compare-and-set**
(`ScanStore.compare_and_transition`), so two concurrent triggers cannot both
win — a duplicate detection can never run.

## Execution and crash safety

The adapter call runs as a FastAPI `BackgroundTasks` job (no new dependency).
The scan is moved into `detecting` *before* the job is scheduled, and the job
runs `DetectionService.run_guarded`, which:

- converts unexpected exceptions into `detection_failed`
  (`reason_code = interrupted`), and
- always settles the scan out of `detecting` in a `finally` block.

No code path can leave a scan stuck in `detecting`.

## Failure reason codes

Failures are recorded on the run rather than swallowed:

| `reason_code` | Cause |
| --- | --- |
| `no_evidence` | No stored observation for the scan. |
| `not_json` | Model text was not JSON (bounded repair re-ask first). |
| `schema_invalid` | Valid JSON, invalid finding payload (bounded repair re-ask). |
| `empty` | Empty model output (bounded repair re-ask). |
| `contradictory` | `no_defects` with findings, or `defects_found` with none. |
| `timeout` | Provider timeout. |
| `provider_unavailable` | Provider/adapter transport failure. |
| `auth_error`, `rate_limited`, `configuration_error`, `budget_exceeded` | Other adapter/config failures. |
| `interrupted` | The run raised an unexpected error and was settled. |

Repair re-asks are bounded by `OMNISIGHT_AI_MAX_REPAIR_ATTEMPTS` (default 1);
transport failures are never retried.

## Raw model output policy

Raw model text is **untrusted**. It is:

- kept **in memory only** (never written to disk, never executed/imported),
- truncated to `OMNISIGHT_DETECTION_MAX_RAW_OUTPUT_BYTES` (default 8192),
- cleared at the start of each run,
- exposed **only** when `OMNISIGHT_DETECTION_EXPOSE_RAW_OUTPUT=true` **and**
  the caller passes `?include_raw=true`,
- carried on the run record — never attached to a finding record.

Validated findings are the only thing the API otherwise returns.

## Candidate bounding

DOM candidates sent to the model are capped at
`OMNISIGHT_DETECTION_MAX_CANDIDATES` (default 50), ranked so interactive and
off-viewport elements come first. Deterministic geometry (`viewport`, `box.*`,
`in_viewport.*`) is derived from the collector's observation, not from the
model.

## API response shapes

See `backend/app/detection_schemas.py`:

- `FindingRecord` — nests the canonical W2.3 `Finding` plus provenance
  (`adapter_label`, `is_mock`, `route`, `viewport`, `evidence_ids`,
  `screenshot_artifact_ids`).
- `DetectionRun` — `adapter_label`, `is_mock`, `model_label`, `prompt_version`,
  `schema_version`, `attempts`, `duration_ms`, `outcome`, `reason_code`,
  `findings_count`, `started_at`/`finished_at`, optional `raw_output`.
- `ScanFindingsResponse` / `DetectionTriggerResponse`.

`adapter_label` is `"MOCK"` wherever a mock produced the record, so mocked
output can never be mistaken for a real inference.

## Limitations

- The generic scan API stores its run, target, immutable request version, and
  state events in PostgreSQL (`qa_runs`, `target_environments`,
  `test_flow_versions`, and `workflow_events`). Detection attempts and
  validated findings persist in `detection_attempts` and `findings`; prior
  findings are marked superseded when a newer attempt replaces them. Raw model
  text stays in memory only. The fixed ShopLite demo also persists run steps in
  `run_steps`; validated proposals use `patch_attempts`.
- Evidence metadata and the artifact-id-to-file registry are still held in
  memory. After a backend restart, scan and finding history remains available,
  but screenshot lookup and a new analysis using the prior evidence are not
  restored yet.
- `OMNISIGHT_ENABLE_VLM_DETECTION` exists but does not gate these endpoints
  yet — the workflow is exercised directly so it can be verified before the
  flag has a consumer.
- The model is still not final: everything above goes through the
  provider-neutral adapter, so no provider, model name or key is hardcoded.
- Detection runs once per request; there is no queue or retry budget yet.
