from __future__ import annotations

import json

import pytest

from core.anymail_mailbox import AnyMailPool


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
    """Scriptable session: queue responses per (method, path-substring)."""

    def __init__(self):
        self.get_queue: list[object] = []
        self.post_queue: list[object] = []
        self.calls: list[tuple[str, str, dict]] = []

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
        return FakeResponse({"ok": True})


def _pool(session, **overrides):
    kwargs = dict(
        base_url="https://mail.example.com",
        api_key="ak_test",
        domain="mail.example.com",
        poll_interval=0,
        session=session,
    )
    kwargs.update(overrides)
    return AnyMailPool(**kwargs)


def test_from_config_requires_api_key():
    with pytest.raises(ValueError):
        AnyMailPool.from_config({"anymail_base_url": "https://mail.example.com"})


def test_get_email_creates_random_mailbox_on_fixed_domain():
    session = FakeSession()
    session.post_queue = [{"ok": True, "account": {"id": "acc1", "email": "u_abc@mail.example.com"}}]
    account = _pool(session).get_email()

    assert account.email == "u_abc@mail.example.com"
    assert account.account_id == "acc1"
    assert account.extra["anymail_since"]  # captured before creation
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "https://mail.example.com/api/accounts")
    assert kwargs["json"]["email"].endswith("@mail.example.com")


def test_get_email_auto_pulls_domain_when_unset():
    session = FakeSession()
    session.get_queue = [{"domains": [{"name": "auto.example.com"}]}]
    session.post_queue = [{"account": {"id": "a", "email": "u_x@auto.example.com"}}]
    account = _pool(session, domain="").get_email()

    assert account.email.endswith("@auto.example.com")
    assert session.calls[0][1].endswith("/api/domains")


def test_get_email_retries_on_409_conflict():
    session = FakeSession()
    session.post_queue = [
        FakeResponse({"error": "account already exists"}, status_code=409),
        {"account": {"id": "a2", "email": "u_ok@mail.example.com"}},
    ]
    account = _pool(session).get_email()

    assert account.account_id == "a2"
    assert sum(1 for c in session.calls if c[0] == "POST") == 2


def test_wait_for_code_returns_server_extracted_code():
    session = FakeSession()
    session.post_queue = [{"account": {"id": "a", "email": "u_a@mail.example.com"}}]
    session.get_queue = [
        {"emails": []},
        {"emails": [{"id": "m1", "code": "482615", "text_body": "code 482615"}]},
    ]
    pool = _pool(session)
    account = pool.get_email()

    assert pool.wait_for_code(account, timeout=1) == "482615"
    latest_calls = [c for c in session.calls if "emails/latest" in c[1]]
    assert latest_calls[0][2]["params"]["to"] == "u_a@mail.example.com"
    assert latest_calls[0][2]["params"]["code_regex"] == r"\d{6}"


def test_wait_for_code_skips_baseline_before_ids():
    session = FakeSession()
    session.post_queue = [{"account": {"id": "a", "email": "u_a@mail.example.com"}}]
    # Same email present at baseline and during polling — must be ignored.
    session.get_queue = [{"emails": [{"id": "old", "code": "111111"}]}]
    pool = _pool(session)
    account = pool.get_email()
    before = pool.get_current_ids(account)

    with pytest.raises(TimeoutError):
        pool.wait_for_code(account, timeout=1, before_ids=before)


def test_delete_after_use_recycles_mailbox():
    session = FakeSession()
    session.post_queue = [{"account": {"id": "acc9", "email": "u_a@mail.example.com"}}]
    session.get_queue = [{"emails": [{"id": "m1", "code": "999999"}]}]
    pool = _pool(session, delete_after_use=True)
    account = pool.get_email()

    assert pool.wait_for_code(account, timeout=1) == "999999"
    assert any(c[0] == "DELETE" and c[1].endswith("/api/accounts/acc9") for c in session.calls)


def test_wait_for_link_extracts_first_url():
    session = FakeSession()
    session.post_queue = [{"account": {"id": "a", "email": "u_a@mail.example.com"}}]
    session.get_queue = [
        {"emails": [{"id": "m1", "html_body": "click https://verify.example.com/x?t=1 now"}]},
    ]
    pool = _pool(session)
    account = pool.get_email()

    assert pool.wait_for_link(account, timeout=1) == "https://verify.example.com/x?t=1"
