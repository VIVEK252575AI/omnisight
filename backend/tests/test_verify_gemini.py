from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pydantic import SecretStr

from app.ai.adapters.gemini import GeminiTextAdapter
from app.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_gemini


def test_verify_gemini_sends_bounded_request_and_never_prints_key(monkeypatch, capsys):
    secret = "private-test-key"
    settings = Settings(
        gemini_api_key=SecretStr(secret),
        gemini_model="gemini-test-model",
        gemini_timeout_seconds=30,
    )
    observed = {}
    monkeypatch.setattr(verify_gemini, "get_settings", lambda: settings)

    def fake_generate(self, **kwargs):
        observed.update(kwargs)
        return '{"service":"gemini","status":"ok"}'

    monkeypatch.setattr(GeminiTextAdapter, "generate_json", fake_generate)

    assert verify_gemini.main() == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "provider": "GEMINI",
        "model": "gemini-test-model",
        "status": "connected",
    }
    assert secret not in captured.out + captured.err
    assert observed["max_output_tokens"] == 256
    assert observed["timeout_seconds"] == 20


def test_verify_gemini_reports_quota_without_provider_body_or_key(monkeypatch, capsys):
    secret = "private-test-key"
    settings = Settings(gemini_api_key=SecretStr(secret))
    monkeypatch.setattr(verify_gemini, "get_settings", lambda: settings)

    def quota_error(self, **kwargs):
        raise verify_gemini.GeminiProviderError("rate_limited", "Gemini quota or rate limit was reached")

    monkeypatch.setattr(GeminiTextAdapter, "generate_json", quota_error)

    assert verify_gemini.main() == 1

    captured = capsys.readouterr()
    assert "Gemini quota or rate limit was reached" in captured.err
    assert secret not in captured.out + captured.err


def test_verify_gemini_fails_before_network_call_when_unconfigured(monkeypatch, capsys):
    monkeypatch.setattr(verify_gemini, "get_settings", lambda: Settings())

    def unexpected_network_call(self, **kwargs):
        raise AssertionError("The adapter must not run without a configured key")

    monkeypatch.setattr(GeminiTextAdapter, "generate_json", unexpected_network_call)

    assert verify_gemini.main() == 1

    captured = capsys.readouterr()
    assert "OMNISIGHT_GEMINI_API_KEY" in captured.err
    assert "api_key" not in captured.out.lower()


def test_verify_gemini_script_runs_directly_without_backend_pythonpath(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_gemini.py"

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "OMNISIGHT_GEMINI_API_KEY" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
