"""Per-account proxy binding must persist through update and read back."""
from __future__ import annotations

from domain.accounts import AccountExportSelection, AccountImportLine, AccountUpdateCommand
from infrastructure.accounts_repository import AccountsRepository


def _make_account(repo: AccountsRepository, email: str):
    repo.import_lines("chatgpt", [AccountImportLine(email=email, password="pw")])
    records = repo.select_for_export(AccountExportSelection(platform="chatgpt"))
    return next(r for r in records if r.email == email)


def test_proxy_defaults_empty_and_survives_update_and_read():
    repo = AccountsRepository()
    acc = _make_account(repo, "proxy-user@example.com")
    assert acc.proxy == ""

    updated = repo.update(acc.id, AccountUpdateCommand(proxy="socks5h://u:p@host:9000"))
    assert updated is not None
    assert updated.proxy == "socks5h://u:p@host:9000"

    reread = repo.get(acc.id)
    assert reread is not None
    assert reread.proxy == "socks5h://u:p@host:9000"


def test_proxy_can_be_cleared():
    repo = AccountsRepository()
    acc = _make_account(repo, "clear-proxy@example.com")
    repo.update(acc.id, AccountUpdateCommand(proxy="http://host:1"))
    cleared = repo.update(acc.id, AccountUpdateCommand(proxy=""))
    assert cleared is not None
    assert cleared.proxy == ""
