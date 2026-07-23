from __future__ import annotations

import json

import pytest

from core.cftemp_mailbox import CloudflareTempEmailPool


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self.payload, str):
            raise ValueError("not json")
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.get_queue: list = []
        self.post_queue: list = []
        self.calls: list = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        payload = self.get_queue.pop(0) if len(self.get_queue) > 1 else self.get_queue[0]
        return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        payload = self.post_queue.pop(0) if len(self.post_queue) > 1 else self.post_queue[0]
        return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        return FakeResponse({"success": True})


def _pool(session, **overrides):
    kwargs = dict(base_url="https://mail.example.com", domain="mail.example.com", poll_interval=0, session=session)
    kwargs.update(overrides)
    return CloudflareTempEmailPool(**kwargs)


def test_from_config_requires_base_url():
    with pytest.raises(ValueError):
        CloudflareTempEmailPool.from_config({"cftemp_base_url": "not-a-url"})


def test_get_email_creates_address_and_keeps_jwt():
    session = FakeSession()
    session.post_queue = [{"jwt": "jwt-abc", "address": "u_x@mail.example.com", "address_id": 3}]
    account = _pool(session).get_email()

    assert account.email == "u_x@mail.example.com"
    assert account.extra["cftemp_jwt"] == "jwt-abc"
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "https://mail.example.com/api/new_address")
    assert kwargs["json"]["domain"] == "mail.example.com"


def test_wait_for_code_extracts_client_side_with_bearer():
    session = FakeSession()
    session.post_queue = [{"jwt": "jwt-1", "address": "u_a@mail.example.com"}]
    session.get_queue = [
        {"results": []},
        {"results": [{"id": "m1", "text": "Your ChatGPT verification code is 482615"}]},
    ]
    pool = _pool(session)
    account = pool.get_email()

    assert pool.wait_for_code(account, timeout=1) == "482615"
    latest = [c for c in session.calls if "parsed_mails" in c[1]]
    assert latest[0][2]["headers"]["Authorization"] == "Bearer jwt-1"


def test_wait_for_code_ignores_html_hex_color():
    session = FakeSession()
    session.post_queue = [{"jwt": "j", "address": "u_a@mail.example.com"}]
    session.get_queue = [
        {"results": [{
            "id": "m1",
            "text": "code 715028",
            "html": '<div style="background:#202123"><h1>715028</h1></div>',
        }]},
    ]
    pool = _pool(session)
    account = pool.get_email()
    assert pool.wait_for_code(account, timeout=1) == "715028"


def test_delete_after_use_recycles_address():
    session = FakeSession()
    session.post_queue = [{"jwt": "j2", "address": "u_a@mail.example.com"}]
    session.get_queue = [{"results": [{"id": "m1", "text": "code: 999999"}]}]
    pool = _pool(session, delete_after_use=True)
    account = pool.get_email()

    assert pool.wait_for_code(account, timeout=1) == "999999"
    assert any(c[0] == "DELETE" and c[1].endswith("/api/delete_address") for c in session.calls)


def test_wait_for_link_extracts_first_url():
    session = FakeSession()
    session.post_queue = [{"jwt": "j", "address": "u_a@mail.example.com"}]
    session.get_queue = [{"results": [{"id": "m1", "html": "go https://verify.example.com/x?t=1 now"}]}]
    pool = _pool(session)
    account = pool.get_email()
    assert pool.wait_for_link(account, timeout=1) == "https://verify.example.com/x?t=1"
