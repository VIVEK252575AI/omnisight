# Week 2 — Safe Action Engine / Patch Proposals (W2.5)

How a validated finding becomes a **reviewable** patch proposal.

Implemented in `backend/app/routers/patches.py`, `backend/app/action_engine.py`,
`backend/app/patch_schemas.py`, `backend/app/patch_paths.py`,
`backend/app/patch_validation.py`, `backend/app/patch_storage.py`,
`backend/app/source_context.py` and `backend/app/ai/adapters/patch.py`.

**Nothing in this slice writes to disk.** No file is created, edited, moved or
deleted, and no patch is applied. A proposal is a stored record that a human (or
a later slice) can review.

## Endpoints

| Method | Path | Success | Failures |
| --- | --- | --- | --- |
| `POST` | `/api/v1/findings/{finding_id}/patch-proposals` | `201` — a proposal record (`proposed` **or** `rejected`) | `404 FINDING_NOT_FOUND`, `409 FINDING_NOT_PATCHABLE` |
| `GET` | `/api/v1/findings/{finding_id}/patch-proposals` | `200` — every attempt so far, oldest first | `404 FINDING_NOT_FOUND` |

`201` is returned for a rejection too: a rejection is a stored, reviewable
outcome with reason codes, not a transport error. A generator or extractor
failure is never reported as success.

## Pipeline

```
finding (validated, W2.3/W2.4)
   │
   ├─ 404 / 409 guard          only a `detected` finding may be proposed against
   ├─ source context           approved files only, size-capped, read-only
   ├─ PatchGenerator           raw text, under a wall-clock timeout
   ├─ PatchExtractor           raw text -> schema -> paths -> applicability ->
   │                           payload checks -> structured PatchProposal (W2.6)
   ├─ safety validation        allow-list, extensions, budgets (defence in depth)
   └─ persist                  `proposed` (claims the finding) or `rejected`
```

The generator boundary (`app/ai/adapters/patch.py`) mirrors the VLM adapter:
provider-neutral, config-selected, replaceable, and it must not parse, execute,
import, interpret or persist anything. Model text is **untrusted** and only
becomes a record after the extractor and validator have both accepted it.

## Finding state

`FindingStatus` gains `patch_proposed`:

- `detected` — proposable;
- `patch_proposed` — a proposal is awaiting review; a further `POST` returns
  `409 FINDING_NOT_PATCHABLE`.

A `rejected` attempt does **not** change the finding, so a corrected attempt is
allowed (bounded retries). The claim is atomic
(`DetectionStore.compare_and_set_finding_status`), so two concurrent proposals
cannot both win.

## Reason codes (closed set)

The set is single-sourced in `RejectionReason` (`app/patch_schemas.py`). One code
per concept, produced by exactly one layer:

| Code | Layer | Meaning |
| --- | --- | --- |
| `no_patch_found` | extraction | no JSON object, and no single `omnisight-patch` block |
| `ambiguous_multiple_patches` | extraction | more than one `omnisight-patch` block |
| `truncated` | extraction | output looks cut off (unbalanced / unterminated fence) |
| `schema_invalid` | extraction | payload did not match the `PatchProposal` schema (or wrong `finding_id`) |
| `path_unsafe` | paths | not a clean relative POSIX path (absolute, `..`, NUL, ...) |
| `forbidden_file` | paths | `.git`, `.env*`, lockfile, CI or build config (not explicitly allowlisted) |
| `unsupported_file_type` | paths | extension not in `OMNISIGHT_PATCH_ALLOWED_EXTENSIONS` |
| `path_not_approved` | paths | not matched by `OMNISIGHT_APPROVED_PATCH_PATHS` |
| `edit_not_applicable` | applicability | `old_text` occurs zero times, or the target file cannot be confirmed |
| `edit_ambiguous` | applicability | `old_text` occurs more than once |
| `forbidden_construct` | payload | forbidden API or unbalanced structure in `new_text` |
| `too_many_files` | budget | more files than `OMNISIGHT_MAX_PATCH_FILES` |
| `too_large` | budget / extraction | edit payload over `MAX_PATCH_BYTES`, or raw text over `PATCH_MAX_RESPONSE_BYTES` |
| `duplicate_path` | budget | the same normalised path appears twice |
| `empty_edit` | budget | an edit's `old_text` is empty/whitespace-only |
| `generator_timeout` | generator | generator exceeded the wall-clock timeout |
| `generator_error` | generator | generator raised / returned non-text |

Path safety, in order:

1. **normalise** — relative POSIX only; absolute paths, drive letters, UNC,
   `..`, NUL bytes and empty segments are rejected (`path_unsafe`);
2. **forbidden** — `.git`, `.env*`, lockfiles, CI config and build config are
   rejected unless a *glob-free* approved pattern names that exact file;
3. **extension** — `.css`/`.scss`/`.jsx`/`.tsx` by default (`unsupported_file_type`);
4. **allow-list** — matched against `OMNISIGHT_APPROVED_PATCH_PATHS`
   (`path_not_approved`).

The extraction / applicability / payload codes are produced by the W2.6
extractor; see [`week2_code_extraction.md`](week2_code_extraction.md).

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `OMNISIGHT_PATCH_GENERATOR` | `mock` | `mock` (offline) or `gemini` (server-side API); unknown values fail fast |
| `OMNISIGHT_GEMINI_API_KEY` | *(unset)* | server-side Gemini credential; required when generator is `gemini` |
| `OMNISIGHT_GEMINI_MODEL` | `gemini-3.6-flash` | configured Gemini text model |
| `OMNISIGHT_PATCH_MOCK_SCENARIO` | `ok` | `ok` \| `timeout` \| `error` \| `invalid_json` \| `empty` \| `prose_with_fence` \| `two_plain_fences` \| `two_tagged_fences` \| `prose_only` \| `truncated` |
| `OMNISIGHT_APPROVED_PATCH_PATHS` | *(empty)* | **allow-list**; empty means everything is rejected |
| `OMNISIGHT_PATCH_ALLOWED_EXTENSIONS` | `.css,.scss,.jsx,.tsx` | patchable extensions |
| `OMNISIGHT_PATCH_SOURCE_ROOT` | *(empty)* | read-only context root |
| `OMNISIGHT_MAX_PATCH_FILES` | `5` | max files per proposal |
| `OMNISIGHT_MAX_PATCH_BYTES` | `4096` | max edit-payload bytes per proposal |
| `OMNISIGHT_PATCH_CONTEXT_MAX_BYTES` | `4096` | cap on source text sent to the generator |
| `OMNISIGHT_PATCH_MAX_RESPONSE_BYTES` | `16384` | cap on raw text handed to the extractor (before parsing) |

Gemini retries HTTP `500`, `502`, `503` and `504` responses at most twice,
with one- and two-second backoffs inside the configured request deadline.
Authentication, quota, network and timeout failures are not retried. A timeout
is treated as a single attempt because the provider may already have processed
the request.

## Status of this slice

- **Tested with mocks / against the local application.** The production default
  extractor is now the real `SourcePatchExtractor` (W2.6): it isolates the patch
  payload from prose and rejects anything unsafe, so a valid MOCK proposal
  reaches `proposed` and an unsafe one is recorded `rejected` with a reason code.
- **Gemini is integrated behind the same generator boundary.** When selected,
  it receives only the backend-built approved source context; its output still
  passes through extraction, exact-match applicability, path allow-list, size,
  and code-safety checks. Unit tests mock provider responses; a live Gemini run
  has also verified detection and patch generation against the local ShopLite
  demo.
- **Controlled ShopLite validation is implemented.** The demo runner captures
  desktop/mobile behavior, requests a finding and proposal, then applies only
  the stored validated proposal in a disposable Docker container with no
  network and bounded resources. The verified run reduced mobile overflow from 167px to
  0px; desktop overflow remained 0px, the order confirmation worked, and the
  browser reported no errors or attempted external requests.
- **No host apply step.** Sandbox verification does not write the proposal to
  the host checkout. Validated edit payloads are stored in PostgreSQL so they
  can be reviewed after an API restart; raw model output is not stored. GitHub publishing remains a separate explicitly
  approved step.
