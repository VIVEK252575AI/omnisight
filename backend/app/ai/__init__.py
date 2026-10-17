"""Provider-neutral AI Gateway boundary (Week 2).

This package is the single boundary between OmniSight and any visual/LLM
model. It contains only:

- ``contracts``  — validated request/result models (no provider knowledge),
- ``errors``     — the AI error taxonomy,
- ``validation`` — semantic checks applied to model output,
- ``providers``  — adapters (a deterministic offline ``fake`` adapter ships now),
- ``gateway``    — the orchestration point that parses + validates output.

Nothing here imports a model SDK or knows a provider/model id. Model output is
always treated as untrusted text and parsed into the ``contracts`` models before
any other module may consume it.
"""

from app.ai.errors import AiError, AiErrorCode

__all__ = ["AiError", "AiErrorCode"]
