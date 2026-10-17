"""Model provider adapters.

An adapter is the only place a concrete provider (SDK, HTTP endpoint, local
runtime) is known. Adapters translate the provider-neutral contracts into a
provider call and return :class:`~app.ai.contracts.RawModelResult` — raw,
untrusted text that the gateway parses and validates.
"""

from app.ai.providers.base import ModelProvider

__all__ = ["ModelProvider"]
