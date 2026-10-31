"""每日首次打开首页时尝试一次外接硬盘备份（E 工作包）。

约束：
- 只在首页 "/" 的 GET 请求上触发；后台线程执行，不阻塞页面。
- 每天最多一次（含失败）；是否已尝试记在备份配置 JSON 的 auto_attempt 里。
- 自动备份的任何异常都必须吞掉，不能影响页面。
- manage.py test / pytest 运行期间不触发，避免测试误写真实目标盘。
"""
from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

from .services import backup as backup_service

logger = logging.getLogger(__name__)

_AUTO_LOCK = threading.Lock()


def _is_test_runner() -> bool:
    """判断当前进程是否 Django 测试/pytest；测试期间绝不自动备份。"""
    try:
        program = Path(sys.argv[0]).name.lower() if sys.argv else ""
        if program.startswith(("pytest", "py.test")):
            return True
        if len(sys.argv) > 1 and str(sys.argv[1]).lower() in {"test", "testserver"}:
            return True
        return "django.test" in sys.modules
    except Exception:  # pragma: no cover
        return False


def should_attempt_daily(cfg=None, *, now=None, config_path=None):
    """今天是否该自动尝试备份；返回 (bool, 原因)。"""
    cfg = cfg if cfg is not None else backup_service.load_config(config_path)
    if not cfg.get("auto_daily", True):
        return False, "自动备份已关闭"
    target = str(cfg.get("target_dir") or "").strip()
    if not target:
        return False, "未配置备份目标，跳过自动备份"
    if not cfg.get("confirmed_external"):
        return False, "尚未确认这是另一块硬盘，跳过自动备份"
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    attempt = cfg.get("auto_attempt") or {}
    if str(attempt.get("date") or "") == today:
        return False, f"今天（{today}）已经尝试过备份，不重复重试"
    return True, ""


def _record_attempt(today: str, result: dict, config_path=None) -> None:
    try:
        cfg = backup_service.load_config(config_path)
        attempt = dict(cfg.get("auto_attempt") or {})
        attempt.update({
            "date": today,
            "result": result.get("result") or ("success" if result.get("ok") else "failed"),
        })
        cfg["auto_attempt"] = attempt
        backup_service.save_config(cfg, config_path)
    except OSError as exc:
        logger.warning("无法记录自动备份尝试状态：%s", exc)


def _attempt(runner, today: str, config_path=None) -> dict:
    try:
        result = runner()
    except Exception as exc:  # noqa: BLE001 - 后台任务绝不能把异常抛到线程外
        logger.exception("自动备份出现未预期异常")
        result = {"ok": False, "result": "failed", "error": f"自动备份异常：{exc}"}
    _record_attempt(today, result, config_path)
    return result


def maybe_run_daily_backup(background=True, *, now=None, runner=None, config_path=None) -> dict:
    """每日一次备份入口。测试传 background=False 同步执行并注入 runner。"""
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    cfg = backup_service.load_config(config_path)
    ok, reason = should_attempt_daily(cfg, now=now, config_path=config_path)
    if not ok:
        return {"attempted": False, "reason": reason, "date": today}
    if not _AUTO_LOCK.acquire(blocking=False):
        return {"attempted": False, "reason": "已有备份任务在进行", "date": today}
    try:
        cfg = backup_service.load_config(config_path)
        ok, reason = should_attempt_daily(cfg, now=now, config_path=config_path)
        if not ok:
            return {"attempted": False, "reason": reason, "date": today}
        attempt = dict(cfg.get("auto_attempt") or {})
        attempt.update({"date": today, "result": "running"})
        cfg["auto_attempt"] = attempt
        backup_service.save_config(cfg, config_path)
    except OSError as exc:
        return {"attempted": False, "reason": f"无法写入备份配置：{exc}", "date": today}
    finally:
        _AUTO_LOCK.release()

    runner = runner or backup_service.run_backup
    if not background:
        result = _attempt(runner, today, config_path)
        return {"attempted": True, "background": False, "date": today, "result": result}

    thread = threading.Thread(
        target=_attempt,
        args=(runner, today, config_path),
        name="br-daily-backup",
        daemon=True,
    )
    thread.start()
    return {"attempted": True, "background": True, "date": today, "thread": thread.name}


class BackupDailyMiddleware:
    """首页首次请求时触发每日备份尝试（失败不影响页面）。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            if self._should_trigger(request):
                maybe_run_daily_backup(background=True)
        except Exception:  # noqa: BLE001 - 备份永远不能拖垮页面
            logger.exception("自动备份触发失败（已忽略，不影响页面）")
        return self.get_response(request)

    @staticmethod
    def _should_trigger(request) -> bool:
        if _is_test_runner():
            return False
        return request.method == "GET" and request.path == "/"
