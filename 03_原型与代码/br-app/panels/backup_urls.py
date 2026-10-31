"""外接硬盘备份页面路由（独立文件，避免与 panels/urls.py 并行修改冲突）。"""
from django.urls import path

from . import backup_views

urlpatterns = [
    path("settings/backup/", backup_views.backup_settings, name="backup_settings"),
]
