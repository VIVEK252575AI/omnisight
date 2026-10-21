# Week 2 — Test Suite Note (W2.7)

Coverage map for the Week 2 VLM + Action Engine work: **valid, malformed,
missing, unsupported and failed** calls. This note is the human-readable
companion to the manifest-driven matrix in
`backend/tests/test_week2_matrix.py`.

## How the suite is organised

| Layer | Where | What it proves |
| --- | --- | --- |
| VLM parsing (W2.3) | `tests/test_findings.py`, `tests/test_week2_matrix.py` | raw text -> `VlmResponse` or a `ParseReason` |
| VLM adapter boundary (W2.2) | `tests/test_vlm_adapter.py`, `tests/test_ai_gateway.py` | transport errors, timeouts, budgets, one bounded repair |
| Detection workflow (W2.4) | `tests/test_detection.py`, `tests/test_week2_matrix.py` | evidence -> findings; every failure settles the scan |
| Action engine (W2.5) | `tests/test_action_engine.py` | proposal -> stored record; nothing written to disk |
| Code extraction (W2.6) | `tests/test_patch_extraction.py`, `tests/test_week2_matrix.py` | raw generator text -> validated `PatchProposal` or a reason code |

`test_week2_matrix.py` is **corpus-driven**: two directories each hold a
`manifest.json` (the single source of truth) plus the raw response files it
names. Adding a fixture file + a manifest entry adds a test; deleting a file or
corrupting an expectation makes the matrix fail loudly
(`test_*_manifest_files_exist_and_no_orphans`).

- `tests/fixtures/vlm_responses/` — raw VLM output -> `parse_vlm_output`
- `tests/fixtures/ai_patch_responses/` — raw generator output -> `SourcePatchExtractor`
  (patch fixtures carry a `__FINDING_ID__` placeholder substituted at test time,
  plus the source tree and allow-list the case must run against)

## Coverage matrix

### VLM output parsing — 12 fixtures

| Scenario | Kind | Expected |
| --- | --- | --- |
| valid_single_finding | fixture | ok / `defects_found` / 1 finding |
| valid_multiple_findings | fixture | ok / `defects_found` / 2 findings |
| valid_no_defects | fixture | ok / `no_defects` / 0 findings |
| valid_needs_inspection | fixture | ok / `needs_inspection` / 0 findings |
| json_in_prose | fixture | ok (one tagged ```` ```json ```` block among prose) |
| missing_required_field | fixture | rejected `schema_invalid` |
| wrong_types | fixture | rejected `schema_invalid` |
| unknown_category | fixture | rejected `unknown_category` |
| contradictory_outcome | fixture | rejected `contradictory` |
| non_json_prose | fixture | rejected `not_json` |
| truncated_json | fixture | rejected `not_json` |
| empty_body | fixture | rejected `empty` |

### Patch extraction — 7 fixtures

| Scenario | Kind | Expected |
| --- | --- | --- |
| valid_css_edit | fixture | accepted |
| valid_tsx_edit | fixture | accepted (balanced JSX) |
| unsupported_file_type | fixture | rejected `unsupported_file_type` |
| unapproved_path | fixture | rejected `path_not_approved` |
| ambiguous_blocks | fixture | rejected `ambiguous_multiple_patches` |
| forbidden_construct | fixture | rejected `forbidden_construct` |
| edit_mismatch | fixture | rejected `edit_not_applicable` |

### Integration failures through the detection API — 7 rows (all MOCK)

Each row drives `POST /api/v1/scans/{scan_id}/detect` with a MOCK analyser and
asserts the scan settles in `detection_failed` with the expected `reason_code`.

| Scenario | Analyser | Reason code |
| --- | --- | --- |
| timeout | `MockVisionAnalyzer("timeout")` | `timeout` |
| provider_unavailable | `MockVisionAnalyzer("provider_error")` | `provider_unavailable` |
| rate_limited | injected `_RateLimitedAnalyzer` (no mock scenario exists) | `rate_limited` |
| adapter_exception | injected `_CrashAnalyzer` (unexpected `RuntimeError`) | `interrupted` |
| oversized_image | `MockVisionAnalyzer(..., max_image_bytes=1)` | `budget_exceeded` |
| malformed_output_not_json | `MockVisionAnalyzer("malformed_text")` | `not_json` |
| malformed_output_schema_invalid | `MockVisionAnalyzer("malformed_schema")` | `schema_invalid` |

## Real vs FIXTURE

**No live model was available** during W2.7, so the corpus is entirely
hand-authored and labelled `kind: fixture`; every integration row runs against
an offline `MOCK` analyser.

- VLM fixtures: **0 real / 12 fixture**
- Patch fixtures: **0 real / 7 fixture**
- Integration rows: **7** (all `MOCK`, offline)

A `real` entry must record a `source` (where it was captured); the provenance
test (`test_corpus_provenance_is_labelled`) enforces that. Real-response tests
are **pending** the VLM provider decision.

## Mutation sanity check

The matrix was shown to fail on deliberate corruption, then reverted:

1. wrong `expected_reason` on `non_json_prose` ->
   `test_vlm_parsing_fixture[non_json_prose]` fails
   (`assert 'not_json' == 'schema_invalid'`);
2. deleting `empty_body.txt` ->
   `test_vlm_manifest_files_exist_and_no_orphans` and
   `test_vlm_parsing_fixture[empty_body]` both fail.

## Latest result

`cd backend && .\.venv\Scripts\python.exe -m pytest tests\ -q`
-> **289 passed, 1 skipped** across three consecutive runs
(4.01s / 4.25s / 4.00s); no order-dependence or flakiness observed. The one skip
is the Windows `os.symlink` test (symlink creation unavailable).

## Known gaps

- No real-model fixtures (provider not finalised).
- `rate_limited` has no `MockVisionAnalyzer` scenario; the matrix injects a
  MOCK analyser instead.
- Detection reason codes are asserted at the API boundary; the underlying
  `AiErrorCode` mapping is covered in `test_ai_gateway.py`.
