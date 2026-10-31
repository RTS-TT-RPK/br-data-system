# BR 数据预览版（Django 单机）

这是“面板驱动数据接入与管理系统”的第一个可运行预览版。目标：用**合成示例种子**模拟插件/Agent 从图片读出来的数据，跑通：

- 通用面板表格 / 表单 / 查看 / 删除
- 宽表 + 成分拆分 + 原文对账 + 合计提示
- 插件/Agent JSON 接入 → 草稿 → 人工审核 → 确认入库
- 按成分/含量查询
- 10 个面板种子：BR 9 张表 + 来料分析

## 启动

双击 启动预览.bat（或命令行）：

~~~bat
python manage.py migrate
python manage.py seed_preview
python manage.py runserver 127.0.0.1:8765
~~~

浏览器打开 http://127.0.0.1:8765/ 。

> 仓库自带合成演示种子 `seeds/legacy_data.json`，首次运行可直接 `seed_preview`；**所有示例数据均为虚构**，不含真实业务数据。

首次 seed_preview 会导入以下**合成示例**数据（全部虚构）：

| 面板 | 行数 | 说明 |
|---|---|---|
| 物料性质 | 3 | 合成示例主数据（示例溶剂A/B/C） |
| 共沸数据 | 3 | 合成示例 |
| 储存位置 | 4 | 合成示例（示例库区1#–4#） |
| 储罐液位体积 | 12 | 合成示例 |
| 物料库存 | 8 | 合成示例；2 行带成分 |
| 来料分析 | 2 | 合成示例 |
| 五金/操作日志 | 2/2 | 合成示例 |
| 投料工艺卡/生产记录 | 1/2 | 合成示例 |

> 以上种子随仓库发布，可直接 seed_preview；所有示例数据均为虚构合成，不含真实库存/来料。主数据（物料/储位/共沸/标定）用于演示；外接硬盘备份见下文，默认不配置目标，本轮未对真实数据执行。

按合成种子灌入后演示库约为：39 条记录、25 条成分、2 条来料分析；无待审批次。

## 页面

- / 面板列表
- /p/<panel_key>/ 通用表格（搜索/分页/导出 CSV）
- /p/<panel_key>/new/ 新增
- /p/<panel_key>/<id>/ 查看
- /p/<panel_key>/<id>/edit/ 编辑
- /ingestions/ 接入批次
- /ingestions/<id>/ 审核
- /analysis/incoming/ 一键来料分析
- /query/ 成分多条件查询
- /data/query/ 通用数据查询（任意面板任意列）
- /components/ 成分查询
- /export/summary.xlsx 一键综合报表
- /settings/backup/ 外接硬盘备份（目标配置 / 检查路径 / 立即备份）

## 一键来料分析

点左侧“来料分析”或来料分析面板上的“一键分析”：

- 每条来料记录提取**含量最高的有机溶剂**（排除水/杂/PH）；
- 到“物料性质”取沸点，到“共沸数据”查这一对是否共沸；
- 计算它与其它成分的沸点差（最小/最大/逐个）；
- 按物料汇总，并输出“每种物料 × 成分”的平均/最高/最低含量；
- 右上角“下载综合 Excel”生成带汇总表、明细、图表的工作簿。

注意：成分名会做常见别名映射（庚烷→正庚烷、乙酯→乙酸乙酯、IPAC→乙酸异丙酯、THF→四氢呋喃、DMF/DMAC/MCH 等）；未收录的成分会显示“未收录/无法判断”，不猜。

## 数据查询（任意面板、任意列）

点左侧“数据查询”：

- 面板可选物料库存、五金配件库、物料性质、共沸、储位、生产记录等全部面板；
- 对任意列设置条件：包含、等于、大于/小于、介于、为空/不为空；
- 结果分页，并可“导出 Excel”。

## 一键综合报表

首页“综合报表”或来料分析页“下载综合 Excel”会生成：

- 综合汇总（KPI、来料按物料汇总、库存概览、五金库）；
- 来料分析明细（主溶剂/沸点差/共沸）；
- 库存明细；
- 成分汇总；
- 物料成分明细；
- 图表（成分出现次数 Top 10）。

样式参照《综合物料报表-修订版》：微软雅黑、深蓝表头、浅蓝汇总行、细边框、冻结表头。

## 条件查询（同一行多个成分阈值）

打开 /query/，或点左侧“条件查询”。

示例：物料名称包含“示例溶剂”，条件 1“示例溶剂B 不低于 70”，条件 2“水 不高于 5”，
即可查“物料名含示例溶剂、示例溶剂B≥70% 且 水≤5%”的记录。

- “不低于 / 高于”：要求该成分存在并达到阈值；
- “不高于 / 低于”：未检出该成分按 0，视为满足；
- “介于”：包含两端；
- 可同时加“面板”“物料名称包含”筛选。

## 模拟插件接入

1. 打开“来料分析”面板，点“用示例库存模拟一批来料”；
2. 系统把物料库存里带成分的 2 行记录转成 2 行来料分析草稿；
3. 打开审核页，左侧字段、右侧成分；可勾选/全部确认；
4. 确认后进入“来料分析”正式面板，成分行可查询。

## API（给插件/Agent）

~~~http
POST /api/ingestions
Content-Type: application/json

{
  "panel_key": "incoming_analysis",
  "source": {"type": "dsh_plugin", "run_id": "20990101_001"},
  "rows": [
    {
      "row_key": "R0001",
      "fields": {
        "日期": "2026-10-27",
        "位置": "示例库区1#",
        "物料": "示例溶剂A",
        "批次": "DEMO-001",
        "桶号": "D01",
        "厂商": "示例供应商",
        "成分明细": "示例溶剂A90% 水5% PH7"
      },
      "components": [
        {"name": "示例溶剂A", "raw_value": "90%"},
        {"name": "水", "raw_value": "5%"},
        {"name": "PH", "raw_value": "7"}
      ],
      "confidence": 0.93
    }
  ]
}
~~~

返回 batch_id 与 review_url，仍需人工审核确认。

幂等/防重：

- 可以带请求头 Idempotency-Key（或 payload 里 idempotency_key）；
- 相同内容重复 POST 会返回原批次，duplicate=true，HTTP 200，不新建；
- 同一个 key 配不同内容会返回 409；
- 批次内 row_key 必须唯一；
- 一次最多 500 行，请求体上限 2MB；
- 重复确认已通过批次不会重复建账；已通过批次不能整批拒绝。

### Agent 参考端

- 客户端：[tools/agent_client.py](tools/agent_client.py)
- 示例数据：[tools/sample_ingestion.json](tools/sample_ingestion.json)

~~~bat
python tools/agent_client.py tools/sample_ingestion.json
~~~

### API 一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /api/health | 健康检查/计数/是否要 token |
| GET | /api/panels | 面板与列 schema |
| GET | /api/records?panel_key=&q=&limit= | 读正式记录（含成分） |
| GET | /api/components?name=&category= | 读成分 |
| GET | /api/analysis/incoming | 来料分析 JSON |
| POST | /api/ingestions | 插件/Agent 送草稿 |
| GET | /api/ingestions/<id> | 批次状态/审核链接 |

预览版默认不鉴权；设置环境变量 BR_API_TOKEN 后，POST /api/ingestions 必须带 X-API-Key。

### L2 reviewed.json 导入（新增）

L2 审核台导出 reviewed.json 后，用命令按映射生成 L3 草稿批次，不直接写正式记录：

~~~bat
python manage.py import_reviewed --reviewed C:\path\reviewed.json --dry-run
python manage.py import_reviewed --reviewed C:\path\reviewed.json
~~~

映射在 seeds/reviewed_maps.json；当前先接“库存明细 → br_inventory”。只导入 status=ok 的行，blocked 默认跳过；同一文件重复执行返回原批次。导入后到 /ingestions/<id>/ 人工确认。

## 数据一致性检查

所有正式写入统一走 panels/services/records.py 的事务服务：Record、旧成分删除、新成分写入、AuditLog 在同一个事务里；原文拼不回去默认拒绝保存。

~~~bat
python manage.py check_consistency
~~~

结构性问题退出码 1；legacy 里“重复成分”只提示、不自动改，需人工判定。

## 外接硬盘备份（未对真实数据执行）

2026-10-28 起支持把工作数据复制到另一块硬盘的独立文件夹。**本轮只实现功能并用临时目录/合成数据测试；目标盘符尚未配置，没有对真实 runs/inbox/pending/archive/golden 或 data/preview.sqlite3 执行任何备份，也不实现恢复。**

- 入口：左侧“备份设置”（/settings/backup/）。填写目标文件夹、勾选“我确认这是另一块硬盘”，可“保存配置 / 检查路径 / 立即备份”。
- 配置：JSON（不是数据库），默认 data/backup_config.json，可用 settings.BACKUP_CONFIG_PATH 覆盖；记录 target_dir、confirmed_external、auto_daily、l1_workspace、last（success/failed/not_configured）与 last_success。
- 内容：db/preview.sqlite3（sqlite3.Connection.backup 一致快照 → 临时文件 → PRAGMA integrity_check 通过后才改正式文件名）、config/panels_snapshot.json + seeds/*.json、l1/runs|inbox|pending|archive|golden|schemas 与 l1/aliases.json、l1/对照表.json、backup_manifest.json（时间/来源/目标/文件数/总字节/数据库检查/排除与失败信息/每条 scope 文件数）。
- 行为：只复制，不移动、不改写来源；不删除旧备份；不做恢复；不覆盖 preview.sqlite3；失败时不把临时文件当成功，也不会偷偷改存到系统盘。
- 失败即明说：目标未配置、盘未接入、不是文件夹、不可写、空间不足、快照失败、完整性检查失败都会明确失败并保留旧备份，页面显示“当前没有新备份”。
- 自动备份：每天第一次打开首页时在后台线程尝试一次；失败后当天不反复重试。测试期间（manage.py test）不会触发。
- 排除：__pycache__、*.pyc、*.tmp、测试数据库 test_*.sqlite3、.env/*.key/*.pem/credentials*、node_modules、.git。
- 验证口径：只报告“副本通过 SQLite 结构检查”，不等于“恢复已验证”。备份测试全部使用 tempfile 合成来源/目标与临时 SQLite，不读写真实数据。

## 单人本机与未来联网边界

- 当前只供单人本机使用：Django 仍绑定 127.0.0.1，不开放局域网；没有登录、密码、角色与用户审计，ALLOWED_HOSTS 未放开。
- 本机 API 保持现有行为范围；不因为“给未来预留”而对外新增开放接口。
- 未来若要联网，至少必须先补：用户身份与登录、按角色的读写权限、操作审计（谁在何时导入/审核/备份）、访问来源限制（HTTPS/反向代理/白名单）、备份与恢复的运维流程。这些本轮都未实现。
- 当前备份功能也只面向“本机用户手动 / 每日一次”，不是多用户共享备份服务。

## 当前限制

- 无登录/权限（预览版）。
- 无原图显示/附件存证（审核页先显示字段与成分；附件模型/接入下一步）。
- 不实现恢复/覆盖数据库；“外接硬盘备份”见上节（功能已实现，尚未对真实数据执行，目标盘也尚未配置）。
- 成分拆分支持 %、‰、ppm、无单位数值和 PH；其他单位先原样保留。
- 面板设计器（新建/改列/复制）尚未做 UI；面板定义先由 seeds/panels.json 提供。
- CSV 可导出宽表；综合报表/查询结果可导出 Excel。
- 预览用 Django runserver；正式版换 waitress + 启动器。
- 已做 payload 级幂等/防重；业务级重复（同一车/同一批次重复送）仍需业务键去重。
- 无 SQL 适配器，只接受 JSON。
- 自动化测试：python manage.py test（含外接硬盘备份的临时目录/合成 SQLite 测试，不使用真实数据）。
- 仓库不发布界面截图（截图含真实库存/来料数据）；仅保留本地 screenshots/。
- 实现架构与 Agent 接入细节见 ../实现架构与Agent接入说明.md。

## 目录

~~~text
br-app/
├─ brpreview/        Django 配置
├─ panels/           模型、视图、模板、成分解析、备份服务（services/backup.py 等）
├─ seeds/            panels.json / reviewed_maps.json / legacy_data.json（合成示例）
├─ static/panels/    CSS
├─ data/             SQLite（运行时生成，不进 git）；backup_config.json 为备份配置
└─ 启动预览.bat
~~~
