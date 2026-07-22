"""Background continuous auto-registration controller."""
from __future__ import annotations

import application.tasks as tk
import application.tasks_query as tq
from application.auto_register import AutoRegisterController
from core.config_store import config_store


def test_start_persists_params_and_status():
    ctrl = AutoRegisterController()
    ctrl._ensure_thread = lambda: None  # don't spawn the loop thread in this test
    status = ctrl.start(
        {"batch": 8, "concurrency": 4, "target": 20, "interval": 30, "executor_type": "protocol", "auto_import": True}
    )
    assert status["enabled"] is True
    assert status["batch"] == 8 and status["concurrency"] == 4
    assert status["target"] == 20 and status["interval"] == 30
    assert status["executor_type"] == "protocol" and status["auto_import"] is True
    assert config_store.get("auto_register_done") == "0"

    ctrl.stop()
    assert config_store.get("auto_register_enabled") == ""


def test_create_round_payload_uses_pool_and_flags(monkeypatch):
    captured = {}
    monkeypatch.setattr(tk, "create_register_task", lambda payload: captured.update(payload=payload) or {"id": "t1"})

    ctrl = AutoRegisterController()
    task_id = ctrl._create_round(3, 2, "protocol", True)

    assert task_id == "t1"
    p = captured["payload"]
    assert p["count"] == 3 and p["concurrency"] == 2
    assert p["executor_type"] == "protocol"
    assert p["proxy"] is None  # empty -> proxy pool
    assert p["extra"]["auto_import_sub2api"] is True
    assert p["extra"]["identity_provider"] == "mailbox"


def test_loop_stops_at_target(monkeypatch):
    # Each round registers 1 account successfully; target 2 -> two rounds then stop.
    monkeypatch.setattr(tk, "create_register_task", lambda payload: {"id": "tX"})
    monkeypatch.setattr(tq.TasksQueryService, "get_task", lambda self, tid: {"status": "succeeded", "success": 1})

    ctrl = AutoRegisterController()
    config_store.set_many(
        {
            "auto_register_enabled": "1",
            "auto_register_batch": "1",
            "auto_register_concurrency": "1",
            "auto_register_target": "2",
            "auto_register_interval": "0",
            "auto_register_done": "0",
        }
    )
    ctrl._loop()

    assert config_store.get("auto_register_done") == "2"
    assert config_store.get("auto_register_enabled") == ""  # auto-disabled at target
    assert ctrl.status()["running"] is False


def test_stop_cancels_current_task(monkeypatch):
    cancelled = []
    monkeypatch.setattr(tk, "request_cancel", lambda tid: cancelled.append(tid))

    ctrl = AutoRegisterController()
    ctrl._ensure_thread = lambda: None
    ctrl._current_task_id = "task-live"
    ctrl.start({"batch": 1})
    ctrl.stop()

    assert cancelled == ["task-live"]
    assert config_store.get("auto_register_enabled") == ""


def test_wait_task_cancels_on_timeout(monkeypatch):
    monkeypatch.setattr(
        tq.TasksQueryService, "get_task", lambda self, tid: {"status": "running", "success": 0}
    )
    cancelled = []
    monkeypatch.setattr(tk, "request_cancel", lambda tid: cancelled.append(tid))

    config_store.set("auto_register_enabled", "1")
    ctrl = AutoRegisterController()
    ctrl._sleep = lambda s: None  # no real waiting

    result = ctrl._wait_task("stuck-task", timeout=0.001)

    assert result == 0
    assert cancelled == ["stuck-task"]
