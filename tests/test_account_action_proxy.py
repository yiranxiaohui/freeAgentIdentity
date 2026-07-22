"""Refresh-quota and query-state must use the account's own bound proxy."""
from __future__ import annotations

import application.tasks as tk
import infrastructure.platform_runtime as pr
from domain.accounts import AccountExportSelection, AccountImportLine, AccountUpdateCommand
from domain.actions import ActionExecutionCommand
from infrastructure.accounts_repository import AccountsRepository


def _make_account(proxy: str) -> int:
    repo = AccountsRepository()
    repo.import_lines("chatgpt", [AccountImportLine(email="q@x.com", password="pw")])
    rec = next(
        r for r in repo.select_for_export(AccountExportSelection(platform="chatgpt")) if r.email == "q@x.com"
    )
    if proxy:
        repo.update(rec.id, AccountUpdateCommand(proxy=proxy))
    return rec.id


class FakePlugin:
    seen: dict = {}

    def __init__(self, config=None):
        FakePlugin.seen = {"proxy": getattr(config, "proxy", None)}

    def check_valid(self, account):
        return True

    def get_last_check_overview(self):
        return {}

    def set_logger(self, fn):
        pass

    def execute_action(self, action_id, account, params):
        return {"ok": True, "data": {}}


def test_refresh_quota_uses_account_proxy(monkeypatch):
    monkeypatch.setattr(tk, "get", lambda platform: FakePlugin)
    account_id = _make_account("socks5h://u:p@host:9000")

    tk._run_single_account_check(account_id)

    assert FakePlugin.seen["proxy"] == "socks5h://u:p@host:9000"


def test_refresh_quota_without_proxy_is_none(monkeypatch):
    monkeypatch.setattr(tk, "get", lambda platform: FakePlugin)
    account_id = _make_account("")

    tk._run_single_account_check(account_id)

    assert FakePlugin.seen["proxy"] is None


def test_query_state_action_uses_account_proxy(monkeypatch):
    monkeypatch.setattr(pr, "load_all", lambda: None)  # avoid importing browser deps
    monkeypatch.setattr(pr, "get", lambda platform: FakePlugin)
    account_id = _make_account("http://host:1234")

    pr.PlatformRuntime().execute_action(
        ActionExecutionCommand(platform="chatgpt", account_id=account_id, action_id="get_account_state")
    )

    assert FakePlugin.seen["proxy"] == "http://host:1234"
