# VLM Engineering (OmniSight)

## Architecture
GeminiVisionAnalyzer (backend/app/ai/adapters/gemini_vision.py) implements VisionAnalyzer. Gateway (backend/app/ai/gateway.py) validates structured output with one bounded repair. Parsing in backend/app/ai/parsing.py. Contracts in backend/app/ai/contracts.py.

## Screenshot handling
Images sent as inline base64 via GeminiTextAdapter.generate_json_with_images (backend/app/ai/adapters/gemini.py). MIME from extension. Budget enforced (vlm_max_image_bytes, vlm_max_images_per_analysis).

## Prompt design
Default prompt version visual-analysis-v3 (backend/app/ai/prompts.py). System instruction adds untrusted-evidence + no-secrets + JSON-only.

## Structured output
responseMimeType application/json. Findings validated against closed categories. Target must be a supplied candidate or null.

## Errors
GeminiProviderError kinds mapped to Vision* errors. No silent fallback to MOCK.

## Security
Screenshots/text/diagnostics are untrusted. No exec/eval. Key server-side only.

## Verification
Post-fix verification uses independent browser evidence (bbox vs viewport) so VLM alone cannot declare success.
