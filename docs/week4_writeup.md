# Week 4 Write-Up — Optimization and Dashboard Contracts (W4.1 – W4.2 done)

- **Owner**: Backend / FastAPI Developer
- **Branch**: `lakshy` (`VIVEK252575AI/omnisight`)
- **Nature**: implementation + verification. Every capability labelled with the
  project's status vocabulary.

---

## 1. Scope and outcome

Week 4 goal: image cropping/chunking optimization + backend contracts for the
React approve/reject dashboard. The React implementation belongs to the frontend
owner; the backend supplies contracts, not UI changes.

Completed in this session:
- **W4.1 — Evidence-pipeline inspection and baseline measurement**: instrumented
  the detection path to record image bytes/dimensions, latency, token usage
  (null for mock), total processing time; produced baseline results.
- **W4.2 — Targeted screenshot crops / chunking with coordinate provenance**:
  crop/tile service, clamping, padding, scaling, coordinate round-trip, tiling,
  feature flag `IMAGE_INPUT_MODE` (default `full`), Pillow added as dependency.

Not yet executed (prompts available): W4.3 (controlled comparison), W4.4
(dashboard contracts), W4.5 (approval/rejection), W4.6 (integration checks),
W4.7 (defect fixes).

Outcome: **the backend now has a measured baseline and a crop/tile service with
provenance**, with full-screenshot mode unchanged and verified by tests. The
default remains `full`; no claim is made that cropping improves detection
without data.

## 2. Features built

### 2.1 Baseline measurement (W4.1)

- **Harness** (`backend/scripts/baseline_measure.py`): runs N=5 repetitions over
  the clipped-checkout case through the normal gateway path (mock adapter),
  recording per run: image_count, image_bytes, image_dimensions, latency_ms,
  usage (null), adapter_label, mode, parse_status, findings_count.
- **Raw results**: `backend/evaluation/week4/baseline_results.json` (per-run).
- **Summary**: `backend/evaluation/week4/baseline_summary.json`
  (count, min/median/max latency, image_bytes).
- **Report**: `docs/week4/baseline.md` with table, environment, limitations.
- **Result**: 5 runs, latency 0.18–0.63 ms (median 0.22), 2828 bytes, 1 finding
  per run, all parse_status ok.
- Token/latency numbers are **unavailable** (mock adapter); stated as such.

### 2.2 Crop/tile service (W4.2)

- **Module** `backend/app/image_crops.py`:
  - `CropSpec` (source_box, padding, scale, reason) and `CropResult`
    (crop_box, output_size, scale, candidate_ref, reason).
  - `clamp_box`: clamps to image bounds, returns None on zero-area or fully
    off-screen.
  - `apply_padding`: pads and re-clamps.
  - `crop_image`: decodes with Pillow, clamps, pads, crops, optionally resizes,
    saves to artifact path; decompression-bomb guard (`MAX_IMAGE_PIXELS`).
  - `tile_image`: vertical chunks with overlap, bounded.
  - `source_to_crop` / `crop_to_source`: coordinate round-trip helpers.
- **Config**: `OMNISIGHT_IMAGE_INPUT_MODE` (full/crops/tiles, default full),
  `OMNISIGHT_IMAGE_CROP_PADDING_PX`, `OMNISIGHT_IMAGE_CROP_MAX_COUNT`,
  `OMNISIGHT_IMAGE_TILE_OVERLAP_PX`, `OMNISIGHT_IMAGE_MAX_DIMENSION`.
- **Dependency**: Pillow 11.0.0 added to requirements (approved).
- **Tests** (`tests/test_image_crops.py`): 9 tests — clamping, padding, scaling,
  coordinate round-trip, tiling bounds, corrupt image rejection. All pass.

### 2.3 Safety

- Image size limits and decompression-bomb protection on input.
- Crops written only under the artifact root (path controlled by caller).
- Cropping failure falls back to full-image mode (documented; fallback logic
  lives in the caller, not the service).
- Full-screenshot mode unchanged when `IMAGE_INPUT_MODE=full`.

---

## 3. Tests

- `test_image_crops.py`: 9 passed.
- `test_vlm_metrics.py`: 1 passed.
- `test_supabase_storage.py`: 1 passed.
- Existing suite baseline preserved (5 pre-existing failures: PyGithub SDK
  missing, contradictory redaction assertion, undefined `status`).

---

## 4. Fixes

- `capture_screenshot.py`: switched `wait_until` from `networkidle` to
  `domcontentloaded` + best-effort `networkidle` (10s) so slow sites no longer
  time out.
- `playwright_captures.py`: async subprocess (Windows compatibility), evidence
  record registration, scan transition to `evidence_collection`.

---

## 5. What I learned

- **Baseline before optimization.** Measuring image bytes and latency first
  prevents claiming improvements without data. With the mock adapter, only local
  processing time and image size are meaningful; token/real latency require a
  live provider.
- **Coordinate provenance must be tested, not assumed.** The round-trip helpers
  (`source_to_crop` / `crop_to_source`) make crop findings mappable back to the
  source screenshot, which is essential for the dashboard's before/after view.
- **Clamping and zero-area handling are easy to get wrong.** A crop box fully
  outside the viewport or with zero area must return None, not silently produce
  an empty image.
- **Pillow is the right minimal dependency.** It provides decode, resize, crop
  and decompression-bomb protection in one package; no need for OpenCV.
- **Default to `full`.** Until data shows cropping helps, keep the original
  full-screenshot path unchanged so nothing regresses.

## 6. Experience

- **The W4.1 baseline was quick because the gateway already returns structured
  results.** No new instrumentation was needed beyond wrapping the call.
- **W4.2's coordinate tests caught a real subtlety.** Scaling and padding
  interact; the round-trip test (source → crop → source) is the single most
  important assertion.
- **Slow sites broke the capture.** `networkidle` never resolves on some pages;
  switching to `domcontentloaded` + best-effort wait fixed both test sites.
- **Honest labelling matters.** The baseline clearly states token/latency are
  unavailable under mock, so no one mistakes local timing for provider latency.

## 7. Understanding

- Week 4 covers **Optimization + Dashboard contracts**: cropping/chunking reduces
  payload only if proven; the backend supplies stable contracts, the React owner
  builds the UI.
- **Optimization is data-gated.** A mode that is cheaper but misses defects must
  be reported as such; the default stays `full` until justified.
- **Coordinate provenance is non-negotiable.** A cropped finding must map back to
  the original screenshot, or the dashboard cannot show before/after correctly.
- **Backend owns contracts, frontend owns UI.** Response models, pagination,
  status enums and error envelopes are the backend's deliverable; layout is not.
- **Human review is the final gate.** Approval records a decision; it never
  merges or deploys.

---

## 8. Verification evidence

| Check | Result |
|---|---|
| Baseline (5 runs, mock) | latency 0.18–0.63 ms, 2828 bytes, 1 finding/run |
| Crop tests | 9 passed |
| VLM metrics test | 1 passed |
| Supabase storage test | 1 passed |
| End-to-end (3 sites) | capture + detect + findings=1, status=detected |

## 9. Status labels

| Capability | Status |
|---|---|
| Baseline measurement (W4.1) | Tested with mocks (token/latency unavailable) |
| Crop/tile service (W4.2) | Tested against the local application |
| Controlled comparison (W4.3) | Not implemented |
| Dashboard contracts (W4.4) | Not implemented |
| Approval/rejection (W4.5) | Not implemented |
| Integration checks (W4.6) | Not implemented |
| Defect fixes (W4.7) | Not implemented |

## 10. Remaining work

- W4.3: run controlled comparison (full vs crops vs tiles) with real or mock
  adapter; report correctness impact, not only cost.
- W4.4: dashboard response models, pagination, seed script, OpenAPI snapshot.
- W4.5: review decisions with audit trail, legal transitions, no merge.
- W4.6: integration/regression checks with seeded dashboard.
- W4.7: triage and fix P0/P1 defects with regression tests.
- Real Supabase persistence: network to port 5432 must be open; .env configured.
- React dashboard: frontend owner's responsibility.
