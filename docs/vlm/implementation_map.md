# VLM Implementation Map (lakshy branch)

1. Screenshot entry: `backend/app/routers/detection.py` (detection endpoint) + `backend/app/detection_service.py` (build_browser_diagnostics, etc.).
2. Artifact resolution: `backend/app/ai/adapters/base.py:51` `resolve_image_paths`; evidence store `backend/app/evidence_storage.py`.
3. Image limits: `backend/app/config.py` `vlm_max_image_bytes`, `vlm_max_images_per_analysis`; enforced in `backend/app/ai/adapters/base.py:65` `enforce_image_budget`.
4. Candidate selectors: `backend/app/ai/adapters/gemini_vision.py` (candidate_selectors from measurements prefixed `box.`).
5. Viewport/browser/diagnostics context: `backend/app/ai/adapters/gemini_vision.py` context dict (route, viewport, browser, browser_diagnostics, measurements).
6. Prompt construction: `backend/app/ai/prompts.py` `load_prompt`; default prompt version configured in detection flow; files `backend/app/ai/prompts/visual-analysis-v1..v3.txt`.
7. Gemini request: `backend/app/ai/adapters/gemini.py` `GeminiTextAdapter.generate_json_with_images` (inlineData base64, responseMimeType application/json).
8. Parsing: `backend/app/ai/parsing.py`; gateway `backend/app/ai/gateway.py:89` `analyze_visual`.
9. Bounded repair: `backend/app/ai/gateway.py` one repair attempt using `structured-output-repair-v1.txt`.
10. Finding validation: `backend/app/ai/contracts.py` (VisualFinding schema); gateway validates.
11. Error handling: `backend/app/ai/errors.py` (Vision* errors); `GeminiProviderError` mapping in gemini_vision.py.
12. Hand-off to workflow: detection router stores findings; repair orchestrator consumes findings.
13. Post-fix verification: `backend/app/patch_verifier.py` / `backend/app/verification.py` (SourcePatchVerifier + VLM re-check).
14. Provenance: `provider_label` (MOCK/GEMINI) + `model_label` in RawVisionResult.

Default adapter: `backend/app/config.py` `vlm_adapter` (default "mock"). Prompt version default set in detection flow.
