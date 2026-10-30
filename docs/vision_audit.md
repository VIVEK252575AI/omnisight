# MR.1 - Vision Audit: Detect a Known Defect from a Screenshot

- **Owner**: Backend / FastAPI Developer
- **Branch**: `lakshy` (`VIVEK252575AI/omnisight`)
- **Date**: 2026-10-21
- **Nature**: verification task; harness built, run, and unit-tested
- **Corpus**: `FIXTURE` - locally generated controlled defects (see section 3)

> **Vision Audit NOT performed on a real model.**
>
> No VLM provider is configured in this repository. Every audit run below used
> the deterministic offline `MOCK` adapter (`adapter_label: MOCK`,
> `is_mock: true`, `model_inference_performed: false`). The MOCK adapter ignores
> the images and returns a canned payload. These runs verify the **harness and
> the detection plumbing only**. They are not evidence of VLM accuracy, and no
> accuracy percentage is claimed.

---

## 1. What was asked and what was actually possible

The task asked for a real-model audit of seven cases (hidden submit button,
clipped checkout button, overlapping elements, low-contrast text, responsive
failure, healthy control, ambiguous case), screenshot-only and with DOM, three
repetitions each, scored against ground truth.

Preconditions found in the repository:

| Requirement | Reality |
|---|---|
| Real VLM adapter | **Missing.** `OMNISIGHT_VLM_ADAPTER=mock` is the only working adapter; the `teammate` adapter is an unimplemented shell. |
| ShopLite application | **Missing.** No React app or source tree in the repo. |
| ShopLite defect screenshots | **Missing.** The only screenshots are `tests/fixtures/screenshots/*.png`, all 1x1 placeholders (70 bytes). |
| Defect manifest / ground truth | **Missing.** `ground_truth.json` did not exist. |
| Collector observations | Only two fixture observations exist; neither is a defect capture. |

The prompt's fallback was therefore used: build the harness, generate a
**labelled fixture corpus**, run the real detection code path with `MOCK`, and
state plainly that no real-model audit happened. No production code was
changed.

## 2. Repository preconditions (verified)

- `git rev-parse --show-toplevel` -> `E:/Infotact project/omnisight`
- Current branch -> `lakshy`; tracking `origin/lakshy`
- Working tree before the task -> clean
- Test suite before the task -> 289 passed, 1 skipped
- No commit, push, merge, rebase, stash, or PR was performed. All new files are
  untracked.

## 3. Corpus - `FIXTURE`, not ShopLite

Location: `backend/tests/fixtures/vision_audit/`

`ground_truth.json` carries `provenance.kind: "FIXTURE"` and this note:

> No ShopLite collector output, defect screenshots or defect manifest exists in
> this repository. These images were drawn locally with known geometry; they are
> not ShopLite captures.

The seven 390x844 PNGs are produced deterministically by
`backend/scripts/generate_vision_audit_fixtures.py` (standard library only; no
Pillow). Each manifest case records the image SHA-256, byte size, viewport,
injected category, target selector, pixel region, and the DOM element shape used
in `with_dom` mode.

| Case | Injected defect | Target | Expectation |
|---|---|---|---|
| `hidden_submit` | `hidden_or_unreachable_element` | `#submit` | defect |
| `clipped_checkout` | `clipped_element` | `#checkout` | defect |
| `overlapping_elements` | `overlapping_elements` | `.promo` | defect |
| `low_contrast_text` | `low_contrast_text` | `.price` | defect |
| `responsive_failure` | `responsive_layout_failure` | `.product-grid` | defect |
| `healthy_control` | none | - | clean |
| `ambiguous_case` | none | - | needs inspection or hedged |

Injection is verified structurally, not by eye: `tests/test_vision_audit.py`
decodes the PNGs (filter-0 rows) and asserts the defining pixels - the clipped
button reaches x=389, the hidden slot is white inside a pale outline, the healthy
slot is accent-filled, the low-contrast text is `rgb(238,238,238)`, and the promo
card paints over the product card. The model that wrote this report cannot accept
image input, so these pixel assertions are the automated substitute for a visual
check; a human should still eyeball the seven images.

## 4. Method

Harness: `backend/scripts/vision_audit.py`. It boots the real FastAPI app with
uvicorn on a real TCP port and drives HTTP only:

1. `POST /api/scans` with the 390x844 viewport and route.
2. `POST /api/v1/scans/{id}/evidence` with an `observation/v1` payload referencing
   the fixture image. `screenshot_only` sends `elements: []`; `with_dom` sends the
   manifest's DOM elements.
3. `POST /api/v1/scans/{id}/detect` (202) and poll
   `GET /api/v1/scans/{id}/findings?include_raw=true` until detection settles.
4. Score the parsed findings against ground truth and store one record per
   (case, mode, repetition).

Server environment for the run: `OMNISIGHT_VLM_ADAPTER=mock`,
`OMNISIGHT_VLM_MOCK_SCENARIO=clipped_button`,
`OMNISIGHT_DETECTION_EXPOSE_RAW_OUTPUT=true`, artifacts root =
`backend/tests/fixtures/vision_audit`.

Scoring definitions (pure functions, unit-tested):

- **detected** - a finding's `category` equals the injected category. Target and
  region plausibility are reported separately, so a right-category/wrong-target
  hit cannot hide.
- **wrong_category** - findings exist but none has the injected category.
- **missed** - the injected-defect case produced no findings.
- **false_positive** - findings on the healthy control.
- **clean** - no findings and a `no_defects`/`detected` outcome on the healthy control.
- **hedged** - ambiguous case with `needs_inspection` or any finding confidence
  `< 0.5`.
- **not_hedged** - ambiguous case answered confidently.
- **error** - detection settled as `detection_failed`.

Target plausibility compares selector id/class tokens (`#checkout` matches
`button#checkout`, not `#submit`). Region plausibility requires positive-area
intersection with the injected box; both `width/height` (finding schema) and
`w/h` (DOM box schema) are normalised.

## 5. Results

Source of every number below: `docs/vision_audit_results.json`
(`summary` object). 42/42 expected records were produced, 0 harness failures.
All 42 records: `adapter_label = MOCK`, `is_mock = true`,
`model_inference_performed = false`, `parse_status = ok`, run-level raw text
present, `detection.duration_ms = 0.0`.

> Read this table as **harness behaviour under a scripted adapter**, not as model
> quality. The single canned MOCK response is a `clipped_element` on `#submit`
> with a fixed region near the top-left; it is unrelated to the images.

| Case | Mode | Runs | Verdicts | Category correct | Target plausible | Region plausible | Stable |
|---|---|---|---|---|---|---|---|
| hidden_submit | screenshot_only | 3 | wrong_category x3 | 0/3 | 0/3 | 0/3 | yes |
| hidden_submit | with_dom | 3 | wrong_category x3 | 0/3 | 0/3 | 0/3 | yes |
| clipped_checkout | screenshot_only | 3 | detected x3 | 3/3 | 0/3 | 0/3 | yes |
| clipped_checkout | with_dom | 3 | detected x3 | 3/3 | 0/3 | 0/3 | yes |
| overlapping_elements | screenshot_only | 3 | wrong_category x3 | 0/3 | 0/3 | 0/3 | yes |
| overlapping_elements | with_dom | 3 | wrong_category x3 | 0/3 | 0/3 | 0/3 | yes |
| low_contrast_text | screenshot_only | 3 | wrong_category x3 | 0/3 | 0/3 | 0/3 | yes |
| low_contrast_text | with_dom | 3 | wrong_category x3 | 0/3 | 0/3 | 0/3 | yes |
| responsive_failure | screenshot_only | 3 | wrong_category x3 | 0/3 | 0/3 | 0/3 | yes |
| responsive_failure | with_dom | 3 | wrong_category x3 | 0/3 | 0/3 | 0/3 | yes |
| healthy_control | screenshot_only | 3 | false_positive x3 | - | - | - | yes |
| healthy_control | with_dom | 3 | false_positive x3 | - | - | - | yes |
| ambiguous_case | screenshot_only | 3 | not_hedged x3 | - | - | - | yes |
| ambiguous_case | with_dom | 3 | not_hedged x3 | - | - | - | yes |

Aggregate (counts only, no percentages):

- Defect runs: **30**. Category-correct: **6/30** (only `clipped_checkout`).
  Target-plausible: **0/30**. Region-plausible: **0/30**.
- Healthy control: **6 runs**, **6 false positives**.
- Ambiguous case: **6 runs**, **0 hedged** (the mock answered confidently).
- Detection failures: **0**.
- Repetition stability: **14/14** case-mode pairs had identical verdicts across
  three repetitions.
- Median end-to-end wall time per case-mode: 10.2-60.3 ms, including scan
  creation, evidence upload, detection and polling. This is mock-local latency,
  not provider latency.

### Hidden submit button - stated plainly

Under MOCK, the hidden-submit case is **MISSED in all 6 runs** (3
screenshot-only, 3 with DOM). The canned response reported `clipped_element` on
`#submit`, not `hidden_or_unreachable_element`; target and region were also
wrong. This says nothing about a real VLM. The hidden-submit result for a real
model is **unknown** because no real model was called.

| Mode | Repetition | Verdict | Plain result |
|---|---|---|---|
| screenshot_only | 1 | wrong_category | missed |
| screenshot_only | 2 | wrong_category | missed |
| screenshot_only | 3 | wrong_category | missed |
| with_dom | 1 | wrong_category | missed |
| with_dom | 2 | wrong_category | missed |
| with_dom | 3 | wrong_category | missed |

## 6. Failures, anomalies, and prompt/schema issues

1. **No real model, no ShopLite evidence.** The main task premise is unmet in the
   repository. The fallback is explicitly labelled throughout the results file
   and this report.
2. **MOCK is server-scoped, not case-scoped.** `OMNISIGHT_VLM_MOCK_SCENARIO` is
   read once at startup; there is no hidden-button scenario. Scripting per-case
   answers would mean restarting per case and would fake the appearance of
   detection. It was deliberately not done.
3. **Detected is category-only.** On `clipped_checkout` the MOCK category matches,
   so the verdict is `detected`, but the target is `#submit` (ground truth
   `#checkout`) and the region does not overlap. The table exposes this instead of
   collapsing it into a single "correct".
4. **Screenshot-only has no target to verify.** With `elements: []`, the parser
   marks every `target_candidate` as `target_unverified`. That is honest, but it
   means target plausibility in `screenshot_only` is necessarily model text vs
   ground truth, not collector-verified geometry.
5. **Per-finding `raw_output` is null.** Raw model text is exposed on the
   detection run (`detection.raw_output`, populated for 42/42 records), not on each
   finding record. The results file stores the run-level text once per record.
6. **Region schema mismatch (handled).** Findings use
   `{x, y, width, height}`; DOM elements use `{x, y, w, h}`. The scorer normalises
   both; any future corpus generator must not assume they are interchangeable.
7. **No image input in this model.** Fixture geometry is asserted by pixel tests,
   but the author could not visually inspect the PNGs.

## 7. Harness verification

| Check | Command | Outcome |
|---|---|---|
| Fixture generation | `.\.venv\Scripts\python.exe scripts\generate_vision_audit_fixtures.py` | 7 images + `ground_truth.json` written |
| New tests | `.\.venv\Scripts\python.exe -m pytest tests\test_vision_audit.py -q` | **12 passed** |
| Full suite | `.\.venv\Scripts\python.exe -m pytest tests\ -q` | **301 passed, 1 skipped** in 5.16 s |
| Audit run | `.\.venv\Scripts\python.exe scripts\vision_audit.py` | **42/42 records**, 0 harness failures, exit 0 |

The 12 new tests cover: manifest labelling and case order, per-image SHA-256/
size/dimensions, pixel-level injection checks, detected/wrong-category/missed,
right-category-wrong-target, detection failure, healthy clean vs false positive,
ambiguous hedged vs confident, selector matching, region normalisation, and summary
counts.

## 8. Status labels

Using the project's vocabulary verbatim:

- **Vision audit against a real model**: **Not implemented** (no provider
  configured; no real adapter to call).
- **Audit harness against the local application**: **Tested against the local
  application** (real uvicorn, real HTTP, real detection endpoints, 42/42 runs).
- **Scoring logic**: **Tested with mocks** and synthetic ground truth
  (12 unit tests).
- **ShopLite defect capture / defect manifest**: **Not implemented** (no
  application, no collector output). The corpus used here is `FIXTURE`.
- **Real VLM integration**: **Not implemented**.
- **Verified against a real external integration**: **not claimed**.

## 9. Known limitations

- The audit does not measure detection accuracy, recall, or false-positive rate
  of any model. Only harness behaviour is verified.
- The corpus is synthetic and small (7 cases, 390x844 only). It does not exercise
  1440x900, hover/focus states, or real ShopLite markup.
- MOCK ignores images entirely; therefore all 42 verdicts reflect a canned string,
  not visual reasoning.
- The harness polls every 100 ms and treats the first non-transitional scan status
  as settled; a very slow real adapter would need a larger `--timeout`.
- The images are generated fixtures, not collector artifacts; their DOM elements
  are hand-authored summaries, not captured DOM.
- No commit/push was performed, so none of this is on `origin/lakshy` yet.

## 10. What is needed to perform the real audit

- **VLM owner**: choose a provider, implement the `VisionAnalyzer` adapter
  (image-capable), inject it through `create_vision_analyzer`, and provide a model
  label. No provider/model/key is hardcoded today.
- **Playwright/collector owner**: capture ShopLite at 390x844 and 1440x900 for the
  four injected defects plus a healthy and an ambiguous page; ship the PNGs,
  `observation.json` per capture, and a ground-truth manifest with category,
  target selector and region.
- **ShopLite owner**: deliver the application and the defect-injection switch.
- Then rerun `scripts/vision_audit.py` unchanged and replace the MOCK results.


> Note: The seven fixture results in this document are MOCK-only (offline adapter). They are not real Gemini inference.
