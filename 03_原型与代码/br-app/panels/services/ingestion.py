"""Agent/插件接入的幂等与输入校验。"""
from __future__ import annotations

import hashlib
import json

MAX_ROWS = 500
MAX_BODY_BYTES = 2_000_000


class IngestionPayloadError(ValueError):
    pass


def payload_fingerprint(payload):
    """稳定哈希：去掉 idempotency_key 后再序列化，避免同一个包换 key 被当成新内容。"""
    body = dict(payload or {})
    body.pop("idempotency_key", None)
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_payload(payload, panel_keys):
    if not isinstance(payload, dict):
        raise IngestionPayloadError("请求体必须是 JSON 对象")
    panel_key = payload.get("panel_key")
    if not panel_key or not isinstance(panel_key, str):
        raise IngestionPayloadError("缺少 panel_key")
    if panel_key not in panel_keys:
        raise IngestionPayloadError(f"未知面板 {panel_key}")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise IngestionPayloadError("rows 必须是非空数组")
    if len(rows) > MAX_ROWS:
        raise IngestionPayloadError(f"一次最多 {MAX_ROWS} 行")
    seen = set()
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise IngestionPayloadError(f"第 {i} 行必须是对象")
        row_key = str(row.get("row_key") or f"R{i:04d}")
        if row_key in seen:
            raise IngestionPayloadError(f"row_key 重复：{row_key}")
        seen.add(row_key)
        fields = row.get("fields", {})
        if fields is not None and not isinstance(fields, dict):
            raise IngestionPayloadError(f"第 {i} 行 fields 必须是对象")
        comps = row.get("components", [])
        if comps is not None and not isinstance(comps, list):
            raise IngestionPayloadError(f"第 {i} 行 components 必须是数组")
    return panel_key, rows
