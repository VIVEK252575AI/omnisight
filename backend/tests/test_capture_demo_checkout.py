from scripts.capture_demo_checkout import _safe_network_url


def test_safe_network_url_removes_credentials_query_and_fragment():
    assert _safe_network_url(
        "https://user:secret@shoplite.local/assets/app.js?token=private#debug"
    ) == "https://shoplite.local/assets/app.js"


def test_safe_network_url_omits_non_http_and_malformed_urls():
    assert _safe_network_url("data:text/plain,secret") is None
    assert _safe_network_url("https://shoplite.local:invalid/path") is None
