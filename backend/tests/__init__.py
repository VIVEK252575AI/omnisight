"""Test package bootstrap: isolate settings before importing the app."""

import os

from app.config import Settings, get_settings

# Pytest imports this package before its conftest and test modules. Keep the
# suite hermetic even when a developer's backend/.env or shell has real
# provider credentials: ordinary tests must not make live provider calls.
for _name in tuple(os.environ):
    if _name.startswith("OMNISIGHT_"):
        os.environ.pop(_name, None)
Settings.model_config["env_file"] = None
get_settings.cache_clear()
