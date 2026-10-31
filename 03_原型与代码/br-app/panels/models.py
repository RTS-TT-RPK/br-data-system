from django.db import models


def make_search_text(data) -> str:
    """把一行宽表的所有值拼成可搜索文本（SQLite JSONField 存中文会被转义，不能直接 LIKE）。"""
    if not isinstance(data, dict):
        return ""
    parts = []
    for v in data.values():
        if v is None:
            continue
        s = str(v).strip()
        if s:
            parts.append(s)
    return " ".join(parts)


class PanelDefinition(models.Model):
    """一张面板的图纸：列、表单布局、校验、样式都是配置。"""

    key = models.CharField("面板标识", max_length=80, unique=True)
    name = models.CharField("面板名", max_length=120)
    row_unit = models.CharField("一行代表什么", max_length=200, blank=True, default="")
    style = models.CharField("样式", max_length=40, default="br_table")
    columns = models.JSONField("列定义", default=list)
    form_layout = models.JSONField("表单布局", default=list)
    measurements = models.JSONField("计量/合计规则", default=dict)
    actions = models.JSONField("可用操作", default=list)
    permissions = models.JSONField("权限", default=dict)
    version = models.IntegerField("版本", default=1)
    status = models.CharField("状态", max_length=20, default="published")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        verbose_name = "面板定义"
        verbose_name_plural = "面板定义"

    STYLE_LABELS = {"br_table": "BR 表格风", "image_review": "审核风", "generic": "通用风"}

    @property
    def style_label(self):
        return self.STYLE_LABELS.get(self.style, "表格风")

    def __str__(self):
        return f"{self.name}（{self.key}）"

    def column_keys(self):
        return [c.get("key") for c in (self.columns or [])]

    def column_by_key(self):
        return {c.get("key"): c for c in (self.columns or [])}


class Record(models.Model):
    """宽表里的一行。"""

    STATUS_DRAFT = "draft"
    STATUS_CONFIRMED = "confirmed"
    STATUS_VOID = "void"
    STATUS_DELETED = "deleted"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "草稿"),
        (STATUS_CONFIRMED, "已确认"),
        (STATUS_VOID, "已作废"),
        (STATUS_DELETED, "已删除"),
    ]
    SOURCE_CHOICES = [
        ("manual", "人工"),
        ("agent", "Agent"),
        ("plugin", "插件"),
        ("import", "导入"),
        ("seed", "种子"),
    ]

    panel_key = models.CharField("面板", max_length=80, db_index=True)
    panel_version = models.IntegerField("面板版本", default=1)
    data_json = models.JSONField("宽表数据", default=dict)
    search_text = models.TextField("搜索文本", blank=True, default="")
    source_type = models.CharField("来源", max_length=20, choices=SOURCE_CHOICES, default="manual")
    source_run_id = models.CharField("来源任务", max_length=120, blank=True, default="")
    source_row_id = models.CharField("来源行号", max_length=80, blank=True, default="")
    status = models.CharField("状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_CONFIRMED)
    image_sha = models.CharField("原图哈希", max_length=64, blank=True, default="")
    confidence = models.FloatField("置信度", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.IntegerField("版本", default=1)
    component_parser_version = models.CharField("成分解析器版本", max_length=20, blank=True, default="v1")

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["panel_key", "status"]),
            models.Index(fields=["source_run_id"]),
        ]
        verbose_name = "记录"
        verbose_name_plural = "记录"

    def __str__(self):
        return f"{self.panel_key}#{self.pk}"


class Component(models.Model):
    """成分明细拆出来的一行。"""

    CATEGORY_CHOICES = [
        ("comp", "成分"),
        ("water", "水"),
        ("misc", "杂/其他"),
        ("other", "指标"),
    ]

    record = models.ForeignKey(Record, on_delete=models.CASCADE, related_name="components")
    field_key = models.CharField("来源列", max_length=80, default="成分明细")
    component_std = models.CharField("标准名", max_length=200, blank=True, default="")
    component_original = models.CharField("原文名", max_length=200, blank=True, default="")
    numeric_value = models.DecimalField("数值", max_digits=14, decimal_places=6, null=True, blank=True)
    unit = models.CharField("单位", max_length=20, blank=True, default="")
    raw_text = models.CharField("图上原文", max_length=500, blank=True, default="")
    category = models.CharField("分类", max_length=20, choices=CATEGORY_CHOICES, default="comp")
    confidence = models.FloatField("置信度", null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["component_std"]),
            models.Index(fields=["category"]),
            models.Index(fields=["field_key"]),
        ]
        verbose_name = "成分"
        verbose_name_plural = "成分"

    def display_value(self):
        if self.numeric_value is None:
            return ""
        if self.unit == "%":
            return f"{float(self.numeric_value) * 100:.4f}".rstrip("0").rstrip(".") + "%"
        text = f"{float(self.numeric_value):g}"
        return f"{text}{self.unit if self.unit != '—' else ''}"

    def __str__(self):
        return f"{self.component_std}={self.display_value()}"


class Reading(models.Model):
    """一行里的普通数值（液位/密度/使用率等），预览版先保留模型。"""

    record = models.ForeignKey(Record, on_delete=models.CASCADE, related_name="readings")
    field_key = models.CharField(max_length=80)
    name = models.CharField(max_length=120)
    numeric_value = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True, default="")
    raw_text = models.CharField(max_length=200, blank=True, default="")
    category = models.CharField(max_length=20, blank=True, default="measurement")

    class Meta:
        ordering = ["id"]
        verbose_name = "读数"
        verbose_name_plural = "读数"


class IngestionBatch(models.Model):
    """插件/Agent 一次送来的数据。"""

    STATUS_CHOICES = [
        ("pending", "待审核"),
        ("approved", "已通过"),
        ("rejected", "已拒绝"),
    ]

    panel_key = models.CharField(max_length=80, db_index=True)
    source_type = models.CharField(max_length=30, default="plugin")
    source_name = models.CharField(max_length=200, blank=True, default="")
    source_run_id = models.CharField(max_length=120, blank=True, default="")
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    idempotency_key = models.CharField(max_length=120, blank=True, default="", db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    note = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_ingestion_idempotency_key",
            ),
        ]
        verbose_name = "接入批次"
        verbose_name_plural = "接入批次"

    def __str__(self):
        return f"{self.panel_key} / {self.source_name} / #{self.pk}"


class IngestionRow(models.Model):
    STATUS_CHOICES = [
        ("draft", "草稿"),
        ("approved", "已通过"),
        ("rejected", "已拒绝"),
    ]

    batch = models.ForeignKey(IngestionBatch, on_delete=models.CASCADE, related_name="rows")
    row_key = models.CharField(max_length=80, blank=True, default="")
    raw_json = models.JSONField(default=dict)
    mapped_json = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    issues = models.JSONField(default=list, blank=True)
    record = models.ForeignKey(Record, null=True, blank=True, on_delete=models.SET_NULL, related_name="ingestion_rows")

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "row_key"], name="uniq_ingestion_batch_row_key"),
        ]
        verbose_name = "接入行"
        verbose_name_plural = "接入行"


class AuditLog(models.Model):
    actor = models.CharField(max_length=80, default="preview")
    action = models.CharField(max_length=80)
    entity_type = models.CharField(max_length=40)
    entity_id = models.CharField(max_length=80, blank=True, default="")
    before_json = models.JSONField(default=dict, blank=True)
    after_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "审计日志"
        verbose_name_plural = "审计日志"
