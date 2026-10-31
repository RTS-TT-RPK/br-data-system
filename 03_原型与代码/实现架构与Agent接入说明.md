# 面板驱动数据应用 · 实现架构与 Agent 接入说明

> 版本：v1.1（数据一致性/幂等加固）　｜　日期：2026-10-28
> 代码位置：03_数据系统开发/03_原型与代码/br-app
> 运行地址：http://127.0.0.1:8765/
> 截图目录：br-app/screenshots/（界面截图含真实数据，未随公开仓库发布）

---

## 1. 一句话定位

把 AI 从图片/单据里提取出来的表格数据接进来，用“像 Excel 一样的面板”做增删改查、成分拆分、条件查询、来料分析和 Excel 汇总；Agent/插件通过 HTTP 接口送数，正式数据必须人工确认。

不是完整 ERP：不做库存复式过账、生产工单执行、排产优化、财务成本。

---

## 2. 运行方式（Windows 原生）

- Windows 10/11 64 位，不需要 WSL/Linux/虚拟机/Docker。
- 双击 br-app/启动预览.bat：建库 → 导入 legacy 种子 → 启动 Django → 打开浏览器。
- 默认只绑定 127.0.0.1:8765；不做备份/恢复（用户明确要求）。
- 暂停/停止：关闭启动窗口或 Ctrl+C。

---

## 3. 总体架构

~~~text
┌───────────────────────────────────────────────────────────────┐
│ Windows 单机                                                   │
│                                                               │
│  Edge/Chrome 浏览器                                            │
│      │ HTML / 表单 / 下载 Excel                                │
│      ▼                                                        │
│  Django 应用（单进程，127.0.0.1:8765）                          │
│   ├─ 面板引擎    PanelDefinition → 通用表格/表单/详情/审核       │
│   ├─ 数据服务    Record / Reading / Component                   │
│   ├─ 成分解析    split_components / reconcile / sum             │
│   ├─ 来料分析    主溶剂 + 沸点 + 共沸                             │
│   ├─ 查询服务    成分条件 / 通用数据查询 / 导出                   │
│   ├─ 报表服务    openpyxl 综合汇总/明细/图表                     │
│   └─ 接入 API    /api/health /panels /records /ingestions ...   │
│      │                                                         │
│      ├─ SQLite（data/preview.sqlite3）                          │
│      └─ seeds/panels.json + seeds/legacy_data.json              │
│                                                               │
│  DSH / Agent / 图像识别插件                                     │
│   └─ HTTP JSON ──→ /api/ingestions ──→ 草稿 ──→ 人工审核 ──→ 正式 │
└───────────────────────────────────────────────────────────────┘
~~~

关键边界：

- Agent 不能直接写正式记录、不能执行 SQL；只能提交草稿。
- 人工在审核页确认后，才生成正式 Record + Component。
- 所有页面、查询、报表读同一份 SQLite 数据，不另存 Excel 副本。

---

## 4. 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 运行 | Windows 10/11 64 位 | 原生，不依赖 WSL/Docker |
| Python | 3.14（本机） / 3.13 目标 | 便携运行时装 3.13 |
| Web | Django 5.2.18 | ORM、模板、迁移、表单、权限基础 |
| DB | SQLite 3 | 单机单写；JSONField 存宽表 |
| 前端 | Django 模板 + 原生 CSS（含少量 JS）+ 本地静态 | 没有 Node/构建链 |
| Excel | openpyxl 3.1.5 | 生成汇总报表/查询导出 |
| 启动 | .bat | 预览版直接 runserver |

---

## 5. 代码结构

~~~text
br-app/
├─ brpreview/              Django 配置
│  ├─ settings.py          SQLite、时区、API_TOKEN 可选
│  └─ urls.py
├─ panels/
│  ├─ models.py            PanelDefinition / Record / Component / Reading /
│  │                       IngestionBatch / IngestionRow / AuditLog
│  ├─ views.py             页面 + API
│  ├─ urls.py
│  ├─ templates/panels/    base/home/table/form/detail/review/query/
│  │                       data_query/incoming_analysis
│  ├─ services/
│  │  ├─ component_parser.py  拆分、归一、对账、合计
│  │  ├─ analysis.py          物料主数据、沸点、共沸、按物料汇总
│  │  ├─ review_import.py     reviewed.json → 映射 → L3 草稿批次
│  │  └─ report.py            openpyxl 综合报表/查询导出
│  ├─ management/commands/seed_preview.py
│  ├─ management/commands/import_reviewed.py
│  ├─ tests.py             28 个自动化测试
│  └─ migrations/
├─ seeds/
│  ├─ panels.json          10 个面板定义（BR 9 + 来料分析）
│  ├─ reviewed_maps.json    L2 sheet → L3 panel/字段映射
│  └─ legacy_data.json     39 条合成示例种子（全部虚构）
├─ static/panels/app.css
├─ tools/
│  ├─ agent_client.py      Agent 推送 JSON 的最小客户端
│  └─ sample_ingestion.json
├─ data/preview.sqlite3    运行时数据库
├─ 示例_综合物料汇总.xlsx   一键报表示例
└─ 启动预览.bat
~~~

---

## 6. 数据模型

### 6.1 panel_definition（面板图纸）

| 字段 | 说明 |
|---|---|
| key | 内部标识，如 br_inventory（不展示给用户） |
| name | 中文名 |
| row_unit | 一行代表什么 |
| style | br_table / image_review / generic |
| columns | JSON：key/label/type/required/width/group |
| form_layout | JSON：表单分组/行 |
| measurements | 成分合计规则（sum_max/categories） |
| actions / permissions | 可用操作/权限 |
| version / status | 版本/发布状态 |

### 6.2 record（宽表一行）

| 字段 | 说明 |
|---|---|
| panel_key | 属于哪个面板 |
| panel_version | 保存时的面板版本 |
| data_json | 整行原值（含成分明细原文） |
| search_text | 所有值拼成的可搜索文本（SQLite JSON 中文 LIKE 的替代） |
| source_type | manual/agent/plugin/import/seed |
| source_run_id / source_row_id | 来源批次/行号 |
| status | draft/confirmed/void/deleted |
| confidence / image_sha | 置信度/原图哈希 |
| component_parser_version | 成分解析器版本（默认 v1） |

### 6.3 component（成分行）

| 字段 | 说明 |
|---|---|
| record_id | 所属记录 |
| field_key | 来源列（成分明细） |
| component_std / component_original | 标准名/原文名 |
| numeric_value | 百分比存小数（90%→0.9） |
| unit | % / — / 其他 |
| raw_text | 图上原文段 |
| category | comp/water/misc/other |

### 6.4 reading（读数行，模型已留）

液位/密度/使用率等可比较数值，一行一个；预览版主要用 data_json + component，reading 预留。

### 6.5 接入批次

- ingestion_batch：panel_key、source、payload、payload_hash、idempotency_key、status（pending/approved/rejected）。
- ingestion_row：row_key、raw_json、mapped_json、issues、record（确认后关联）；(batch,row_key) 唯一。
- 相同 payload_hash 或相同 idempotency_key 的重复 POST 返回原批次，不重复建账。

### 6.6 当前数据量（按合成种子）

- 面板配置 10 个（BR 9 + 来料分析）。
- 按合成种子灌入后：记录约 39 条（含来料分析 2 条）；成分约 25 条；待审核批次 0。
- 主数据：示例物料 3、示例共沸 3、示例储位 4、示例液位标定 12。

---

## 7. 面板引擎

### 7.1 配置驱动

一个面板定义 = 列 + 表单布局 + 校验 + 样式 + 操作。页面只写一次：

- /p/<panel_key>/ 通用表格；
- /p/<panel_key>/new/、/p/<panel_key>/<id>/edit/ 通用表单；
- /p/<panel_key>/<id>/ 通用详情；
- 导出 CSV。

加新面板 = 加一份 JSON，不改路由/模板；特殊列类型才加渲染器。

### 7.2 样式

- br_table：细边框、深蓝表头视觉、紧凑、固定表头；
- image_review：审核页风格；
- generic：通用。

### 7.3 首批面板

BR 9 张：物料性质、共沸数据、储存位置、储罐液位体积、五金配件库、物料库存、操作日志、车间投料工艺卡、车间生产记录；另加“来料分析”。

---

## 8. 成分解析与存储

### 8.1 拆分规则

原格：示例溶剂B60%、示例溶剂C30%、杂、水1%、PH7

拆成：

| 成分 | 数值 | 单位 | 原文 | 分类 |
|---|---|---|---|---|
| 示例溶剂B | 0.6 | % | 示例溶剂B60% | comp |
| 示例溶剂C | 0.3 | % | 示例溶剂C30% | comp |
| 杂 | — | — | 杂 | misc |
| 水 | 0.01 | % | 水1% | water |
| PH | 7 | — | PH7 | other |

### 8.2 对账（硬规则）

所有 raw_text 按顺序拼回去（忽略空白/顿号/逗号/分号/斜杠）必须等于原格；
拼不上 → 页面提示/草稿问题，不静默写。

### 8.3 合计

- 水不计；杂有值才计；PH 等指标不计；
- 超过 sum_max 报“计入合计异常”；
- 不自动改数。

### 8.4 别名

常见别名：庚烷→正庚烷、乙酯→乙酸乙酯、IPAC→乙酸异丙酯、THF→四氢呋喃、DMF/DMAC/MCH 等；未收录显示未识别。

---

## 9. 来料分析算法

输入：来料分析记录的成分行。
主数据：物料性质（沸点）、共沸数据、水（100℃ 内置）。

步骤：

1. 取 category=comp 且百分比最高的成分为“最高有机溶剂”（水/杂/PH 排除）；
2. 查物料性质得沸点；范围值取中点（如 150-180→165），保留原范围标签；
3. 对其余每个成分（comp/water）：
   - 查沸点；
   - 算 |主沸点 − 其它沸点|；
   - 查共沸表（按标准名成对，双向）；
   - 结果：是（共沸点/难度）/否（无稳定共沸）/未收录/不确定；
4. 汇总 min/max 沸点差、总体是否共沸；
5. 输出按物料汇总、物料×成分明细、逐条明细。

示例（合成数据）：

> 示例混料1：主溶剂示例溶剂B 60%（沸点 120.0℃）；示例溶剂C 30%（145.0℃，差 25.0℃）；水 1%（100℃，差 20.0℃）；示例溶剂B-示例溶剂C 共沸点 110.0℃ → 共沸“是”。

已知：示例种子里的成分都带百分比；若真实记录没有可比较的百分比成分 → 显示“未识别/无法判断”，不猜。

---

## 10. 查询

### 10.1 成分条件查询 /query/

同一行多个成分阈值：

- 条件：不低于/高于/不高于/低于/介于；
- “不高于/低于”把未检出成分按 0，所以“甲苯≤5%”会包括不含甲苯的记录；
- 示例：物料名称含“示例溶剂”，示例溶剂B≥70，水≤5 → 当前 2 条；
- 实现：Django ORM Exists，负条件用 NOT EXISTS；避免多成分 join 互相干扰。

### 10.2 通用数据查询 /data/query/

- 任意面板、任意列；
- 操作：包含/不包含/等于/不等于/大于/小于/介于/为空/不为空；
- 最多 5 个条件；
- 结果分页；导出 Excel（openpyxl，带样式/冻结表头）；
- 实现：Python 内存过滤（当前数据量小；以后可下推 SQL/索引）。

### 10.3 成分查询 /components/

单成分按分类/面板/百分比范围查，适合“含水最高”这类快速浏览。

---

## 11. Agent / 插件接入（重点）

### 11.1 数据流

~~~text
图像识别插件
   │ 识别结果（宽表字段 + components/拆开 + 原文）
   ▼
DSH Agent / HTTP 工具
   │ POST /api/ingestions  (JSON)
   ▼
应用：校验/拆分/对账/幂等 → ingestion_batch + ingestion_row(draft)
   │
   ▼
人工审核页 /ingestions/<id>/  ← 左字段右成分（原图位预留）
   │ 确认选中/全部
   ▼
正式 Record + Component（status=confirmed）→ 出现在来料分析面板
~~~

### 11.2 当前 API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /api/health | 健康检查/计数/是否要求 token |
| GET | /api/panels | 全部面板与列 schema |
| GET | /api/records?panel_key=&q=&limit= | 读正式记录（含成分） |
| GET | /api/components?name=&category= | 读成分 |
| GET | /api/analysis/incoming | 一键来料分析 JSON |
| POST | /api/ingestions | 插件/Agent 送草稿（核心） |
| GET | /api/ingestions/<id> | 批次状态/行数/审核链接 |

送数幂等：可带 Idempotency-Key 头或 payload.idempotency_key；相同内容重复 POST 返回原批次（duplicate=true，HTTP 200）；同 key 不同内容返回 409；一次最多 500 行、请求体 2MB。

### 11.3 送数 JSON 示例

~~~json
{
  "panel_key": "incoming_analysis",
  "source": {"type": "dsh_agent", "name": "示例 Agent", "run_id": "DEMO-AGENT-001"},
  "rows": [{
    "row_key": "R0001",
    "fields": {
      "日期": "2026-10-27", "位置": "示例库区1#", "物料": "示例来料",
      "批次": "DEMO-01", "桶号": "D01", "厂商": "示例供应商",
      "成分明细": "示例溶剂A90% 水5%"
    },
    "components": [
      {"name": "示例溶剂A", "raw_value": "90%"},
      {"name": "水", "raw_value": "5%"}
    ],
    "uncertain": [],
    "confidence": 0.95
  }]
}
~~~

返回：

~~~json
{"ok": true, "batch_id": 10, "rows": 1, "review_url": "/ingestions/10/", "message": "已接收为草稿，需人工审核确认"}
~~~

### 11.4 DSH Agent 怎么调

两种方式：

1. HTTP 工具：DSH Agent 直接发请求到 http://127.0.0.1:8765/api/ingestions。
2. 本地脚本：Agent 把 JSON 写成文件，调用 python tools/agent_client.py tools/sample_ingestion.json。

已提供：br-app/tools/agent_client.py、br-app/tools/sample_ingestion.json。实测返回 batch_id，审核后进入正式面板。

### 11.5 L2 reviewed.json 导入（本轮新增）

L2 审核台导出 reviewed.json 后，不需要手工复制；用管理命令按映射生成 **L3 待审核草稿**：

~~~bat
cd C:\Users\<用户名>\Desktop\数据系统开发\03_数据系统开发\03_原型与代码\br-app
python manage.py import_reviewed --reviewed C:\...\reviewed.json --dry-run
python manage.py import_reviewed --reviewed C:\...\reviewed.json
~~~

- 映射：seeds/reviewed_maps.json；先接“库存明细 → br_inventory”，其他表按同样格式增加。
- 只把 status=ok 的行导入；blocked 行默认跳过，可用 --include-blocked 强制收。
- 人工改过的成分数值会转成规范文本，再交给 L3 重新解析；不会直接写正式记录。
- 幂等：同一 reviewed 文件重复执行会返回原批次，不重复建账。
- 导入后到 /ingestions/<id>/ 人工确认；命令本身没有 --approve 选项。

### 11.6 安全边界


- 默认只绑本机；无登录（预览版）。
- 写接口可选 X-API-Key：设置环境变量 BR_API_TOKEN 后，POST 必须带该头；默认不设（预览方便）。
- 正式使用前必须加：token 生命周期、作用域、限流、审计查看、附件上传校验。
- Agent 永远只能提交草稿；不能直接改正式数据/执行 SQL。
- 当前没有实现 SQL 适配器：只接受 JSON。插件如果只出 SQL，需要中间层转 JSON，或下一步加“白名单 INSERT 解析”。

### 11.7 MCP

当前未实现。推荐后续薄适配：把 /api/health、/api/records、/api/analysis/incoming、/api/ingestions 包装成 MCP 工具，调同一 REST，不写第二套业务逻辑。

---

## 12. Excel 报表

### 12.1 生成方式

- 首页“一键综合报表”或来料分析页“下载综合 Excel”；
- 使用 openpyxl，直接生成 .xlsx（不依赖 Office/WPS；不用宏）。

### 12.2 工作簿结构

1. 综合汇总：KPI、来料按物料汇总、库存概览、五金库；
2. 来料分析明细：主溶剂/沸点/沸点差/共沸；
3. 库存明细：12 列；
4. 成分汇总；
5. 物料成分明细：物料 × 成分平均/最高/最低；
6. 图表：来料成分出现次数 Top 10。

### 12.3 样式

参照《综合物料报表-修订版》：

- 标题：微软雅黑 16pt 加粗、色 #1B2A4A；
- 表头：深蓝 #1B2A4A、白字、加粗、细边框；
- 汇总/KPI：浅蓝 #D6E4F0；
- 数据：微软雅黑 10.5，细边框；
- A4 横向、按宽缩放、冻结表头、自动筛选。

示例：br-app/示例_综合物料汇总.xlsx（含真实数据，未随公开仓库发布）

---

## 13. 页面地图

| 路由 | 页面 |
|---|---|
| / | 首页：统计/面板卡/一键入口 |
| /p/<key>/ | 通用表格（搜索/分页/CSV） |
| /p/<key>/new、/<id>/edit | 通用表单 |
| /p/<key>/<id>/ | 详情（成分拆行） |
| /ingestions/ | 接入批次 |
| /ingestions/<id>/ | 审核（字段+成分） |
| /analysis/incoming/ | 一键来料分析 |
| /query/ | 成分多条件 |
| /data/query/ | 通用数据查询 |
| /components/ | 成分查询 |

截图：界面截图含真实库存/来料数据，未随公开仓库发布（只留在本地 br-app/screenshots/）。

---

## 14. 测试与优化记录

### 14.1 自动化测试

命令：

~~~bat
python manage.py test
~~~

结果：28 个测试全部通过（约 0.3 秒）。

覆盖：

- 成分拆分/对账/合计；
- 页面 200；
- 手动新增/编辑/删除；
- 插件 API → 草稿 → 审核确认；
- 成分多条件查询；
- 通用数据查询 + 导出；
- 综合报表 xlsx 可打开、含指定 sheet、关键单元格；
- Agent API（health/records/analysis）；
- 批次详情 API；
- 事务回滚：创建/编辑成分失败不留下半成品；
- 重复 POST 幂等、重复 row_key 拒绝；
- 重复确认/拒绝已通过批次保护；
- 来料分析黄金样本（主溶剂/沸点/沸点差/共沸状态）；
- 一致性检查命令。

### 14.2 性能测量（本机预览数据）

| 页面/接口 | 查询数 | 耗时 |
|---|---|---|
| 首页 | 6 | ~82ms |
| 物料库存（50 行/页） | 6 | ~26ms |
| 来料分析页 | 8 | ~64ms |
| 通用数据查询（8 行筛选） | 3 | ~18ms |
| 综合报表 .xlsx | 15 | ~634ms |
| API 记录（50 行） | 2 | ~6ms |

### 14.3 本轮修的问题

1. 中文 JSON 在 SQLite 上 icontains 检索不到 → 新增 search_text 字段并回填，搜索/查询恢复。
2. 来料模拟接入时成分名重复（“示例溶剂B示例溶剂B”）→ 修正 payload 兼容逻辑。
3. 审核页“原文段拼不回去”误报 → 修正重复名后对账通过。
4. 首页每个面板单独 count（N+1）→ 一次 aggregate。
5. /api/records 每条记录单独查成分（N+1）→ prefetch。
6. 综合报表库存概览重复计算含水 → 一次遍历。
7. 成分合计标签“合计”容易误解（水不计）→ 改“计入合计”。
8. 面板卡片/表格显示内部英文 key → 全部改中文/隐藏。

---

## 15. 已知限制与风险

| 问题 | 影响 | 建议 |
|---|---|---|
| 无登录/默认无 token | 本机以外不可暴露 | 正式版加账号 + token + 权限 |
| 业务级去重未做 | 同一车/批次换一个来源重复送仍可能重复建账 | 加图片哈希/业务键/疑似重复提示；payload 级幂等已做 |
| 无 SQL 适配器 | 只出 SQL 的插件不能直接接 | 中间层转 JSON，或白名单 INSERT 解析 |
| 别名覆盖有限 | 未收录成分显示“未识别” | 用对照表扩充别名 |
| 示例记录缺少百分比成分 | 主溶剂“未识别” | 合成种子已避免；真实单据常见，保留人工处理 |
| 报表为静态值 | 无公式联动 | 需要联动再设计公式版 |
| 无原图附件页 | 审核看不到图 | 下一步加 attachment 上传/预览 |
| 无面板设计器 UI | 加面板要改 seeds JSON | 下一步做设计器 |
| runserver | 非生产服务器 | 换 waitress + 启动器 |
| 无备份 | 用户明确不要 | 接受风险，导出 Excel 留档 |
| SQLite 单写 | 高并发/多终端会瓶颈 | 2–5 用户可；再多换 PostgreSQL |

---

## 16. 下一步优先级

1. 原图上传 + 审核页左图右表（Agent 证据链）；
2. 业务级去重（图片哈希 + 业务键 + 人工疑似重复）；
3. 面板设计器 UI（新建/复制/改列/改样式）；
4. Agent token 生命周期/限流/审计；
5. MCP 薄适配；
6. 报表增加驾驶舱/储区图（如果需要复刻原工作簿）；
7. waitress + 启动器打包；
8. Gemini 前端改造落地（截图/审查包含真实数据，未随公开仓库发布）。

---

## 17. 给 GPT 的审核重点

1. 这个架构是否真的“面板配置驱动”，有没有隐藏写死？
2. 宽表 data_json + 成分长表拆分，是否会出现数据不一致/查询瓶颈？
3. 成分拆分/对账/合计规则是否有漏洞？
4. 来料分析的主溶剂/沸点差/共沸口径是否成立？未收录处理是否诚实？
5. 条件查询的“未检出按 0”语义是否符合业务？
6. Agent 接入链路（JSON→草稿→人工审核→正式）是否安全？还缺什么？
7. payload 级幂等已做、业务级去重未做；正式用之前还必须补什么？
8. Excel 报表是否足够接近参考样式？还缺哪些表/图？
9. 28 个测试是否足以证明账/数据正确？还缺哪些用例？
10. Windows 原生 + runserver + SQLite 的交付风险？

---

## 18. 2026-10-28 加固记录（v1.1）

针对 GPT 评审的 F1/F3/F5：

1. **统一事务写入**：新增 panels/services/records.py；Record + Component 全量重建 + AuditLog 同事务；手动新增/编辑、Agent 审核确认统一走 save_record；严格对账失败拒绝写入；记录增加 component_parser_version。
2. **幂等/防重**：IngestionBatch 增加 payload_hash/idempotency_key；IngestionRow 增加 (batch,row_key) 唯一约束；重复 POST 返回原批次；同 key 不同内容 409；重复确认/已通过批次拒绝保护；审核/拒绝写 AuditLog。
3. **测试补强**：28 个测试，新增事务回滚、幂等、重复 row_key、重复确认、黄金分析样本、报表关键单元格、一致性命令。
4. **分析口径降级**：主溶剂/共沸记录措辞加限定；页面和报表加“未收录不等于不存在共沸、未计算多组分汽液平衡”的提示；状态改为有记录/无记录/部分未收录/数据不足。
5. **一致性检查**：新增 check_consistency 命令；结构性问题退出 1；重复成分先提示、不自动改（示例数据 Record#9001/9002 原文含重复段；合成种子本身无重复段）。
6. **别名查询统一**：成分条件查询用 alias_variants 把“庚烷/正庚烷”等视为同一成分，避免查询口径和分析口径不一致。
7. **前端**：本轮不改视觉，只产出 11 张截图 + Gemini 审查说明 + 前端审查包 zip（截图含真实数据，未随公开仓库发布），等 Gemini 给改造建议。

当前仍是预览版：业务级去重、原图附件、登录权限、SQL 适配器、MCP、面板设计器未做。
