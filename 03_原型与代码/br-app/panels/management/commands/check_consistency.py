"""检查 Record.data_json 成分原文与 Component 派生行是否一致。

用法：
    python manage.py check_consistency
    python manage.py check_consistency --limit 200
退出码：0 = 无问题；1 = 有问题。
"""
from django.core.management.base import BaseCommand

from panels.models import IngestionBatch, PanelDefinition, Record
from panels.services.component_parser import reconcile


class Command(BaseCommand):
    help = "检查正式记录 / 成分行 / 接入批次的一致性"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500, help="最多输出多少条问题")
        parser.add_argument("--fail-on-duplicates", action="store_true", help="重复成分也按失败处理")

    def handle(self, *args, **options):
        issues = []
        warnings = []
        panels = {p.key: p for p in PanelDefinition.objects.all()}
        records = (
            Record.objects.exclude(status=Record.STATUS_DELETED)
            .prefetch_related("components")
            .order_by("id")
        )
        for rec in records.iterator(chunk_size=200):
            panel = panels.get(rec.panel_key)
            if not panel:
                issues.append(f"Record#{rec.pk}: 面板 {rec.panel_key} 不存在")
                continue
            comps = list(rec.components.all())
            for col in panel.columns or []:
                if col.get("type") != "components":
                    continue
                key = col.get("key")
                text = (rec.data_json or {}).get(key, "") or ""
                field_comps = [c for c in comps if c.field_key == key]
                if text and not field_comps:
                    issues.append(f"Record#{rec.pk}: 「{key}」有原文但没有成分行")
                elif not text and field_comps:
                    issues.append(f"Record#{rec.pk}: 「{key}」没有原文但有 {len(field_comps)} 条成分行")
                elif text and field_comps and not reconcile(text, [{"raw_text": c.raw_text} for c in field_comps]):
                    issues.append(f"Record#{rec.pk}: 「{key}」成分行拼不回原文")
                names = [c.component_std for c in field_comps]
                dup = sorted({n for n in names if names.count(n) > 1})
                if dup:
                    target = issues if options["fail_on_duplicates"] else warnings
                    target.append(f"Record#{rec.pk}: 「{key}」重复成分 {', '.join(dup)}（原文本身可能含多段，需人工判定）")
            if not (rec.search_text or "").strip():
                issues.append(f"Record#{rec.pk}: search_text 为空")

        approved_without_record = IngestionBatch.objects.filter(
            status="approved", rows__status="approved", rows__record__isnull=True
        ).count()
        if approved_without_record:
            issues.append(f"接入批次：{approved_without_record} 条已通过行没有关联正式记录")

        for line in issues[: max(1, options["limit"])]:
            self.stdout.write(self.style.ERROR("FAIL  " + line))
        for line in warnings[: max(1, options["limit"])]:
            self.stdout.write(self.style.WARNING("WARN  " + line))
        if issues:
            self.stdout.write(self.style.ERROR(f"发现 {len(issues)} 个结构性问题、{len(warnings)} 个提示"))
            raise SystemExit(1)
        if warnings:
            self.stdout.write(self.style.WARNING(f"一致性结构通过；另有 {len(warnings)} 个提示（重复成分等），建议人工核对"))
        else:
            self.stdout.write(self.style.SUCCESS(f"一致性检查通过：{records.count()} 条记录"))
