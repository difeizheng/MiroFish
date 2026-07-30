# docker-patches — 镜像热修复补丁（历史留档）

> **状态：已归档（2026-07）**。所有补丁已回填本地源码并打入自建镜像 `mirofish:local`，
> docker-compose.yml 的单文件挂载已全部移除，本目录仅作历史参考，不再生效。
> 唯一例外：分页修复补丁已彻底过时（上游 zep-cloud 3.25 + 新 zep_paging.py 游标分页已修复）。

官方镜像 `ghcr.nju.edu.cn/666ghj/mirofish:latest` 落后于本地源码，且镜像内 zep-cloud 为 3.13（本地源码锁定 3.25）。
这些补丁通过 docker-compose.yml 的**单文件只读挂载**注入容器，容器重建（`docker compose down && up`）也不会丢失。

## 补丁清单

| 文件 | 挂载到容器路径 | 修复内容 |
|---|---|---|
| `backend/zep_paging.py` | `/app/backend/app/utils/zep_paging.py` | **分页 bug**：镜像原版请求 page_size=100 后用 `len(batch) < 100` 判断末页，但 Zep 服务端单页上限 50 → 永远只读第一页（50 节点/50 边）。补丁改为 page_size=50 + uuid_cursor 翻页 + 游标不前进保护 + 重试。兼容镜像内 zep-cloud 3.13（本地源码版用 3.25 的 `zep-next-cursor` 响应头，不能直接拷入镜像） |
| `backend/graph_builder.py` | `/app/backend/app/services/graph_builder.py` | **图谱数据本地缓存**：`get_graph_data()` 全量拉取需翻 241+ 页（2~5 分钟），改为缓存到 `backend/uploads/graphs/<graph_id>.json`（uploads 是 volume 天然持久），页面秒开；`force_refresh=True` 强制重拉；`delete_graph()` 同步清缓存；同 graph_id 加锁防并发重复拉取 |
| `backend/graph_api.py` | `/app/backend/app/api/graph.py` | `GET /api/graph/data/<id>` 支持 `?refresh=1` 强制刷新参数 |
| `frontend/api_graph.js` | `/app/frontend/src/api/graph.js` | `getGraphData(graphId, forceRefresh)` 透传 refresh 参数 |
| `frontend/MainView.vue` | `/app/frontend/src/views/MainView.vue` | 「刷新图谱」按钮和图谱构建完成后的最终加载改为强制刷新；其余加载走缓存 |
| `frontend/GraphPanel.vue` | `/app/frontend/src/components/GraphPanel.vue` | **大图渐进式探索**：默认只渲染度数 Top-N 核心骨架（50/100/150/200/300/500 可调，默认 150）；双击节点展开/收起一跳邻居；实体类型图例可点击过滤；搜索框全图模糊匹配并居中定位；节点半径按度数缩放；边标签在边多且缩小时自动隐藏、放大后显示。解决 2700+ 节点全量 SVG 渲染卡死问题 |
| `backend/zep_entity_reader.py` | `/app/backend/app/services/zep_entity_reader.py` | **Agent 实体质量过滤**：原来 1264 个本体类型实体全量建 Agent（含“安凯汽车”“废水处理站”等无关实体和“公司/发行人/董事会”等招股书高频词），改为三道过滤——①通用名黑名单（约 25 个 + `公司A` 类占位符 + 纯数字符号）②度数阈值（默认度>=3，不取边时读图谱缓存算度数）③按度数降序截断（默认 Top150）。效果 1264 → 150，且只对幸存者做边富集（prepare 明显提速）。env 可调：`AGENT_MIN_DEGREE`、`AGENT_MAX_ENTITIES`、`AGENT_EXCLUDE_NAMES`（逗号分隔追加黑名单），写进根目录 `.env` 后 `docker compose up -d` 生效 |
| `backend/zep_tools.py` | `/app/backend/app/services/zep_tools.py` | **Zep 离线降级**：Zep 云端额度耗尽后报告 Agent 的检索/统计/实体读取全挂。`.env` 设 `GRAPH_LOCAL_ONLY=1` 后所有读路径（search_graph/get_all_nodes/get_all_edges/get_node_detail/统计/实体摘要）直接走本地图谱缓存 JSON；未设该变量时 Zep 调用失败自动降级缓存（缓存不存在才抛错）。本地搜索为关键词匹配（原代码自带降级路径），语义排序弱于云端但报告 Agent 可用 |
| `backend/zep_graph_memory_updater.py` | `/app/backend/app/services/zep_graph_memory_updater.py` | **记忆回写降级**：模拟中 Agent 活动回写 Zep（graph.add），额度耗尽后每批重试 3 次浪费且记忆丢失。GRAPH_LOCAL_ONLY=1 时跳过 Zep；正常模式下重试耗尽的失败批次，两种情况都落盘 `backend/uploads/graphs/<graph_id>.memory.jsonl`（恢复 Zep 后可重放） |
| `backend/run_parallel_simulation.py` | `/app/backend/scripts/run_parallel_simulation.py` | **双 LLM 隔离**：原版 `create_model` 只改进程级 `OPENAI_API_KEY` 环境变量，Reddit（use_boost=True）后初始化会覆盖，导致 Twitter/Reddit 两平台调用全走 `LLM_BOOST_*`；补丁改为 `ModelFactory.create` 显式传 `api_key`/`url`，Twitter 真正走通用配置、Reddit 走加速配置。另含 **`RateLimitedModel` 限流包装器**：以 base_url 为 key 分桶限流（双服务商时两平台各自独立并发，单服务商时自动共享一桶；每桶默认 8，`LLM_MAX_CONCURRENCY` 可调）+ 遇 429 按 Retry-After/指数退避重试（默认 8 次，`LLM_RATE_LIMIT_RETRIES` 可调），解决 150 Agent 瞬时并发打爆 new-api 中转产生大量 429 的问题 |

## 生效方式

补丁在 `docker-compose.yml` 的 volumes 中挂载，执行 `docker compose up -d` 重建容器后生效。

**Zep 额度耗尽应对**：`.env` 加 `GRAPH_LOCAL_ONLY=1` + `docker compose up -d`（已配置）。恢复 Zep 后删除该行切回云端；期间**不要**点「刷新图谱」或重建图谱（强制走 Zep 会失败）。

**本地源码不同步警告**：`zep_entity_reader.py`/`zep_tools.py`/`zep_graph_memory_updater.py`/`graph_builder.py`（缓存）的本地源码是更新的上游版本，结构不同，补丁尚未回填——本地跑 `run.py` 时这些修复不生效，重建镜像前需先移植。

## 长期方案

用本地源码 `docker build` 自己的镜像（本地代码 zep-cloud 3.25 已修复分页），届时删除本目录及 compose 中对应挂载即可。
注意：缓存功能（补丁 2/3）本地源码也没有，重建镜像前需先把缓存逻辑合入源码。
