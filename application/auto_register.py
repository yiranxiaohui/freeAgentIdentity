"""后台持续自动注册控制器。

开启后按保存的配置不断创建注册任务：上一批跑完（或到间隔）就自动起下一批，
直到达到目标数（可选）或被手动停止。开关状态持久化，进程重启后自动恢复继续。
复用普通注册流程（默认邮箱 provider、代理池、执行方式、边注册边导入 Sub2API）。
"""
from __future__ import annotations

import logging
import threading
import time

from core.config_store import config_store

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on", "y"}
_TERMINAL = {"succeeded", "failed", "cancelled"}


def _int(value, default=0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


class AutoRegisterController:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._running = False
        self._current_task_id = ""

    def _cfg(self) -> dict:
        return {
            "enabled": _truthy(config_store.get("auto_register_enabled", "")),
            "batch": max(_int(config_store.get("auto_register_batch", 5), 5), 1),
            "concurrency": min(max(_int(config_store.get("auto_register_concurrency", 3), 3), 1), 5),
            "target": max(_int(config_store.get("auto_register_target", 0), 0), 0),
            "interval": max(_int(config_store.get("auto_register_interval", 10), 10), 0),
            "done": max(_int(config_store.get("auto_register_done", 0), 0), 0),
            "executor_type": str(config_store.get("auto_register_executor", "") or "").strip(),
            "auto_import": _truthy(config_store.get("auto_register_auto_import", "")),
        }

    def status(self) -> dict:
        cfg = self._cfg()
        return {
            **cfg,
            "running": self._running,
            "current_task_id": self._current_task_id,
        }

    # ── control ──────────────────────────────────────────────────────
    def start(self, params: dict) -> dict:
        config_store.set_many(
            {
                "auto_register_batch": str(max(_int(params.get("batch"), 5), 1)),
                "auto_register_concurrency": str(min(max(_int(params.get("concurrency"), 3), 1), 5)),
                "auto_register_target": str(max(_int(params.get("target"), 0), 0)),
                "auto_register_interval": str(max(_int(params.get("interval"), 10), 0)),
                "auto_register_executor": str(params.get("executor_type") or "").strip(),
                "auto_register_auto_import": "1" if params.get("auto_import") else "",
                "auto_register_done": "0",
                "auto_register_enabled": "1",
            }
        )
        self._ensure_thread()
        return self.status()

    def stop(self) -> dict:
        config_store.set("auto_register_enabled", "")
        # 一并取消正在跑的那批，别让它继续占用平台槽位。
        self._cancel(self._current_task_id)
        self._wake.set()
        return self.status()

    def _cancel(self, task_id: str) -> None:
        if not task_id:
            return
        try:
            from application.tasks import request_cancel

            request_cancel(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("取消后台注册任务失败 %s: %s", task_id, exc)

    def resume_if_enabled(self) -> None:
        if self._enabled():
            self._ensure_thread()

    # ── internals ────────────────────────────────────────────────────
    def _enabled(self) -> bool:
        return _truthy(config_store.get("auto_register_enabled", ""))

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            self._wake.set()
            return
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="auto-register")
        self._thread.start()

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._wake.wait(timeout=seconds)
        self._wake.clear()

    def _loop(self) -> None:
        self._running = True
        try:
            while self._enabled():
                cfg = self._cfg()
                target, done, batch = cfg["target"], cfg["done"], cfg["batch"]
                if target > 0 and done >= target:
                    break
                n = batch if target <= 0 else min(batch, target - done)
                if n <= 0:
                    break
                try:
                    task_id = self._create_round(n, cfg["concurrency"], cfg["executor_type"], cfg["auto_import"])
                    # 批次超时保护：每个账号给 120s，最少 5 分钟。超时则取消该批，
                    # 释放平台槽位、继续下一批，避免一个卡死的批次把队列堵死。
                    timeout = max(300, n * 120)
                    success = self._wait_task(task_id, timeout)
                    config_store.set("auto_register_done", str(done + max(success, 0)))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("后台自动注册一轮失败: %s", exc)
                    self._sleep(min(cfg["interval"] or 10, 30))
                    continue
                finally:
                    self._current_task_id = ""
                if not self._enabled():
                    break
                self._sleep(cfg["interval"])
        finally:
            # 到达目标数：自动关闭开关。
            if self._enabled() and self._cfg()["target"] > 0 and self._cfg()["done"] >= self._cfg()["target"]:
                config_store.set("auto_register_enabled", "")
            self._running = False
            self._current_task_id = ""

    def _create_round(self, count: int, concurrency: int, executor_type: str, auto_import: bool) -> str:
        from application.tasks import create_register_task

        payload = {
            "count": count,
            "concurrency": concurrency,
            "executor_type": executor_type or str(config_store.get("default_executor", "") or "protocol"),
            "captcha_solver": "auto",
            "proxy": None,  # 留空 → 走代理池
            "extra": {
                "identity_provider": "mailbox",
                "auto_download_agent_identity": False,
                "auto_import_sub2api": bool(auto_import),
            },
        }
        task = create_register_task(payload)
        task_id = str(task.get("id") or task.get("task_id") or "")
        self._current_task_id = task_id
        return task_id

    def _wait_task(self, task_id: str, timeout: float = 0) -> int:
        from application.tasks_query import TasksQueryService

        query = TasksQueryService()
        deadline = time.monotonic() + timeout if timeout > 0 else None
        while self._enabled():
            info = query.get_task(task_id)
            if not info:
                return 0
            if str(info.get("status") or "") in _TERMINAL:
                return _int(info.get("success"), 0)
            if deadline is not None and time.monotonic() > deadline:
                logger.warning("后台自动注册批次超时(%ss)，取消任务 %s", int(timeout), task_id)
                self._cancel(task_id)
                # 给它一点时间转入终态，然后按已成功数计入。
                self._sleep(3)
                return _int((query.get_task(task_id) or {}).get("success"), 0)
            self._sleep(2)
        return 0


auto_register_controller = AutoRegisterController()
