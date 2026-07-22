"""Configurable OTP wait timeout + faster AnyMail polling default."""
from __future__ import annotations

from core.config_store import config_store
from core.anymail_mailbox import AnyMailPool
from platforms.chatgpt.plugin import _resolve_otp_timeout


def test_otp_timeout_default_is_90():
    assert _resolve_otp_timeout() == 90


def test_otp_timeout_override_and_clamp():
    config_store.set("otp_wait_timeout", "60")
    assert _resolve_otp_timeout() == 60

    config_store.set("otp_wait_timeout", "5")  # below floor
    assert _resolve_otp_timeout() == 30

    config_store.set("otp_wait_timeout", "9999")  # above ceiling
    assert _resolve_otp_timeout() == 600

    config_store.set("otp_wait_timeout", "")  # empty -> default
    assert _resolve_otp_timeout() == 90


def test_anymail_poll_interval_default_is_1_5():
    pool = AnyMailPool(base_url="https://mail.example.com", api_key="k")
    assert pool.poll_interval == 1.5
