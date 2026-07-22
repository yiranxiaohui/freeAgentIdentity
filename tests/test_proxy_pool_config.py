"""Proxy pool text: parsing + /config persistence."""
from __future__ import annotations

from application.tasks import _parse_proxy_pool_text
from infrastructure.config_repository import ConfigRepository


def test_parse_proxy_pool_text_skips_blank_and_comments():
    text = "socks5h://u:p@a:1\n\n  # a comment\n// another\n  http://b:2  \n"
    assert _parse_proxy_pool_text(text) == ["socks5h://u:p@a:1", "http://b:2"]


def test_parse_proxy_pool_text_empty():
    assert _parse_proxy_pool_text("") == []
    assert _parse_proxy_pool_text("   \n# only comment\n") == []


def test_round_robin_assignment_by_index():
    pool = _parse_proxy_pool_text("p0\np1\np2")
    assigned = [pool[i % len(pool)] for i in range(5)]
    assert assigned == ["p0", "p1", "p2", "p0", "p1"]


def test_proxy_pool_text_persists_via_config():
    repo = ConfigRepository()
    repo.update_flat({"proxy_pool_text": "socks5h://u:p@a:1\nhttp://b:2"})
    assert repo.get_flat()["proxy_pool_text"] == "socks5h://u:p@a:1\nhttp://b:2"
