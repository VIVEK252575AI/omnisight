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
   ├─ PatchExtractor           raw text -> structured PatchProposal  (W2.6)
   ├─ safety validation        allow-list, extensions, budgets
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

`path_unsafe`, `forbidden_file`, `extension_not_allowed`, `path_not_allowed`,
`too_many_files`, `too_large`, `duplicate_path`, `empty_edit`,
`invalid_proposal`, `generator_timeout`, `generator_error`,
`extractor_not_implemented`.

Path safety, in order:

1. **normalise** — relative POSIX only; absolute paths, drive letters, UNC,
   `..`, NUL bytes and empty segments are rejected (`path_unsafe`);
2. **forbidden** — `.git`, `.env*`, lockfiles, CI config and build config are
   rejected unless a *glob-free* approved pattern names that exact file;
3. **extension** — `.css`/`.scss`/`.jsx`/`.tsx` by default;
4. **allow-list** — matched against `OMNISIGHT_APPROVED_PATCH_PATHS`.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `OMNISIGHT_PATCH_GENERATOR` | `mock` | replaceable generator; unknown values fail fast |
| `OMNISIGHT_PATCH_MOCK_SCENARIO` | `ok` | `ok` \| `timeout` \| `error` \| `invalid_json` \| `empty` |
| `OMNISIGHT_APPROVED_PATCH_PATHS` | *(empty)* | **allow-list**; empty means everything is rejected |
| `OMNISIGHT_PATCH_ALLOWED_EXTENSIONS` | `.css,.scss,.jsx,.tsx` | patchable extensions |
| `OMNISIGHT_PATCH_SOURCE_ROOT` | *(empty)* | read-only context root |
| `OMNISIGHT_MAX_PATCH_FILES` | `5` | max files per proposal |
| `OMNISIGHT_MAX_PATCH_BYTES` | `4096` | max edit-payload bytes per proposal |
| `OMNISIGHT_PATCH_CONTEXT_MAX_BYTES` | `4096` | cap on source text sent to the generator |

## Status of this slice

- **Tested with mocks / against the local application.** With the production
  default (`StubPatchExtractor`) every proposal is rejected with
  `extractor_not_implemented`; that is the honest state until W2.6 lands. The
  `proposed` path is covered by tests that inject a labelled test-double
  extractor.
- **Blocked on the ShopLite handoff.** No ShopLite source tree exists in this
  repository, so `OMNISIGHT_APPROVED_PATCH_PATHS` defaults to empty and
  everything is rejected on `path_not_allowed`.
- **No apply step.** Approving and applying a patch (isolated worktree, rerun
  of detection, `patch_verified`) is a later slice.
