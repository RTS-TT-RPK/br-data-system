"""把 L2 reviewed.json 映射成 L3 草稿批次（不直接写正式记录）。

校验口径按《三层修订接口契约（2026-10-28）》第 3 节执行：
1. 版本：只有 reviewed/v2 能正式导入；reviewed/v1 仅允许 --dry-run 预览，正式导入报错。
2. 身份：run_id 非空；source 必须是对象且 source.raw_sha256 是 64 位十六进制。
3. 交付状态：正式导入要求 deliverability.deliverable is True 且 status == "deliverable"；
   draft 只在 dry-run 里允许预览，并打印不可交付原因。
4. 行状态：每行 status 必须是 "ok" / "blocked"；缺失或其它值一律报错（不能当 ok）。
5. 行号：同一 sheet 内 row_id 不得重复。
6. 工作表：只处理映射里声明的工作表；未映射的工作表只计数并报告“不导入”，不算错误。
7. 字段：映射 fields 引用的源字段必须存在于行 fields（允许为空字符串，但键须存在）；
   mapping.required 声明的源字段缺失或为空 → 报错。
8. 单位：成分 unit 必须在 allowed_component_units 里（默认含 %、‰、ppm、mg/L、g/L、空、—）。
9. 数值：用 raw_value（必要时拼 name）按 L3 解析口径算出的数值，必须与 value 相等
   （相对误差 ≤ 1e-6），防止 5% 与 0.05 混用。
10. 文本：行里同时有成分原文列（component_source）和 components 时，把原文拆成
    “成分型 token”后与 components 按 (名称, 规范数值) 多重集比较。
11. 声明：deliverability.blocked_rows 必须等于实际 status="blocked" 的行数。
12. 全部通过后只把 status="ok" 的行写成 IngestionBatch(status="pending") 草稿批次，
    IngestionRow.row_key = row_id，payload 行带 source{run_id,sheet,row_id,image_id}；
    不创建正式 Record。
13. --include-blocked 必须同时给出 --force-reason；批次 note、审计和行标记里留下
    “强制包含阻塞行：原因…”，CLI 打印风险提示。强制行仍须通过上面的字段/单位/数值校验。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.db import transaction

from panels.models import AuditLog, IngestionBatch, IngestionRow, PanelDefinition
# 复用 L3 自己的解析口径（component_parser 是唯一裁判），不另写一套换算。
from panels.services.component_parser import _parse_token, split_components
from panels.services.ingestion import IngestionPayloadError, payload_fingerprint, validate_payload

DEFAULT_MAP = "seeds/reviewed_maps.json"
SUPPORTED_SCHEMA_VERSION = "reviewed/v2"
LEGACY_SCHEMA_VERSION = "reviewed/v1"
DEFAULT_ALLOWED_COMPONENT_UNITS = ["%", "‰", "ppm", "mg/L", "g/L", "", "—"]
REL_TOL = Decimal("1e-6")
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ReviewImportError(ValueError):
    """reviewed 文件校验不通过 / 映射配置有误。errors 保存全部中文原因。"""

    def __init__(self, message, errors=None, report=None):
        super().__init__(message)
        self.errors = list(errors or [])
        self.report = report or {}


# ---------------------------------------------------------------------------
# 读文件 / 映射
# ---------------------------------------------------------------------------

def _read_json(path):
    p = Path(path)
    if not p.exists():
        raise ReviewImportError(f"文件不存在：{p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReviewImportError(f"JSON 读不了：{p}（{exc}）") from exc


def load_mapping(path=None):
    p = Path(path) if path else Path(settings.BASE_DIR) / DEFAULT_MAP
    data = _read_json(p)
    if not isinstance(data, dict) or not isinstance(data.get("sheets"), dict):
        raise ReviewImportError("映射文件缺少 sheets 对象")
    if not data["sheets"]:
        raise ReviewImportError("映射文件里没有任何工作表")
    return data, p


def _text(value):
    return "" if value is None else str(value)


def _mapping_schema_versions(mapping):
    raw = mapping.get("reviewed_schema_versions")
    if isinstance(raw, list) and raw:
        return [str(x).strip() for x in raw if str(x).strip()]
    return [SUPPORTED_SCHEMA_VERSION]


def _allowed_component_units(mapping, spec):
    raw = None
    if isinstance(spec, dict) and spec.get("allowed_component_units") is not None:
        raw = spec.get("allowed_component_units")
    elif mapping.get("allowed_component_units") is not None:
        raw = mapping.get("allowed_component_units")
    if raw is None:
        raw = DEFAULT_ALLOWED_COMPONENT_UNITS
    if not isinstance(raw, (list, tuple)):
        raise ReviewImportError("映射 allowed_component_units 必须是数组")
    return [str(x) for x in raw]


# ---------------------------------------------------------------------------
# 数值 / 成分原文解析（与 L3 component_parser 同一口径）
# ---------------------------------------------------------------------------

def _to_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return dec if dec.is_finite() else None


def _num_equal(a, b):
    if a is None or b is None:
        return False
    if a == b:
        return True
    denom = max(abs(a), abs(b))
    if denom == 0:
        return False
    return abs(a - b) / denom <= REL_TOL


def _fmt_dec(value):
    if value is None:
        return "（空）"
    return str(value)


def _component_text(comp):
    """按契约 1.2.1 用“有效原文值” raw_value 还原可被 L3 解析的一段文本。"""
    name = _text(comp.get("name")).strip()
    raw = _text(comp.get("raw_value")).strip()
    if not raw:
        return name
    if name and not raw.startswith(name):
        return f"{name}{raw}"
    return raw


def _parse_component_text(text):
    """单段成分文本 → L3 解析结果（name/numeric_value/unit/category）。"""
    return _parse_token(text) if text else None


# ---------------------------------------------------------------------------
# 版本 / 身份 / 交付状态 / 行状态
# ---------------------------------------------------------------------------

def _empty_report(schema_version, *, legacy=False):
    return {
        "schema_version": schema_version,
        "legacy": legacy,
        "run_id": "",
        "raw_sha256": "",
        "status": "",
        "deliverable": False,
        "reasons": [],
        "warnings": [],
        "errors": [],
        "unmapped": [],
        "sheets": [],
    }


def _validate_identity(reviewed, errors):
    run_id = _text(reviewed.get("run_id")).strip()
    if not run_id:
        errors.append("缺少 run_id：无法追踪来源任务")
    source = reviewed.get("source")
    raw_sha = ""
    if not isinstance(source, dict):
        errors.append("缺少 source 对象：无法追踪来源（需要 source.raw_sha256）")
    else:
        raw_sha = _text(source.get("raw_sha256")).strip()
        if not HEX64_RE.match(raw_sha):
            shown = raw_sha or "缺失"
            errors.append(f"source.raw_sha256 必须是 64 位十六进制（当前：{shown}）")
    return run_id, raw_sha


def _validate_deliverability(reviewed, *, dry_run, errors, warnings):
    info = {"status": "", "deliverable": False, "reasons": []}
    data = reviewed.get("deliverability")
    if not isinstance(data, dict):
        errors.append("缺少 deliverability 对象：无法判断文件是否可交付")
        return info
    status = _text(data.get("status")).strip()
    deliverable = data.get("deliverable")
    reasons = data.get("reasons")
    reasons = [str(r).strip() for r in reasons if str(r).strip()] if isinstance(reasons, list) else []
    info.update({"status": status, "deliverable": deliverable is True, "reasons": reasons})

    if status not in ("deliverable", "draft"):
        errors.append(f"deliverability.status 非法：{status or '缺失'}（只允许 deliverable / draft）")
    if not isinstance(deliverable, bool):
        errors.append("deliverability.deliverable 必须是 true/false 布尔值")
    elif status in ("deliverable", "draft") and deliverable != (status == "deliverable"):
        errors.append("deliverability 自相矛盾：status 与 deliverable 不一致")

    if status == "draft":
        if not reasons:
            warnings.append("draft 文件没有给出不可交付原因（deliverability.reasons 为空）")
        if not dry_run:
            why = "；".join(reasons) if reasons else "未给出原因"
            errors.append(f"文件不可交付（draft）：{why}。请先在 L2 解决阻塞问题并重新导出可交付版本")
    elif status == "deliverable" and deliverable is not True and not dry_run:
        errors.append("正式导入要求 deliverability.deliverable == true")
    return info


def _validate_rows(reviewed, errors):
    raw_rows = reviewed.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        errors.append("缺少 rows 数组（或为空）：没有可导入的数据")
        return []
    rows = []
    seen = {}
    for index, row in enumerate(raw_rows, start=1):
        tag = f"第 {index} 行"
        if not isinstance(row, dict):
            errors.append(f"{tag}：不是 JSON 对象")
            continue
        row_id = _text(row.get("row_id")).strip()
        sheet = _text(row.get("sheet")).strip()
        if row_id:
            tag = f"行 {row_id}"
        else:
            errors.append(f"{tag}：缺少 row_id")
        if not sheet:
            errors.append(f"{tag}：缺少 sheet")
        status = row.get("status")
        status_text = status.strip() if isinstance(status, str) else ""
        if status_text not in ("ok", "blocked"):
            shown = "缺失" if status is None else repr(status)
            errors.append(f"{tag}：status {shown}，只允许 ok / blocked（不能把缺状态当 ok）")
            status_text = ""
        if row_id and sheet:
            key = (sheet, row_id)
            if key in seen:
                errors.append(
                    f"同一工作表「{sheet}」内 row_id 重复：{row_id}（第 {seen[key]} 行与第 {index} 行）"
                )
            else:
                seen[key] = index
        rows.append(
            {
                "index": index,
                "row": row,
                "tag": tag,
                "row_id": row_id,
                "sheet": sheet,
                "status": status_text,
                "image_id": _text(row.get("image_id")).strip(),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 映射解析
# ---------------------------------------------------------------------------

def _resolve_mapped_sheets(mapping, errors):
    panel_map = {p.key: p for p in PanelDefinition.objects.all()}
    resolved = []
    for sheet_key, spec in (mapping.get("sheets") or {}).items():
        sheet = str(sheet_key)
        if not isinstance(spec, dict):
            errors.append(f"映射「{sheet}」必须是对象")
            continue
        panel_key = _text(spec.get("panel_key")).strip()
        if not panel_key:
            errors.append(f"映射「{sheet}」缺少 panel_key")
            continue
        panel = panel_map.get(panel_key)
        columns = set(panel.column_keys()) if panel is not None else set()
        if panel is None:
            errors.append(f"映射「{sheet}」指向不存在的面板：{panel_key}")

        fields = spec.get("fields")
        if fields is None:
            fields = {}
        if not isinstance(fields, dict):
            errors.append(f"映射「{sheet}」的 fields 必须是对象")
            fields = {}
        for target, source_field in fields.items():
            if not isinstance(source_field, str) or not source_field.strip():
                errors.append(f"映射「{sheet}」的目标字段「{target}」缺少源字段名")
                continue
            if columns and str(target) not in columns:
                errors.append(
                    f"映射「{sheet}」的目标字段「{target}」不在面板「{panel_key}」的列定义里"
                )

        templates = spec.get("templates")
        if templates is None:
            templates = {}
        if not isinstance(templates, dict):
            errors.append(f"映射「{sheet}」的 templates 必须是对象")
            templates = {}
        for target, template in templates.items():
            if not isinstance(template, str):
                errors.append(f"映射「{sheet}」的模板「{target}」必须是字符串")
            if columns and str(target) not in columns:
                errors.append(
                    f"映射「{sheet}」的目标字段「{target}」不在面板「{panel_key}」的列定义里"
                )

        required = spec.get("required")
        if required is None:
            required = []
        if not isinstance(required, list):
            errors.append(f"映射「{sheet}」的 required 必须是源字段名数组")
            required = []
        required = [str(x).strip() for x in required if str(x).strip()]

        resolved.append(
            {
                "sheet": sheet,
                "spec": spec,
                "panel_key": panel_key,
                "panel": panel,
                "fields": fields,
                "templates": templates,
                "required": required,
                "component_source": _text(spec.get("component_source")).strip() or "成分明细",
                "component_field": _text(spec.get("component_field")).strip(),
                "allowed_units": _allowed_component_units(mapping, spec),
            }
        )
    return resolved


def _collect_unmapped(rows, mapped_sheets):
    counts = {}
    for rec in rows:
        sheet = rec["sheet"]
        if sheet and sheet not in mapped_sheets:
            counts[sheet] = counts.get(sheet, 0) + 1
    return [{"sheet": sheet, "rows": n} for sheet, n in counts.items()]


# ---------------------------------------------------------------------------
# 行级校验（只查会被导入的行；blocked 默认跳过，--include-blocked 时才一起查）
# ---------------------------------------------------------------------------

def _validate_row_payload(rec, sheet_info, errors, warnings):
    row = rec["row"]
    tag = rec["tag"]
    fields = row.get("fields")
    if not isinstance(fields, dict):
        errors.append(f"{tag}：缺少 fields 对象")
        fields = {}

    for target, source_field in sheet_info["fields"].items():
        if source_field not in fields:
            errors.append(f"{tag}：映射引用的源字段「{source_field}」在 fields 里不存在")
    for source_field in sheet_info["required"]:
        if source_field not in fields:
            errors.append(f"{tag}：缺少必填源字段「{source_field}」")
        elif not _text(fields.get(source_field)).strip():
            errors.append(f"{tag}：必填源字段「{source_field}」为空")

    if rec["status"] in ("ok", "blocked") and not rec["image_id"]:
        errors.append(f"{tag}：缺少 image_id，无法保留原图来源追踪")

    comps = row.get("components")
    if comps is None:
        comps = []
    if not isinstance(comps, list):
        errors.append(f"{tag}：components 必须是数组")
        comps = []

    allowed_units = sheet_info["allowed_units"]
    component_names = set()
    numeric_components = []
    for index, comp in enumerate(comps, start=1):
        comp_tag = f"{tag} 的成分 {index}"
        if not isinstance(comp, dict):
            errors.append(f"{comp_tag} 不是 JSON 对象")
            continue
        name = _text(comp.get("name")).strip()
        if name:
            component_names.add(name)
        else:
            errors.append(f"{comp_tag} 缺少 name")
        unit = _text(comp.get("unit")).strip()
        if unit not in allowed_units:
            errors.append(
                f"{comp_tag}（{name or '未命名'}）单位「{unit or '空'}」不在允许列表 {allowed_units} 里"
            )
        raw_value = _text(comp.get("raw_value")).strip()
        if not raw_value:
            errors.append(f"{comp_tag}（{name or '未命名'}）缺少 raw_value（有效原文值）")
        parsed = _parse_component_text(_component_text(comp))
        value = _to_decimal(comp.get("value"))
        if parsed is None or parsed.get("numeric_value") is None:
            if value is not None:
                errors.append(
                    f"{comp_tag}（{name or '未命名'}）raw_value「{raw_value}」解析不出数值，但 value={_fmt_dec(value)}"
                )
            elif raw_value:
                warnings.append(f"{comp_tag}（{name or '未命名'}）是无数值的描述性成分，未检查数值一致性")
        else:
            if value is None:
                errors.append(
                    f"{comp_tag}（{name or '未命名'}）缺少可比较的 value（规范值），raw_value「{raw_value}」"
                )
            elif not _num_equal(value, parsed["numeric_value"]):
                errors.append(
                    f"{comp_tag}（{name or '未命名'}）数值对不上：raw_value「{raw_value}」按 L3 口径={_fmt_dec(parsed['numeric_value'])}，"
                    f"value={_fmt_dec(value)}（相对误差需 ≤ {REL_TOL}）"
                )
            if _text(parsed.get("unit")).strip() != unit:
                errors.append(
                    f"{comp_tag}（{name or '未命名'}）单位对不上：raw_value 解析为「{parsed.get('unit')}」，unit 声明「{unit}」"
                )
            if parsed.get("name") and name and parsed["name"] != name:
                errors.append(
                    f"{comp_tag}：raw_value「{raw_value}」里的名字「{parsed['name']}」与 name「{name}」不一致"
                )
        if parsed is not None and parsed.get("numeric_value") is not None and value is not None:
            numeric_components.append((name, parsed["numeric_value"]))

    # 10) 成分原文 vs components 多重集
    source_text = _text(fields.get(sheet_info["component_source"])).strip()
    if source_text and comps:
        expected = _token_multiset(source_text, component_names)
        actual = _component_multiset(numeric_components)
        if expected != actual:
            missing = expected - actual
            extra = actual - expected
            parts = []
            if missing:
                parts.append("原文有而 components 缺：" + "、".join(_fmt_pair(k) for k in missing.elements()))
            if extra:
                parts.append("components 有而原文没有：" + "、".join(_fmt_pair(k) for k in extra.elements()))
            detail = "；".join(parts) or "（内容不一致）"
            errors.append(
                f"{tag}：成分原文与 components 不一致（{sheet_info['component_source']}「{source_text}」；{detail}）"
            )


def _token_multiset(text, component_names):
    """把成分原文拆成“成分型 token”的多重集（排除 PH 之类无量值指标与“杂”等说明）。"""
    counter = Counter()
    for token in split_components(text):
        numeric = token.get("numeric_value")
        if numeric is None:
            continue
        unit = _text(token.get("unit")).strip()
        if unit in ("", "—") and token.get("name") not in component_names:
            continue
        counter[(token.get("name") or "", numeric)] += 1
    return counter


def _component_multiset(numeric_components):
    return Counter(numeric_components)


def _fmt_pair(key):
    name, numeric = key
    return f"{name or '（无名）'}={_fmt_dec(numeric)}"


# ---------------------------------------------------------------------------
# 映射后的 payload
# ---------------------------------------------------------------------------

def _payload_components(row):
    comps = row.get("components") or []
    out = []
    for comp in comps:
        if not isinstance(comp, dict):
            continue
        text = _component_text(comp)
        name = _text(comp.get("name")).strip()
        out.append({"name": name, "raw_value": text, "raw_text": text})
    return out


def _mapped_fields(row, sheet_info):
    source = row.get("fields") or {}
    fields = {}
    for target, source_field in sheet_info["fields"].items():
        if isinstance(source_field, str):
            fields[target] = source.get(source_field)
    for target, template in sheet_info["templates"].items():
        try:
            fields[target] = str(template).format(**{k: _text(v) for k, v in source.items()})
        except (KeyError, ValueError, IndexError):
            fields[target] = ""
    comps = _payload_components(row)
    component_field = sheet_info["component_field"]
    if component_field:
        # 原文列已经是“有效值”的单一来源（契约 1.2.1 规则 4）；优先保留原文，
        # 这样 PH、杂 等非成分 token 不会在导入时被丢掉。
        source_text = _text(source.get(sheet_info["component_source"])).strip()
        if source_text:
            fields[component_field] = source_text
        elif comps:
            fields[component_field] = " ".join(c["raw_value"] for c in comps if c["raw_value"])
    return fields, comps


def _build_sheet_payload(
    recs, sheet_info, run_id, raw_sha256, mapping, deliverability, *, reviewed_at="", include_blocked
):
    payload_rows = []
    blocked_ids = []
    for rec in recs:
        row = rec["row"]
        blocked = rec["status"] == "blocked"
        if blocked:
            blocked_ids.append(rec["row_id"])
        fields, comps = _mapped_fields(row, sheet_info)
        source_info = {
            "run_id": run_id,
            "sheet": sheet_info["sheet"],
            "row_id": rec["row_id"],
            "image_id": rec["image_id"],
            "reviewed_status": "blocked" if blocked else "ok",
        }
        if blocked:
            source_info["forced_blocked"] = True
        payload_rows.append(
            {
                "row_key": rec["row_id"],
                "fields": fields,
                "components": comps,
                "confidence": row.get("confidence"),
                "source": source_info,
            }
        )
    if not payload_rows:
        return None, blocked_ids
    payload = {
        "schema_version": "reviewed_import/v2",
        "mapping_version": mapping.get("version", 2),
        "panel_key": sheet_info["panel_key"],
        "source": {
            "type": "plugin",
            "name": "review_web reviewed.json",
            "run_id": run_id,
            "sheet": sheet_info["sheet"],
            "raw_sha256": raw_sha256,
            "reviewed_at": _text(reviewed_at),
            "schema_version": SUPPORTED_SCHEMA_VERSION,
            "deliverability": {
                "status": _text(deliverability.get("status")),
                "deliverable": deliverability.get("deliverable") is True,
            },
        },
        "rows": payload_rows,
    }
    return payload, blocked_ids


# ---------------------------------------------------------------------------
# 主流程：分析（v2 严格校验 / v1 只读预览）
# ---------------------------------------------------------------------------

def analyze_reviewed(reviewed, mapping, *, dry_run=False, include_blocked=False):
    """校验 reviewed 并生成待创建批次描述。失败抛 ReviewImportError（不写任何东西）。"""
    if not isinstance(reviewed, dict):
        raise ReviewImportError("reviewed 必须是 JSON 对象")
    schema_version = _text(reviewed.get("schema_version")).strip()
    if not schema_version:
        raise ReviewImportError("缺少 schema_version：无法判断 reviewed 格式版本")
    if schema_version == SUPPORTED_SCHEMA_VERSION:
        return _analyze_v2(reviewed, mapping, dry_run=dry_run, include_blocked=include_blocked)
    if schema_version == LEGACY_SCHEMA_VERSION:
        if not dry_run:
            raise ReviewImportError(
                "旧格式 reviewed/v1：不能正式导入。请用 L2 审核台重新导出 reviewed/v2 后再导入；"
                "旧格式只允许 --dry-run 预览。"
            )
        return _preview_legacy(reviewed, mapping, include_blocked=include_blocked)
    raise ReviewImportError(
        f"未知的 reviewed 版本：{schema_version}（当前只支持 reviewed/v2 正式导入；"
        f"{LEGACY_SCHEMA_VERSION} 仅 dry-run 预览）"
    )


def _analyze_v2(reviewed, mapping, *, dry_run, include_blocked):
    report = _empty_report(SUPPORTED_SCHEMA_VERSION)
    errors = report["errors"]
    warnings = report["warnings"]

    run_id, raw_sha256 = _validate_identity(reviewed, errors)
    report["run_id"] = run_id
    report["raw_sha256"] = raw_sha256

    deliverability = dict(reviewed.get("deliverability") or {}) if isinstance(reviewed.get("deliverability"), dict) else {}
    report["reviewed_at"] = _text(reviewed.get("reviewed_at"))
    delivery_info = _validate_deliverability(reviewed, dry_run=dry_run, errors=errors, warnings=warnings)
    report.update(
        {
            "status": delivery_info["status"],
            "deliverable": delivery_info["deliverable"],
            "reasons": delivery_info["reasons"],
        }
    )

    mapping_versions = _mapping_schema_versions(mapping)
    if SUPPORTED_SCHEMA_VERSION not in mapping_versions:
        errors.append(f"映射声明的 reviewed_schema_versions {mapping_versions} 不包含 {SUPPORTED_SCHEMA_VERSION}")

    rows = _validate_rows(reviewed, errors)
    sheet_infos = _resolve_mapped_sheets(mapping, errors)
    mapped_sheets = {info["sheet"] for info in sheet_infos}
    report["unmapped"] = _collect_unmapped(rows, mapped_sheets)

    actual_blocked = sum(1 for rec in rows if rec["status"] == "blocked")
    declared_blocked = deliverability.get("blocked_rows")
    if isinstance(declared_blocked, bool) or not isinstance(declared_blocked, int):
        errors.append("deliverability.blocked_rows 缺失或不是整数：必须等于实际阻塞行数")
    elif declared_blocked != actual_blocked:
        errors.append(
            f"deliverability.blocked_rows={declared_blocked} 与实际 status=\"blocked\" 的行数 {actual_blocked} 不符"
        )
    if delivery_info["deliverable"] and actual_blocked:
        warnings.append(
            f"文件声明可交付，但仍含 {actual_blocked} 行阻塞数据：默认跳过这些行；"
            f"如确需导入请同时使用 --include-blocked 与 --force-reason"
        )

    panel_keys = set(PanelDefinition.objects.values_list("key", flat=True))
    for info in sheet_infos:
        sheet = info["sheet"]
        group = [rec for rec in rows if rec["sheet"] == sheet]
        ok_rows = [rec for rec in group if rec["status"] == "ok"]
        blocked_rows = [rec for rec in group if rec["status"] == "blocked"]
        importable = list(ok_rows)
        if include_blocked:
            importable.extend(blocked_rows)
        item = {
            "sheet": sheet,
            "panel_key": info["panel_key"],
            "total": len(group),
            "ok": len(ok_rows),
            "blocked": len(blocked_rows),
            "imported": len(importable),
            "skipped": len(group) - len(importable),
            "forced": len(blocked_rows) if include_blocked else 0,
            "blocked_row_ids": [rec["row_id"] for rec in blocked_rows],
            "payload": None,
            "batch_id": None,
            "duplicate": False,
        }
        report["sheets"].append(item)
        for rec in importable:
            _validate_row_payload(rec, info, errors, warnings)
        if importable and info["panel"] is not None:
            payload, blocked_ids = _build_sheet_payload(
                importable, info, run_id, raw_sha256, mapping, deliverability,
                reviewed_at=report.get("reviewed_at", ""), include_blocked=include_blocked,
            )
            item["blocked_row_ids"] = blocked_ids
            try:
                panel_key, _rows = validate_payload(payload, panel_keys)
            except IngestionPayloadError as exc:
                errors.append(f"工作表「{sheet}」映射后的批次不合法：{exc}")
                continue
            payload["panel_key"] = panel_key
            fingerprint = payload_fingerprint(payload)
            payload["idempotency_key"] = f"reviewed:{run_id}:{sheet}:{fingerprint[:16]}"
            item["payload"] = payload
            item["fingerprint"] = fingerprint

    if not any(item.get("imported") for item in report["sheets"]):
        warnings.append("没有任何可导入的行：映射工作表的行可能全是阻塞行，请核对文件与映射配置")

    if errors:
        report["ok"] = False
        raise ReviewImportError(
            "reviewed 导入校验未通过，未创建任何批次：\n- " + "\n- ".join(errors),
            errors=errors,
            report=report,
        )
    report["ok"] = True
    return report


def _preview_legacy(reviewed, mapping, *, include_blocked=False):
    """reviewed/v1 只读预览：不做 v2 严格校验，绝不写数据。"""
    report = _empty_report(LEGACY_SCHEMA_VERSION, legacy=True)
    warnings = report["warnings"]
    if include_blocked:
        warnings.append("旧格式 reviewed/v1 不支持 --include-blocked：预览仍只展示旧规则下的可导入行")
    warnings.append(
        "旧格式 reviewed/v1：只做映射预览，不能正式导入。请用 L2 审核台重新导出 reviewed/v2。"
    )
    run_id = _text(reviewed.get("run_id")).strip()
    report["run_id"] = run_id
    if not run_id:
        warnings.append("旧格式缺少 run_id：无法追踪来源（预览仅用于查看映射）")
    source = reviewed.get("source")
    raw_sha = _text(source.get("raw_sha256")).strip() if isinstance(source, dict) else ""
    report["raw_sha256"] = raw_sha
    if not HEX64_RE.match(raw_sha):
        warnings.append("旧格式缺少合法的 source.raw_sha256：正式导入需要（v2）")
    report["status"] = "legacy-preview"
    report["deliverable"] = False
    report["reasons"] = ["旧格式 reviewed/v1，不能作为正式导入来源"]

    raw_rows = reviewed.get("rows")
    if not isinstance(raw_rows, list):
        raise ReviewImportError("reviewed/v1 缺少 rows 数组")
    rows = []
    missing_status = 0
    for index, row in enumerate(raw_rows, start=1):
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        if not isinstance(status, str) or status.strip() not in ("ok", "blocked"):
            missing_status += 1
            status = "ok"  # 旧格式预览按旧规则处理，但仍会给出警告
        rows.append(
            {
                "index": index,
                "row": row,
                "tag": f"第 {index} 行",
                "row_id": _text(row.get("row_id")).strip() or f"R{index:03d}",
                "sheet": _text(row.get("sheet")).strip(),
                "status": status,
                "image_id": _text(row.get("image_id")).strip(),
            }
        )
    if missing_status:
        warnings.append(f"有 {missing_status} 行缺少合法 status：旧格式预览按“未标记阻塞”处理；v2 会把缺状态当错误")
    if any(rec["status"] == "blocked" for rec in rows):
        warnings.append("预览里已按旧规则跳过 status=blocked 的行（正式导入只认 v2 的 ok 行）")

    sheet_infos = _resolve_mapped_sheets(mapping, report["errors"])
    mapped_sheets = {info["sheet"] for info in sheet_infos}
    report["unmapped"] = _collect_unmapped(rows, mapped_sheets)
    if report["errors"]:
        raise ReviewImportError("映射配置有误：\n- " + "\n- ".join(report["errors"]), errors=report["errors"], report=report)

    deliverability = {"status": "legacy-preview", "deliverable": False, "reviewed_at": ""}
    for info in sheet_infos:
        group = [rec for rec in rows if rec["sheet"] == info["sheet"]]
        importable = [rec for rec in group if rec["status"] != "blocked"]
        item = {
            "sheet": info["sheet"],
            "panel_key": info["panel_key"],
            "total": len(group),
            "ok": len(importable),
            "blocked": len(group) - len(importable),
            "imported": len(importable),
            "skipped": len(group) - len(importable),
            "forced": 0,
            "blocked_row_ids": [rec["row_id"] for rec in group if rec["status"] == "blocked"],
            "payload": None,
            "batch_id": None,
            "duplicate": False,
        }
        report["sheets"].append(item)
        if importable and info["panel"] is not None:
            item["payload"], _ids = _build_sheet_payload(
                importable, info, run_id, raw_sha, mapping, deliverability,
                reviewed_at=_text(reviewed.get("reviewed_at")).strip(), include_blocked=False,
            )
    report["ok"] = True
    return report


# ---------------------------------------------------------------------------
# 写批次
# ---------------------------------------------------------------------------

def _batch_note(report, item, *, include_blocked, force_reason):
    reason = (force_reason or "").strip()
    if include_blocked and item.get("forced"):
        ids = "、".join(item.get("blocked_row_ids") or [])
        note = f"强制包含阻塞行：{reason}（{report['run_id']}，阻塞 {item['forced']} 行：{ids}）"
    else:
        note = f"L2 reviewed/v2 {report['run_id']}：导入 {item['imported']} 行，跳过阻塞 {item['skipped']} 行"
    return note[:300]


def _create_batch(report, item, *, actor, include_blocked, force_reason):
    payload = item["payload"]
    fingerprint = item["fingerprint"]
    key = payload.get("idempotency_key", "")
    existing = None
    if key:
        existing = IngestionBatch.objects.filter(idempotency_key=key).first()
    if existing is None:
        existing = IngestionBatch.objects.filter(payload_hash=fingerprint, status__in=["pending", "approved"]).first()
    if existing is not None:
        item["batch_id"] = existing.pk
        item["duplicate"] = True
        return item

    note = _batch_note(report, item, include_blocked=include_blocked, force_reason=force_reason)
    forced_rows = [r for r in payload["rows"] if (r.get("source") or {}).get("forced_blocked")]
    with transaction.atomic():
        batch = IngestionBatch.objects.create(
            panel_key=item["panel_key"],
            source_type="plugin",
            source_name="review_web reviewed.json（reviewed/v2）",
            source_run_id=report["run_id"][:120],
            payload=payload,
            payload_hash=fingerprint,
            idempotency_key=key[:120],
            status="pending",
            note=note,
        )
        rows = []
        for payload_row in payload["rows"]:
            issues = []
            if (payload_row.get("source") or {}).get("forced_blocked"):
                issues.append(
                    f"来源行在 L2 为阻塞状态，被强制导入（原因：{(force_reason or '').strip()}），须人工核对后再确认"
                )
            rows.append(
                IngestionRow(
                    batch=batch,
                    row_key=_text(payload_row.get("row_key"))[:80],
                    raw_json=payload_row,
                    mapped_json=payload_row.get("fields", {}) or {},
                    status="draft",
                    issues=issues,
                )
            )
        IngestionRow.objects.bulk_create(rows)
        AuditLog.objects.create(
            actor=actor,
            action="ingestion.import_reviewed",
            entity_type="IngestionBatch",
            entity_id=str(batch.pk),
            after_json={
                "run_id": report["run_id"],
                "raw_sha256": report.get("raw_sha256", ""),
                "sheet": item["sheet"],
                "panel_key": item["panel_key"],
                "rows": len(rows),
                "skipped_blocked": item["skipped"],
                "forced_blocked": len(forced_rows),
                "force_reason": (force_reason or "").strip(),
                "schema_version": SUPPORTED_SCHEMA_VERSION,
                "deliverability_status": report.get("status", ""),
            },
        )
        if forced_rows:
            AuditLog.objects.create(
                actor=actor,
                action="ingestion.import_reviewed.force_blocked",
                entity_type="IngestionBatch",
                entity_id=str(batch.pk),
                after_json={
                    "run_id": report["run_id"],
                    "sheet": item["sheet"],
                    "reason": (force_reason or "").strip(),
                    "note": f"强制包含阻塞行：{(force_reason or '').strip()}",
                    "row_ids": [r.get("row_key") for r in forced_rows],
                },
            )
    item["batch_id"] = batch.pk
    item["duplicate"] = False
    return item


def import_reviewed(
    reviewed_path,
    mapping_path=None,
    dry_run=False,
    include_blocked=False,
    actor="import",
    force_reason="",
):
    """读取 reviewed、校验、按映射生成草稿批次；dry_run 只校验不写。"""
    if include_blocked and not (force_reason or "").strip():
        raise ReviewImportError(
            "强制包含阻塞行必须给出原因：--include-blocked 只能和 --force-reason 一起用，"
            "原因会写进批次 note 和审计日志。"
        )
    reviewed = _read_json(reviewed_path)
    mapping, map_path = load_mapping(mapping_path)
    report = analyze_reviewed(
        reviewed, mapping, dry_run=dry_run, include_blocked=include_blocked
    )
    report["mapping_path"] = str(map_path)
    report["dry_run"] = bool(dry_run)
    report["include_blocked"] = bool(include_blocked)
    report["force_reason"] = (force_reason or "").strip()
    if dry_run:
        return report, map_path
    for item in report["sheets"]:
        if not item.get("payload"):
            continue
        _create_batch(
            report, item, actor=actor, include_blocked=include_blocked, force_reason=force_reason
        )
    return report, map_path
