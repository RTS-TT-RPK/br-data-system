# 03_原型与代码

预览版已建在 br-app/。双击 br-app/启动预览.bat，或按下面命令启动。

相关文档：
- 实现架构与Agent接入说明.md（v1.1，含事务/幂等/测试）
- br-app/README.md
- ../00_方案与评审/工作区经验归档_2026-10-28.md
- br-app/screenshots/（界面截图含真实数据，未随公开仓库发布）

~~~text
br-app/
├─ brpreview/               Django 配置
├─ panels/
│  ├─ models.py             PanelDefinition / Record / Component /
│  │                        IngestionBatch / IngestionRow / AuditLog
│  ├─ views.py              页面 + API
│  ├─ services/
│  │  ├─ component_parser.py
│  │  ├─ records.py         正式写入事务服务
│  │  ├─ ingestion.py       幂等/校验
│  │  ├─ review_import.py   reviewed.json → L3 草稿
│  │  ├─ analysis.py        来料分析
│  │  └─ report.py          Excel 报表
│  ├─ templates/panels/
│  ├─ management/commands/  seed_preview / check_consistency / import_reviewed
│  └─ tests.py              28 个自动化测试
├─ seeds/                   panels.json / reviewed_maps.json / legacy_data.json（合成示例）
├─ static/panels/app.css
├─ tools/                   agent_client.py / sample_ingestion.json
├─ screenshots/             页面截图（含真实数据，未随公开仓库发布）
├─ data/preview.sqlite3
├─ 示例_综合物料汇总.xlsx（含真实数据，未随公开仓库发布）
└─ 启动预览.bat
~~~

启动：

~~~bat
cd br-app
python manage.py migrate
python manage.py seed_preview
python manage.py runserver 127.0.0.1:8765
~~~

开发前先读：
- ../00_方案与评审/架构设计说明_v2.0_面板驱动与Agent接入.md（早期方案）
- ../00_方案与评审/数据结构_宽表长表与成分明细.md
- ../00_方案与评审/工作区经验归档_2026-10-28.md（当前经验/踩坑）
- ../实现架构与Agent接入说明.md（当前实现）

硬规则：不为任何一张具体表写专用页面；面板/样式的列、校验、布局全部走配置；AI 不许写死列名。
Windows 原生：不要引入 WSL/Linux/虚拟机/Docker；启动/安装脚本用 PowerShell/批处理。
