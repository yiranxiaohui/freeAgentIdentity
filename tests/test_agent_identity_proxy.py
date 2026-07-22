"""Agent Identity minting must go through the account's own proxy.

Regression: certificate registration hit auth.openai.com from the server IP and
got HTTP 403 unsupported_country_region_territory. The account's bound proxy
must be applied (via the thread-local) around register_identity.
"""
from __future__ import annotations

from types import SimpleNamespace

import application.account_exports as ae
import platforms.chatgpt.from_credentials as fc


def test_account_proxy_is_active_during_register_identity(monkeypatch):
    # Bypass token parsing: pretend the account has usable Agent Identity claims.
    monkeypatch.setattr(ae, "_chatgpt_export_payload", lambda item: {"access_token": "at", "id_token": "it"})
    monkeypatch.setattr(
        ae,
        "_chatgpt_auth_info",
        lambda token: {"chatgpt_account_id": "acc", "chatgpt_user_id": "usr"},
    )

    seen_proxy = {}

    def fake_register_identity(tokens, *, auth_api_base_url, codex_base_url):
        # Capture the proxy visible to the HTTP layer at call time.
        seen_proxy["value"] = fc.get_thread_proxy_url()
        return {"certificate": "x"}

    monkeypatch.setattr(fc, "register_identity", fake_register_identity)
    monkeypatch.setattr(fc, "certificate_to_sub2api_export", lambda cert: {"accounts": [{"name": "a@x.com"}]})

    item = SimpleNamespace(email="a@x.com", proxy="socks5h://u:p@jpn-1.example.xyz:12121")
    result = ae._make_agent_identity_sub2api_json(item)

    assert result == {"accounts": [{"name": "a@x.com"}]}
    assert seen_proxy["value"] == "socks5h://u:p@jpn-1.example.xyz:12121"
    # Thread-local restored afterwards (no leakage to other work on this thread).
    assert fc.get_thread_proxy_url() is None


def test_no_account_proxy_leaves_thread_proxy_untouched(monkeypatch):
    monkeypatch.setattr(ae, "_chatgpt_export_payload", lambda item: {"access_token": "at", "id_token": "it"})
    monkeypatch.setattr(
        ae,
        "_chatgpt_auth_info",
        lambda token: {"chatgpt_account_id": "acc", "chatgpt_user_id": "usr"},
    )

    seen_proxy = {}
    monkeypatch.setattr(
        fc,
        "register_identity",
        lambda tokens, *, auth_api_base_url, codex_base_url: seen_proxy.update(value=fc.get_thread_proxy_url()) or {"c": 1},
    )
    monkeypatch.setattr(fc, "certificate_to_sub2api_export", lambda cert: {"accounts": []})

    item = SimpleNamespace(email="a@x.com", proxy="")
    ae._make_agent_identity_sub2api_json(item)

    assert seen_proxy["value"] is None
