"""Sub2API integration keys must survive the /config allow-list round-trip."""
from __future__ import annotations

from infrastructure.config_repository import ConfigRepository


def test_sub2api_keys_are_persisted_and_returned():
    repo = ConfigRepository()

    updated = repo.update_flat(
        {
            "sub2api_base_url": "https://sub2api.example.com",
            "sub2api_api_key": "ak_secret",
        }
    )

    assert set(updated) >= {"sub2api_base_url", "sub2api_api_key"}

    flat = repo.get_flat()
    assert flat["sub2api_base_url"] == "https://sub2api.example.com"
    assert flat["sub2api_api_key"] == "ak_secret"
