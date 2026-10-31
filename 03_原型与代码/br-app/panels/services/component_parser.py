"""成分明细拆分、归一、对账。

规则来源：02_图像识别 工作区的“拆开/原文段/对账”约定。
预览版只做 % 与无单位数值；其他单位先原样保留。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

SEP_RE = re.compile(r"[\s、,，;；/]+")
WATER_WORDS = ("水", "水分", "含水量", "水份", "含水")
MISC_WORDS = ("杂", "杂质", "杂分", "其他", "残余", "不纯物")
OTHER_WORDS = ("PH", "PH值", "酸度", "色度", "浊度", "电导", "电导率")


def _dec(value: str):
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return None


def classify(name: str) -> str:
    n = (name or "").strip().upper()
    if not n:
        return "misc"
    if n in [w.upper() for w in WATER_WORDS]:
        return "water"
    if n in [w.upper() for w in MISC_WORDS]:
        return "misc"
    if n in [w.upper() for w in OTHER_WORDS] or n.startswith("PH"):
        return "other"
    return "comp"


def _parse_token(token: str):
    """把一段（不含分隔符）拆成 name / numeric / unit / category。"""
    token = token.strip()
    raw = token
    if not token:
        return None

    # PH=7 / PH:7 / PH7
    m = re.match(r"^(PH|ph|PH值|酸碱度)\s*[=:：]?\s*(\d+(?:\.\d+)?)$", token)
    if m:
        num = _dec(m.group(2))
        return {
            "name": "PH",
            "numeric_value": num,
            "unit": "—",
            "raw_text": raw,
            "category": "other",
        }

    # 名字 + 数值 + 可选单位
    m = re.match(r"^(.+?)(\d+(?:\.\d+)?)\s*(%|‰|ppm|mg/L|g/L)?$", token)
    if m:
        name = m.group(1).strip(" =:：、,，;；")
        num = _dec(m.group(2))
        unit = m.group(3) or ""
        if unit == "%":
            numeric = (num / Decimal("100")) if num is not None else None
            unit_out = "%"
        elif unit == "‰":
            numeric = (num / Decimal("1000")) if num is not None else None
            unit_out = "‰"
        elif unit == "ppm":
            numeric = (num / Decimal("1000000")) if num is not None else None
            unit_out = "ppm"
        elif unit in ("mg/L", "g/L"):
            numeric = num
            unit_out = unit
        else:
            numeric = num
            unit_out = ""
        return {
            "name": name or "",
            "numeric_value": numeric,
            "unit": unit_out,
            "raw_text": raw,
            "category": classify(name),
        }

    # 没有数字：杂 / 不红 / 酸峰 / 描述性文字 这类
    cat = classify(token)
    if cat == "comp":
        cat = "misc"  # 没数值的描述性文字不应当成正常成分参与查询/合计
    return {
        "name": token,
        "numeric_value": None,
        "unit": "",
        "raw_text": raw,
        "category": cat,
    }


def split_components(text: str, field_key: str = "成分明细"):
    """把一格文字拆成成分行。返回 dict 列表（未入库）。"""
    text = (text or "").strip()
    if not text:
        return []
    tokens = [t.strip() for t in SEP_RE.split(text) if t.strip()]
    rows = []
    for token in tokens:
        parsed = _parse_token(token)
        if not parsed:
            continue
        parsed["field_key"] = field_key
        parsed["display"] = _format_numeric(parsed.get("numeric_value"), parsed.get("unit", ""))
        rows.append(parsed)
    return rows


def _format_numeric(numeric_value, unit: str) -> str:
    if numeric_value is None:
        return ""
    try:
        v = float(numeric_value)
    except Exception:
        return str(numeric_value)
    if unit == "%":
        out = f"{v * 100:.4f}".rstrip("0").rstrip(".")
        return out + "%"
    text = f"{v:g}"
    if unit in ("", "—"):
        return text
    return f"{text}{unit}"


def _strip_seps(text: str) -> str:
    return SEP_RE.sub("", text or "")


def reconcile(text: str, components) -> bool:
    """原文段按顺序拼回去（忽略分隔符）必须等于原格原文。"""
    original = _strip_seps(text)
    joined = "".join(_strip_seps(c.get("raw_text", "")) for c in components)
    return original == joined


def _get(c, key, default=None):
    if isinstance(c, dict):
        return c.get(key, default)
    return getattr(c, key, default)


def component_sum_percent(components) -> float:
    """参与合计的百分比之和（%）。水不计，杂有值才计，其他不计。

    同时支持 dict 和 Django Component 模型对象。
    """
    total = 0.0
    for c in components:
        if _get(c, "unit") != "%" or _get(c, "numeric_value") is None:
            continue
        if _get(c, "category") in ("water", "other"):
            continue
        total += float(_get(c, "numeric_value")) * 100
    return total


def _payload_component(name: str, raw_value: str, segment: str, field_key: str):
    name = (name or "").strip()
    raw_value = (raw_value or "").strip()
    segment = (segment or "").strip()
    if segment:
        text = segment
    elif raw_value:
        # 兼容两种插件写法：raw_value 既可能是整段（示例溶剂B60%），也可能只是数值（60%）
        if name and not raw_value.startswith(name):
            text = name + raw_value
        else:
            text = raw_value
    else:
        text = name
    parsed = _parse_token(text)
    if not parsed:
        return None
    if name and not parsed.get("name"):
        parsed["name"] = name
        parsed["category"] = classify(name)
    return {
        "name": parsed["name"],
        "numeric_value": parsed["numeric_value"],
        "unit": parsed["unit"],
        "raw_text": text,
        "category": parsed["category"],
        "field_key": field_key,
        "display": _format_numeric(parsed["numeric_value"], parsed["unit"]),
    }


def components_from_payload(row: dict, field_key: str = "成分明细"):
    """从插件 payload 的一行里取成分（兼容 components / 拆开 / 原文）。"""
    # 1) components: [{name, raw_value, raw_segment}]
    comps = row.get("components")
    if comps:
        out = []
        for item in comps:
            c = _payload_component(
                item.get("name") or "",
                item.get("raw_value") or "",
                item.get("raw_segment") or item.get("raw_text") or "",
                field_key,
            )
            if c:
                out.append(c)
        return out

    # 2) 拆开: {列名: [{名字, 值, 原文段}]}
    split = row.get("拆开") or row.get("split")
    if split:
        items = split.get(field_key) or next(iter(split.values()), [])
        out = []
        for item in items:
            c = _payload_component(
                item.get("名字") or item.get("name") or "",
                item.get("值") or item.get("value") or "",
                item.get("原文段") or item.get("raw_segment") or "",
                field_key,
            )
            if c:
                out.append(c)
        return out

    # 3) 原文
    fields = row.get("fields") or {}
    text = fields.get(field_key) or row.get(field_key) or ""
    return split_components(text, field_key)
