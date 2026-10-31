"""reviewed.json（reviewed/v2）导入校验测试（契约第 3 节 / D 工作包）。

全部用内存测试库 + 临时目录里的合成样例：
- 不读取、不写入真实 runs/、inbox/、pending/、archive/、golden/、原图；
- 不写 data/preview.sqlite3（Django test 用内存库）。
"""
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.template.loader import get_template
from django.test import TestCase, override_settings

from panels.models import AuditLog, IngestionBatch, PanelDefinition, Record
from panels.services.review_import import ReviewImportError, import_reviewed

MAP_PATH = str(Path(settings.BASE_DIR) / "seeds" / "reviewed_maps.json")
RAW_SHA = "a" * 64
INVENTORY_COLUMNS = [
    "储位", "储存区域", "日期编号", "来源", "名称", "性质",
    "成分明细", "备注", "数量/液位", "体积", "重量", "总价",
]


def _inventory_row(row_id="R075", *, status="ok", image_id="测试7", component_text="THF79.9% 水11% PH8"):
    return {
        "row_id": row_id,
        "image_id": image_id,
        "sheet": "库存明细",
        "row_role": "data",
        "object_label": "THF-杂-低",
        "required_fields": [],
        "status": status,
        "raw_hash": "b" * 64,
        "fields": {
            "库位": "示例库区3#",
            "储存区域": "示例库位3#",
            "编号": "P-26-09-011",
            "物料名称": "THF-杂-低",
            "来源": "合金",
            "状态": "水高",
            "规格": "方箱",
            "性质": "杂原料",
            "数量": "3",
            "成分明细": component_text,
        },
        "components": [
            {"name": "THF", "raw_value": "79.9%", "value": "0.799", "unit": "%",
             "recognized": None, "from_human": False, "field": "成分明细", "revision_error": None},
            {"name": "水", "raw_value": "11%", "value": "0.11", "unit": "%",
             "recognized": None, "from_human": False, "field": "成分明细", "revision_error": None},
        ],
        "uncertain": [],
    }


def _reviewed(rows=None, *, deliverable=True, blocked_rows=0, status=None, **overrides):
    rows = rows if rows is not None else [_inventory_row()]
    if status is None:
        status = "deliverable" if deliverable else "draft"
    reasons = [] if status == "deliverable" else ["还有 1 条阻塞问题未解决"]
    data = {
        "schema_version": "reviewed/v2",
        "authority": "L2 review_state + row_overrides",
        "run_id": "20990101_001",
        "reviewed_at": "2026-10-28T20:00:00",
        "deliverability": {
            "status": status,
            "deliverable": deliverable,
            "blocking_issues": 0 if deliverable else 1,
            "info_issues": 6,
            "blocked_rows": blocked_rows,
            "stale_decisions": 0,
            "revision_errors": 0,
            "reasons": reasons,
        },
        "source": {
            "workspace": "C:/tmp/l1-fixture",
            "created": "2026-10-14 23:03:32",
            "raw_sha256": RAW_SHA,
            "raw_run_id": "20990101_001",
        },
        "stats": {"images": 1, "rows": len(rows), "sheets": 1, "issues": 0,
                  "blocking_issues": 0, "info_issues": 0, "resolved_issues": 0,
                  "blocked_rows": blocked_rows, "components": 2},
        "images": [{"image_id": "测试7", "file": "runs/20990101_001/source/测试7.png",
                    "sha256": "c" * 64, "sheet_label": "库存明细", "date_text": "",
                    "url": "/source?run_id=20990101_001&image_id=测试7"}],
        "sheets": [{"name": "库存明细", "columns": ["库位", "储存区域", "编号", "物料名称"],
                    "row_count": len(rows)}],
        "rows": rows,
        "issues": [],
        "decisions": {"issue_decisions": {}, "row_overrides": {}, "archive_decisions": {}},
        "archive_plan": {},
    }
    data.update(overrides)
    return data


class ReviewImportTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        PanelDefinition.objects.create(
            key="br_inventory",
            name="物料库存",
            row_unit="一行 = 一个对象",
            columns=[
                {"key": key, "label": key, "type": "components" if key == "成分明细" else "text"}
                for key in INVENTORY_COLUMNS
            ],
            form_layout=[],
            measurements={"enabled": True, "unit": "%", "sum_max": 100},
            actions=[],
            permissions={},
            version=1,
            status="published",
        )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, reviewed, name="reviewed.json"):
        path = Path(self._tmp.name) / name
        path.write_text(json.dumps(reviewed, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def _call(self, reviewed, *, dry_run=False, include_blocked=False, force_reason="", mapping=MAP_PATH):
        out, err = StringIO(), StringIO()
        call_command(
            "import_reviewed",
            reviewed=self._write(reviewed),
            mapping=mapping,
            dry_run=dry_run,
            include_blocked=include_blocked,
            force_reason=force_reason,
            stdout=out,
            stderr=err,
        )
        return out.getvalue(), err.getvalue()

    def _call_error(self, reviewed, **kwargs):
        with self.assertRaises(CommandError) as ctx:
            self._call(reviewed, **kwargs)
        return str(ctx.exception)

    def assert_no_batch(self):
        self.assertEqual(IngestionBatch.objects.count(), 0)


class ValidV2Tests(ReviewImportTestBase):
    def test_dry_run_succeeds_and_creates_nothing(self):
        out, _err = self._call(_reviewed(), dry_run=True)
        self.assertIn("可交付（deliverable）", out)
        self.assertIn("库存明细 -> br_inventory", out)
        self.assertIn("可导入 1 行", out)
        self.assertIn("dry-run", out)
        self.assertIn("R075", out)  # 预览行带 row_key
        self.assert_no_batch()
        self.assertEqual(Record.objects.count(), 0)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_import_creates_pending_draft_with_source_tracking(self):
        out, _err = self._call(_reviewed())
        self.assertIn("待审核", out)
        batch = IngestionBatch.objects.get(source_run_id="20990101_001")
        self.assertEqual(batch.status, "pending")
        self.assertEqual(batch.rows.count(), 1)
        row = batch.rows.get()
        self.assertEqual(row.row_key, "R075")
        self.assertEqual(row.status, "draft")
        self.assertEqual(
            row.raw_json["source"],
            {"run_id": "20990101_001", "sheet": "库存明细", "row_id": "R075",
             "image_id": "测试7", "reviewed_status": "ok"},
        )
        self.assertEqual(batch.payload["source"]["run_id"], "20990101_001")
        self.assertEqual(batch.payload["source"]["raw_sha256"], RAW_SHA)
        self.assertEqual(batch.payload["source"]["schema_version"], "reviewed/v2")
        self.assertTrue(AuditLog.objects.filter(action="ingestion.import_reviewed").exists())
        # 导入只是草稿：不创建正式 Record
        self.assertEqual(Record.objects.count(), 0)

    def test_approve_keeps_non_component_tokens_in_text(self):
        """成分明细里的 PH8 不是计量项，但导入不能把它丢掉（契约 1.2.1 规则 4）。"""
        self._call(_reviewed())
        batch = IngestionBatch.objects.get(source_run_id="20990101_001")
        self.client.post(f"/ingestions/{batch.pk}/approve/", {})
        rec = Record.objects.filter(panel_key="br_inventory", source_run_id="20990101_001").latest("id")
        self.assertEqual(rec.data_json["成分明细"], "THF79.9% 水11% PH8")
        names = {c.component_std for c in rec.components.all()}
        self.assertEqual(names, {"THF", "水", "PH"})
        ph = rec.components.get(component_std="PH")
        self.assertEqual(ph.category, "other")

    def test_import_is_idempotent(self):
        self._call(_reviewed())
        out, _err = self._call(_reviewed())
        self.assertEqual(IngestionBatch.objects.count(), 1)
        self.assertIn("相同内容已存在", out)

    def test_unmapped_sheet_is_counted_but_not_imported(self):
        extra = _inventory_row("R090", image_id="测试3")
        extra["sheet"] = "进料记录"
        extra["fields"] = {"日期": "2026-09-23", "塔号": "6#", "原料名称": "THF",
                           "数量": "12", "进料时间": "8:00", "原料贮存位置": "示例库区3#", "备注": ""}
        extra["components"] = []
        reviewed = _reviewed([_inventory_row(), extra])
        out, _err = self._call(reviewed, dry_run=True)
        self.assertIn("未映射工作表（不导入）：进料记录 1 行", out)
        self.assert_no_batch()

    def test_duplicate_row_id_across_sheets_is_allowed(self):
        extra = _inventory_row("R075", image_id="测试3")
        extra["sheet"] = "进料记录"
        extra["fields"] = {"日期": "2026-09-23", "塔号": "6#", "原料名称": "THF",
                           "数量": "12", "进料时间": "8:00", "原料贮存位置": "示例库区3#", "备注": ""}
        extra["components"] = []
        out, _err = self._call(_reviewed([_inventory_row("R075"), extra]), dry_run=True)
        self.assertIn("可导入 1 行", out)
        self.assert_no_batch()

    def test_default_skips_blocked_rows_and_counts_them(self):
        blocked = _inventory_row("R076", status="blocked")
        reviewed = _reviewed([_inventory_row(), blocked], deliverable=True, blocked_rows=1)
        out, _err = self._call(reviewed)
        self.assertIn("跳过阻塞 1 行", out)
        batch = IngestionBatch.objects.get(source_run_id="20990101_001")
        self.assertEqual(batch.rows.count(), 1)
        self.assertEqual(batch.rows.get().row_key, "R075")
        self.assertIn("跳过阻塞 1 行", batch.note)

    def test_review_page_shows_source_run_and_l2_link(self):
        self._call(_reviewed())
        batch = IngestionBatch.objects.get(source_run_id="20990101_001")
        with override_settings(REVIEW_WEB_URL="http://127.0.0.1:9999/"):
            resp = self.client.get(f"/ingestions/{batch.pk}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("20990101_001", html)
        self.assertIn("去 L2 核对原图", html)
        self.assertIn("http://127.0.0.1:9999/?run_id=20990101_001", html)
        self.assertIn("库存明细 · R075", html)
        self.assertIn("测试7", html)

    def test_review_page_does_not_render_link_without_review_web_url(self):
        self._call(_reviewed())
        batch = IngestionBatch.objects.get(source_run_id="20990101_001")
        rows = [{"row": row, "comps": [], "issues": []} for row in batch.rows.all()]
        html = get_template("panels/ingestion_review.html").render(
            {"batch": batch, "panel": None, "rows": rows}
        )
        self.assertIn("入库批次", html)
        self.assertNotIn("去 L2 核对原图", html)
        self.assertNotIn("?run_id=", html)


class DraftGateTests(ReviewImportTestBase):
    def test_formal_import_rejects_draft_and_lists_reasons(self):
        reviewed = _reviewed(deliverable=False, status="draft")
        message = self._call_error(reviewed)
        self.assertIn("不可交付", message)
        self.assertIn("还有 1 条阻塞问题未解决", message)
        self.assert_no_batch()

    def test_dry_run_allows_draft_but_prints_reasons(self):
        reviewed = _reviewed(deliverable=False, status="draft")
        out, _err = self._call(reviewed, dry_run=True)
        self.assertIn("不可交付（draft）", out)
        self.assertIn("还有 1 条阻塞问题未解决", out)
        self.assert_no_batch()

    def test_deliverable_flag_must_be_bool(self):
        reviewed = _reviewed()
        reviewed["deliverability"]["deliverable"] = "true"
        message = self._call_error(reviewed, dry_run=True)
        self.assertIn("必须是 true/false 布尔值", message)

    def test_deliverable_and_status_must_agree(self):
        reviewed = _reviewed()
        reviewed["deliverability"]["deliverable"] = False
        message = self._call_error(reviewed, dry_run=True)
        self.assertIn("自相矛盾", message)


class VersionAndIdentityTests(ReviewImportTestBase):
    def _legacy_reviewed(self):
        return {
            "schema_version": "reviewed/v1",
            "run_id": "20990101_001",
            "source": {"workspace": "C:/tmp/l1-fixture", "raw_sha256": RAW_SHA},
            "rows": [_inventory_row()],
        }

    def test_v1_formal_import_is_rejected(self):
        message = self._call_error(self._legacy_reviewed())
        self.assertIn("旧格式", message)
        self.assertIn("reviewed/v2", message)
        self.assert_no_batch()

    def test_v1_dry_run_is_preview_only(self):
        out, _err = self._call(self._legacy_reviewed(), dry_run=True)
        self.assertIn("旧格式", out)
        self.assertIn("不能正式导入", out)
        self.assertIn("库存明细", out)
        self.assert_no_batch()

    def test_unknown_schema_version_rejected(self):
        reviewed = _reviewed(schema_version="reviewed/v9")
        message = self._call_error(reviewed, dry_run=True)
        self.assertIn("未知", message)
        self.assert_no_batch()

    def test_missing_run_id_rejected(self):
        reviewed = _reviewed()
        reviewed.pop("run_id")
        message = self._call_error(reviewed, dry_run=True)
        self.assertIn("缺少 run_id", message)

    def test_bad_source_sha256_rejected(self):
        for value in (None, "", "abc", "z" * 64):
            with self.subTest(value=value):
                reviewed = _reviewed()
                if value is None:
                    reviewed.pop("source")
                else:
                    reviewed["source"]["raw_sha256"] = value
                message = self._call_error(reviewed, dry_run=True)
                self.assertIn("source", message)
                self.assert_no_batch()


class RowValidationTests(ReviewImportTestBase):
    def test_missing_row_status_is_error_not_ok(self):
        row = _inventory_row()
        row.pop("status")
        message = self._call_error(_reviewed([row]), dry_run=True)
        self.assertIn("status", message)
        self.assertIn("缺状态", message)
        self.assert_no_batch()

    def test_invalid_row_status_rejected(self):
        row = _inventory_row()
        row["status"] = "pending"
        message = self._call_error(_reviewed([row]), dry_run=True)
        self.assertIn("status", message)

    def test_duplicate_row_id_in_same_sheet_rejected(self):
        message = self._call_error(
            _reviewed([_inventory_row("R075"), _inventory_row("R075")]), dry_run=True
        )
        self.assertIn("row_id 重复", message)
        self.assert_no_batch()

    def test_missing_mapped_source_field_rejected(self):
        row = _inventory_row()
        row["fields"].pop("库位")
        message = self._call_error(_reviewed([row]), dry_run=True)
        self.assertIn("库位", message)
        self.assertIn("不存在", message)

    def test_required_source_field_empty_rejected(self):
        row = _inventory_row()
        row["fields"]["物料名称"] = ""
        message = self._call_error(_reviewed([row]), dry_run=True)
        self.assertIn("必填源字段", message)
        self.assertIn("物料名称", message)

    def test_illegal_component_unit_rejected(self):
        row = _inventory_row()
        row["components"][0]["unit"] = "斤"
        message = self._call_error(_reviewed([row]), dry_run=True)
        self.assertIn("单位", message)
        self.assertIn("斤", message)

    def test_value_must_match_raw_value(self):
        row = _inventory_row()  # 原文 79.9%，value 却是 0.5：典型的 5% / 0.5 混用
        row["components"][0]["value"] = "0.5"
        message = self._call_error(_reviewed([row]), dry_run=True)
        self.assertIn("数值对不上", message)

    def test_component_text_must_match_components(self):
        row = _inventory_row(component_text="THF88% 水11% PH8")
        message = self._call_error(_reviewed([row]), dry_run=True)
        self.assertIn("成分原文与 components 不一致", message)

    def test_blocked_rows_declaration_mismatch_rejected(self):
        row = _inventory_row("R075", status="blocked")
        reviewed = _reviewed([row], deliverable=False, status="draft", blocked_rows=0)
        message = self._call_error(reviewed, dry_run=True)
        self.assertIn("blocked_rows", message)
        self.assertIn("不符", message)

    def test_no_rows_rejected(self):
        message = self._call_error(_reviewed([]), dry_run=True)
        self.assertIn("rows", message)

    def test_unknown_panel_in_mapping_rejected(self):
        mapping = json.loads(Path(MAP_PATH).read_text(encoding="utf-8"))
        mapping["sheets"]["库存明细"]["panel_key"] = "br_not_exists"
        map_path = Path(self._tmp.name) / "mapping.json"
        map_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        message = self._call_error(_reviewed(), dry_run=True, mapping=str(map_path))
        self.assertIn("不存在的面板", message)

    def test_mapping_target_not_in_panel_columns_rejected(self):
        mapping = json.loads(Path(MAP_PATH).read_text(encoding="utf-8"))
        mapping["sheets"]["库存明细"]["fields"]["不存在的列"] = "物料名称"
        map_path = Path(self._tmp.name) / "mapping.json"
        map_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        message = self._call_error(_reviewed(), dry_run=True, mapping=str(map_path))
        self.assertIn("不在面板", message)


class ForceIncludeBlockedTests(ReviewImportTestBase):
    def _reviewed_with_blocked(self):
        blocked = _inventory_row("R076", status="blocked")
        return _reviewed([_inventory_row(), blocked], deliverable=True, blocked_rows=1)

    def test_include_blocked_requires_force_reason(self):
        message = self._call_error(self._reviewed_with_blocked(), include_blocked=True)
        self.assertIn("force-reason", message)
        self.assert_no_batch()

    def test_service_rejects_include_blocked_without_reason(self):
        path = self._write(self._reviewed_with_blocked())
        with self.assertRaises(ReviewImportError) as ctx:
            import_reviewed(path, MAP_PATH, include_blocked=True)
        self.assertIn("force-reason", str(ctx.exception))
        self.assert_no_batch()

    def test_include_blocked_with_reason_marks_rows_and_audit(self):
        out, _err = self._call(
            self._reviewed_with_blocked(),
            include_blocked=True,
            force_reason="现场已确认，先导入草稿再核对",
        )
        self.assertIn("风险提示", out)
        self.assertIn("现场已确认，先导入草稿再核对", out)
        batch = IngestionBatch.objects.get(source_run_id="20990101_001")
        self.assertEqual(batch.rows.count(), 2)
        self.assertIn("强制包含阻塞行：现场已确认，先导入草稿再核对", batch.note)
        forced = batch.rows.get(row_key="R076")
        self.assertTrue(forced.raw_json["source"]["forced_blocked"])
        self.assertTrue(any("阻塞" in text for text in forced.issues))
        self.assertTrue(
            AuditLog.objects.filter(
                action="ingestion.import_reviewed.force_blocked",
                after_json__isnull=False,
            ).exists()
        )
        audit = AuditLog.objects.get(action="ingestion.import_reviewed.force_blocked")
        self.assertEqual(audit.after_json["reason"], "现场已确认，先导入草稿再核对")
        self.assertIn("强制包含阻塞行：现场已确认，先导入草稿再核对", audit.after_json["note"])
        self.assertEqual(audit.after_json["row_ids"], ["R076"])
        # 强制导入仍然只是草稿，不创建正式记录
        self.assertEqual(Record.objects.count(), 0)
