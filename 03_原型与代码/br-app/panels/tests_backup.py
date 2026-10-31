"""外接硬盘备份的合成测试（契约第 5 节 / E 工作包）。

所有测试只使用 tempfile 临时目录、合成 SQLite 与合成 L1 目录；
绝不读、写、复制真实的 runs/、inbox/、pending/、archive/、golden/
或 data/preview.sqlite3。备份目标也用临时目录。

覆盖的失败路径全部通过 patch 注入（磁盘满 / 快照失败 / 完整性失败），
不依赖真实磁盘配额或外接硬盘。
"""
from __future__ import annotations

import errno
import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings

from panels import backup_middleware
from panels.services import backup as backup_service

DiskUsage = namedtuple("DiskUsage", "total used free")
NOW = datetime(2026, 10, 29, 20, 0, 0)


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _hash_tree(root: Path) -> dict:
    result = {}
    root = Path(root)
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _sha256_file(path)
    return result


def _make_sqlite(path: Path, rows: int = 4) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO records (name) VALUES (?)",
                         [(f"合成记录{i}",) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()
    return path


def _count_rows(path: Path) -> int:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        conn.close()


class BackupTestBase(TestCase):
    """临时合成环境：L3 应用 / L1 工作区 / 外接盘目标 / 配置 / 旧备份。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="br_backup_test_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

        # 合成 L3 应用（只复制 seeds；不读真实 br-app）
        self.app = self.tmp / "br-app"
        self.seeds = self.app / "seeds"
        self.seeds.mkdir(parents=True)
        _write(self.seeds / "panels.json",
               json.dumps([{"key": "p1", "name": "合成面板", "columns": []}], ensure_ascii=False))
        _write(self.seeds / "reviewed_maps.json", json.dumps({"maps": []}, ensure_ascii=False))
        # 合成数据库（绝不用真实 data/preview.sqlite3）
        self.db = _make_sqlite(self.app / "data" / "preview.sqlite3", rows=4)

        # 合成 L1 工作区：正常证据 + 各种应被排除的文件
        self.l1 = self.tmp / "l1"
        run_dir = self.l1 / "runs" / "20260101_001"
        _write(run_dir / "raw.json", '{"run_id": "20260101_001"}')
        _write(run_dir / "decisions.json", '{"run_id": "20260101_001"}')
        _write(run_dir / "review_state.json", '{"run_id": "20260101_001"}')
        _write(run_dir / "reviewed.json", '{"schema_version": "reviewed/v2"}')
        _write_bytes(run_dir / "source" / "IMG_0001.jpg", b"fake-jpeg-bytes")
        _write(run_dir / "__pycache__" / "raw.cpython-314.pyc", "cache")
        _write(run_dir / "残留.pyc", "pyc")
        _write(run_dir / "临时.tmp", "tmp")
        _write(run_dir / ".tmp" / "half.tmp", "tmp-dir")
        _write(run_dir / "test_rows.sqlite3", "test-db")
        _write(run_dir / ".env", "SECRET=1")
        _write(run_dir / "credentials.json", "{}")
        _write(run_dir / "secret.key", "key")
        _write(run_dir / "node_modules" / "pkg.js", "js")
        _write_bytes(self.l1 / "inbox" / "待识别.jpg", b"inbox-jpeg")
        _write_bytes(self.l1 / "pending" / "看不清.jpg", b"pending-jpeg")
        _write_bytes(self.l1 / "archive" / "2026-01-01" / "已归档.jpg", b"archive-jpeg")
        _write(self.l1 / "golden" / "答案.json", "{}")
        _write(self.l1 / "schemas" / "库存明细_v1.json", "{}")
        _write(self.l1 / "aliases.json", "{}")
        _write(self.l1 / "对照表.json", "{}")
        # 不在备份范围内的普通文件，不应被复制
        _write(self.l1 / "README.md", "not in scope")

        # 合成“另一块硬盘”的目标目录（临时目录）
        self.target = self.tmp / "external"
        self.target.mkdir()
        # 旧备份：任何失败都不能删掉它
        self.old_backup = self.target / "backup_20200101_000000"
        self.old_backup.mkdir()
        _write(self.old_backup / "old_marker.txt", "旧备份必须保留")

        # 配置 JSON（不在真实 data/ 下）
        self.config = self.tmp / "config" / "backup_config.json"
        self.write_config()

    # --- 工具 -----------------------------------------------------

    def write_config(self, **overrides):
        cfg = {
            "target_dir": str(self.target),
            "confirmed_external": True,
            "auto_daily": False,
            "l1_workspace": str(self.l1),
            "last": {"result": "", "at": "", "target": "", "backup_dir": "",
                     "error": "", "files": 0, "bytes": 0, "db_integrity": ""},
            "last_success": {"result": "", "at": "", "target": "", "backup_dir": "",
                             "error": "", "files": 0, "bytes": 0, "db_integrity": ""},
            "auto_attempt": {"date": "", "result": ""},
        }
        cfg.update(overrides)
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg

    def read_config(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def run_synth(self, **overrides) -> dict:
        kwargs = dict(config_path=self.config, db_path=self.db,
                      l1_workspace=self.l1, app_dir_path=self.app, now=NOW)
        kwargs.update(overrides)
        return backup_service.run_backup(**kwargs)

    def new_backup_dirs(self):
        return sorted(p for p in self.target.glob("backup_2026*") if p.is_dir())

    def staging_dirs(self):
        return sorted(self.target.glob(".backup_*"))

    def assert_old_backup_intact(self):
        self.assertTrue(self.old_backup.is_dir(), "旧备份目录被删除")
        self.assertTrue((self.old_backup / "old_marker.txt").is_file(), "旧备份内容被删除")

    def assert_no_success_or_staging(self):
        self.assertEqual(self.new_backup_dirs(), [], "失败时不应产生成功备份目录")
        self.assertEqual(self.staging_dirs(), [], "失败时不应留下临时目录")
        self.assert_old_backup_intact()


class BackupSuccessTests(BackupTestBase):
    def test_success_manifest_matches_files_and_sources_untouched(self):
        before_l1 = _hash_tree(self.l1)
        before_seeds = _hash_tree(self.seeds)
        before_db = _sha256_file(self.db)
        before_rows = _count_rows(self.db)

        result = self.run_synth()

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["result"], "success")
        self.assertEqual(result["db_integrity"], "ok")
        backup_dir = Path(result["backup_dir"])
        self.assertTrue(backup_dir.is_dir())
        self.assertEqual(backup_dir.name, "backup_20261029_200000")
        self.assertTrue((backup_dir / "backup_manifest.json").is_file())

        # 数据库快照：正式文件名存在、临时 .tmp 已改名、结构检查 ok、行数与源一致
        db_copy = backup_dir / "db" / "preview.sqlite3"
        self.assertTrue(db_copy.is_file())
        self.assertFalse((backup_dir / "db" / "preview.sqlite3.tmp").exists())
        ok, message = backup_service.check_sqlite_integrity(db_copy)
        self.assertTrue(ok, message)
        self.assertEqual(message, "ok")
        self.assertEqual(_count_rows(db_copy), before_rows)

        # 期望复制的关键文件都在
        for rel in (
            "l1/runs/20260101_001/raw.json",
            "l1/runs/20260101_001/decisions.json",
            "l1/runs/20260101_001/review_state.json",
            "l1/runs/20260101_001/reviewed.json",
            "l1/runs/20260101_001/source/IMG_0001.jpg",
            "l1/inbox/待识别.jpg",
            "l1/pending/看不清.jpg",
            "l1/archive/2026-01-01/已归档.jpg",
            "l1/golden/答案.json",
            "l1/schemas/库存明细_v1.json",
            "l1/aliases.json",
            "l1/对照表.json",
            "config/panels_snapshot.json",
            "seeds/panels.json",
            "seeds/reviewed_maps.json",
        ):
            self.assertTrue((backup_dir / rel).is_file(), f"缺少备份文件：{rel}")

        # 清单与实际文件完全一致（不含清单自身）
        manifest = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
        actual = [p for p in backup_dir.rglob("*")
                  if p.is_file() and p.name != "backup_manifest.json"]
        self.assertEqual(manifest["files"], len(actual))
        self.assertEqual(manifest["bytes"], sum(p.stat().st_size for p in actual))
        self.assertEqual(manifest["schema"], "br_backup_manifest/v1")
        self.assertEqual(manifest["result"], "success")
        self.assertEqual(manifest["error"], "")
        self.assertEqual(manifest["target"]["backup_dir"], str(backup_dir))
        self.assertEqual(manifest["source"]["db_path"], str(self.db))
        self.assertEqual(manifest["source"]["l1_workspace"], str(self.l1))
        self.assertEqual(manifest["db"]["integrity"], "ok")
        self.assertEqual(manifest["db"]["method"], "sqlite3.Connection.backup")
        self.assertEqual(manifest["failures"], [])
        self.assertEqual(manifest["missing"], [])

        scopes = {scope["name"]: scope for scope in manifest["scopes"]}
        self.assertEqual(scopes["db"]["files"], 1)
        self.assertEqual(scopes["db"]["integrity"], "ok")
        self.assertEqual(scopes["config"]["files"], 1)
        self.assertEqual(scopes["l1/runs"]["files"], 5)
        self.assertEqual(scopes["l1/inbox"]["files"], 1)
        self.assertEqual(scopes["l1/pending"]["files"], 1)
        self.assertEqual(scopes["l1/archive"]["files"], 1)
        self.assertEqual(scopes["l1/golden"]["files"], 1)
        self.assertEqual(scopes["l1/schemas"]["files"], 1)
        self.assertEqual(scopes["l1"]["files"], 2)
        for scope in manifest["scopes"]:
            if scope["name"].startswith("l1/"):
                self.assertGreaterEqual(scope["files"], 1, scope)

        # 面板快照内容来自合成 seeds
        snapshot = json.loads((backup_dir / "config" / "panels_snapshot.json").read_text(encoding="utf-8"))
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["panels"][0]["key"], "p1")

        # 排除项：不进备份，清单里有数量与原因
        for rel in (
            "l1/runs/20260101_001/__pycache__",
            "l1/runs/20260101_001/.tmp",
            "l1/runs/20260101_001/临时.tmp",
            "l1/runs/20260101_001/test_rows.sqlite3",
            "l1/runs/20260101_001/.env",
            "l1/runs/20260101_001/credentials.json",
            "l1/runs/20260101_001/secret.key",
            "l1/runs/20260101_001/node_modules",
            "l1/README.md",
        ):
            self.assertFalse((backup_dir / rel).exists(), f"排除项被复制：{rel}")
        excluded = manifest["excluded"]
        self.assertGreaterEqual(excluded["count"], 7)
        reasons = set(excluded["by_reason"].keys())
        self.assertIn("Python 缓存", reasons)
        self.assertIn("临时文件", reasons)
        self.assertIn("测试数据库", reasons)
        self.assertIn("环境变量文件", reasons)
        self.assertIn("凭据文件", reasons)
        self.assertIn("密钥文件", reasons)
        self.assertIn("缓存/版本库目录", reasons)

        # 只复制：来源一个字节都不能变
        self.assertEqual(_hash_tree(self.l1), before_l1, "L1 来源被改动")
        self.assertEqual(_hash_tree(self.seeds), before_seeds, "seed 配置被改动")
        self.assertEqual(_sha256_file(self.db), before_db, "合成数据库被改动")

        # 旧备份保留；无遗留临时目录
        self.assert_old_backup_intact()
        self.assertEqual(self.staging_dirs(), [])

        # 状态写回配置
        cfg = self.read_config()
        self.assertEqual(cfg["last"]["result"], "success")
        self.assertEqual(cfg["last"]["backup_dir"], str(backup_dir))
        self.assertEqual(cfg["last"]["files"], manifest["files"])
        self.assertEqual(cfg["last"]["bytes"], manifest["bytes"])
        self.assertEqual(cfg["last"]["db_integrity"], "ok")
        self.assertEqual(cfg["last_success"]["result"], "success")

        # 页面状态：成功可以显示“已备份”
        status = backup_service.get_status(self.config)
        self.assertTrue(status["has_new_backup"])
        self.assertEqual(status["status_label"], "已备份")
        self.assertIn("上次备份成功", status["message"])

    def test_second_backup_uses_new_directory_and_keeps_old(self):
        first = self.run_synth()
        self.assertTrue(first["ok"], first.get("error"))
        second = self.run_synth(now=datetime(2026, 10, 28, 21, 0, 0))
        self.assertTrue(second["ok"], second.get("error"))
        self.assertNotEqual(first["backup_dir"], second["backup_dir"])
        self.assertTrue(Path(first["backup_dir"]).is_dir())
        self.assertTrue(Path(second["backup_dir"]).is_dir())
        self.assertTrue(self.old_backup.is_dir())

    def test_target_same_drive_is_warning_not_error(self):
        report = backup_service.validate_target(
            {"target_dir": str(self.target), "confirmed_external": True, "l1_workspace": str(self.l1)},
            target_dir=str(self.target),
            app_dir_path=self.app,
            l1_path=self.l1,
        )
        self.assertTrue(report["ok"], report["errors"])
        same, _warning = backup_service._same_drive(self.target, self.app / "data")
        self.assertEqual(report["same_drive"], same)
        if same:
            self.assertTrue(any("同一个盘符" in w for w in report["warnings"]))
        # 目标放在应用 data 目录 / L1 工作区内必须直接拒绝
        bad = backup_service.validate_target(
            {"target_dir": str(self.app / "data"), "confirmed_external": True},
            target_dir=str(self.app / "data"),
            app_dir_path=self.app,
            l1_path=self.l1,
        )
        self.assertFalse(bad["ok"])
        self.assertTrue(any("data 目录" in e for e in bad["errors"]))
        bad_l1 = backup_service.validate_target(
            {"target_dir": str(self.l1 / "inbox"), "confirmed_external": True},
            target_dir=str(self.l1 / "inbox"),
            app_dir_path=self.app,
            l1_path=self.l1,
        )
        self.assertFalse(bad_l1["ok"])
        self.assertTrue(any("L1 工作区" in e for e in bad_l1["errors"]))

    def test_snapshot_works_in_chinese_path(self):
        # 真实工作区路径含中文（数据系统开发/02_图像识别），这里用中文目录验证 SQLite URI 处理
        src = _make_sqlite(self.tmp / "中文 目录" / "库 文件.sqlite3", rows=2)
        dest = self.tmp / "快照 目录" / "副本.sqlite3"
        size = backup_service.snapshot_sqlite(src, dest)
        self.assertGreater(size, 0)
        ok, message = backup_service.check_sqlite_integrity(dest)
        self.assertTrue(ok, message)
        self.assertEqual(_count_rows(dest), 2)

    def test_validate_setup_with_synthetic_db(self):
        with patch.object(backup_service, "default_db_path", return_value=self.db):
            report = backup_service.validate_setup(
                {"target_dir": str(self.target), "confirmed_external": True},
                target_dir=str(self.target),
                l1_workspace=str(self.l1),
                app_dir_path=self.app,
            )
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["db_path"], str(self.db))


class BackupFailureTests(BackupTestBase):
    def test_unconfigured_target_is_not_configured_and_never_success(self):
        self.write_config(target_dir="", confirmed_external=False)

        status = backup_service.get_status(self.config)
        self.assertFalse(status["configured"])
        self.assertFalse(status["has_new_backup"])
        self.assertEqual(status["result"], "not_configured")
        self.assertIn("未配置备份目标", status["message"])
        self.assertIn("当前没有新备份", status["message"])

        result = self.run_synth()
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "not_configured")
        self.assertIn("未配置备份目标", result["error"])
        self.assert_no_success_or_staging()
        self.assertFalse(any(self.tmp.rglob("backup_2026*")))

        cfg = self.read_config()
        self.assertEqual(cfg["last"]["result"], "not_configured")
        self.assertEqual(cfg["last_success"]["result"], "")

    def test_confirmed_external_required(self):
        self.write_config(confirmed_external=False)
        result = self.run_synth()
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "failed")
        self.assertIn("我确认", result["error"])
        self.assert_no_success_or_staging()

        status = backup_service.get_status(self.config)
        self.assertFalse(status["configured"])
        self.assertIn("还没有勾选", status["message"])

    def test_target_missing_fails_and_creates_nothing(self):
        missing = self.tmp / "未接入的硬盘" / "BR备份"
        self.write_config(target_dir=str(missing))
        result = self.run_synth()
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])
        self.assertFalse(missing.exists())
        self.assertEqual(self.new_backup_dirs(), [])
        self.assertEqual(self.staging_dirs(), [])
        self.assert_old_backup_intact()
        self.assertIn("当前没有新备份", backup_service.get_status(self.config)["message"])

    def test_relative_target_rejected(self):
        self.write_config(target_dir="相对备份目录")
        result = self.run_synth()
        self.assertFalse(result["ok"])
        self.assertIn("绝对路径", result["error"])
        self.assertFalse((Path.cwd() / "相对备份目录").exists())
        self.assert_no_success_or_staging()

    def test_relative_l1_workspace_rejected(self):
        result = self.run_synth(l1_workspace=Path("相对L1工作区"))
        self.assertFalse(result["ok"])
        self.assertIn("L1 工作区必须填写绝对路径", result["error"])
        self.assert_no_success_or_staging()

    def test_target_is_file_fails(self):
        not_a_dir = self.tmp / "not_a_dir.txt"
        _write(not_a_dir, "file")
        self.write_config(target_dir=str(not_a_dir))
        result = self.run_synth()
        self.assertFalse(result["ok"])
        self.assertIn("不是文件夹", result["error"])
        self.assertEqual(self.new_backup_dirs(), [])
        self.assert_old_backup_intact()

    def test_target_not_writable_fails_and_keeps_old_backup(self):
        with patch.object(backup_service, "_check_writable",
                          return_value=(False, "目标目录不可写：模拟权限不足")):
            result = self.run_synth()
        self.assertFalse(result["ok"])
        self.assertIn("不可写", result["error"])
        self.assert_no_success_or_staging()

    def test_disk_full_precheck_fails(self):
        with patch.object(backup_service.shutil, "disk_usage",
                          return_value=DiskUsage(total=1000, used=999, free=1)):
            result = self.run_synth()
        self.assertFalse(result["ok"])
        self.assertIn("空间不足", result["error"])
        self.assert_no_success_or_staging()
        self.assertFalse(any(self.tmp.rglob("backup_2026*")))

    def test_disk_full_during_copy_fails(self):
        def _no_space(src, dest):
            raise OSError(errno.ENOSPC, "No space left on device (模拟磁盘写满)")

        with patch.object(backup_service, "_copy_file", side_effect=_no_space):
            result = self.run_synth()
        self.assertFalse(result["ok"])
        self.assertIn("备份写入失败", result["error"])
        self.assert_no_success_or_staging()
        self.assertFalse(list(self.target.rglob("preview.sqlite3.tmp")))
        self.assertNotEqual(backup_service.load_config(self.config)["last"]["result"], "success")

    def test_snapshot_failure_injected(self):
        with patch.object(backup_service, "snapshot_sqlite",
                          side_effect=backup_service.BackupError("模拟快照失败")):
            result = self.run_synth()
        self.assertFalse(result["ok"])
        self.assertIn("模拟快照失败", result["error"])
        self.assert_no_success_or_staging()
        self.assertFalse(list(self.target.rglob("preview.sqlite3.tmp")))

    def test_integrity_failure_injected(self):
        with patch.object(backup_service, "check_sqlite_integrity",
                          return_value=(False, "*** in database main ***（模拟损坏）")):
            result = self.run_synth()
        self.assertFalse(result["ok"])
        self.assertIn("完整性检查失败", result["error"])
        self.assert_no_success_or_staging()
        self.assertFalse(list(self.target.rglob("preview.sqlite3.tmp")))
        cfg = self.read_config()
        self.assertEqual(cfg["last"]["result"], "failed")
        self.assertNotEqual(cfg["last"]["db_integrity"], "ok")

    def test_failed_run_status_says_no_new_backup(self):
        self.write_config(target_dir=str(self.tmp / "未接入"))
        self.run_synth()
        status = backup_service.get_status(self.config)
        self.assertFalse(status["has_new_backup"])
        self.assertEqual(status["status_label"], "备份失败")
        self.assertIn("当前没有新备份", status["message"])
        cfg = self.read_config()
        self.assertEqual(cfg["last"]["result"], "failed")
        self.assertEqual(cfg["last_success"]["result"], "")

    def test_corrupt_config_reported_honestly(self):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text("{ 这不是 JSON", encoding="utf-8")
        status = backup_service.get_status(self.config)
        self.assertFalse(status["configured"])
        self.assertFalse(status["has_new_backup"])
        self.assertIn("无法读取", status["message"])
        self.assertIn("当前没有新备份", status["message"])


class DailyBackupTests(BackupTestBase):
    def setUp(self):
        super().setUp()
        self.write_config(auto_daily=True)
        self.calls = []

    def _runner(self, result=None):
        payload = result or {"ok": True, "result": "success"}

        def _call():
            self.calls.append(datetime.now())
            return dict(payload)

        return _call

    def test_once_per_day_then_next_day_retries(self):
        first = backup_middleware.maybe_run_daily_backup(
            background=False, now=datetime(2026, 10, 28, 9, 0, 0),
            runner=self._runner(), config_path=self.config)
        self.assertTrue(first["attempted"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.read_config()["auto_attempt"]["date"], "2026-10-28")

        again_same_day = backup_middleware.maybe_run_daily_backup(
            background=False, now=datetime(2026, 10, 28, 18, 0, 0),
            runner=self._runner(), config_path=self.config)
        self.assertFalse(again_same_day["attempted"])
        self.assertIn("已经尝试过", again_same_day["reason"])
        self.assertEqual(len(self.calls), 1)

        next_day = backup_middleware.maybe_run_daily_backup(
            background=False, now=datetime(2026, 10, 29, 9, 0, 0),
            runner=self._runner(), config_path=self.config)
        self.assertTrue(next_day["attempted"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.read_config()["auto_attempt"]["date"], "2026-10-29")

    def test_failed_attempt_not_retried_same_day(self):
        runner = self._runner({"ok": False, "result": "failed", "error": "模拟备份失败"})
        first = backup_middleware.maybe_run_daily_backup(
            background=False, now=datetime(2026, 10, 28, 9, 0, 0),
            runner=runner, config_path=self.config)
        self.assertTrue(first["attempted"])
        self.assertEqual(first["result"]["result"], "failed")
        self.assertEqual(self.read_config()["auto_attempt"]["result"], "failed")

        second = backup_middleware.maybe_run_daily_backup(
            background=False, now=datetime(2026, 10, 28, 10, 0, 0),
            runner=runner, config_path=self.config)
        self.assertFalse(second["attempted"])
        self.assertEqual(len(self.calls), 1)

    def test_auto_disabled_or_unconfigured_skips(self):
        self.write_config(auto_daily=False)
        disabled = backup_middleware.maybe_run_daily_backup(
            background=False, now=datetime(2026, 10, 28, 9, 0, 0),
            runner=self._runner(), config_path=self.config)
        self.assertFalse(disabled["attempted"])
        self.assertIn("关闭", disabled["reason"])

        self.write_config(target_dir="", auto_daily=True)
        unconfigured = backup_middleware.maybe_run_daily_backup(
            background=False, now=datetime(2026, 10, 28, 9, 0, 0),
            runner=self._runner(), config_path=self.config)
        self.assertFalse(unconfigured["attempted"])
        self.assertIn("未配置备份目标", unconfigured["reason"])
        self.assertEqual(self.calls, [])

    def test_runner_exception_is_recorded_not_raised(self):
        def _boom():
            raise RuntimeError("模拟线程内异常")

        with self.assertLogs("panels.backup_middleware", level="ERROR"):
            outcome = backup_middleware.maybe_run_daily_backup(
                background=False, now=datetime(2026, 10, 28, 9, 0, 0),
                runner=_boom, config_path=self.config)
        self.assertTrue(outcome["attempted"])
        self.assertEqual(outcome["result"]["result"], "failed")
        self.assertIn("自动备份异常", outcome["result"]["error"])


class BackupMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.response = object()

    def _call(self, path="/", method="get"):
        middleware = backup_middleware.BackupDailyMiddleware(lambda request: self.response)
        request = getattr(self.factory, method)(path)
        return middleware(request)

    def test_only_home_get_triggers(self):
        with patch.object(backup_middleware, "_is_test_runner", return_value=False), \
                patch.object(backup_middleware, "maybe_run_daily_backup") as mocked:
            self.assertIs(self._call("/"), self.response)
            self.assertEqual(mocked.call_count, 1)
            self._call("/ingestions/")
            self._call("/settings/backup/")
            self._call("/", method="post")
            self.assertEqual(mocked.call_count, 1)

    def test_no_auto_backup_while_test_runner(self):
        # 真实测试进程必须被识别，避免测试误对真实目标盘执行备份
        self.assertTrue(backup_middleware._is_test_runner())
        with patch.object(backup_middleware, "maybe_run_daily_backup") as mocked:
            self._call("/")
            self.assertEqual(mocked.call_count, 0)

    def test_trigger_exception_does_not_break_page(self):
        with patch.object(backup_middleware, "_is_test_runner", return_value=False), \
                patch.object(backup_middleware, "maybe_run_daily_backup",
                             side_effect=RuntimeError("模拟触发异常")):
            with self.assertLogs("panels.backup_middleware", level="ERROR"):
                self.assertIs(self._call("/"), self.response)


class BackupViewsTests(BackupTestBase):
    """设置页 GET/POST/立即备份：来源全部为临时合成数据。"""

    def setUp(self):
        super().setUp()
        self.overrides = override_settings(
            BACKUP_CONFIG_PATH=str(self.config),
            BACKUP_L1_WORKSPACE=str(self.l1),
        )
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)
        # 本类不需要旧备份干扰断言
        shutil.rmtree(self.old_backup, ignore_errors=True)

    def _post(self, action, **fields):
        data = {
            "target_dir": str(self.target),
            "l1_workspace": str(self.l1),
            "confirmed_external": "on",
            "auto_daily": "on",
            "action": action,
        }
        data.update(fields)
        return self.client.post("/settings/backup/", data, follow=True)

    def test_get_page_shows_unconfigured_honestly(self):
        if self.config.exists():
            self.config.unlink()
        response = self.client.get("/settings/backup/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("外接硬盘备份", content)
        self.assertIn("未配置备份目标，当前没有新备份", content)
        self.assertIn("当前没有新备份", content)
        self.assertIn("csrfmiddlewaretoken", content)
        self.assertIn("我确认这是另一块硬盘", content)

    def test_home_page_shows_backup_status_and_entry(self):
        if self.config.exists():
            self.config.unlink()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("备份设置", content)
        self.assertIn("未配置备份目标，当前没有新备份", content)
        self.assertIn("外接硬盘备份", content)

    def test_post_save_persists_config(self):
        response = self._post("save", target_dir=str(self.target))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "备份配置已保存")
        cfg = self.read_config()
        self.assertEqual(cfg["target_dir"], str(self.target))
        self.assertTrue(cfg["confirmed_external"])
        self.assertTrue(cfg["auto_daily"])
        self.assertEqual(cfg["l1_workspace"], str(self.l1))

    def test_post_check_missing_target_reports_error(self):
        missing = self.tmp / "没有接入"
        with patch.object(backup_service, "default_db_path", return_value=self.db):
            response = self._post("check", target_dir=str(missing))
        self.assertContains(response, "路径检查失败")
        self.assertContains(response, "不存在")
        self.assertFalse(missing.exists())

    def test_post_immediate_backup_with_synthetic_sources(self):
        with patch.object(backup_service, "default_db_path", return_value=self.db), \
                patch.object(backup_service, "app_dir", return_value=self.app):
            response = self._post("backup")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "备份完成")
        self.assertContains(response, "上次备份成功")
        cfg = self.read_config()
        self.assertEqual(cfg["last"]["result"], "success")
        backups = self.new_backup_dirs()
        self.assertEqual(len(backups), 1, backups)
        manifest = json.loads((backups[0] / "backup_manifest.json").read_text(encoding="utf-8"))
        # 证明用的是 patch 进去的合成库，不是真实 data/preview.sqlite3
        self.assertEqual(manifest["source"]["db_path"], str(self.db))
        self.assertEqual(manifest["source"]["app_dir"], str(self.app))
        self.assertTrue((backups[0] / "db" / "preview.sqlite3").is_file())
        self.assertEqual(_count_rows(backups[0] / "db" / "preview.sqlite3"), 4)

    def test_post_immediate_backup_unconfigured_does_not_claim_success(self):
        with patch.object(backup_service, "default_db_path", return_value=self.db):
            response = self._post("backup", target_dir="", confirmed_external="")
        content = response.content.decode("utf-8")
        self.assertIn("未配置备份目标", content)
        self.assertNotIn("备份完成", content)
        self.assertEqual(self.new_backup_dirs(), [])
