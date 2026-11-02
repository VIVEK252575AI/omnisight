# Gemini Setup (VLM)

## Env var
`OMNISIGHT_GEMINI_API_KEY` (verified in `backend/app/config.py`).

## PowerShell (session only)
```
$env:OMNISIGHT_GEMINI_API_KEY="YOUR_KEY"
$env:OMNISIGHT_VLM_ADAPTER="gemini"
$env:OMNISIGHT_GEMINI_MODEL="gemini-3-flash-preview"
cd backend
$env:PYTHONPATH="."
.\.venv\Scripts\python.exe scripts\verify_gemini.py
```

## Connectivity
`scripts/verify_gemini.py` proves API access only, not image inference.

## Real screenshot inference
See `docs/vlm/implementation_map.md`. Run the gateway with a resolver returning a real PNG; provenance must show GEMINI / is_mock=False.

## Error diagnosis
- 401/403 -> auth; 429 -> rate limit/quota; 404 -> model unavailable; timeout -> VisionTimeoutError.
- Never log the key; redaction in `app/github_delivery.py:redact_github_token` (reused pattern).

## Avoiding committed secrets
`.env` is git-ignored. Never commit raw outputs containing image data.
