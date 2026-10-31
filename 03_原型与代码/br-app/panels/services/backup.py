"""外接硬盘备份服务（对应三层修订契约第 5 节，E 工作包）。

设计原则（与契约一致）：

- 只复制，不移动、不改写来源；不删除旧备份；不实现恢复；不覆盖 preview.sqlite3。
- 数据库用 sqlite3.Connection.backup 做一致快照：先写目标盘临时文件，
  对副本执行 PRAGMA integrity_check，通过后才改成正式文件名。
- 目标未配置 / 盘未接入 / 不可写 / 空间不足 / 快照或校验失败 -> 明确失败，
  保留旧备份，状态写进配置 JSON 的 last，页面显示“当前没有新备份”。
- 备份目录先在目标盘上的隐藏临时目录 .backup_<时间戳>.tmp 里组装成功，
  最后整体改名为 backup_<时间戳>；失败时只清掉本次临时目录，不碰旧备份。
- 测试全部使用临时来源与临时目标；本模块不读真实 runs/inbox 等目录，
  除非调用方显式传入真实路径（由视图/中间件在用户配置后才发生）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# --- 常量 ---------------------------------------------------------------

CONFIG_FILENAME = "backup_config.json"
MANIFEST_NAME = "backup_manifest.json"
MANIFEST_SCHEMA = "br_backup_manifest/v1"
PANELS_SNAPSHOT_NAME = "panels_snapshot.json"

#: L1/L2 证据范围（相对 L1 工作区）
L1_SCOPE_DIRS = ("runs", "inbox", "pending", "archive", "golden", "schemas")
L1_SCOPE_FILES = ("aliases.json", "对照表.json")

#: 目标磁盘至少留出的余量，避免写满外接盘
MIN_FREE_SPACE = 64 * 1024 * 1024

#: 默认 L1 工作区（当前用户桌面；不硬编码用户名）
DEFAULT_L1_WORKSPACE = str(Path.home() / "Desktop" / "数据系统开发" / "02_图像识别")

EXCLUDED_DIR_NAMES = {"__pycache__", ".git", "node_modules", ".pytest_cache", ".tmp"}
EXCLUDED_DIR_REASON = "缓存/版本库目录"
EXCLUDED_SUFFIX_REASONS = (
    ((".pyc",), "Python 缓存"),
    ((".tmp",), "临时文件"),
    ((".key",), "密钥文件"),
    ((".pem",), "密钥文件"),
    ((".sqlite3-wal", ".sqlite3-shm"), "SQLite 临时文件"),
)
SECRET_PREFIXES = ("credentials",)

#: 数据库快照正式相对路径（成功后去掉 .tmp）
DB_SNAPSHOT_REL = "db/preview.sqlite3"


class BackupError(Exception):
    """备份流程中可直接展示给用户的中文错误。"""


# --- Django setting 读取（延迟导入，便于测试在无 Django 时只测纯函数） ----

def _setting(name, default=None):
    try:
        from django.conf import settings
    except Exception:  # pragma: no cover - 非 Django 环境兜底
        return default
    return getattr(settings, name, default)


def app_dir() -> Path:
    """L3 应用目录（br-app）。视图/中间件调用时读 settings.BASE_DIR。"""
    base = _setting("BASE_DIR", None)
    if base:
        return Path(str(base))
    # 本文件位于 <br-app>/panels/services/backup.py
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    """备份配置文件路径：settings.BACKUP_CONFIG_PATH 可覆盖，默认 data/backup_config.json。"""
    configured = str(_setting("BACKUP_CONFIG_PATH", "") or "").strip()
    if configured:
        return Path(configured)
    return app_dir() / "data" / CONFIG_FILENAME


_default_config_path = config_path


def default_db_path() -> Path:
    """L3 正式数据库路径（测试通过 patch 本函数注入临时 sqlite）。"""
    try:
        from django.conf import settings
    except Exception:  # pragma: no cover
        return app_dir() / "data" / "preview.sqlite3"
    name = ((getattr(settings, "DATABASES", {}) or {}).get("default") or {}).get("NAME", "")
    return Path(str(name))


def _resolve_l1_workspace(cfg: dict) -> Path:
    """L1 工作区优先级：settings.BACKUP_L1_WORKSPACE > 配置 JSON > 默认路径。"""
    override = str(_setting("BACKUP_L1_WORKSPACE", "") or "").strip()
    if override:
        return Path(override)
    stored = str((cfg or {}).get("l1_workspace") or "").strip()
    if stored:
        return Path(stored)
    return Path(DEFAULT_L1_WORKSPACE)


# --- 配置 JSON 读写 ------------------------------------------------------

def _default_config() -> dict:
    empty_last = {
        "result": "",
        "at": "",
        "target": "",
        "backup_dir": "",
        "error": "",
        "files": 0,
        "bytes": 0,
        "db_integrity": "",
    }
    return {
        "target_dir": "",
        "confirmed_external": False,
        "auto_daily": True,
        "l1_workspace": "",
        "last": dict(empty_last),
        "last_success": dict(empty_last),
        "auto_attempt": {"date": "", "result": ""},
    }


def _merge_config(raw) -> dict:
    cfg = _default_config()
    if not isinstance(raw, dict):
        return cfg
    for key in ("target_dir", "confirmed_external", "auto_daily", "l1_workspace"):
        if key in raw:
            cfg[key] = raw[key]
    for key in ("last", "last_success", "auto_attempt"):
        if isinstance(raw.get(key), dict):
            merged = dict(cfg[key])
            merged.update(raw[key])
            cfg[key] = merged
    return cfg


def atomic_write_json(path, data) -> Path:
    """UTF-8 无 BOM、LF、同目录临时文件 + os.replace 的原子写。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target


def load_config(path=None) -> dict:
    """读取配置；文件不存在或损坏都不抛异常，交给页面显示可读原因。"""
    config_file = Path(path) if path else _default_config_path()
    try:
        raw = json.loads(config_file.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        cfg = _default_config()
        cfg["_load_error"] = ""
        cfg["_config_file"] = str(config_file)
        return cfg
    except (OSError, ValueError) as exc:
        cfg = _default_config()
        cfg["_load_error"] = f"备份配置文件无法读取：{exc}"
        cfg["_config_file"] = str(config_file)
        return cfg
    cfg = _merge_config(raw)
    cfg["_load_error"] = ""
    cfg["_config_file"] = str(config_file)
    return cfg


def save_config(cfg: dict, path=None) -> Path:
    config_file = Path(path) if path else _default_config_path()
    payload = {k: v for k, v in (cfg or {}).items() if not str(k).startswith("_")}
    return atomic_write_json(config_file, payload)


# --- 小工具 -------------------------------------------------------------

def human_size(num) -> str:
    try:
        value = float(num or 0)
    except (TypeError, ValueError):
        return str(num)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[index]}"


def _now(now=None) -> datetime:
    return now if now is not None else datetime.now()


def _iso_now(now=None) -> str:
    return _now(now).isoformat(timespec="seconds")


def _timestamp(now=None) -> str:
    return _now(now).strftime("%Y%m%d_%H%M%S")


def _same_drive(a: Path, b: Path):
    """盘符相同返回 (True, 提示)；仅按盘符判断，不断言物理硬盘。"""
    try:
        drive_a = os.path.splitdrive(os.path.abspath(str(a)))[0].lower()
        drive_b = os.path.splitdrive(os.path.abspath(str(b)))[0].lower()
    except (OSError, ValueError):
        return False, ""
    if drive_a and drive_b and drive_a == drive_b:
        return True, (
            f"备份目标与数据目录在同一个盘符（{drive_a}）；"
            "应用无法据此判断是不是同一块物理硬盘，请确认目标确实位于另一块硬盘上"
        )
    return False, ""


def _path_is_within(child: Path, parent: Path) -> bool:
    try:
        child_resolved = Path(child).resolve()
        parent_resolved = Path(parent).resolve()
    except (OSError, RuntimeError):
        return False
    try:
        child_resolved.relative_to(parent_resolved)
        return True
    except ValueError:
        return False


def _check_writable(path: Path):
    """在目标目录里建一个探针文件验证可写，返回 (ok, 错误)。"""
    probe = None
    try:
        fd, probe = tempfile.mkstemp(prefix=".br_backup_probe_", dir=str(path))
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"ok")
            fh.flush()
            os.fsync(fh.fileno())
        return True, ""
    except OSError as exc:
        return False, f"目标目录不可写：{exc}"
    finally:
        if probe:
            try:
                os.unlink(probe)
            except OSError:
                pass


def _free_space(path: Path):
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


# --- 目标校验 / 状态 -----------------------------------------------------

def validate_target(cfg, *, target_dir=None, app_dir_path=None, l1_path=None) -> dict:
    """校验备份目标：存在 / 是目录 / 可写 / 空间 / 与数据盘不同（仅警告）。

    返回 dict（全部为可展示的中文）：ok / errors / warnings / target /
    exists / is_dir / free_bytes / free_human / same_drive。
    """
    result = {
        "ok": False,
        "errors": [],
        "warnings": [],
        "target": "",
        "exists": False,
        "is_dir": False,
        "writable": False,
        "free_bytes": None,
        "free_human": "",
        "same_drive": False,
        "same_drive_warning": "",
    }
    target_text = str(target_dir if target_dir is not None else (cfg or {}).get("target_dir") or "").strip()
    result["target"] = target_text
    l1 = Path(l1_path) if l1_path else _resolve_l1_workspace(cfg or {})
    app = Path(app_dir_path) if app_dir_path else app_dir()

    if not target_text:
        result["errors"].append("未配置备份目标，当前没有新备份")
        return result

    if not Path(target_text).is_absolute():
        result["errors"].append(f"备份目标必须填写绝对路径（例如 E:/BR备份）：{target_text}")
        return result

    if not (cfg or {}).get("confirmed_external"):
        result["errors"].append("首次设置必须勾选“我确认这是另一块硬盘”后才能备份")

    target = Path(target_text)
    result["exists"] = target.exists()
    result["is_dir"] = target.is_dir()
    if not result["exists"]:
        result["errors"].append(f"目标文件夹不存在：{target_text}（外接硬盘可能未接入）")
        return result
    if not result["is_dir"]:
        result["errors"].append(f"目标不是文件夹：{target_text}")
        return result

    if _path_is_within(target, app / "data"):
        result["errors"].append("备份目标不能放在应用的 data 目录内")
    if _path_is_within(target, app):
        result["errors"].append("备份目标不能放在 L3 应用目录内")
    if _path_is_within(target, l1):
        result["errors"].append("备份目标不能放在 L1 工作区内")

    same, warning = _same_drive(target, app / "data")
    result["same_drive"] = same
    result["same_drive_warning"] = warning
    if same:
        result["warnings"].append(warning)

    writable, write_error = _check_writable(target)
    result["writable"] = writable
    if not writable:
        result["errors"].append(write_error)

    free = _free_space(target)
    result["free_bytes"] = free
    result["free_human"] = human_size(free) if free is not None else ""
    if free is None:
        result["errors"].append(f"无法读取目标磁盘可用空间：{target_text}")
    elif free < MIN_FREE_SPACE:
        result["errors"].append(
            f"目标磁盘可用空间不足：至少需要 {human_size(MIN_FREE_SPACE)}，当前可用 {human_size(free)}"
        )

    result["ok"] = not result["errors"]
    return result


def validate_setup(cfg, *, target_dir=None, l1_workspace=None, app_dir_path=None) -> dict:
    """设置页“检查路径”：目标目录 + L1 工作区 + 应用数据库路径是否存在。"""
    l1 = Path(l1_workspace) if l1_workspace else _resolve_l1_workspace(cfg or {})
    report = validate_target(cfg, target_dir=target_dir, app_dir_path=app_dir_path, l1_path=l1)
    if not l1.is_absolute():
        report["errors"].append(f"L1 工作区必须填写绝对路径：{l1}")
    elif not l1.exists():
        report["errors"].append(f"L1 工作区不存在：{l1}")
    elif not l1.is_dir():
        report["errors"].append(f"L1 工作区不是文件夹：{l1}")
    db = default_db_path()
    if not db.is_file():
        report["errors"].append(f"L3 数据库文件不存在：{db}")
    report["l1_workspace"] = str(l1)
    report["db_path"] = str(db)
    report["ok"] = not report["errors"]
    return report


def get_status(config_path=None, cfg=None) -> dict:
    """页面/导航用的备份状态（诚实描述：没有成功备份就说“当前没有新备份”）。"""
    config_file = Path(config_path) if config_path else _default_config_path()
    cfg = cfg if cfg is not None else load_config(config_file)
    target_text = str(cfg.get("target_dir") or "").strip()
    confirmed = bool(cfg.get("confirmed_external"))
    l1 = _resolve_l1_workspace(cfg)
    last = dict(cfg.get("last") or {})
    last_success = dict(cfg.get("last_success") or {})

    status = {
        "config_path": str(config_file),
        "config_error": cfg.get("_load_error") or "",
        "configured": bool(target_text) and confirmed,
        "target_configured": bool(target_text),
        "target_dir": target_text,
        "confirmed_external": confirmed,
        "auto_daily": bool(cfg.get("auto_daily", True)),
        "l1_workspace": str(l1),
        "last": last,
        "last_success": last_success,
        "target_exists": False,
        "target_is_dir": False,
        "free_bytes": None,
        "free_human": "",
        "warnings": [],
        "has_new_backup": False,
        "result": "not_configured",
        "tone": "warn",
        "status_label": "未配置",
        "message": "未配置备份目标，当前没有新备份",
    }

    if status["config_error"]:
        status["message"] = f"{status['config_error']}；当前没有新备份"
        status["tone"] = "bad"
        status["status_label"] = "配置不可读"
        return status

    if not target_text:
        return status

    # 目标当前是否可用（只做轻量检查，不在 GET 时创建探针文件）
    target = Path(target_text)
    status["target_exists"] = target.exists()
    status["target_is_dir"] = target.is_dir()
    if status["target_is_dir"]:
        same, warning = _same_drive(target, app_dir() / "data")
        if same:
            status["warnings"].append(warning)
        free = _free_space(target)
        status["free_bytes"] = free
        status["free_human"] = human_size(free) if free is not None else ""
    else:
        status["warnings"].append(f"目标文件夹当前不可用：{target_text}（外接硬盘可能未接入）")

    if not confirmed:
        status["message"] = (
            "已填写备份目标，但还没有勾选“我确认这是另一块硬盘”；当前没有新备份"
        )
        status["status_label"] = "待确认"
        status["tone"] = "warn"
        return status

    # 上次成功：只有备份目录确实还在，才显示“已备份”
    last_result = str(last.get("result") or "")
    backup_dir = str(last.get("backup_dir") or "")
    if last_result == "success" and backup_dir and Path(backup_dir).is_dir():
        status["has_new_backup"] = True
        status["result"] = "success"
        status["tone"] = "ok"
        status["status_label"] = "已备份"
        status["message"] = (
            f"上次备份成功：{last.get('at') or '—'} → {backup_dir}"
            f"（{int(last.get('files') or 0)} 个文件，{human_size(last.get('bytes'))}；"
            f"数据库结构检查 {last.get('db_integrity') or '未知'}）"
        )
        return status

    if last_result == "failed":
        status["result"] = "failed"
        status["tone"] = "bad"
        status["status_label"] = "备份失败"
        status["message"] = (
            f"上次备份失败（{last.get('at') or '—'}）：{last.get('error') or '未知原因'}；"
            "当前没有新备份，旧备份未删除"
        )
        return status

    if last_result == "success" and backup_dir:
        # 曾经成功过，但现在备份目录不在了（盘未接入 / 被手动移走）
        status["result"] = "failed"
        status["tone"] = "bad"
        status["status_label"] = "备份目录不可见"
        status["message"] = (
            f"上次成功记录的备份目录已不存在：{backup_dir}；当前没有新备份"
        )
        return status

    if not status["target_is_dir"]:
        status["message"] = (
            f"备份目标不存在（外接硬盘可能未接入）：{target_text}；当前没有新备份"
        )
        status["tone"] = "bad"
        status["status_label"] = "目标不可用"
        return status

    status["result"] = "never"
    status["status_label"] = "待备份"
    status["tone"] = "warn"
    status["message"] = "已配置备份目标，尚未执行备份；当前没有新备份"
    return status


# --- SQLite 快照 --------------------------------------------------------

def _sqlite_uri(path: Path, mode: str = "ro") -> str:
    return Path(path).resolve().as_uri() + f"?mode={mode}"


def snapshot_sqlite(src_db, dest_path) -> int:
    """用 Connection.backup 生成一致快照，返回快照字节数。失败抛 BackupError。"""
    src = Path(src_db)
    dest = Path(dest_path)
    if not src.is_file():
        raise BackupError(f"L3 数据库文件不存在：{src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_conn = sqlite3.connect(_sqlite_uri(src), uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise BackupError(f"无法打开数据库快照来源：{src}：{exc}") from exc
    try:
        target_conn = sqlite3.connect(str(dest), timeout=30)
        try:
            source_conn.backup(target_conn)
            target_conn.commit()
        finally:
            target_conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"数据库快照失败：{exc}") from exc
    finally:
        source_conn.close()
    return dest.stat().st_size


def check_sqlite_integrity(db_path):
    """对副本执行 PRAGMA integrity_check，返回 (ok, 说明)。"""
    path = Path(db_path)
    try:
        conn = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=30)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"完整性检查无法执行：{exc}"
    messages = [str(row[0]) for row in rows]
    if len(messages) == 1 and messages[0].strip().lower() == "ok":
        return True, "ok"
    return False, "；".join(messages[:5]) or "完整性检查没有返回 ok"


# --- 来源扫描与排除 -----------------------------------------------------

def excluded_reason(path: Path) -> str:
    name = Path(path).name
    lowered = name.lower()
    if lowered == ".env" or lowered.startswith(".env."):
        return "环境变量文件"
    if lowered.startswith(SECRET_PREFIXES):
        return "凭据文件"
    if lowered.startswith("test_") and lowered.endswith((".sqlite3", ".sqlite", ".db")):
        return "测试数据库"
    for suffixes, reason in EXCLUDED_SUFFIX_REASONS:
        if lowered.endswith(suffixes):
            return reason
    return ""


def _walk_scope_files(root: Path):
    """递归列出 root 下应复制的文件；返回 (files[(相对路径, Path)], excluded[(相对路径, 原因)])。"""
    files = []
    excluded = []
    for dirpath, dirnames, filenames in os.walk(root):
        kept = []
        for dirname in sorted(dirnames):
            lowered = dirname.lower()
            if lowered in EXCLUDED_DIR_NAMES:
                rel_dir = (Path(dirpath) / dirname).relative_to(root).as_posix()
                reason = "临时文件" if lowered == ".tmp" else EXCLUDED_DIR_REASON
                excluded.append((rel_dir + "/", reason))
            else:
                kept.append(dirname)
        dirnames[:] = kept
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                excluded.append((rel, "符号链接"))
                continue
            reason = excluded_reason(path)
            if reason:
                excluded.append((rel, reason))
                continue
            files.append((rel, path))
    return files, excluded


def _collect_sources(*, app: Path, l1: Path) -> dict:
    """收拾本次要复制的范围（不含数据库快照本身）。"""
    seeds = []
    excluded = []  # [(展示路径, 原因)]
    missing = []

    seeds_dir = Path(app) / "seeds"
    if seeds_dir.is_dir():
        for path in sorted(seeds_dir.glob("*.json")):
            if not path.is_file():
                continue
            reason = excluded_reason(path)
            if reason:
                excluded.append((f"seeds/{path.name}", reason))
                continue
            seeds.append(path)
    else:
        missing.append("seeds/（目录不存在，未复制面板与导入映射种子）")

    l1_files = []  # [(scope_label, 相对 l1 的目标路径, 源文件)]
    for scope in L1_SCOPE_DIRS:
        root = Path(l1) / scope
        if not root.exists():
            missing.append(f"l1/{scope}")
            continue
        if not root.is_dir():
            missing.append(f"l1/{scope}（不是文件夹）")
            continue
        found, skipped = _walk_scope_files(root)
        for rel, reason in skipped:
            excluded.append((f"l1/{scope}/{rel}", reason))
        for rel, path in found:
            l1_files.append((f"l1/{scope}", f"{scope}/{rel}", path))

    for name in L1_SCOPE_FILES:
        path = Path(l1) / name
        if path.is_file():
            l1_files.append(("l1", name, path))
        elif not path.exists():
            missing.append(f"l1/{name}")
        else:
            missing.append(f"l1/{name}（不是文件）")

    return {
        "seeds": seeds,
        "l1_files": l1_files,
        "excluded": excluded,
        "missing": missing,
    }


def _build_panels_snapshot(app: Path, now=None) -> dict:
    """面板配置快照：把 seeds/panels.json 收进 config/panels_snapshot.json。"""
    seeds_path = Path(app) / "seeds" / "panels.json"
    payload = {
        "schema": "br_panels_snapshot/v1",
        "generated_at": _iso_now(now),
        "source": "seeds/panels.json",
        "available": False,
        "panels": [],
    }
    try:
        raw = json.loads(seeds_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        payload["note"] = "seeds/panels.json 不存在，面板快照为空"
        return payload
    except (OSError, ValueError) as exc:
        payload["note"] = f"seeds/panels.json 无法读取：{exc}"
        return payload
    payload["available"] = True
    payload["panels"] = raw
    return payload


# --- 复制与目录 ---------------------------------------------------------

def _copy_file(src: Path, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dest)
    except OSError as exc:
        raise BackupError(f"复制失败：{src} → {dest}：{exc}") from exc
    return dest.stat().st_size


def _create_staging(target: Path, stamp: str) -> Path:
    staging = target / f".backup_{stamp}.tmp"
    suffix = 2
    while staging.exists():
        staging = target / f".backup_{stamp}_{suffix}.tmp"
        suffix += 1
    staging.mkdir(parents=True)
    return staging


def _unique_backup_dir(target: Path, stamp: str) -> Path:
    candidate = target / f"backup_{stamp}"
    suffix = 2
    while candidate.exists():
        candidate = target / f"backup_{stamp}_{suffix}"
        suffix += 1
    return candidate


def _cleanup_dir(path) -> None:
    if path is None:
        return
    shutil.rmtree(str(path), ignore_errors=True)


def _excluded_summary(items):
    by_reason = {}
    for _path, reason in items:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    detail = [{"path": path, "reason": reason} for path, reason in items[:200]]
    return {
        "count": len(items),
        "by_reason": by_reason,
        "items": detail,
        "truncated": len(items) > len(detail),
    }


def _write_backup(*, staging: Path, final_dir: Path, target: Path, db: Path,
                  plan: dict, app: Path, l1: Path, now=None, warnings=None) -> dict:
    created_at = _iso_now(now)
    warnings = list(warnings or [])
    files = 0
    total_bytes = 0
    scopes = []

    # 1) 数据库一致快照：临时文件 -> integrity_check -> 正式文件名
    db_dir = staging / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_tmp = db_dir / "preview.sqlite3.tmp"
    db_bytes = snapshot_sqlite(db, db_tmp)
    integrity_ok, integrity_message = check_sqlite_integrity(db_tmp)
    if not integrity_ok:
        raise BackupError(f"数据库快照完整性检查失败：{integrity_message}")
    db_final = db_dir / "preview.sqlite3"
    os.replace(db_tmp, db_final)
    files += 1
    total_bytes += db_bytes
    scopes.append({
        "name": "db",
        "destination": DB_SNAPSHOT_REL,
        "files": 1,
        "bytes": db_bytes,
        "integrity": integrity_message,
        "source": str(db),
    })

    # 2) L3 面板配置快照
    snapshot = _build_panels_snapshot(app, now=now)
    snapshot_path = staging / "config" / PANELS_SNAPSHOT_NAME
    atomic_write_json(snapshot_path, snapshot)
    snapshot_bytes = snapshot_path.stat().st_size
    files += 1
    total_bytes += snapshot_bytes
    scopes.append({
        "name": "config",
        "destination": f"config/{PANELS_SNAPSHOT_NAME}",
        "files": 1,
        "bytes": snapshot_bytes,
    })

    # 3) seeds/*.json（面板与导入映射配置）
    seed_files = seed_bytes = 0
    for path in plan.get("seeds") or []:
        copied = _copy_file(path, staging / "seeds" / path.name)
        seed_files += 1
        seed_bytes += copied
    files += seed_files
    total_bytes += seed_bytes
    scopes.append({
        "name": "seeds",
        "destination": "seeds",
        "files": seed_files,
        "bytes": seed_bytes,
    })

    # 4) L1 证据（只复制）
    groups = {}
    for scope_label, dest_rel, path in plan.get("l1_files") or []:
        copied = _copy_file(path, staging / "l1" / dest_rel)
        group = groups.setdefault(scope_label, {"files": 0, "bytes": 0})
        group["files"] += 1
        group["bytes"] += copied
        files += 1
        total_bytes += copied
    for scope_label in [f"l1/{scope}" for scope in L1_SCOPE_DIRS] + ["l1"]:
        group = groups.get(scope_label, {"files": 0, "bytes": 0})
        scopes.append({
            "name": scope_label,
            "destination": "l1" if scope_label == "l1" else f"l1/{scope_label.split('/', 1)[1]}",
            "files": group["files"],
            "bytes": group["bytes"],
        })

    # 5) 清单（不算自身）
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "created_at": created_at,
        "result": "success",
        "error": "",
        "target": {"dir": str(target), "backup_dir": str(final_dir)},
        "source": {
            "app_dir": str(app),
            "db_path": str(db),
            "db_size": db.stat().st_size,
            "l1_workspace": str(l1),
            "l1_scope_dirs": list(L1_SCOPE_DIRS),
            "l1_scope_files": list(L1_SCOPE_FILES),
            "seeds": [Path(p).name for p in plan.get("seeds") or []],
        },
        "files": files,
        "bytes": total_bytes,
        "files_note": "文件数与总字节不含 backup_manifest.json 自身",
        "db": {
            "source": str(db),
            "snapshot": DB_SNAPSHOT_REL,
            "method": "sqlite3.Connection.backup",
            "integrity": integrity_message,
            "bytes": db_bytes,
        },
        "scopes": scopes,
        "excluded": _excluded_summary(plan.get("excluded") or []),
        "missing": list(plan.get("missing") or []),
        "warnings": warnings,
        "failures": [],
    }
    atomic_write_json(staging / MANIFEST_NAME, manifest)
    return manifest


# --- 运行备份 ------------------------------------------------------------

def _base_result(result: str, *, at: str, target: str, error: str = "") -> dict:
    return {
        "ok": False,
        "result": result,
        "at": at,
        "target": target,
        "backup_dir": "",
        "error": error,
        "files": 0,
        "bytes": 0,
        "db_integrity": "",
        "manifest": None,
        "warnings": [],
    }


def _persist_last(cfg: dict, result: dict, config_file: Path) -> None:
    last = {
        "result": result.get("result") or "",
        "at": result.get("at") or "",
        "target": result.get("target") or "",
        "backup_dir": result.get("backup_dir") or "",
        "error": result.get("error") or "",
        "files": int(result.get("files") or 0),
        "bytes": int(result.get("bytes") or 0),
        "db_integrity": result.get("db_integrity") or "",
    }
    cfg["last"] = last
    if result.get("result") == "success":
        cfg["last_success"] = dict(last)
    save_config(cfg, config_file)


def run_backup(config_path=None, db_path=None, l1_workspace=None,
               app_dir_path=None, now=None) -> dict:
    """执行一次备份，返回结果 dict；同时把 last/last_success 写回配置 JSON。"""
    config_file = Path(config_path) if config_path else _default_config_path()
    cfg = load_config(config_file)
    result = _run_backup(cfg, db_path=db_path, l1_workspace=l1_workspace,
                         app_dir_path=app_dir_path, now=now)
    try:
        _persist_last(cfg, result, config_file)
    except OSError as exc:
        result.setdefault("warnings", []).append(f"备份状态未能写入配置文件：{exc}")
    return result


def _run_backup(cfg: dict, *, db_path=None, l1_workspace=None,
                app_dir_path=None, now=None) -> dict:
    at = _iso_now(now)
    app = Path(app_dir_path) if app_dir_path else app_dir()
    l1 = Path(l1_workspace) if l1_workspace else _resolve_l1_workspace(cfg)
    db = Path(db_path) if db_path else default_db_path()
    target_text = str(cfg.get("target_dir") or "").strip()
    result = _base_result("failed", at=at, target=target_text)
    warnings = result["warnings"]

    if not target_text:
        result.update(result="not_configured",
                      error="未配置备份目标，当前没有新备份（未执行任何复制）")
        return result

    if not l1.is_absolute():
        result["error"] = f"L1 工作区必须填写绝对路径：{l1}"
        return result

    if not l1.is_dir():
        result["error"] = f"L1 工作区不存在或不是文件夹：{l1}"
        return result

    if not db.is_file():
        result["error"] = f"L3 数据库文件不存在：{db}"
        return result

    target_check = validate_target(cfg, target_dir=target_text,
                                   app_dir_path=app, l1_path=l1)
    warnings.extend(target_check["warnings"])
    if not target_check["ok"]:
        result["error"] = "；".join(target_check["errors"])
        return result

    target = Path(target_text)
    try:
        plan = _collect_sources(app=app, l1=l1)
    except OSError as exc:
        result["error"] = f"扫描备份来源失败：{exc}"
        return result

    source_bytes = sum(path.stat().st_size for path in plan["seeds"])
    source_bytes += sum(path.stat().st_size for _s, _d, path in plan["l1_files"])
    required = db.stat().st_size + source_bytes + MIN_FREE_SPACE
    free = _free_space(target)
    if free is None:
        result["error"] = f"无法读取目标磁盘可用空间：{target}"
        return result
    if free < required:
        result["error"] = (
            f"目标磁盘空间不足：至少需要约 {human_size(required)}，"
            f"当前可用 {human_size(free)}；未开始复制"
        )
        return result

    stamp = _timestamp(now)
    final_dir = _unique_backup_dir(target, stamp)
    staging = None
    try:
        staging = _create_staging(target, stamp)
        manifest = _write_backup(staging=staging, final_dir=final_dir, target=target,
                                 db=db, plan=plan, app=app, l1=l1, now=now,
                                 warnings=warnings)
        os.rename(str(staging), str(final_dir))
        staging = None
        result.update(
            ok=True,
            result="success",
            backup_dir=str(final_dir),
            error="",
            files=int(manifest.get("files") or 0),
            bytes=int(manifest.get("bytes") or 0),
            db_integrity=str((manifest.get("db") or {}).get("integrity") or ""),
            manifest=manifest,
        )
        return result
    except BackupError as exc:
        result["error"] = str(exc)
    except OSError as exc:
        result["error"] = f"备份写入失败：{exc}"
    except Exception as exc:  # noqa: BLE001 - 备份失败必须变成可读状态，不能让页面崩
        logger.exception("备份过程出现未预期异常")
        result["error"] = f"备份失败：{exc}"
    finally:
        _cleanup_dir(staging)
    result["result"] = "failed"
    return result

