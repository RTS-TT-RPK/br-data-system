from __future__ import annotations

import csv
import json
import re
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import AuditLog, Component, IngestionBatch, IngestionRow, PanelDefinition, Record
from .services.component_parser import (
    component_sum_percent,
    components_from_payload,
    reconcile,
)
from .services.ingestion import IngestionPayloadError, payload_fingerprint, validate_payload
from .services.records import ComponentReconcileError, save_record


def _panel(key):
    return get_object_or_404(PanelDefinition, key=key)


def _component_dicts(record):
    return [
        {
            "name": c.component_std or c.component_original,
            "raw_text": c.raw_text,
            "numeric_value": c.numeric_value,
            "unit": c.unit,
            "category": c.category,
            "display": c.display_value(),
        }
        for c in record.components.all()
    ]


def _sum_ok(comps, panel):
    if not comps:
        return True
    max_sum = float((panel.measurements or {}).get("sum_max", 100.0))
    return component_sum_percent(comps) <= max_sum + 0.5


def _display_value(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def home(request):
    counts = dict(
        Record.objects.exclude(status=Record.STATUS_DELETED)
        .values_list("panel_key")
        .annotate(n=Count("id"))
    )
    panels = []
    for p in PanelDefinition.objects.all():
        panels.append({"panel": p, "count": counts.get(p.key, 0)})
    pending = IngestionBatch.objects.filter(status="pending").count()
    comp_count = Component.objects.count()
    record_total = Record.objects.exclude(status=Record.STATUS_DELETED).count()
    priority = {"br_material_property": 0, "br_inventory": 1, "incoming_analysis": 2}
    panels.sort(key=lambda x: (priority.get(x["panel"].key, 99), x["panel"].name))
    return render(
        request,
        "panels/home.html",
        {
            "panels": panels,
            "pending": pending,
            "component_count": comp_count,
            "record_total": record_total,
            "panel_count": len(panels),
        },
    )


def panel_table(request, panel_key):
    panel = _panel(panel_key)
    qs = (
        Record.objects.filter(panel_key=panel_key)
        .exclude(status=Record.STATUS_DELETED)
        .prefetch_related("components")
        .order_by("id")
    )
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(search_text__icontains=q))
    status = request.GET.get("status") or ""
    if status:
        qs = qs.filter(status=status)
    total = qs.count()
    page_obj = Paginator(qs, 50).get_page(request.GET.get("page") or 1)

    rows = []
    for rec in page_obj.object_list:
        comps = _component_dicts(rec)
        cells = []
        for col in panel.columns or []:
            value = _display_value((rec.data_json or {}).get(col["key"], ""))
            cell = {"col": col, "value": value, "components": None, "sum": None, "bad": False}
            if col.get("type") == "components":
                cell["components"] = comps
                if comps:
                    cell["sum"] = round(component_sum_percent(comps), 3)
                    cell["bad"] = not _sum_ok(comps, panel)
            cells.append(cell)
        rows.append({"record": rec, "cells": cells})

    return render(
        request,
        "panels/panel_table.html",
        {"panel": panel, "rows": rows, "page_obj": page_obj, "total": total, "q": q, "status": status},
    )


def _form_fields(panel, values=None):
    values = values or {}
    return [
        {
            "col": col,
            "value": values.get(col["key"], ""),
            "is_components": col.get("type") == "components",
        }
        for col in panel.columns or []
    ]


def panel_export(request, panel_key):
    """把当前筛选结果导出成 CSV（Excel 可直接打开，带 BOM）。"""
    panel = _panel(panel_key)
    qs = Record.objects.filter(panel_key=panel_key).exclude(status=Record.STATUS_DELETED).order_by("id")
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(search_text__icontains=q))
    status = request.GET.get("status") or ""
    if status:
        qs = qs.filter(status=status)
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="{panel_key}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow([c.get("label", c.get("key")) for c in (panel.columns or [])])
    for rec in qs.iterator():
        writer.writerow([(rec.data_json or {}).get(c.get("key"), "") for c in (panel.columns or [])])
    return response


def record_new(request, panel_key):
    panel = _panel(panel_key)
    if request.method == "POST":
        data = {col["key"]: (request.POST.get(col["key"]) or "").strip() for col in panel.columns or []}
        try:
            save_record(panel, data, source_type="manual", actor="human", strict=True)
        except ComponentReconcileError as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "panels/panel_form.html",
                {"panel": panel, "record": None, "fields": _form_fields(panel, data)},
            )
        messages.success(request, "已新增一行")
        return redirect("panel_table", panel_key=panel_key)
    return render(request, "panels/panel_form.html", {"panel": panel, "record": None, "fields": _form_fields(panel)})


def record_edit(request, panel_key, pk):
    panel = _panel(panel_key)
    rec = get_object_or_404(Record, pk=pk, panel_key=panel_key)
    if request.method == "POST":
        data = {col["key"]: (request.POST.get(col["key"]) or "").strip() for col in panel.columns or []}
        try:
            rec, _ = save_record(panel, data, record=rec, actor="human", strict=True)
        except ComponentReconcileError as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "panels/panel_form.html",
                {
                    "panel": panel,
                    "record": rec,
                    "fields": _form_fields(panel, data),
                    "components": _component_dicts(rec),
                },
            )
        messages.success(request, "已保存")
        return redirect("panel_table", panel_key=panel_key)
    return render(
        request,
        "panels/panel_form.html",
        {
            "panel": panel,
            "record": rec,
            "fields": _form_fields(panel, rec.data_json or {}),
            "components": _component_dicts(rec),
        },
    )


def record_detail(request, panel_key, pk):
    panel = _panel(panel_key)
    rec = get_object_or_404(Record, pk=pk, panel_key=panel_key)
    fields = [
        {"col": col, "value": _display_value((rec.data_json or {}).get(col["key"], ""))}
        for col in panel.columns or []
    ]
    return render(
        request,
        "panels/record_detail.html",
        {"panel": panel, "record": rec, "fields": fields, "components": _component_dicts(rec)},
    )


def record_delete(request, panel_key, pk):
    rec = get_object_or_404(Record, pk=pk, panel_key=panel_key)
    if request.method == "POST":
        rec.status = Record.STATUS_DELETED
        rec.save(update_fields=["status", "updated_at"])
        AuditLog.objects.create(action="record.delete", entity_type="Record", entity_id=str(rec.pk))
        messages.success(request, "已删除（软删除，可查审计）")
    return redirect("panel_table", panel_key=panel_key)


def ingestion_list(request):
    batches = list(IngestionBatch.objects.annotate(row_count=Count("rows")).all())
    panel_map = dict(PanelDefinition.objects.values_list("key", "name"))
    for b in batches:
        b.panel_name = panel_map.get(b.panel_key, b.panel_key)
    return render(request, "panels/ingestion_list.html", {"batches": batches})


def _build_simulated_batch():
    records = (
        Record.objects.filter(panel_key="br_inventory", status=Record.STATUS_CONFIRMED)
        .prefetch_related("components")
        .order_by("id")
    )
    rows = []
    for rec in records:
        comps = list(rec.components.all())
        if not comps:
            continue
        d = rec.data_json or {}
        fields = {
            "日期": d.get("日期编号") or "2026-10-27",
            "位置": d.get("储位", ""),
            "物料": d.get("名称", ""),
            "批次": d.get("日期编号") or f"模拟-{rec.pk}",
            "桶号": d.get("储位", ""),
            "厂商": d.get("来源", ""),
            "成分明细": d.get("成分明细", ""),
            "备注": "旧数据模拟（假设为插件读图结果）",
        }
        rows.append(
            {
                "row_key": f"R{rec.pk:04d}",
                "fields": fields,
                "components": [
                    {"name": c.component_std or c.component_original, "raw_value": c.raw_text}
                    for c in comps
                ],
                "uncertain": [],
                "confidence": 0.9,
            }
        )
    return rows


def ingestion_simulate(request):
    rows = _build_simulated_batch()
    if not rows:
        messages.warning(request, "没有带成分的库存行可模拟；请先导入种子数据")
        return redirect("ingestion_list")

    payload = {"panel_key": "incoming_analysis", "rows": rows}
    fingerprint = payload_fingerprint(payload)
    existing = IngestionBatch.objects.filter(
        payload_hash=fingerprint, status__in=["pending", "approved"]
    ).first()
    if existing:
        messages.info(request, "相同内容的模拟批次已存在，直接打开审核页，未重复生成")
        return redirect("ingestion_review", pk=existing.pk)

    seq = IngestionBatch.objects.filter(source_run_id__startswith="sim-legacy-").count() + 1
    run_id = f"sim-legacy-{seq:03d}"
    with transaction.atomic():
        batch = IngestionBatch.objects.create(
            panel_key="incoming_analysis",
            source_type="simulation",
            source_name="旧 BR 数据库（模拟插件）",
            source_run_id=run_id,
            payload=payload,
            payload_hash=fingerprint,
            status="pending",
            note="把旧数据库物料库存里带成分的行，当作插件从图片读出来的来料分析数据（演示批次）",
        )
        IngestionRow.objects.bulk_create(
            [
                IngestionRow(
                    batch=batch,
                    row_key=r.get("row_key", ""),
                    raw_json=r,
                    mapped_json=r.get("fields", {}),
                    status="draft",
                )
                for r in rows
            ]
        )
    messages.success(request, f"已生成模拟批次：{len(rows)} 行，待审核")
    return redirect("ingestion_review", pk=batch.pk)


def ingestion_review(request, pk):
    batch = get_object_or_404(IngestionBatch, pk=pk)
    panel = PanelDefinition.objects.filter(key=batch.panel_key).first()
    rows = []
    for row in batch.rows.all():
        payload_row = row.raw_json or {}
        comps = components_from_payload(payload_row)
        issues = list(row.issues or [])
        if panel and any(c.get("type") == "components" for c in (panel.columns or [])) and not comps:
            issues.append("没有解析出成分明细")
        src = (payload_row.get("fields") or {}).get("成分明细", "")
        if comps and src and not reconcile(src, comps):
            issues.append("原文段拼不回去（需人工核对）")
        rows.append({"row": row, "comps": comps, "issues": issues})
    return render(request, "panels/ingestion_review.html", {"batch": batch, "panel": panel, "rows": rows})


def _approve_rows(batch, rows):
    panel = PanelDefinition.objects.filter(key=batch.panel_key).first()
    if not panel:
        return 0, ["面板不存在，无法确认"]
    created = 0
    failures = []
    source_type = "plugin" if batch.source_type != "simulation" else "import"
    for row in rows:
        if row.status == "approved":
            continue
        payload_row = row.raw_json or {}
        fields = payload_row.get("fields") or {}
        missing = [
            (col.get("label") or col.get("key"))
            for col in (panel.columns or [])
            if col.get("type") == "components" and not str(fields.get(col.get("key")) or "").strip()
        ]
        if missing:
            msg = "缺少成分原文：" + "、".join(missing)
            issues = list(row.issues or [])
            if msg not in issues:
                issues.append(msg)
            row.issues = issues
            row.save(update_fields=["issues"])
            failures.append(f"{row.row_key or row.pk}：{msg}")
            continue
        try:
            with transaction.atomic():
                rec, _comps = save_record(
                    panel,
                    fields,
                    source_type=source_type,
                    source_run_id=batch.source_run_id,
                    source_row_id=row.row_key,
                    status=Record.STATUS_CONFIRMED,
                    confidence=payload_row.get("confidence"),
                    actor="human",
                    strict=True,
                    audit_action="ingestion.approve",
                )
                row.record = rec
                row.status = "approved"
                row.save(update_fields=["record", "status"])
        except ComponentReconcileError as exc:
            issues = list(row.issues or [])
            msg = str(exc)
            if msg not in issues:
                issues.append(msg)
            row.issues = issues
            row.save(update_fields=["issues"])
            failures.append(f"{row.row_key or row.pk}：{msg}")
            continue
        created += 1
    return created, failures


def ingestion_approve(request, pk):
    batch = get_object_or_404(IngestionBatch, pk=pk)
    if request.method == "POST":
        if batch.status != "pending":
            messages.info(request, f"批次状态为“{batch.get_status_display()}”，不能重复确认")
            return redirect("ingestion_review", pk=pk)
        selected = request.POST.getlist("row_ids")
        qs = batch.rows.all()
        if selected and request.POST.get("approve_selected"):
            qs = qs.filter(pk__in=selected)
        try:
            with transaction.atomic():
                count, failures = _approve_rows(batch, list(qs))
                remaining = batch.rows.exclude(status="approved").exists()
                batch.status = "pending" if remaining else "approved"
                if not remaining:
                    batch.approved_at = timezone.now()
                batch.save(update_fields=["status", "approved_at"])
                AuditLog.objects.create(
                    actor="human",
                    action="ingestion.batch.approve",
                    entity_type="IngestionBatch",
                    entity_id=str(batch.pk),
                    after_json={"created": count, "failures": failures, "status": batch.status},
                )
        except Exception as exc:
            messages.error(request, f"确认失败，本批未写入任何记录：{exc}")
            return redirect("ingestion_review", pk=pk)
        if failures:
            messages.warning(request, f"已确认 {count} 行；{len(failures)} 行因成分问题未确认：" + "；".join(failures[:3]))
        else:
            messages.success(request, f"已确认入库：{count} 行")
    return redirect("ingestion_review", pk=pk)


def ingestion_reject(request, pk):
    batch = get_object_or_404(IngestionBatch, pk=pk)
    if request.method == "POST":
        if batch.status != "pending":
            messages.info(request, f"批次状态为“{batch.get_status_display()}”，不能整批拒绝")
            return redirect("ingestion_review", pk=pk)
        with transaction.atomic():
            batch.status = "rejected"
            batch.rows.filter(status="draft").update(status="rejected")
            batch.save(update_fields=["status"])
            AuditLog.objects.create(
                actor="human",
                action="ingestion.batch.reject",
                entity_type="IngestionBatch",
                entity_id=str(batch.pk),
                after_json={"status": "rejected"},
            )
        messages.success(request, "整批已拒绝")
    return redirect("ingestion_list")


def _condition_value_ok(value, op, v1, v2=None):
    """单个成分数值是否满足条件（与筛选用的比较口径一致）。"""
    if op == "gte":
        return value >= v1
    if op == "gt":
        return value > v1
    if op == "lte":
        return value <= v1
    if op == "lt":
        return value < v1
    if op == "between":
        return v2 is not None and v1 <= value <= v2
    return False


def _components_for_variants(components, variants):
    """按 alias_variants 的同一套别名口径，找出这一行里匹配条件的百分比成分。"""
    wanted = {str(v).strip().lower() for v in variants if str(v or "").strip()}
    found = []
    for c in components:
        if c.unit != "%" or c.numeric_value is None:
            continue
        std = (c.component_std or "").strip().lower()
        original = (c.component_original or "").strip().lower()
        if std in wanted or original in wanted:
            found.append(c)
    return found


def _condition_check(cond, components, missing_as_zero):
    """结果里“条件核对”的说明：缺失=未知；勾选开关才把缺失按 0 假设。"""
    name = cond["name"]
    found = _components_for_variants(components, cond["variants"])
    satisfied = [
        c
        for c in found
        if _condition_value_ok(c.numeric_value, cond["op"], cond["dec"], cond["dec2"])
    ]
    if satisfied:
        comp = max(satisfied, key=lambda c: float(c.numeric_value))
        notes = []
        if float(comp.numeric_value) == 0:
            notes.append("明确记录为 0")
        comp_name = comp.component_std or comp.component_original or ""
        if comp_name and comp_name.strip().lower() != name.strip().lower():
            notes.append(f"别名命中：条件“{name}”按记录“{comp_name}”匹配")
        return {
            "name": name,
            "display": comp.display_value(),
            "op": cond["op"],
            "ok": True,
            "note": "；".join(notes),
        }
    if found:
        comp = max(found, key=lambda c: float(c.numeric_value))
        return {
            "name": name,
            "display": comp.display_value(),
            "op": cond["op"],
            "ok": False,
            "note": "记录值不满足该条件",
        }
    if cond["op"] in ("lte", "lt") and missing_as_zero:
        return {
            "name": name,
            "display": "未记录（按 0 假设）",
            "op": cond["op"],
            "ok": True,
            "note": "查询假设，不是实测",
        }
    return {
        "name": name,
        "display": "无记录（未知）",
        "op": cond["op"],
        "ok": False,
        "note": "没有该成分记录",
    }


def combo_query(request):
    """按同一行的多个成分阈值组合筛选记录（例如：庚烷>=80% 且 甲苯<=5%）。

    口径：缺失成分 = 未知，不是 0；只有明确记录为 0 才是零。
    lte/lt 默认只匹配“该成分有记录且不高于阈值”；勾选 missing_as_zero 才把缺失按 0 假设。
    筛选和结果说明都走 alias_variants 同一套别名口径。
    """
    from .services.analysis import alias_variants

    panel_key = request.GET.get("panel_key") or ""
    material = (request.GET.get("material") or "").strip()
    panel_map = dict(PanelDefinition.objects.values_list("key", "name"))
    missing_as_zero = (request.GET.get("missing_as_zero") or "").strip().lower() in (
        "1", "on", "true", "yes",
    )

    cond_re = re.compile(r"^cond_name_(\d+)$")
    indices = sorted({int(m.group(1)) for key in request.GET if (m := cond_re.match(key))})
    conditions = []
    errors = []
    for i in indices:
        name = (request.GET.get(f"cond_name_{i}") or "").strip()
        op = request.GET.get(f"cond_op_{i}") or "gte"
        raw = (request.GET.get(f"cond_val_{i}") or "").strip()
        raw2 = (request.GET.get(f"cond_val_{i}_2") or "").strip()
        if not name and not raw:
            continue
        if not name or not raw:
            errors.append(f"第 {i} 个条件缺少成分名或数值")
            continue
        try:
            val = Decimal(raw) / Decimal("100")
            val2 = Decimal(raw2) / Decimal("100") if raw2 else None
        except Exception:
            errors.append(f"第 {i} 个条件的数值格式不对")
            continue
        if op == "between" and val2 is None:
            errors.append(f"第 {i} 个条件选了“介于”，但没填第二个数值")
            continue
        conditions.append({
            "idx": i, "name": name, "op": op, "value": raw, "value2": raw2,
            "dec": val, "dec2": val2, "variants": alias_variants(name),
        })

    qs = Record.objects.filter(status=Record.STATUS_CONFIRMED).prefetch_related("components").order_by("id")
    if panel_key:
        qs = qs.filter(panel_key=panel_key)
    if material:
        qs = qs.filter(search_text__icontains=material)

    for c in conditions:
        name_q = Q()
        for variant in c["variants"]:
            name_q |= Q(component_std__iexact=variant) | Q(component_original__iexact=variant)
        # 只有带百分比数值的记录才算“有记录”；没有数值不等于 0。
        base = Component.objects.filter(
            record=OuterRef("pk"), unit="%", numeric_value__isnull=False
        ).filter(name_q)
        op = c["op"]
        val = c["dec"]
        val2 = c["dec2"]
        if op == "gte":
            qs = qs.filter(Exists(base.filter(numeric_value__gte=val)))
        elif op == "gt":
            qs = qs.filter(Exists(base.filter(numeric_value__gt=val)))
        elif op == "lte":
            if missing_as_zero:
                # 显式勾选“缺失按 0”（假设）：没有记录或记录值不高于阈值都算命中。
                qs = qs.filter(~Exists(base.filter(numeric_value__gt=val)))
            else:
                # 默认：缺失=未知，必须真有记录且不高于阈值。
                qs = qs.filter(Exists(base.filter(numeric_value__lte=val)))
        elif op == "lt":
            if missing_as_zero:
                qs = qs.filter(~Exists(base.filter(numeric_value__gte=val)))
            else:
                qs = qs.filter(Exists(base.filter(numeric_value__lt=val)))
        elif op == "between":
            qs = qs.filter(Exists(base.filter(numeric_value__gte=val, numeric_value__lte=val2)))

    total = qs.count()
    page_obj = Paginator(qs, 50).get_page(request.GET.get("page") or 1)

    rows = []
    for rec in page_obj.object_list:
        d = rec.data_json or {}
        comps = list(rec.components.all())
        material_name = d.get("名称") or d.get("物料") or ""
        location = d.get("储位") or d.get("位置") or ""
        area = d.get("储存区域") or ""
        summary = d.get("成分明细") or ""
        checks = [_condition_check(cond, comps, missing_as_zero) for cond in conditions]
        rows.append({
            "record": rec,
            "panel_name": panel_map.get(rec.panel_key, rec.panel_key),
            "material": material_name,
            "location": location,
            "area": area,
            "summary": summary,
            "checks": checks,
        })

    alias_notes = []
    for c in conditions:
        if len(c["variants"]) > 1:
            others = sorted({
                str(v).strip()
                for v in c["variants"]
                if str(v).strip().lower() != c["name"].strip().lower()
            })
            if others:
                alias_notes.append(
                    f"第 {c['idx']} 条“{c['name']}”按别名与“{'、'.join(others)}”视为同一成分"
                )

    condition_slots = list(range(1, max(6, len(conditions) + 1)))
    cond_map = {c["idx"]: c for c in conditions}
    condition_rows = []
    for i in condition_slots:
        c = cond_map.get(i)
        condition_rows.append(
            {
                "i": i,
                "name": c["name"] if c else "",
                "op": c["op"] if c else "gte",
                "value": c["value"] if c else "",
                "value2": c["value2"] if c else "",
            }
        )
    return render(
        request,
        "panels/combo_query.html",
        {
            "page_obj": page_obj,
            "total": total,
            "panels": PanelDefinition.objects.all(),
            "panel_key": panel_key,
            "material": material,
            "panel_map": panel_map,
            "conditions": conditions,
            "cond_map": cond_map,
            "condition_rows": condition_rows,
            "errors": errors,
            "rows": rows,
            "missing_as_zero": missing_as_zero,
            "alias_notes": "；".join(alias_notes),
        },
    )


def component_query(request):
    name = (request.GET.get("name") or "").strip()
    category = request.GET.get("category") or ""
    panel_key = request.GET.get("panel_key") or ""
    min_pct = request.GET.get("min_pct") or ""
    max_pct = request.GET.get("max_pct") or ""
    qs = Component.objects.select_related("record").filter(unit="%").order_by("-numeric_value")
    if name:
        qs = qs.filter(Q(component_std__icontains=name) | Q(component_original__icontains=name))
    if category:
        qs = qs.filter(category=category)
    if panel_key:
        qs = qs.filter(record__panel_key=panel_key)
    if min_pct:
        try:
            qs = qs.filter(numeric_value__gte=Decimal(min_pct) / Decimal("100"))
        except Exception:
            pass
    if max_pct:
        try:
            qs = qs.filter(numeric_value__lte=Decimal(max_pct) / Decimal("100"))
        except Exception:
            pass
    total = qs.count()
    page_obj = Paginator(qs, 50).get_page(request.GET.get("page") or 1)
    panel_map = dict(PanelDefinition.objects.values_list("key", "name"))
    for c in page_obj.object_list:
        c.panel_name = panel_map.get(c.record.panel_key, c.record.panel_key)
    return render(
        request,
        "panels/component_query.html",
        {
            "page_obj": page_obj,
            "total": total,
            "panels": PanelDefinition.objects.all(),
            "panel_map": panel_map,
            "name": name,
            "category": category,
            "panel_key": panel_key,
            "min_pct": min_pct,
            "max_pct": max_pct,
        },
    )


def _as_float(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None


def _match_condition(actual, op, q, q2=""):
    if op == "empty":
        return actual in (None, "", [])
    if op == "not_empty":
        return actual not in (None, "", [])
    text = "" if actual is None else str(actual)
    q = "" if q is None else str(q)
    if op == "contains":
        return q.lower() in text.lower()
    if op == "not_contains":
        return q.lower() not in text.lower()
    if op == "eq":
        if text == q:
            return True
        a, b = _as_float(actual), _as_float(q)
        return a is not None and b is not None and abs(a - b) < 1e-9
    if op == "ne":
        return not _match_condition(actual, "eq", q)
    a = _as_float(actual)
    b = _as_float(q)
    c = _as_float(q2)
    if a is None or b is None:
        return False
    if op == "gt":
        return a > b
    if op == "gte":
        return a >= b
    if op == "lt":
        return a < b
    if op == "lte":
        return a <= b
    if op == "between":
        return c is not None and b <= a <= c
    return False


def _parse_conditions(request):
    slot_re = re.compile(r"^field_(\d+)$")
    indices = sorted({int(m.group(1)) for key in request.GET if (m := slot_re.match(key))})
    conds = []
    for i in indices:
        field = (request.GET.get(f"field_{i}") or "").strip()
        op = request.GET.get(f"op_{i}") or "contains"
        val = request.GET.get(f"val_{i}") or ""
        val2 = request.GET.get(f"val_{i}_2") or ""
        if field:
            conds.append({"i": i, "field": field, "op": op, "val": val, "val2": val2})
    return conds


def _filter_panel_records(panel, conds):
    records = list(
        Record.objects.filter(panel_key=panel.key, status=Record.STATUS_CONFIRMED).order_by("id")
    )
    out = []
    for rec in records:
        d = rec.data_json or {}
        ok = True
        for c in conds:
            if not _match_condition(d.get(c["field"]), c["op"], c["val"], c["val2"]):
                ok = False
                break
        if ok:
            out.append(rec)
    return out


def data_query(request):
    panels = list(PanelDefinition.objects.all())
    default_key = "br_inventory" if any(p.key == "br_inventory" for p in panels) else (panels[0].key if panels else "")
    panel_key = request.GET.get("panel_key") or default_key
    panel = next((p for p in panels if p.key == panel_key), panels[0] if panels else None)
    if panel is None:
        return JsonResponse({"ok": False, "error": "没有面板"}, status=400)
    conds = _parse_conditions(request)
    records = _filter_panel_records(panel, conds) if conds else list(
        Record.objects.filter(panel_key=panel.key, status=Record.STATUS_CONFIRMED).order_by("id")
    )
    total = len(records)
    page_obj = Paginator(records, 50).get_page(request.GET.get("page") or 1)
    cols = panel.columns or []
    rows = []
    for rec in page_obj.object_list:
        rows.append({
            "record": rec,
            "cells": [(rec.data_json or {}).get(c.get("key"), "") for c in cols],
        })
    cond_map = {c["i"]: c for c in conds}
    slots = list(range(1, max(1, len(conds)) + 1)) if conds else [1]
    condition_rows = []
    for i in slots:
        c = cond_map.get(i)
        condition_rows.append({
            "i": i,
            "field": c["field"] if c else "",
            "op": c["op"] if c else "contains",
            "val": c["val"] if c else "",
            "val2": c["val2"] if c else "",
        })
    return render(request, "panels/data_query.html", {
        "panels": panels,
        "panel": panel,
        "panel_key": panel.key,
        "columns": cols,
        "condition_rows": condition_rows,
        "rows": rows,
        "page_obj": page_obj,
        "total": total,
        "query_string": request.GET.urlencode(),
    })


def data_query_export(request):
    panels = list(PanelDefinition.objects.all())
    panel_key = request.GET.get("panel_key") or (panels[0].key if panels else "")
    panel = next((p for p in panels if p.key == panel_key), None)
    if panel is None:
        return JsonResponse({"ok": False, "error": "没有面板"}, status=400)
    conds = _parse_conditions(request)
    records = _filter_panel_records(panel, conds) if conds else list(
        Record.objects.filter(panel_key=panel.key, status=Record.STATUS_CONFIRMED).order_by("id")
    )
    from .services.report import build_query_export
    buf = build_query_export(panel, records)
    response = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{panel.key}_query.xlsx"'
    return response


def incoming_analysis(request):
    from .services.analysis import analyze_incoming, summarize_by_material, component_summary, material_component_summary
    rows = analyze_incoming()
    summary = summarize_by_material(rows)
    comps = component_summary()[:15]
    mat_comps = material_component_summary()
    unknown_main_count = sum(1 for r in rows if r.get("main_unknown"))
    return render(
        request,
        "panels/incoming_analysis.html",
        {
            "rows": rows,
            "summary": summary,
            "comps": comps,
            "mat_comps": mat_comps,
            "unknown_main_count": unknown_main_count,
        },
    )


def summary_report_export(request):
    from .services.report import build_summary_report
    buf = build_summary_report()
    response = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="material_summary.xlsx"'
    return response


@require_http_methods(["GET"])
def api_health(request):
    return JsonResponse({
        "ok": True,
        "time": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "panels": PanelDefinition.objects.count(),
        "records": Record.objects.exclude(status=Record.STATUS_DELETED).count(),
        "components": Component.objects.count(),
        "pending_batches": IngestionBatch.objects.filter(status="pending").count(),
        "database": "sqlite",
        "auth_required": bool(getattr(settings, "API_TOKEN", "")),
    })


@require_http_methods(["GET"])
def api_records(request):
    panel_key = request.GET.get("panel_key") or ""
    q = (request.GET.get("q") or "").strip()
    try:
        limit = min(200, max(1, int(request.GET.get("limit") or 50)))
    except Exception:
        limit = 50
    qs = (
        Record.objects.filter(status=Record.STATUS_CONFIRMED)
        .exclude(status=Record.STATUS_DELETED)
        .prefetch_related("components")
        .order_by("id")
    )
    if panel_key:
        qs = qs.filter(panel_key=panel_key)
    if q:
        qs = qs.filter(search_text__icontains=q)
    items = list(qs[:limit])
    return JsonResponse({
        "ok": True,
        "count": len(items),
        "items": [
            {
                "id": r.pk,
                "panel_key": r.panel_key,
                "data": r.data_json,
                "components": [
                    {"name": c.component_std, "value": float(c.numeric_value) if c.numeric_value is not None else None,
                     "unit": c.unit, "raw": c.raw_text, "category": c.category}
                    for c in r.components.all()
                ],
            }
            for r in items
        ],
    })


@require_http_methods(["GET"])
def api_ingestion_detail(request, pk):
    batch = get_object_or_404(IngestionBatch, pk=pk)
    return JsonResponse({
        "ok": True,
        "id": batch.pk,
        "panel_key": batch.panel_key,
        "status": batch.status,
        "source": batch.source_name,
        "rows": batch.rows.count(),
        "review_url": f"/ingestions/{batch.pk}/",
        "created_at": batch.created_at.isoformat(timespec="seconds"),
    })


@require_http_methods(["GET"])
def api_analysis_incoming(request):
    from .services.analysis import analyze_incoming
    rows = analyze_incoming()
    return JsonResponse({"ok": True, "count": len(rows), "items": rows[:200]})

# ---------- API（插件/Agent 用；预览版不做鉴权） ----------

@require_http_methods(["GET"])
def api_panels(request):
    return JsonResponse(
        {
            "ok": True,
            "panels": [
                {
                    "key": p.key,
                    "name": p.name,
                    "row_unit": p.row_unit,
                    "columns": p.columns,
                    "version": p.version,
                }
                for p in PanelDefinition.objects.all()
            ],
        }
    )


@require_http_methods(["GET"])
def api_components(request):
    qs = Component.objects.select_related("record").all()
    name = request.GET.get("name")
    if name:
        qs = qs.filter(Q(component_std__icontains=name) | Q(component_original__icontains=name))
    category = request.GET.get("category")
    if category:
        qs = qs.filter(category=category)
    items = list(qs[:500])
    return JsonResponse(
        {
            "ok": True,
            "count": len(items),
            "items": [
                {
                    "record_id": c.record_id,
                    "panel_key": c.record.panel_key,
                    "component": c.component_std,
                    "numeric_value": float(c.numeric_value) if c.numeric_value is not None else None,
                    "unit": c.unit,
                    "raw_text": c.raw_text,
                    "category": c.category,
                }
                for c in items
            ],
        }
    )


def _existing_batch_response(batch, message):
    return JsonResponse(
        {
            "ok": True,
            "duplicate": True,
            "batch_id": batch.pk,
            "rows": batch.rows.count(),
            "review_url": f"/ingestions/{batch.pk}/",
            "message": message,
        },
        status=200,
    )


@csrf_exempt
@require_http_methods(["POST"])
def api_ingestions(request):
    expected = getattr(settings, "API_TOKEN", "")
    if expected and request.headers.get("X-API-Key") != expected:
        return JsonResponse({"ok": False, "error": "需要 X-API-Key"}, status=401)
    if len(request.body) > 2_000_000:
        return JsonResponse({"ok": False, "error": "请求体过大（上限 2MB）"}, status=413)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "JSON 解析失败"}, status=400)

    panel_keys = set(PanelDefinition.objects.values_list("key", flat=True))
    try:
        panel_key, rows = validate_payload(payload, panel_keys)
    except IngestionPayloadError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    source = payload.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    key = (request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "").strip()[:120]
    fingerprint = payload_fingerprint(payload)

    if key:
        existing = IngestionBatch.objects.filter(idempotency_key=key).first()
        if existing:
            if existing.payload_hash and existing.payload_hash != fingerprint:
                return JsonResponse(
                    {"ok": False, "error": "同一个 Idempotency-Key 被用于不同内容"},
                    status=409,
                )
            return _existing_batch_response(existing, "相同幂等键已处理过，返回原批次")

    existing = IngestionBatch.objects.filter(
        payload_hash=fingerprint, status__in=["pending", "approved"]
    ).first()
    if existing:
        return _existing_batch_response(existing, "相同内容已存在，未重复创建批次")

    try:
        with transaction.atomic():
            batch = IngestionBatch.objects.create(
                panel_key=panel_key,
                source_type=source.get("type", "plugin"),
                source_name=source.get("name", "API 推送"),
                source_run_id=str(source.get("run_id", ""))[:120],
                payload=payload,
                payload_hash=fingerprint,
                idempotency_key=key,
                status="pending",
            )
            IngestionRow.objects.bulk_create(
                [
                    IngestionRow(
                        batch=batch,
                        row_key=str(r.get("row_key") or f"R{i:04d}")[:80],
                        raw_json=r,
                        mapped_json=r.get("fields", {}) or {},
                        status="draft",
                    )
                    for i, r in enumerate(rows, start=1)
                ]
            )
    except IntegrityError:
        existing = None
        if key:
            existing = IngestionBatch.objects.filter(idempotency_key=key).first()
        if existing is None:
            existing = IngestionBatch.objects.filter(payload_hash=fingerprint).first()
        if existing:
            return _existing_batch_response(existing, "并发重复提交，已返回原批次")
        return JsonResponse({"ok": False, "error": "批次写入冲突，请重试"}, status=409)
    return JsonResponse(
        {
            "ok": True,
            "duplicate": False,
            "batch_id": batch.pk,
            "rows": len(rows),
            "review_url": f"/ingestions/{batch.pk}/",
            "message": "已接收为草稿，需人工审核确认",
        },
        status=201,
    )
