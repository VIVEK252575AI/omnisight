# Week 2 — Structured Visual-Finding Schema (W2.3)

This document is the contract for how a vision-model (VLM) response is turned
into a validated, persisted finding record. It is implemented in
`backend/app/ai/parsing.py` (`parse_vlm_output`) and the models in
`backend/app/ai/contracts.py`.

The schema is the **single canonical finding vocabulary** for Week 2. It
replaces the earlier LLD-only category set; anything that produced findings
(the MOCK adapter, the gateway, tests) is reconciled onto this schema.

## Finding categories (closed set)

| Category | Meaning |
| --- | --- |
| `clipped_element` | An element is partially outside the viewport/container. |
| `overlapping_elements` | Two elements overlap unintentionally. |
| `low_contrast_text` | Text has insufficient contrast against its background. |
| `responsive_layout_failure` | Layout breaks at the captured viewport. |
| `hidden_or_unreachable_element` | An element is invisible or cannot be reached. |
| `needs_inspection` | Something looks wrong but more evidence is required. |

An unknown category is **rejected** (`unknown_category`); it is never coerced.

## Outcomes

`defects_found` · `no_defects` · `needs_inspection`.

## Response shape

```json
{
  "schema_version": 1,
  "outcome": "defects_found",
  "summary": "Primary button is clipped at 390px width.",
  "findings": [
    {
      "category": "clipped_element",
      "target_candidate": "#submit",
      "observation": "The button extends past the right viewport edge.",
      "evidence": "geometry: button.x=380 width=220 viewport.width=390",
      "potential_causes": ["fixed pixel width"],
      "severity": "high",
      "confidence": 0.9,
      "region": {"x": 380, "y": 40, "width": 220, "height": 48}
    }
  ]
}
```

A healthy page returns:

```json
{ "schema_version": 1, "outcome": "no_defects", "summary": "No defects.", "findings": [] }
```

## Validation rules

Applied by `parse_vlm_output(raw_text, candidates=None)`:

1. **Conservative JSON extraction** — whole-text JSON, or exactly one tagged
   ```json block. No `eval`/`exec`. Prose, truncated JSON, or multiple blocks
   are rejected (`not_json`).
2. **Schema** — required fields, correct types, `confidence` in `[0, 1]`,
   `severity` in the severity enum (`schema_invalid`).
3. **Closed category set** — unknown category rejected (`unknown_category`).
4. **Target verification** — `target_candidate` must match a supplied DOM
   candidate; otherwise the finding is kept but flagged `target_unverified=true`.
5. **Contradiction** — `no_defects` with findings, or `defects_found` without
   findings, is rejected (`contradictory`).
6. **Duplicates** — findings sharing `(category, target_candidate)` are merged.
7. **Sanitisation** — control characters stripped and free text capped before
   the record is returned.
8. **Backend-assigned fields** — `finding_id`, `status` and `target_unverified`
   are set by the backend and never trusted from the model.

`parse_vlm_output` **never raises**: every failure becomes a `ParseResult`
with `status="rejected"` and one of the reason codes:

`empty` · `not_json` · `schema_invalid` · `unknown_category` · `contradictory`.

Reason codes map to gateway `AiError` codes in `backend/app/ai/gateway.py`;
`empty`, `not_json`, `schema_invalid` and `unknown_category` are repairable
(subject to the bounded repair re-ask), `contradictory` is not.

## Versioning

The prompt that elicits this schema is version-controlled as
`backend/app/ai/prompts/visual-analysis-v2.txt`
(`VISUAL_ANALYSIS_PROMPT_VERSION = "visual-analysis-v2"`). `v1` is retained for
reproducibility of earlier analyses.
