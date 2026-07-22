"""Import-as-you-go: each registered account triggers a single-account push."""
from __future__ import annotations

from types import SimpleNamespace

import application.tasks as tk
import application.account_exports as ae


class FakeLogger:
    def __init__(self):
        self.messages = []

    def log(self, msg, level="info", **kwargs):
        self.messages.append((level, msg))


def test_truthy_flag():
    assert tk._truthy_flag("true") and tk._truthy_flag("1") and tk._truthy_flag("on")
    assert not tk._truthy_flag("") and not tk._truthy_flag("false") and not tk._truthy_flag(None)


def test_auto_import_pushes_single_account_with_proxy(monkeypatch):
    captured = {}

    def fake_push(self, selection, proxy=""):
        captured["ids"] = list(selection.ids)
        captured["proxy"] = proxy
        return {"pushed": 1, "group_bound": 2}

    monkeypatch.setattr(ae.AccountExportsService, "push_agent_identity_to_sub2api", fake_push)

    logger = FakeLogger()
    tk._auto_import_account_to_sub2api(42, "a@x.com", "socks5h://h:1", logger)

    assert captured["ids"] == [42]
    assert captured["proxy"] == "socks5h://h:1"
    assert any("已导入 Sub2API: a@x.com" in m for _, m in logger.messages)
    assert any("绑定分组(2)" in m for _, m in logger.messages)


def test_auto_import_failure_is_logged_not_raised(monkeypatch):
    def boom(self, selection, proxy=""):
        raise RuntimeError("未配置 Sub2API")

    monkeypatch.setattr(ae.AccountExportsService, "push_agent_identity_to_sub2api", boom)

    logger = FakeLogger()
    # Must not raise — registration should not fail because of an import error.
    tk._auto_import_account_to_sub2api(1, "b@x.com", None, logger)

    assert any(level == "error" and "导入 Sub2API 失败" in msg for level, msg in logger.messages)
