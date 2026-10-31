"""预览版 Django 配置：Windows 单机、本地 SQLite、无外部依赖。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# 非生产占位密钥：公开仓库只用于本地演示；正式部署用环境变量 BR_SECRET_KEY 覆盖。
SECRET_KEY = os.environ.get("BR_SECRET_KEY") or "demo-only-not-a-secret-change-me"
DEBUG = True
# 预览版默认不校验；正式给 Agent 用时设置环境变量 BR_API_TOKEN，
# 写接口需带 X-API-Key 头。
API_TOKEN = os.environ.get("BR_API_TOKEN", "")
# L2 审核台地址；只用于前端“去 L2 核对”链接，不改变后端 API 契约。
REVIEW_WEB_URL = os.environ.get("BR_REVIEW_WEB_URL", "http://127.0.0.1:8770/")
# 外接硬盘备份（E 工作包）：配置文件默认 data/backup_config.json；
# 测试用 override_settings 覆盖，或用环境变量指向临时配置。
BACKUP_CONFIG_PATH = os.environ.get("BR_BACKUP_CONFIG", str(DATA_DIR / "backup_config.json"))
# L1 工作区（证据来源）；留空时读配置 JSON 里的 l1_workspace，再退回代码默认值。
BACKUP_L1_WORKSPACE = os.environ.get("BR_BACKUP_L1_WORKSPACE", "")
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "panels",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # 每天首次打开首页时尝试一次外接硬盘备份（后台线程，失败不影响页面；测试期间不触发）。
    "panels.backup_middleware.BackupDailyMiddleware",
]

ROOT_URLCONF = "brpreview.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "panels.context_processors.nav_counts",
            ],
        },
    },
]

WSGI_APPLICATION = "brpreview.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "preview.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
