"""Settings/config endpoint for the dashboard."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings

router = APIRouter(prefix="/api/v1", tags=["settings"])


@router.get("/settings")
def get_settings_view(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "vlm_adapter": settings.vlm_adapter,
        "vlm_provider": getattr(settings, "vlm_provider", "gemini"),
        "gemini_model": settings.gemini_model,
        "openai_model": getattr(settings, "openai_model", None),
        "anthropic_model": getattr(settings, "anthropic_model", None),
        "grok_model": getattr(settings, "grok_model", None),
        "providers_configured": {
            "gemini": bool(settings.gemini_api_key or os.getenv("GEMINI_API_KEY") or os.getenv("OMNISIGHT_GEMINI_API_KEY")),
            "openai": bool(settings.openai_api_key or os.getenv("OPENAI_API_KEY") or os.getenv("OMNISIGHT_OPENAI_API_KEY")),
            "anthropic": bool(settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OMNISIGHT_ANTHROPIC_API_KEY")),
            "grok": bool(settings.grok_api_key or os.getenv("GROK_API_KEY") or os.getenv("OMNISIGHT_GROK_API_KEY") or os.getenv("XAI_API_KEY")),
        },
        "image_input_mode": settings.image_input_mode,
    }
