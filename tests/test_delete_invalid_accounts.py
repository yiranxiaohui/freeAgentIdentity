"""One-click delete invalid accounts (local + Sub2API remote)."""
from __future__ import annotations

import application.account_exports as ae
from application.accounts import AccountsService
from domain.accounts import AccountExportSelection, AccountImportLine, AccountUpdateCommand
from infrastructure.accounts_repository import AccountsRepository


def _seed():
    repo = AccountsRepository()
    repo.import_lines(
        "chatgpt",
        [AccountImportLine(email="bad@x.com", password="pw"), AccountImportLine(email="good@x.com", password="pw")],
    )
    recs = {r.email: r for r in repo.select_for_export(AccountExportSelection(platform="chatgpt"))}
    repo.update(recs["bad@x.com"].id, AccountUpdateCommand(lifecycle_status="invalid"))
    return repo, recs


def test_delete_invalid_removes_local_and_remote(monkeypatch):
    repo, recs = _seed()
    monkeypatch.setattr(ae, "_sub2api_config", lambda: ("https://s.example.com", "ak"))
    deleted_remote = []
    monkeypatch.setattr(
        ae, "delete_sub2api_account_by_email", lambda base, key, email: deleted_remote.append(email) or True
    )

    result = AccountsService().delete_invalid_accounts()

    assert result["total"] == 1
    assert result["local_deleted"] == 1
    assert result["remote_deleted"] == 1
    assert result["remote_enabled"] is True
    assert deleted_remote == ["bad@x.com"]

    # invalid gone, valid kept
    remaining = {r.email for r in repo.select_for_export(AccountExportSelection(platform="chatgpt"))}
    assert remaining == {"good@x.com"}


def test_delete_invalid_skips_remote_when_unconfigured(monkeypatch):
    repo, recs = _seed()
    monkeypatch.setattr(ae, "_sub2api_config", lambda: ("", ""))
    called = []
    monkeypatch.setattr(ae, "delete_sub2api_account_by_email", lambda *a, **k: called.append(1) or True)

    result = AccountsService().delete_invalid_accounts()

    assert result["total"] == 1 and result["local_deleted"] == 1
    assert result["remote_enabled"] is False
    assert result["remote_deleted"] == 0
    assert called == []  # remote never attempted
