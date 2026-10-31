"""导入面板定义与 legacy 主数据，生成可浏览的预览数据。

用法：
    python manage.py seed_preview
    python manage.py seed_preview --reset
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from panels.models import Component, IngestionBatch, PanelDefinition, Record, make_search_text
from panels.services.component_parser import reconcile, split_components, component_sum_percent

SEED_DIR = Path(settings.BASE_DIR) / "seeds"


class Command(BaseCommand):
    help = "导入面板定义与 legacy 种子数据（预览版）"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="清空记录/成分后重新导入")

    def handle(self, *args, **options):
        if options["reset"]:
            Component.objects.all().delete()
            Record.objects.all().delete()
            IngestionBatch.objects.all().delete()
            self.stdout.write("已清空记录、成分与接入批次")

        panels = json.loads((SEED_DIR / "panels.json").read_text(encoding="utf-8"))
        for p in panels:
            PanelDefinition.objects.update_or_create(
                key=p["key"],
                defaults={
                    "name": p["name"],
                    "row_unit": p.get("row_unit", ""),
                    "style": p.get("style", "br_table"),
                    "columns": p.get("columns", []),
                    "form_layout": p.get("form_layout", []),
                    "measurements": p.get("measurements", {}),
                    "actions": p.get("actions", []),
                    "permissions": p.get("permissions", {}),
                    "version": p.get("version", 1),
                    "status": p.get("status", "published"),
                },
            )
        self.stdout.write(f"面板定义：{len(panels)} 个")

        if Record.objects.exists():
            self.stdout.write(self.style.WARNING("已有记录，跳过数据导入；需要重导请加 --reset"))
            return

        data = json.loads((SEED_DIR / "legacy_data.json").read_text(encoding="utf-8"))
        total_records = 0
        total_components = 0
        issue_rows = 0
        for panel_key, rows in data.items():
            panel = PanelDefinition.objects.filter(key=panel_key).first()
            if not panel:
                continue
            component_cols = [c["key"] for c in panel.columns if c.get("type") == "components"]
            objs = []
            for row in rows:
                objs.append(
                    Record(
                        panel_key=panel_key,
                        panel_version=panel.version,
                        data_json=row,
                        search_text=make_search_text(row),
                        source_type="seed",
                        status=Record.STATUS_CONFIRMED,
                    )
                )
            Record.objects.bulk_create(objs, batch_size=200)
            total_records += len(objs)
            if component_cols:
                for rec in Record.objects.filter(panel_key=panel_key).order_by("id"):
                    for col in component_cols:
                        text = (rec.data_json or {}).get(col, "")
                        if not text:
                            continue
                        comps = split_components(text, col)
                        if not reconcile(text, comps):
                            issue_rows += 1
                        Component.objects.bulk_create(
                            [
                                Component(
                                    record=rec,
                                    field_key=col,
                                    component_std=c["name"],
                                    component_original=c["name"],
                                    numeric_value=c["numeric_value"],
                                    unit=c["unit"],
                                    raw_text=c["raw_text"],
                                    category=c["category"],
                                )
                                for c in comps
                            ],
                            batch_size=200,
                        )
                        total_components += len(comps)
            self.stdout.write(f"  {panel.name}（{panel_key}）：{len(objs)} 行")

        self.stdout.write(
            self.style.SUCCESS(
                f"完成：{total_records} 条记录，{total_components} 条成分；"
                f"原文对账不通过 {issue_rows} 行（不阻断预览，可在页面查看）"
            )
        )
        # 简单对账统计
        bad_sum = 0
        for rec in Record.objects.filter(panel_key="br_inventory"):
            comps = list(rec.components.all())
            if comps and component_sum_percent(comps) > 100.5:
                bad_sum += 1
        if bad_sum:
            self.stdout.write(self.style.WARNING(f"成分合计超过 100.5% 的库存行：{bad_sum}（legacy 测试数据，仅提示）"))
