"""工作包 G：查询与来料分析口径测试（合成数据，仅内存测试库）。

覆盖：
1. 主溶剂只从 category="comp" 的百分比成分选；只有水/只有 PH/只有杂/只有其它指标/
   只有无单位数值/没有成分 → "无法识别主溶剂"，不回退水、杂、other、PH。
2. 缺成分 = 未知，不是 0；明确记录为 0 才是零。
3. lte/lt 默认要求成分有记录；missing_as_zero 才按 0 假设并标注。
4. 别名筛选与结果说明使用同一套 alias_variants 口径（命中时给别名说明，不误报未检出）。
5. 来料分析页保留"沸点差与二元共沸数据是资料提示，不是工艺结论"。
6. 汇总函数对 None / "无法识别主溶剂" 不崩；正常样例的分析值与旧口径一致。
"""
from decimal import Decimal

from django.test import TestCase

from panels.models import Component, PanelDefinition, Record, make_search_text
from panels.services.analysis import (
    MAIN_UNKNOWN,
    MasterData,
    analyze_incoming,
    analyze_record,
    summarize_by_material,
)
from panels.services.records import save_record


def _panel(key, name, columns):
    return PanelDefinition.objects.create(
        key=key, name=name, row_unit="一行 = 一个对象", columns=columns,
        form_layout=[], measurements={}, actions=[], permissions={}, version=1,
        status="published",
    )


def _record(panel_key, data, source="seed"):
    return Record.objects.create(
        panel_key=panel_key, panel_version=1, data_json=data,
        search_text=make_search_text(data), source_type=source,
        status=Record.STATUS_CONFIRMED,
    )


def _component(record, name, pct, unit="%", category="comp", raw=""):
    value = Decimal(str(pct)) / Decimal("100") if unit == "%" else Decimal(str(pct))
    return Component.objects.create(
        record=record, field_key="成分明细", component_std=name,
        component_original=name, numeric_value=value, unit=unit,
        raw_text=raw or f"{name}{pct}%", category=category,
    )


def _analysis_panel():
    return _panel("incoming_analysis", "来料分析", [
        {"key": "日期", "label": "日期", "type": "text", "width": 120},
        {"key": "位置", "label": "位置", "type": "text", "width": 120},
        {"key": "物料", "label": "物料", "type": "text", "width": 120},
        {"key": "成分明细", "label": "成分明细", "type": "components", "width": 300},
    ])


def _inventory_panel():
    return _panel("br_inventory", "物料库存", [
        {"key": "名称", "label": "名称", "type": "text", "width": 120},
        {"key": "储位", "label": "储位", "type": "text", "width": 120},
        {"key": "储存区域", "label": "储存区域", "type": "text", "width": 120},
        {"key": "成分明细", "label": "成分明细", "type": "components", "width": 300},
    ])


class MainSolventTests(TestCase):
    """主溶剂口径：只认可识别的有机百分比成分，不拿水/杂/PH/其它指标顶替。"""

    @classmethod
    def setUpTestData(cls):
        _analysis_panel()
        cls.ids = {}
        cases = []

        rec = _record("incoming_analysis", {"物料": "只有水", "成分明细": "水100%"})
        _component(rec, "水", 100, category="water")
        cases.append(("只有水", rec.pk))

        rec = _record("incoming_analysis", {"物料": "只有PH", "成分明细": "PH7"})
        _component(rec, "PH", 7, unit="—", category="other")
        cases.append(("只有PH", rec.pk))

        rec = _record("incoming_analysis", {"物料": "只有杂", "成分明细": "杂10%"})
        _component(rec, "杂", 10, category="misc")
        cases.append(("只有杂", rec.pk))

        rec = _record("incoming_analysis", {"物料": "空成分", "成分明细": ""})
        cases.append(("空成分", rec.pk))

        # 其它指标（category=other）即使带百分比也不能当主溶剂。
        rec = _record("incoming_analysis", {"物料": "只有其它指标", "成分明细": "酸度5%"})
        _component(rec, "酸度", 5, category="other")
        cases.append(("只有其它指标", rec.pk))

        # 有数值但没有百分比单位：不是"百分比成分"，不能参与主溶剂判定。
        rec = _record("incoming_analysis", {"物料": "只有无单位数值", "成分明细": "庚烷5"})
        _component(rec, "庚烷", 5, unit="")
        cases.append(("只有无单位数值", rec.pk))

        rec = _record("incoming_analysis", {"物料": "水多有机少", "成分明细": "水95% 甲苯3%"})
        _component(rec, "水", 95, category="water")
        _component(rec, "甲苯", 3, category="comp")
        cases.append(("水多有机少", rec.pk))

        cls.ids = dict(cases)

    def _analyze(self, material):
        rec = Record.objects.get(pk=self.ids[material])
        return analyze_record(rec, MasterData())

    def test_water_only_cannot_be_main(self):
        row = self._analyze("只有水")
        self.assertEqual(row["main_name"], MAIN_UNKNOWN)
        self.assertIsNone(row["main_pct"])
        self.assertIsNone(row["main_bp"])
        self.assertTrue(row["main_unknown"])
        self.assertEqual(row["azeo_flag"], "无法判断")
        self.assertIn("资料提示", row["azeo_note"])
        # 有记录的水仍应列在"其它成分"里供人查看，但不能成为主溶剂。
        self.assertIn("水", row["other_text"])

    def test_ph_only_cannot_be_main(self):
        row = self._analyze("只有PH")
        self.assertEqual(row["main_name"], MAIN_UNKNOWN)
        self.assertIsNone(row["main_pct"])
        self.assertTrue(row["main_unknown"])

    def test_misc_only_cannot_be_main(self):
        row = self._analyze("只有杂")
        self.assertEqual(row["main_name"], MAIN_UNKNOWN)
        self.assertTrue(row["main_unknown"])

    def test_other_indicator_cannot_be_main(self):
        row = self._analyze("只有其它指标")
        self.assertEqual(row["main_name"], MAIN_UNKNOWN)
        self.assertTrue(row["main_unknown"])

    def test_non_percent_component_cannot_be_main(self):
        row = self._analyze("只有无单位数值")
        self.assertEqual(row["main_name"], MAIN_UNKNOWN)
        self.assertTrue(row["main_unknown"])

    def test_no_components_cannot_be_main(self):
        row = self._analyze("空成分")
        self.assertEqual(row["main_name"], MAIN_UNKNOWN)
        self.assertIsNone(row["main_pct"])
        self.assertEqual(row["others"], [])
        self.assertEqual(row["other_text"], "")
        self.assertEqual(row["azeo_flag"], "无法判断")

    def test_organic_component_wins_over_water(self):
        row = self._analyze("水多有机少")
        self.assertEqual(row["main_name"], "甲苯")
        self.assertNotEqual(row["main_name"], "水")
        self.assertAlmostEqual(row["main_pct"], 3.0, places=6)
        self.assertFalse(row["main_unknown"])

    def test_incoming_rows_include_unknown_main(self):
        rows = analyze_incoming()
        by_material = {row["material"]: row for row in rows}
        self.assertIn("只有水", by_material)
        self.assertEqual(by_material["只有水"]["main_name"], MAIN_UNKNOWN)
        self.assertEqual(by_material["水多有机少"]["main_name"], "甲苯")
        self.assertEqual(sum(1 for row in rows if row["main_unknown"]), 6)


class AnalysisGoldenValuesTests(TestCase):
    """旧口径的正常样例值必须保持不变（不改 panels/tests.py）。"""

    @classmethod
    def setUpTestData(cls):
        _analysis_panel()
        _panel("br_material_property", "物料性质", [
            {"key": "中文名", "label": "中文名", "type": "text", "width": 120},
            {"key": "沸点(℃)", "label": "沸点", "type": "text", "width": 100},
        ])
        _panel("br_azeotrope", "共沸数据", [
            {"key": "组分A", "label": "组分A", "type": "text", "width": 120},
            {"key": "组分B", "label": "组分B", "type": "text", "width": 120},
            {"key": "共沸点(℃)", "label": "共沸点", "type": "text", "width": 100},
            {"key": "备注", "label": "备注", "type": "text", "width": 200},
        ])
        _record("br_material_property", {"中文名": "正庚烷", "沸点(℃)": "98.4"})
        _record("br_material_property", {"中文名": "甲苯", "沸点(℃)": "111"})
        _record("br_azeotrope", {
            "组分A": "正庚烷", "组分B": "甲苯",
            "共沸点(℃)": "无恒定共沸", "备注": "不共沸",
        })
        rec = _record("incoming_analysis", {
            "日期": "2026-10-27", "位置": "示例库区1#", "物料": "示例混料2",
            "成分明细": "庚烷85% 甲苯3% 水1%",
        })
        _component(rec, "庚烷", 85, category="comp", raw="庚烷85%")
        _component(rec, "甲苯", 3, category="comp", raw="甲苯3%")
        _component(rec, "水", 1, category="water", raw="水1%")

    def test_analysis_values_unchanged(self):
        rows = analyze_incoming()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["main_name"], "庚烷")
        self.assertAlmostEqual(row["main_pct"], 85.0, places=6)
        self.assertAlmostEqual(row["main_bp"], 98.4, places=6)
        self.assertAlmostEqual(row["min_gap"], 1.6, places=6)
        self.assertAlmostEqual(row["max_gap"], 12.6, places=6)
        self.assertEqual(row["azeo_label"], "部分未收录")
        by_name = {o["name"]: o for o in row["others"]}
        self.assertAlmostEqual(by_name["甲苯"]["gap"], 12.6, places=6)
        self.assertAlmostEqual(by_name["水"]["gap"], 1.6, places=6)

    def test_summary_keeps_normal_values(self):
        summary = summarize_by_material(analyze_incoming())
        self.assertEqual(len(summary), 1)
        item = summary[0]
        self.assertEqual(item["material"], "示例混料2")
        self.assertEqual(item["main_name"], "庚烷")
        self.assertAlmostEqual(item["avg_pct"], 85.0, places=6)
        self.assertAlmostEqual(item["avg_gap"], 1.6, places=6)
        self.assertAlmostEqual(item["bp"], 98.4, places=6)


class SummaryNoneSafetyTests(TestCase):
    """汇总函数对 None / 无法识别主溶剂不崩。"""

    def test_summarize_none_rows(self):
        rows = [
            {
                "material": "只有水", "main_name": MAIN_UNKNOWN, "main_pct": None,
                "min_gap": None, "main_bp": None, "azeo_flag": "无法判断",
            },
            {
                "material": "只有水", "main_name": MAIN_UNKNOWN, "main_pct": None,
                "min_gap": None, "main_bp": None, "azeo_flag": "无法判断",
            },
        ]
        out = summarize_by_material(rows)
        self.assertEqual(len(out), 1)
        item = out[0]
        self.assertEqual(item["main_name"], MAIN_UNKNOWN)
        self.assertTrue(item["main_unknown"])
        self.assertIsNone(item["avg_pct"])
        self.assertIsNone(item["avg_gap"])
        self.assertIsNone(item["bp"])
        self.assertEqual(item["azeo_flag"], "无法判断")
        self.assertEqual(item["azeo_label"], "数据不足")

    def test_summarize_empty_and_none(self):
        self.assertEqual(summarize_by_material([]), [])
        self.assertEqual(summarize_by_material(None), [])

    def test_summarize_missing_keys_do_not_crash(self):
        out = summarize_by_material([{"material": "X"}])
        self.assertEqual(out[0]["main_name"], MAIN_UNKNOWN)
        self.assertIsNone(out[0]["avg_pct"])


class ComboQueryMissingVsZeroTests(TestCase):
    """lte/lt 默认要求有记录；明确 0 与缺失必须区分。"""

    @classmethod
    def setUpTestData(cls):
        panel = _inventory_panel()
        cls.ids = {}
        for name, text in [
            ("缺失料", "庚烷90%"),
            ("零料", "庚烷80% 甲苯0%"),
            ("低料", "庚烷70% 甲苯2%"),
            ("高料", "庚烷60% 甲苯8%"),
        ]:
            rec, _ = save_record(panel, {
                "名称": name, "储位": "示例库区1#", "储存区域": "示例库区1#", "成分明细": text,
            })
            cls.ids[name] = rec.pk

    def _names(self, response):
        return [row["record"].data_json.get("名称") for row in response.context["rows"]]

    def _checks(self, response):
        return {
            row["record"].data_json.get("名称"): row["checks"][0]
            for row in response.context["rows"]
        }

    def test_lte_default_requires_component_record(self):
        r = self.client.get("/query/", {
            "panel_key": "br_inventory",
            "cond_name_1": "甲苯", "cond_op_1": "lte", "cond_val_1": "5",
        })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context["missing_as_zero"])
        self.assertEqual(self._names(r), ["零料", "低料"])
        # 明确记录为 0 的行要显示 0% 并注明是明确记录，而不是"未知"。
        zero = self._checks(r)["零料"]
        self.assertTrue(zero["ok"])
        self.assertIn("0%", zero["display"])
        self.assertIn("明确记录为 0", zero["note"])

    def test_lte_default_does_not_treat_missing_as_zero(self):
        r = self.client.get("/query/", {
            "panel_key": "br_inventory",
            "cond_name_1": "甲苯", "cond_op_1": "lte", "cond_val_1": "100",
        })
        self.assertNotIn("缺失料", self._names(r))
        self.assertIn("高料", self._names(r))

    def test_lte_missing_as_zero_matches_and_marks_assumption(self):
        r = self.client.get("/query/", {
            "panel_key": "br_inventory",
            "cond_name_1": "甲苯", "cond_op_1": "lte", "cond_val_1": "5",
            "missing_as_zero": "1",
        })
        self.assertTrue(r.context["missing_as_zero"])
        self.assertEqual(self._names(r), ["缺失料", "零料", "低料"])
        missing = self._checks(r)["缺失料"]
        self.assertTrue(missing["ok"])
        self.assertEqual(missing["display"], "未记录（按 0 假设）")
        self.assertIn("查询假设，不是实测", missing["note"])
        # 明确记录 0 与缺失显示不同
        self.assertIn("明确记录为 0", self._checks(r)["零料"]["note"])

    def test_lt_default_and_missing_as_zero(self):
        base = {
            "panel_key": "br_inventory",
            "cond_name_1": "甲苯", "cond_op_1": "lt", "cond_val_1": "1",
        }
        r = self.client.get("/query/", base)
        self.assertEqual(self._names(r), ["零料"])
        r2 = self.client.get("/query/", dict(base, missing_as_zero="on"))
        self.assertEqual(self._names(r2), ["缺失料", "零料"])

    def test_lte_zero_only_matches_explicit_zero_by_default(self):
        r = self.client.get("/query/", {
            "panel_key": "br_inventory",
            "cond_name_1": "甲苯", "cond_op_1": "lte", "cond_val_1": "0",
        })
        self.assertEqual(self._names(r), ["零料"])
        r2 = self.client.get("/query/", {
            "panel_key": "br_inventory",
            "cond_name_1": "甲苯", "cond_op_1": "lte", "cond_val_1": "0",
            "missing_as_zero": "1",
        })
        self.assertEqual(self._names(r2), ["缺失料", "零料"])

    def test_gte_still_requires_record_even_with_switch(self):
        r = self.client.get("/query/", {
            "panel_key": "br_inventory",
            "cond_name_1": "甲苯", "cond_op_1": "gte", "cond_val_1": "0",
            "missing_as_zero": "1",
        })
        self.assertEqual(self._names(r), ["零料", "低料", "高料"])
        self.assertNotIn("缺失料", self._names(r))

    def test_page_explains_unknown_vs_assumed_zero(self):
        r = self.client.get("/query/")
        self.assertContains(r, "把缺失成分按 0 处理（假设，不是实测）")
        self.assertContains(r, "无记录（未知）")
        r2 = self.client.get("/query/", {
            "cond_name_1": "甲苯", "cond_op_1": "lte", "cond_val_1": "5",
            "missing_as_zero": "1",
        })
        self.assertContains(r2, "未记录（按 0 假设）")
        r3 = self.client.get("/query/", {
            "cond_name_1": "甲苯", "cond_op_1": "lte", "cond_val_1": "5",
        })
        self.assertContains(r3, "缺失成分按“未知”处理")


class ComboQueryAliasTests(TestCase):
    """筛选与结果说明使用同一套 alias_variants；命中时给别名说明，不误报未检出。"""

    @classmethod
    def setUpTestData(cls):
        panel = _inventory_panel()
        cls.ids = {}
        for name, text in [("庚烷料", "庚烷85% 甲苯3%"), ("正庚烷料", "正庚烷80% 甲苯3%")]:
            rec, _ = save_record(panel, {
                "名称": name, "储位": "示例库区2#", "储存区域": "示例库区2#", "成分明细": text,
            })
            cls.ids[name] = rec.pk

    def test_alias_condition_matches_and_explains(self):
        r = self.client.get("/query/", {
            "panel_key": "br_inventory",
            "cond_name_1": "正庚烷", "cond_op_1": "gte", "cond_val_1": "80",
        })
        self.assertEqual(r.status_code, 200)
        rows = r.context["rows"]
        self.assertEqual(len(rows), 2)
        self.assertIn("庚烷料", [row["record"].data_json["名称"] for row in rows])
        # 页面给别名口径说明
        self.assertIn("别名", r.context["alias_notes"])
        self.assertContains(r, "别名口径")
        by_material = {row["record"].data_json["名称"]: row["checks"][0] for row in rows}
        # 记录写“庚烷”，条件写“正庚烷”：命中且说明是别名命中，不能提示未检出。
        alias_check = by_material["庚烷料"]
        self.assertTrue(alias_check["ok"])
        self.assertIn("85%", alias_check["display"])
        self.assertIn("别名命中", alias_check["note"])
        self.assertIn("庚烷", alias_check["note"])
        self.assertNotIn("未检出", alias_check["display"])
        # 记录写“正庚烷”：正常名字命中，不需要别名说明。
        exact_check = by_material["正庚烷料"]
        self.assertTrue(exact_check["ok"])
        self.assertNotIn("别名命中", exact_check["note"])

    def test_alias_condition_other_direction(self):
        r = self.client.get("/query/", {
            "panel_key": "br_inventory",
            "cond_name_1": "庚烷", "cond_op_1": "gte", "cond_val_1": "80",
        })
        rows = r.context["rows"]
        self.assertEqual(len(rows), 2)
        by_material = {row["record"].data_json["名称"]: row["checks"][0] for row in rows}
        self.assertTrue(by_material["庚烷料"]["ok"])
        self.assertNotIn("别名命中", by_material["庚烷料"]["note"])
        self.assertIn("别名命中", by_material["正庚烷料"]["note"])

    def test_missing_alias_component_not_misreported(self):
        # 预测"无记录"时不能报成"未检出（按 0）"：关闭开关=未知，开启开关=按 0 假设。
        from decimal import Decimal as _Decimal

        from panels.views import _condition_check

        cond = {
            "name": "正庚烷", "op": "lt", "dec": _Decimal("0.01"),
            "dec2": None, "variants": ["正庚烷", "庚烷"],
        }
        unknown = _condition_check(cond, [], missing_as_zero=False)
        self.assertEqual(unknown["display"], "无记录（未知）")
        self.assertFalse(unknown["ok"])
        assumed = _condition_check(cond, [], missing_as_zero=True)
        self.assertEqual(assumed["display"], "未记录（按 0 假设）")
        self.assertTrue(assumed["ok"])
        self.assertNotIn("未检出", assumed["display"])


class IncomingAnalysisPageTests(TestCase):
    """页面展示：无法识别主溶剂 + 保留资料提示语。"""

    @classmethod
    def setUpTestData(cls):
        _analysis_panel()
        rec = _record("incoming_analysis", {
            "日期": "2026-10-27", "位置": "示例库区1#", "物料": "只有水",
            "成分明细": "水100%",
        })
        _component(rec, "水", 100, category="water")
        rec2 = _record("incoming_analysis", {
            "日期": "2026-10-27", "位置": "示例库区2#", "物料": "正常料",
            "成分明细": "甲苯3%",
        })
        _component(rec2, "甲苯", 3, category="comp")

    def test_page_shows_unknown_main_and_disclaimer(self):
        r = self.client.get("/analysis/incoming/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["unknown_main_count"], 1)
        self.assertContains(r, MAIN_UNKNOWN)
        self.assertContains(r, "沸点差与二元共沸数据是资料提示，不是工艺结论")
        rows = {row["material"]: row for row in r.context["rows"]}
        self.assertEqual(rows["只有水"]["main_name"], MAIN_UNKNOWN)
        self.assertTrue(rows["只有水"]["main_unknown"])
        self.assertEqual(rows["正常料"]["main_name"], "甲苯")
        summary = {item["material"]: item for item in r.context["summary"]}
        self.assertTrue(summary["只有水"]["main_unknown"])
        self.assertEqual(summary["正常料"]["main_name"], "甲苯")

    def test_api_marks_unknown_main(self):
        r = self.client.get("/api/analysis/incoming")
        self.assertEqual(r.status_code, 200)
        items = {item["material"]: item for item in r.json()["items"]}
        self.assertTrue(items["只有水"]["main_unknown"])
        self.assertEqual(items["只有水"]["main_name"], MAIN_UNKNOWN)
        self.assertFalse(items["正常料"]["main_unknown"])
