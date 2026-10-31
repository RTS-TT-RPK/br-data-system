"""来料分析：主成分、沸点差、共沸判断。

数据源：来料分析的成分行 + 物料性质（沸点）+ 共沸数据。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from panels.models import Component, Record

ALIASES = {
    "庚烷": "正庚烷",
    "乙酯": "乙酸乙酯",
    "IPAC": "乙酸异丙酯",
    "THF": "四氢呋喃",
    "DMF": "N,N-二甲基甲酰胺",
    "DMAC": "N,N-二甲基乙酰胺",
    "MCH": "甲基环己烷",
    "甲醚": "甲基叔丁基醚",
    "二甲基": "二甲苯",
    "三氧甲烷": "三氯甲烷",
}

WATER_BP = 100.0

AZEO_LABELS = {
    "是": "有共沸记录",
    "否": "无共沸记录",
    "部分未收录": "部分未收录",
    "无法判断": "数据不足",
    "部分": "部分未收录",
}

# 没有可识别有机成分时的固定说法：不得回退用水/杂/其它指标/PH 顶替主溶剂。
MAIN_UNKNOWN = "无法识别主溶剂"
MAIN_UNKNOWN_NOTE = "没有可识别的有机成分（只有水/杂/PH 或缺少百分比成分），不拿水/杂/PH 顶替主溶剂"


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "")).upper()


def canonical(name: str) -> str:
    raw = str(name or "").strip()
    return ALIASES.get(_norm(raw), ALIASES.get(raw, raw))


def alias_variants(name):
    """查询时把“庚烷/正庚烷”这类别名都算上，避免查询和分析用不同口径。"""
    target = canonical(name)
    out = {str(name or "").strip(), target}
    for alias, full in ALIASES.items():
        if canonical(full) == target:
            out.add(alias)
            out.add(full)
    return [x for x in out if x]


def parse_bp(value):
    """返回 (midpoint, label)。范围取中点；文本“无/—”返回 None。"""
    if value is None:
        return None, ""
    s = str(value).strip()
    if not s or s in ("-", "—", "/", "无"):
        return None, ""
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*[-~至]\s*(-?\d+(?:\.\d+)?)", s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return (a + b) / 2, f"{a:g}-{b:g}"
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if m:
        return float(m.group(0)), s
    return None, ""


class MasterData:
    def __init__(self):
        self.materials = {}   # canonical -> {name, bp, bp_label, cas, density}
        self.azeotropes = {}  # (canonA, canonB) -> {point, point_label, stable, difficulty, note}
        self._load_materials()
        self._load_azeotropes()

    def _load_materials(self):
        for rec in Record.objects.filter(panel_key="br_material_property", status=Record.STATUS_CONFIRMED):
            d = rec.data_json or {}
            raw_name = str(d.get("中文名") or "").strip()
            if not raw_name:
                continue
            bp, bp_label = parse_bp(d.get("沸点(℃)"))
            item = {
                "name": raw_name,
                "bp": bp,
                "bp_label": bp_label,
                "cas": d.get("CAS号") or "",
                "density": d.get("密度(g/mL)") or "",
            }
            self.materials[canonical(raw_name)] = item
            # 别名也指向同一物料
            for alias, full in ALIASES.items():
                if _norm(full) == _norm(raw_name):
                    self.materials[canonical(alias)] = item
        # 水不在物料性质表里，但共沸/沸点分析需要
        self.materials.setdefault("水", {"name": "水", "bp": WATER_BP, "bp_label": "100", "cas": "7732-18-5", "density": "1.0"})

    def _load_azeotropes(self):
        for rec in Record.objects.filter(panel_key="br_azeotrope", status=Record.STATUS_CONFIRMED):
            d = rec.data_json or {}
            a = str(d.get("组分A") or "").strip()
            b = str(d.get("组分B") or "").strip()
            if not a or not b:
                continue
            point, label = parse_bp(d.get("共沸点(℃)"))
            raw_point = str(d.get("共沸点(℃)") or "")
            if any(x in raw_point for x in ("无", "不共沸", "不形成")):
                stable = False
            elif point is not None:
                stable = True
            else:
                stable = None
            key = tuple(sorted([canonical(a), canonical(b)]))
            self.azeotropes[key] = {
                "point": point,
                "point_label": label,
                "stable": stable,
                "difficulty": d.get("分离难度") or "",
                "note": d.get("备注") or "",
                "a": a,
                "b": b,
            }

    def bp_of(self, name: str):
        item = self.materials.get(canonical(name))
        return (item["bp"], item["bp_label"]) if item else (None, "")

    def azeo_of(self, a: str, b: str):
        key = tuple(sorted([canonical(a), canonical(b)]))
        return self.azeotropes.get(key)


def _display_pct(component) -> float | None:
    if component.numeric_value is None or component.unit != "%":
        return None
    return float(component.numeric_value) * 100


def _fmt_pct(v):
    if v is None:
        return ""
    return f"{v:.4f}".rstrip("0").rstrip(".") + "%"


def _display_name(component) -> str:
    return component.component_std or component.component_original or ""


def _record_without_main(record: Record, master: MasterData, numeric, data):
    """没有可识别的有机百分比成分：主溶剂=无法识别主溶剂。

    水、杂/其它、PH 即使有数值也不能顶替主溶剂；沸点差/共沸一律“无法判断”。
    """
    others = []
    for c in numeric:
        name = _display_name(c)
        if not name:
            continue
        pct = _display_pct(c)
        bp, bp_label = master.bp_of(name)
        others.append(
            {
                "name": name,
                "pct": pct,
                "pct_label": _fmt_pct(pct),
                "bp": bp,
                "bp_label": bp_label,
                "gap": None,
                "gap_label": "",
                "azeo_status": "无法判断",
                "azeo_detail": "没有主溶剂，无法判断共沸",
            }
        )
    others.sort(key=lambda x: (-(x["pct"] or 0), x["name"]))
    other_text = "；".join(
        f"{o['name']} {o['pct_label']}"
        + (
            f"（沸点{o['bp_label'] or '?'}℃，无主溶剂未计算沸点差）"
            if o["bp"] is not None
            else "（沸点未收录，无主溶剂未计算沸点差）"
        )
        for o in others
    )
    return {
        "record_id": record.pk,
        "date": data.get("日期") or data.get("日期编号") or "",
        "location": data.get("位置") or data.get("储位") or "",
        "material": data.get("物料") or data.get("名称") or "",
        "raw_text": data.get("成分明细") or "",
        "main_name": MAIN_UNKNOWN,
        "main_original": "",
        "main_pct": None,
        "main_pct_label": "",
        "main_bp": None,
        "main_bp_label": "",
        "main_unknown": True,
        "main_note": MAIN_UNKNOWN_NOTE,
        "others": others,
        "min_gap": None,
        "max_gap": None,
        "min_gap_label": "",
        "max_gap_label": "",
        "azeo_flag": "无法判断",
        "azeo_label": AZEO_LABELS["无法判断"],
        "azeo_note": "无法识别主溶剂，沸点差与二元共沸无法判断（资料提示，不是工艺结论）",
        "other_text": other_text,
    }


def analyze_record(record: Record, master: MasterData):
    comps = list(record.components.all())
    numeric = [c for c in comps if _display_pct(c) is not None]
    # 主溶剂只从“可识别的有机成分”里选：category=comp 且带百分比数值。
    # 没有任何有机成分时不再回退到水/杂/其它指标/PH。
    comp_candidates = [c for c in numeric if c.category == "comp"]
    main = max(comp_candidates, key=lambda c: _display_pct(c) or 0) if comp_candidates else None
    d0 = record.data_json or {}
    if main is None:
        return _record_without_main(record, master, numeric, d0)

    main_pct = _display_pct(main)
    main_name = _display_name(main)
    main_bp, main_bp_label = master.bp_of(main_name)

    others = []
    for c in numeric:
        if c.pk == main.pk:
            continue
        if c.category not in ("comp", "water"):
            continue
        pct = _display_pct(c)
        name = _display_name(c)
        bp, bp_label = master.bp_of(name)
        gap = abs(main_bp - bp) if (main_bp is not None and bp is not None) else None
        az = master.azeo_of(main_name, name)
        if az is None:
            azeo_status = "未收录"
            azeo_detail = "共沸表里没有这一对"
        elif az["stable"] is True:
            azeo_status = "是"
            azeo_detail = f"共沸点 {az['point_label'] or az['point']}℃" + (f"，分离难度：{az['difficulty']}" if az["difficulty"] else "")
        elif az["stable"] is False:
            azeo_status = "否"
            azeo_detail = f"表中注明：{az['note'] or '无稳定共沸'}"
        else:
            azeo_status = "不确定"
            azeo_detail = az["note"] or "共沸点数据不明确"
        others.append(
            {
                "name": name,
                "pct": pct,
                "pct_label": _fmt_pct(pct),
                "bp": bp,
                "bp_label": bp_label,
                "gap": gap,
                "gap_label": f"{gap:.1f}" if gap is not None else "",
                "azeo_status": azeo_status,
                "azeo_detail": azeo_detail,
            }
        )
    others.sort(key=lambda x: (-(x["pct"] or 0), x["name"]))

    gaps = [o["gap"] for o in others if o["gap"] is not None]
    min_gap = min(gaps) if gaps else None
    max_gap = max(gaps) if gaps else None
    azeo_yes = [o for o in others if o["azeo_status"] == "是"]
    azeo_known = [o for o in others if o["azeo_status"] in ("是", "否")]
    if azeo_yes:
        azeo_flag = "是"
        azeo_note = "；".join(f"{o['name']}：{o['azeo_detail']}" for o in azeo_yes)
    elif not azeo_known:
        azeo_flag = "无法判断"
        azeo_note = "其它成分的沸点/共沸数据未收录"
    elif len(azeo_known) < len([o for o in others if o["bp"] is not None]):
        azeo_flag = "部分未收录"
        azeo_note = "部分成分共沸数据未收录"
    else:
        azeo_flag = "否"
        azeo_note = "其它成分与主成分未发现稳定共沸"

    d = record.data_json or {}
    return {
        "record_id": record.pk,
        "date": d.get("日期") or d.get("日期编号") or "",
        "location": d.get("位置") or d.get("储位") or "",
        "material": d.get("物料") or d.get("名称") or "",
        "raw_text": d.get("成分明细") or "",
        "main_name": main_name,
        "main_original": main.component_original,
        "main_pct": main_pct,
        "main_pct_label": _fmt_pct(main_pct),
        "main_bp": main_bp,
        "main_bp_label": main_bp_label,
        "main_unknown": False,
        "main_note": "",
        "others": others,
        "min_gap": min_gap,
        "max_gap": max_gap,
        "min_gap_label": f"{min_gap:.1f}" if min_gap is not None else "",
        "max_gap_label": f"{max_gap:.1f}" if max_gap is not None else "",
        "azeo_flag": azeo_flag,
        "azeo_label": AZEO_LABELS.get(azeo_flag, azeo_flag),
        "azeo_note": azeo_note,
        "other_text": "；".join(
            f"{o['name']} {o['pct_label']}" + (f"（沸点{o['bp_label'] or '?'}℃，差{o['gap_label']}℃）" if o["bp"] is not None else "（沸点未收录）")
            for o in others
        ),
    }


def analyze_incoming():
    master = MasterData()
    rows = []
    for rec in (
        Record.objects.filter(panel_key="incoming_analysis", status=Record.STATUS_CONFIRMED)
        .prefetch_related("components")
        .order_by("id")
    ):
        item = analyze_record(rec, master)
        if item:
            rows.append(item)
    return rows


def summarize_by_material(rows):
    """按物料汇总；对“无法识别主溶剂”/None 值不崩，也不把未知当 0。"""
    groups = defaultdict(list)
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        groups[str(r.get("material") or "（未填）")].append(r)
    out = []
    for material, items in sorted(groups.items()):
        mains = Counter(str(x.get("main_name") or MAIN_UNKNOWN) for x in items)
        main_name, main_count = mains.most_common(1)[0]
        pcts = [x.get("main_pct") for x in items if x.get("main_pct") is not None]
        gaps = [x.get("min_gap") for x in items if x.get("min_gap") is not None]
        flags = [x.get("azeo_flag") for x in items]
        azeo_yes = sum(1 for f in flags if f == "是")
        if azeo_yes:
            azeo_flag = "是"
        elif flags and all(f == "否" for f in flags):
            azeo_flag = "否"
        elif flags and all(f == "无法判断" for f in flags):
            azeo_flag = "无法判断"
        else:
            azeo_flag = "部分"
        bp = next(
            (
                x.get("main_bp")
                for x in items
                if x.get("main_name") == main_name and x.get("main_bp") is not None
            ),
            None,
        )
        out.append(
            {
                "material": material,
                "count": len(items),
                "main_name": main_name,
                "main_count": main_count,
                "main_unknown": main_name == MAIN_UNKNOWN,
                "unknown_main_count": sum(
                    1 for x in items if (x.get("main_name") or MAIN_UNKNOWN) == MAIN_UNKNOWN
                ),
                "avg_pct": sum(pcts) / len(pcts) if pcts else None,
                "avg_gap": sum(gaps) / len(gaps) if gaps else None,
                "bp": bp,
                "azeo_yes": azeo_yes,
                "azeo_flag": azeo_flag,
                "azeo_label": AZEO_LABELS.get(azeo_flag, azeo_flag),
            }
        )
    return out


def component_summary():
    rows = list(
        Component.objects.select_related("record")
        .filter(record__panel_key="incoming_analysis", unit="%")
        .exclude(numeric_value=None)
    )
    agg = defaultdict(list)
    for c in rows:
        agg[c.component_std].append(float(c.numeric_value) * 100)
    out = []
    for name, vals in agg.items():
        out.append(
            {
                "name": name,
                "count": len(vals),
                "avg": sum(vals) / len(vals),
                "max": max(vals),
                "min": min(vals),
            }
        )
    out.sort(key=lambda x: (-x["count"], -x["avg"]))
    return out



def material_component_summary():
    """按物料 × 成分汇总（每种物料对应哪些成分、平均/最高/最低含量）。"""
    from collections import defaultdict
    groups = defaultdict(lambda: defaultdict(list))
    for rec in Record.objects.filter(panel_key="incoming_analysis", status=Record.STATUS_CONFIRMED).prefetch_related("components"):
        material = (rec.data_json or {}).get("物料") or (rec.data_json or {}).get("名称") or "（未填）"
        for c in rec.components.all():
            if c.unit != "%" or c.numeric_value is None or c.category not in ("comp", "water"):
                continue
            groups[material][c.component_std].append(float(c.numeric_value) * 100)
    out = []
    for material, comps in groups.items():
        for name, vals in comps.items():
            out.append({
                "material": material,
                "component": name,
                "count": len(vals),
                "avg": sum(vals) / len(vals),
                "max": max(vals),
                "min": min(vals),
            })
    out.sort(key=lambda x: (x["material"], -x["avg"]))
    return out
