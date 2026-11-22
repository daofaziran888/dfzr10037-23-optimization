# 知谱工坊 · 元数据知识图谱与智能探查平台

面向元数据实体抽取、关系建模、知识图谱构建、图谱搜索、实体链接、消岐融合、路径推理、子图探查与图谱演化追踪的元数据知识图谱与智能探查平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实体抽取 | `entity_extract` | 实体定义 | 实体编号、实体类型、数据来源 |
| 关系建模 | `relation_model` | 关系类型 | 关系编号、关系名、主体类型 |
| 图谱构建 | `graph_build` | 构建任务 | 任务编号、数据范围、图类型 |
| 图谱搜索 | `graph_search` | 搜索模板 | 模板编号、搜索类型、查询语法 |
| 实体链接 | `entity_link` | 链接规则 | 规则编号、源实体、目标实体 |
| 消岐融合 | `disambig` | 消岐配置 | 配置编号、歧义类型、候选实体 |
| 路径推理 | `path_reason` | 推理任务 | 任务编号、起始实体、目标实体 |
| 子图探查 | `subgraph_exp` | 探查配置 | 配置编号、探查种子、扩展策略 |
| 图谱演化 | `graph_evolve` | 演化记录 | 记录编号、变更类型、影响实体 |
| 质量审计 | `quality_audit` | 审计报告 | 报告编号、审计范围、实体准确率 |
| 元数据标注 | `metadata_tag` | 标注任务 | 任务编号、标注对象、标注类型 |
| 分类体系 | `taxonomy` | 分类树 | 分类编号、分类名称、父分类 |
| 查询缓存 | `query_cache` | 缓存条目 | 条目编号、查询模板、结果哈希 |
| 图谱索引 | `graph_index` | 索引配置 | 索引编号、目标属性、索引类型 |
| 数据血缘 | `data_lineage` | 血缘链路 | 链路编号、上游实体、下游实体 |
| 推荐引擎 | `reco_engine` | 推荐规则 | 规则编号、推荐场景、源实体 |
| 实体归并 | `entity_merge` | 归并任务 | 任务编号、源实体、目标实体 |
| 批量导入 | `batch_load` | 导入任务 | 任务编号、数据文件、目标图谱 |
| 图谱报告 | `report_gen` | 图谱报表 | 报告编号、报告类型、数据范围 |
| 图谱比对 | `graph_compare` | 比对任务 | 任务编号、基准图、对比图 |
| 定时任务 | `schedule_job` | 调度作业 | 作业编号、任务类型、关联图谱 |
| 治理看板 | `govern_dash` | 治理指标 | 指标编号、指标名称、评估对象 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 版本化索引覆盖段（增量合并）

旧链路推给图谱索引的覆盖只记实体数量，构建范围交叠时看不出重复。
`backend/app/services/coverage.py` 提供进程内覆盖段台账，配合
`backend/app/routers/index_coverage.py` 的 `/api/index-coverage/*` 接口实现：

- **版本化覆盖段**：每段带版本号、实体标识集合与每实体的构建代（`build_generation`），
  每次索引读取都能追到各段使用的构建代与构建编号。
- **回执一致性**：构建交付必须带 `receipt_entity_count`，与实际覆盖结果不一致直接拒收。
- **代仲裁与墓碑保护**：两代构建覆盖交叠时按“新代优先”仲裁并留痕冲突；
  已确认删除的实体进入墓碑，较旧构建不能将其恢复。
- **就绪/发布分离**：交付核对通过仅到“已就绪”，发布后才进入生效覆盖；
  读取接口（`/read`、`/search/{entity_id}`）分区返回 `published` 与 `ready_only`。
- **暂缓不阻塞**：某段核对卡住可 `hold`，其他段照常交付、发布与检索，
  `release` 后可重新送达（同 `build_id` 重复送达按幂等处理，只记一次）。
- **并发发布幂等**：同一覆盖段并发发布只生效一次。
- **待办同步与补构建**：发布生效后自动写入 `graph_build` 待办（`GRAP-IDXSYNC-*`）；
  `supplement-plan/{graph_generation}` 对照图快照生成补构建清单（`SUPP-*`）。
- **历史标识兼容**：`aliases` 登记历史实体标识到当前标识的归一映射，取数自动解析。
- **图谱构建联动**：`graph_build` 构建完成自动登记覆盖段（编号 `COV-GB-*`，链路 L5）。

前端在导航“版本化覆盖段”（`/index-coverage`，`frontend/src/views/CoverageView.vue`）
提供覆盖段登记、构建交付、暂缓/恢复、核对、发布、检索与补构建清单操作。

端到端核对脚本（标准库实现，无需额外依赖）：

```bash
cd backend
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 &
.venv/bin/python -I tests/e2e_index_coverage.py
```

领域：数据工程
子方向：数据分析与数据应用
