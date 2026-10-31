from django.conf import settings

from .models import IngestionBatch
from .services import backup as backup_service


def _backup_status():
    """导航/首页用的备份状态；任何异常都不能拖垮页面。"""
    try:
        return backup_service.get_status()
    except Exception:
        return {
            "configured": False,
            "target_dir": "",
            "target_configured": False,
            "confirmed_external": False,
            "auto_daily": True,
            "l1_workspace": "",
            "config_path": "",
            "config_error": "",
            "last": {},
            "last_success": {},
            "free_bytes": None,
            "free_human": "",
            "warnings": [],
            "has_new_backup": False,
            "result": "unknown",
            "tone": "warn",
            "status_label": "状态未知",
            "message": "备份状态读取失败，当前没有新备份",
        }


def nav_counts(request):
    try:
        pending = IngestionBatch.objects.filter(status="pending").count()
    except Exception:
        pending = 0
    review_web_url = getattr(settings, "REVIEW_WEB_URL", "http://127.0.0.1:8770/")
    return {
        "pending_count": pending,
        "review_web_base": review_web_url,
        "review_web_url": review_web_url,
        "backup_status": _backup_status(),
    }
