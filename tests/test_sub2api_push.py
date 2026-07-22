"""Auto-import to Sub2API: merge Agent Identity accounts and POST to sub2api."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import application.account_exports as ae
from application.account_exports import AccountExportsService
from domain.accounts import AccountExportSelection


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _selection():
    return AccountExportSelection(platform="chatgpt", ids=[1, 2], select_all=False)


def test_push_requires_configuration(monkeypatch):
    monkeypatch.setattr(ae, "_sub2api_config", lambda: ("", ""))
    with pytest.raises(ValueError):
        AccountExportsService().push_agent_identity_to_sub2api(_selection())


def test_push_merges_accounts_and_posts_with_api_key(monkeypatch):
    monkeypatch.setattr(ae, "_sub2api_config", lambda: ("https://sub2api.example.com", "ak_secret"))
    monkeypatch.setattr(
        AccountExportsService,
        "_load_chatgpt_items",
        lambda self, selection: [SimpleNamespace(email="a@x.com"), SimpleNamespace(email="b@x.com")],
    )
    monkeypatch.setattr(
        ae,
        "_make_agent_identity_sub2api_json",
        lambda item: {"type": "sub2api-data", "version": 1, "proxies": [], "accounts": [{"name": item.email}]},
    )

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse(200, {"data": {"account_created": 2, "account_failed": 0}})

    monkeypatch.setattr("requests.post", fake_post)

    result = AccountExportsService().push_agent_identity_to_sub2api(_selection())

    assert captured["url"] == "https://sub2api.example.com/api/v1/admin/accounts/data"
    assert captured["kwargs"]["headers"]["x-api-key"] == "ak_secret"
    body = captured["kwargs"]["json"]
    assert body["skip_default_group_bind"] is True
    assert body["data"]["type"] == "sub2api-data"
    assert [a["name"] for a in body["data"]["accounts"]] == ["a@x.com", "b@x.com"]
    assert result["pushed"] == 2
    assert result["sub2api_result"]["account_created"] == 2


def test_push_carries_registration_proxy(monkeypatch):
    monkeypatch.setattr(ae, "_sub2api_config", lambda: ("https://sub2api.example.com", "ak"))
    monkeypatch.setattr(
        AccountExportsService,
        "_load_chatgpt_items",
        lambda self, selection: [SimpleNamespace(email="a@x.com")],
    )
    monkeypatch.setattr(ae, "_make_agent_identity_sub2api_json", lambda item: {"accounts": [{"name": item.email}]})

    captured = {}

    def fake_post(url, **kwargs):
        captured["body"] = kwargs["json"]
        return FakeResponse(200, {"data": {"account_created": 1}})

    monkeypatch.setattr("requests.post", fake_post)

    AccountExportsService().push_agent_identity_to_sub2api(
        _selection(), proxy="socks5h://user:pass@jpn-1.example.xyz:12121"
    )

    data = captured["body"]["data"]
    assert len(data["proxies"]) == 1
    proxy = data["proxies"][0]
    assert proxy["protocol"] == "socks5h"
    assert proxy["host"] == "jpn-1.example.xyz"
    assert proxy["port"] == 12121
    assert proxy["username"] == "user" and proxy["password"] == "pass"
    expected_key = "socks5h|jpn-1.example.xyz|12121|user|pass"
    assert proxy["proxy_key"] == expected_key
    assert data["accounts"][0]["proxy_key"] == expected_key


def test_push_uses_each_accounts_own_proxy(monkeypatch):
    monkeypatch.setattr(ae, "_sub2api_config", lambda: ("https://s.example.com", "ak"))
    monkeypatch.setattr(
        AccountExportsService,
        "_load_chatgpt_items",
        lambda self, selection: [
            SimpleNamespace(email="a@x.com", proxy="socks5h://ua:pa@host-a:1111"),
            SimpleNamespace(email="b@x.com", proxy="http://host-b:2222"),
        ],
    )
    monkeypatch.setattr(ae, "_make_agent_identity_sub2api_json", lambda item: {"accounts": [{"name": item.email}]})

    captured = {}
    monkeypatch.setattr(
        "requests.post",
        lambda url, **kw: captured.update(body=kw["json"]) or FakeResponse(200, {"data": {}}),
    )

    # fallback proxy passed in, but each account's own proxy must win.
    AccountExportsService().push_agent_identity_to_sub2api(_selection(), proxy="http://fallback:9999")

    data = captured["body"]["data"]
    keys = {p["proxy_key"] for p in data["proxies"]}
    assert keys == {"socks5h|host-a|1111|ua|pa", "http|host-b|2222||"}
    by_name = {a["name"]: a["proxy_key"] for a in data["accounts"]}
    assert by_name["a@x.com"] == "socks5h|host-a|1111|ua|pa"
    assert by_name["b@x.com"] == "http|host-b|2222||"


def test_push_without_proxy_leaves_proxies_empty(monkeypatch):
    monkeypatch.setattr(ae, "_sub2api_config", lambda: ("https://s.example.com", "ak"))
    monkeypatch.setattr(
        AccountExportsService,
        "_load_chatgpt_items",
        lambda self, selection: [SimpleNamespace(email="a@x.com")],
    )
    monkeypatch.setattr(ae, "_make_agent_identity_sub2api_json", lambda item: {"accounts": [{"name": item.email}]})
    captured = {}
    monkeypatch.setattr("requests.post", lambda url, **kw: captured.update(body=kw["json"]) or FakeResponse(200, {"data": {}}))

    AccountExportsService().push_agent_identity_to_sub2api(_selection(), proxy="")

    assert captured["body"]["data"]["proxies"] == []
    assert "proxy_key" not in captured["body"]["data"]["accounts"][0]


def test_push_raises_on_sub2api_http_error(monkeypatch):
    monkeypatch.setattr(ae, "_sub2api_config", lambda: ("https://sub2api.example.com", "ak"))
    monkeypatch.setattr(
        AccountExportsService,
        "_load_chatgpt_items",
        lambda self, selection: [SimpleNamespace(email="a@x.com")],
    )
    monkeypatch.setattr(ae, "_make_agent_identity_sub2api_json", lambda item: {"accounts": [{"name": item.email}]})
    monkeypatch.setattr(
        "requests.post",
        lambda url, **kwargs: FakeResponse(401, {}, text="invalid api key"),
    )

    with pytest.raises(RuntimeError) as excinfo:
        AccountExportsService().push_agent_identity_to_sub2api(_selection())
    assert "401" in str(excinfo.value)
