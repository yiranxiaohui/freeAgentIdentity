"""Protocol registration must honour non-Outlook mailbox providers.

Regression: the /tasks/register endpoint used to force mail_provider=local_ms_pool
for protocol mode and reject anything without an Outlook pool, so AnyMail / API
mailbox could never be used for protocol registration.

Calls the endpoint function directly (not via TestClient) to avoid importing the
browser-automation platforms, which need optional heavyweight deps.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import api.task_commands as task_commands
from api.task_commands import RegisterTaskRequest, create_register_task


def test_protocol_with_non_outlook_provider_skips_outlook_pool(monkeypatch):
    captured = {}

    def fake_create(payload):
        captured["payload"] = payload
        return {"task_id": "t-123"}

    monkeypatch.setattr(task_commands.command_service, "create_register_task", fake_create)

    result = create_register_task(
        RegisterTaskRequest(
            count=1,
            executor_type="protocol",
            extra={"mail_provider": "anymail"},
        )
    )

    assert result == {"task_id": "t-123"}
    assert captured["payload"]["extra"]["mail_provider"] == "anymail"


def test_protocol_with_outlook_provider_still_requires_pool(monkeypatch):
    monkeypatch.setattr(
        task_commands.command_service,
        "create_register_task",
        lambda payload: {"task_id": "should-not-reach"},
    )

    with pytest.raises(HTTPException) as excinfo:
        create_register_task(
            RegisterTaskRequest(
                count=1,
                executor_type="protocol",
                extra={"mail_provider": "local_ms_pool"},
            )
        )

    assert excinfo.value.status_code == 400
    assert "Outlook 账号池" in str(excinfo.value.detail)
