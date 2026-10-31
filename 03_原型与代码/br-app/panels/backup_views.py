"""外接硬盘备份设置页（E 工作包）：查看状态 / 保存配置 / 检查路径 / 立即备份。"""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render

from .services import backup as backup_service


def _posted_form(request) -> dict:
    return {
        "target_dir": (request.POST.get("target_dir") or "").strip().strip('"').strip("'"),
        "l1_workspace": (request.POST.get("l1_workspace") or "").strip().strip('"').strip("'"),
        "confirmed_external": request.POST.get("confirmed_external") == "on",
        "auto_daily": request.POST.get("auto_daily") == "on",
    }


def _save_form(request) -> dict:
    """把表单写进配置 JSON（先保存、再校验，失败原因用 messages 说明）。"""
    posted = _posted_form(request)
    cfg = backup_service.load_config()
    cfg["target_dir"] = posted["target_dir"]
    cfg["l1_workspace"] = posted["l1_workspace"]
    cfg["confirmed_external"] = posted["confirmed_external"]
    cfg["auto_daily"] = posted["auto_daily"]
    backup_service.save_config(cfg)
    return posted


def _form_values(status: dict) -> dict:
    return {
        "target_dir": status.get("target_dir") or "",
        "l1_workspace": status.get("l1_workspace") or "",
        "confirmed_external": status.get("confirmed_external") or False,
        "auto_daily": status.get("auto_daily", True),
        "config_path": status.get("config_path") or "",
    }


def _run_check(request):
    posted = _posted_form(request)
    cfg = {
        "target_dir": posted["target_dir"],
        "confirmed_external": posted["confirmed_external"],
        "l1_workspace": posted["l1_workspace"],
    }
    report = backup_service.validate_setup(
        cfg,
        target_dir=posted["target_dir"],
        l1_workspace=posted["l1_workspace"] or None,
    )
    for error in report.get("errors") or []:
        messages.error(request, f"路径检查失败：{error}")
    for warning in report.get("warnings") or []:
        messages.warning(request, warning)
    if report.get("ok"):
        messages.success(
            request,
            f"路径检查通过：目标可写，可用空间 {report.get('free_human') or '未知'}；"
            f"L1 工作区 {report.get('l1_workspace')}",
        )
    elif not report.get("errors"):
        messages.warning(request, "路径检查未通过，但没有返回具体原因")


def _run_backup(request):
    result = backup_service.run_backup()
    if result.get("ok"):
        messages.success(
            request,
            "备份完成：{dir}（{files} 个文件，{size}；数据库结构检查 {integrity}）".format(
                dir=result.get("backup_dir") or "",
                files=result.get("files") or 0,
                size=backup_service.human_size(result.get("bytes")),
                integrity=result.get("db_integrity") or "未知",
            ),
        )
    elif result.get("result") == "not_configured":
        messages.warning(request, result.get("error") or "未配置备份目标，当前没有新备份")
    else:
        messages.error(
            request,
            f"备份失败：{result.get('error') or '未知原因'}。"
            "旧的备份没有被删除，当前没有新备份。",
        )
    for warning in result.get("warnings") or []:
        messages.warning(request, warning)


def backup_settings(request):
    if request.method == "POST":
        action = (request.POST.get("action") or "save").strip()
        try:
            if action == "check":
                _run_check(request)
            elif action == "backup":
                posted = _save_form(request)
                if not posted["target_dir"]:
                    messages.warning(request, "未配置备份目标，当前没有新备份")
                else:
                    _run_backup(request)
            else:
                posted = _save_form(request)
                if not posted["target_dir"]:
                    messages.success(request, "已保存：当前没有配置备份目标，不会执行真实备份")
                else:
                    messages.success(request, "备份配置已保存；可点“检查路径”核对目标，或“立即备份”")
        except OSError as exc:
            messages.error(request, f"保存备份配置失败：{exc}")
        return redirect("backup_settings")

    status = backup_service.get_status()
    return render(
        request,
        "panels/backup_settings.html",
        {"status": status, "form": _form_values(status), "last": status.get("last") or {},
         "last_success": status.get("last_success") or {}},
    )
