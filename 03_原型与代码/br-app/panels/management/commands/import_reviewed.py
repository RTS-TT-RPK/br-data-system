from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from panels.services.review_import import ReviewImportError, import_reviewed


class Command(BaseCommand):
    help = "把 L2 的 reviewed.json（reviewed/v2）按映射配置校验后导入为 L3 待审核草稿（不直接写正式记录）"

    def add_arguments(self, parser):
        parser.add_argument("--reviewed", required=True, help="reviewed.json 路径（正式导入要求 reviewed/v2 且可交付）")
        parser.add_argument("--map", dest="mapping", default=None, help="映射 JSON 路径，默认 seeds/reviewed_maps.json")
        parser.add_argument("--dry-run", action="store_true", help="只校验并打印交付状态和诊断，不创建批次")
        parser.add_argument(
            "--include-blocked",
            action="store_true",
            help="强制连 L2 阻塞行也导入（必须同时给出 --force-reason；风险自负，行上会标记阻塞来源）",
        )
        parser.add_argument(
            "--force-reason",
            default="",
            help="强制包含阻塞行的原因（写进批次 note 与审计日志，便于追查）",
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        include_blocked = bool(options["include_blocked"])
        force_reason = (options.get("force_reason") or "").strip()

        if include_blocked and not force_reason:
            raise CommandError(
                "--include-blocked 必须同时给出 --force-reason \"原因\"：强制包含 L2 阻塞行有风险，"
                "原因会写进批次 note 和审计日志，供以后追查。"
            )

        try:
            report, map_path = import_reviewed(
                options["reviewed"],
                options["mapping"],
                dry_run=dry_run,
                include_blocked=include_blocked,
                actor="import_reviewed",
                force_reason=force_reason,
            )
        except ReviewImportError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"映射：{map_path}")
        self.stdout.write(f"模式：{'dry-run（只校验，不建批次）' if dry_run else '创建待审核草稿批次'}")
        self.stdout.write(
            f"文件版本：{report.get('schema_version')}；任务：{report.get('run_id') or '（缺失）'}；"
            f"来源 raw_sha256：{report.get('raw_sha256') or '（缺失）'}"
        )

        status = report.get("status") or ""
        if status == "legacy-preview":
            self.stdout.write("交付状态：旧格式预览（不可交付）")
        elif report.get("deliverable") and status == "deliverable":
            self.stdout.write(self.style.SUCCESS("交付状态：可交付（deliverable）"))
        else:
            self.stdout.write(self.style.WARNING(f"交付状态：不可交付（{status or '未知'}）"))
            reasons = report.get("reasons") or []
            if reasons:
                for reason in reasons:
                    self.stdout.write(f"  - 不可交付原因：{reason}")
            else:
                self.stdout.write("  - 不可交付原因：文件没有给出 reasons")

        for warning in report.get("warnings") or []:
            self.stdout.write(self.style.WARNING(f"提示：{warning}"))

        if report.get("sheets"):
            self.stdout.write("工作表：")
        for item in report.get("sheets") or []:
            line = (
                f"  {item['sheet']} -> {item['panel_key']}：共 {item['total']} 行，"
                f"可导入 {item['imported']} 行，跳过阻塞 {item['skipped']} 行"
            )
            if item.get("forced"):
                line += f"，其中强制包含阻塞 {item['forced']} 行"
            if item.get("duplicate"):
                line += f"；相同内容已存在，返回批次 #{item['batch_id']}（未重复创建）"
            elif item.get("batch_id"):
                line += f"；批次 #{item['batch_id']}（待审核）"
            elif item.get("imported"):
                line += "；dry-run 未创建批次"
            else:
                line += "；没有可导入的行"
            self.stdout.write(line)
            if item.get("payload"):
                first = item["payload"]["rows"][0]
                src = first.get("source") or {}
                self.stdout.write(
                    f"    预览首行：row_key={first.get('row_key')} 来源任务={src.get('run_id')} "
                    f"图片={src.get('image_id') or '（无）'} fields={first.get('fields')}"
                )
            if item.get("batch_id") and not item.get("duplicate"):
                self.stdout.write(f"    审核页：/ingestions/{item['batch_id']}/")

        for item in report.get("unmapped") or []:
            self.stdout.write(f"  未映射工作表（不导入）：{item['sheet']} {item['rows']} 行")

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS("dry-run：校验通过，未创建任何批次、未写任何正式记录。")
            )
            return

        created = [i for i in report["sheets"] if i.get("batch_id") and not i.get("duplicate")]
        duplicates = [i for i in report["sheets"] if i.get("duplicate")]
        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f"已创建 {len(created)} 个待审核草稿批次；命令不会直接写正式记录，"
                    "仍需在 L3 识别入库记录页人工确认。"
                )
            )
        elif duplicates:
            self.stdout.write("相同内容已存在，未重复创建批次（幂等返回）。")
        else:
            self.stdout.write(self.style.WARNING("没有创建任何批次：没有可导入的行。"))
        if include_blocked and any(i.get("forced") for i in report["sheets"]):
            self.stdout.write(self.style.WARNING(f"风险提示：本次强制包含 L2 阻塞行（原因：{force_reason}）。"))
            self.stdout.write(
                self.style.WARNING(
                    "这些行已标记“来源行在 L2 为阻塞状态、被强制导入”，确认前必须逐行核对；"
                    "审计日志记录了原因。"
                )
            )
