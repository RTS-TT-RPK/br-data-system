"""正式记录的唯一写入入口。

原则：
- data_json 里的成分原文是事实来源；Component 是可重建的派生数据。
- Record、Component 重建、AuditLog 在同一个数据库事务里完成。
- 原文拼不回去时默认拒绝写入（strict=True），不静默保存坏数据。
"""
from __future__ import annotations

from django.db import transaction

from panels.models import AuditLog, Component, Record, make_search_text
from panels.services.component_parser import reconcile, split_components

PARSER_VERSION = "v1"


class ComponentReconcileError(ValueError):
    """成分原文与拆分结果对不上，或拆分结果为空。"""

    def __init__(self, column_label, message):
        self.column_label = column_label
        self.message = message
        super().__init__(f"{column_label}：{message}")


def _component_columns(panel):
    return [c for c in (panel.columns or []) if c.get("type") == "components"]


def parse_components(panel, data, strict=True):
    """按面板列解析全部成分字段，返回 [(col, text, comps), ...]。"""
    parsed = []
    for col in _component_columns(panel):
        key = col.get("key")
        text = (data or {}).get(key, "")
        text = "" if text is None else str(text).strip()
        comps = split_components(text, key) if text else []
        if text and not comps:
            err = ComponentReconcileError(col.get("label") or key, "没有解析出任何成分")
            if strict:
                raise err
        if text and comps and not reconcile(text, comps):
            err = ComponentReconcileError(col.get("label") or key, "原文段拼不回去，请检查成分写法")
            if strict:
                raise err
        parsed.append((col, text, comps))
    return parsed


@transaction.atomic
def save_record(
    panel,
    data,
    record=None,
    *,
    source_type="manual",
    source_run_id="",
    source_row_id="",
    status=Record.STATUS_CONFIRMED,
    confidence=None,
    actor="preview",
    strict=True,
    audit_action=None,
):
    """创建/更新 Record + 重建 Component + 写 AuditLog，全部成功或全部回滚。"""
    data = dict(data or {})
    parsed = parse_components(panel, data, strict=strict)

    before = {}
    if record is None:
        record = Record(
            panel_key=panel.key,
            panel_version=panel.version,
            source_type=source_type,
            source_run_id=source_run_id,
            source_row_id=source_row_id,
            status=status,
            confidence=confidence,
        )
        action = audit_action or "record.create"
    else:
        # 锁住当前行，避免两个页面同时改同一条记录
        record = Record.objects.select_for_update().get(pk=record.pk)
        before = dict(record.data_json or {})
        action = audit_action or "record.update"
        record.version += 1

    record.panel_version = panel.version
    record.data_json = data
    record.search_text = make_search_text(data)
    record.component_parser_version = PARSER_VERSION
    if record.pk is None:
        record.save()
    else:
        record.save(
            update_fields=[
                "panel_version", "data_json", "search_text",
                "component_parser_version", "version", "updated_at",
            ]
        )

    # 整体重建，避免“更新成功、旧成分没删/新成分只写一半”
    Component.objects.filter(record=record).delete()
    component_rows = []
    for col, text, comps in parsed:
        for c in comps:
            component_rows.append(
                Component(
                    record=record,
                    field_key=col.get("key"),
                    component_std=c["name"],
                    component_original=c["name"],
                    numeric_value=c["numeric_value"],
                    unit=c["unit"],
                    raw_text=c["raw_text"],
                    category=c["category"],
                )
            )
    if component_rows:
        Component.objects.bulk_create(component_rows)

    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type="Record",
        entity_id=str(record.pk),
        before_json=before,
        after_json={
            "data": data,
            "component_count": len(component_rows),
            "parser_version": PARSER_VERSION,
        },
    )
    return record, component_rows
