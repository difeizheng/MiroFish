# MiroFish × Graphiti 自建图谱服务 — 实现方案（方案 D）

> 目标：用自建 **Graphiti（Zep 开源内核）+ Neo4j** 完全替代 Zep Cloud，摆脱云端额度限制。
> 现状兜底：本方案落地前，`GRAPH_LOCAL_ONLY=1` 离线模式（补丁 9/10）已保证现有项目可用。
> 编写日期：2026-08；基于容器镜像 `ghcr.nju.edu.cn/666ghj/mirofish:latest`（zep-cloud 3.13 快照）实际代码盘点。

---

## 1. 目标与非目标

### 目标
1. 图谱**构建**（本体定义、文本摄取、episode 处理）不依赖 Zep Cloud
2. 图谱**读取**（节点/边分页拉取、搜索、统计）不依赖 Zep Cloud
3. 模拟**记忆回写**（Agent 活动 → episode）不依赖 Zep Cloud
4. 现有图谱数据（2734 节点 / 9263 边）**零 token 迁移**到自建库，uuid/fact/时间字段完整保留
5. 通过环境变量一键切换 Zep / Graphiti，随时可回滚

### 非目标
- 不追求与 Zep Cloud 完全一致的检索排序质量（Zep 的 cross-encoder 是托管模型，自建用 RRF/向量替代，质量略降但可用）
- 不改前端、不改报告 Agent 的工具调用协议（对上层完全透明）
- 不改动本地新版源码（本地源码是更新的上游版本，本方案只作用于 Docker 部署，与既有补丁策略一致）

---

## 2. 现状盘点：MiroFish 实际使用的 Zep API 面

对容器内 4 个服务文件 + 分页工具逐行盘点，**全部 Zep 依赖收敛为 11 个方法**（这是整个方案的核心事实，替代面比想象小）：

| # | 调用点 | 方法签名 | 用途 | 调用文件 |
|---|---|---|---|---|
| 1 | `client.graph.create(graph_id, name, description)` | 建图 | graph_builder.py |
| 2 | `client.graph.set_ontology(graph_ids=[...], entities={...}, edges={...})` | 设置本体（entity/edge 类型，值为动态 Pydantic 模型） | graph_builder.py |
| 3 | `client.graph.add_batch(graph_id, episodes=[EpisodeData(data, type="text")])` → `[{uuid_}]` | 批量摄取文本块 | graph_builder.py |
| 4 | `client.graph.add(graph_id, type="text", data=...)` | 单条摄取（模拟记忆回写） | zep_graph_memory_updater.py |
| 5 | `client.graph.episode.get(uuid_)` → `.processed` | 轮询摄取完成状态 | graph_builder.py |
| 6 | `client.graph.node.get_by_graph_id(graph_id, limit, uuid_cursor)` | 分页拉全部节点 | zep_paging.py |
| 7 | `client.graph.edge.get_by_graph_id(graph_id, limit, uuid_cursor)` | 分页拉全部边 | zep_paging.py |
| 8 | `client.graph.node.get(uuid_)` → 节点对象 | 单节点详情 | zep_tools.py / zep_entity_reader.py |
| 9 | `client.graph.node.get_entity_edges(node_uuid=)` | 某节点的关联边 | zep_entity_reader.py |
| 10 | `client.graph.search(graph_id, query, limit, scope, reranker="cross_encoder")` → `.edges` / `.nodes` | 混合检索（报告 Agent） | zep_tools.py |
| 11 | `client.graph.delete(graph_id)` | 删图 | graph_builder.py |

**数据形状约定**（下游缓存 JSON、GraphPanel、实体过滤全部依赖这个形状，shim 必须原样产出）：

```
节点: {uuid, name, labels[], summary, attributes{}, created_at}
边:   {uuid, name, fact, source_node_uuid, target_node_uuid,
       source_node_name, target_node_name, attributes{},
       created_at, valid_at, invalid_at, expired_at, episodes[]}
```

**关键设计决策由此得出**：不重构上层代码，而是写一个 **zep_cloud API 兼容垫片（shim）**——
实现上述 11 个方法的 `GraphitiZepShim` 类，把 4 处 `Zep(api_key=...)` 构造点改为按环境变量返回 shim。
上层（graph_builder / zep_tools / zep_entity_reader / memory_updater / 全部补丁）**零改动**。

---

## 3. 总体架构

```
┌─────────────────────────────────────────────────────────┐
│  MiroFish 后端（容器）                                    │
│                                                          │
│  graph_builder / zep_tools / entity_reader / updater    │
│         │  全部通过 get_graph_client() 获取 client       │
│         ▼                                                │
│  ┌──────────────────────┐    ┌────────────────────────┐ │
│  │ ZepCloud (现状)       │    │ GraphitiZepShim (新增)  │ │
│  │ GRAPH_PROVIDER=zep   │    │ GRAPH_PROVIDER=graphiti│ │
│  └──────────────────────┘    └───────────┬────────────┘ │
│                                          │              │
│                           ┌──────────────┴───────────┐  │
│                           ▼                          ▼  │
│                    graphiti-core                本地嵌入模型 │
│                    (LLM=MiniMax经new-api)      (bge 系列)  │
└──────────────────────────────┬───────────────────────────┘
                               ▼
                    ┌─────────────────────┐
                    │  Neo4j 5.x Community │  ← 新增 compose 服务
                    │  (bolt://neo4j:7687) │
                    └─────────────────────┘
```

### 为什么选 shim 而不是 Provider 抽象层重构

| 维度 | shim 方案 | Provider 抽象重构 |
|---|---|---|
| 改动面 | 新增 1 个文件 + 改 4 处构造点 | 改 5+ 个业务文件的所有调用点 |
| 与既有 10 个补丁的兼容性 | 天然兼容（补丁都在上层） | 需要重新移植补丁 |
| 上游合并 | 不影响 | 与上游新版源码冲突面大 |
| 风险 | shim 内部复杂但隔离 | 回归面广 |

**决策：shim 方案。**

### 技术选型

| 组件 | 选择 | 理由 |
|---|---|---|
| 图数据库 | **Neo4j 5.26+ Community**（compose 新服务） | graphiti 一等公民后端；自带向量索引+全文索引；社区版免费够用。备选 FalkorDB（更轻量，但生态次之） |
| 抽取框架 | **graphiti-core**（钉版，实施时取最新稳定版并验证 API） | Zep 开源内核，语义最接近：episode → 实体/边抽取、group_id ≈ Zep graph_id、支持自定义 entity/edge 类型 |
| 抽取 LLM | 现有 MiniMax-M2.7（经 new-api，OpenAI 兼容） | graphiti 的 LLMClient 支持自定义 base_url；套并发限流（复用 RateLimitedModel 的信号量思路） |
| 嵌入模型 | **本地 bge-small-zh-v1.5**（约 100MB，HuggingFace，HF_ENDPOINT=hf-mirror.com 已配好） | 零 API 成本、中文好；质量优先可换 bge-m3（约 2GB）。若 new-api 上游有 /v1/embeddings 则优先走 API |

---

## 4. 分阶段实施

### 阶段 0：基础设施（约 0.5 天）

**4.1 compose 加 Neo4j 服务**

```yaml
  neo4j:
    image: neo4j:5.26-community
    container_name: mirofish-neo4j
    restart: unless-stopped
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-mirofish2024}
      NEO4J_server_memory_heap_initial__size: 512m
      NEO4J_server_memory_heap_max__size: 1G
      NEO4J_server_memory_pagecache_size: 256m
      # graphiti 只需要自带的全文/向量索引，无需 APOC
    volumes:
      - neo4j_data:/data
    # 不对外暴露端口，仅容器网络内访问

volumes:
  neo4j_data:
```

**4.2 镜像扩展**（官方镜像没有 graphiti，单文件挂载解决不了，需要一层薄镜像）：

```dockerfile
# docker-patches/Dockerfile
FROM ghcr.nju.edu.cn/666ghj/mirofish:latest
RUN cd /app/backend && uv pip install --no-cache \
    "graphiti-core==<钉版>" "neo4j>=5.26" "sentence-transformers>=2.7"
```
compose 改为 `build: { context: ., dockerfile: docker-patches/Dockerfile }`（替代原 `image:` 字段，或 build 后打 tag 引用）。

**4.3 `.env` 新增**

```bash
GRAPH_PROVIDER=graphiti        # zep | graphiti，默认 zep（不改现有行为）
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=mirofish2024
GRAPHITI_EMBED_MODEL=BAAI/bge-small-zh-v1.5   # 或走 API: GRAPHITI_EMBED_BASE_URL/GRAPHITI_EMBED_API_KEY
GRAPHITI_LLM_MAX_CONCURRENCY=4  # 抽取 LLM 并发（摄取期调用密集，比模拟期更敏感）
```

**验收**：`docker compose up -d` 后，容器内 `python -c "from graphiti_core import Graphiti"` 通过，Neo4j 7474 可登录。

---

### 阶段 1：shim 核心（约 1.5 天）— 新文件 `backend/app/services/graphiti_shim.py`

**5.1 异步桥（最重要的工程细节）**

graphiti-core 全异步；MiroFish 是同步 Flask + 同步脚本。Neo4j AsyncDriver 绑定创建它的事件循环，因此必须**单事件循环 + 后台线程**：

```python
class _AsyncBridge:
    """守护线程里跑唯一事件循环，所有 graphiti 协程经 run_coroutine_threadsafe 提交"""
    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self.graphiti = None  # 在循环内初始化 Graphiti(...)
    def run(self, coro, timeout=300):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)
```

Flask debug 模式有 reloader 双进程——桥必须是模块级单例（`functools.lru_cache` 或模块全局），且启动时 `build_indices_and_constraints()` 幂等执行一次。

**5.2 对象模型模拟**

zep_cloud SDK 返回的是带属性的对象（`node.uuid_`、`edge.fact`），用简单的命名空间类模拟：

```python
class _Obj:  # 同时支持 obj.x 和 getattr 默认值
    def __init__(self, **kw): self.__dict__.update(kw)
```
节点对象字段：`uuid_/uuid, name, labels, summary, attributes, created_at`
边对象字段：`uuid_/uuid, name, fact, source_node_uuid, target_node_uuid, created_at, valid_at, invalid_at, expired_at, episodes, attributes`

**5.3 11 个方法逐一映射**

| Zep 方法 | shim 实现 |
|---|---|
| `graph.create(graph_id, name, description)` | 幂等 no-op：Neo4j 里不需要显式建图，group_id 随首个 episode 自然存在。可选：写一个 `(:GraphMeta {graph_id, name, description})` 节点供 list/inspect |
| `graph.set_ontology(graph_ids, entities, edges)` | 存入进程内 dict + 落盘 `uploads/graphs/<id>.ontology.json`（重启不丢）。结构 `{entity_types: {name: {description, fields}}, edge_types: {...}, edge_type_map: {...}}`，供 add 时动态重建 Pydantic 模型 |
| `graph.add_batch(graph_id, episodes)` | 串行/限流并发地对每个 episode 调 `graphiti.add_episode(name=uuid4, episode_body=data, source=EpisodeType.text, group_id=graph_id, reference_time=now, entity_types=动态模型, edge_types=..., edge_type_map=...)`；**同步阻塞**特性意味着天然"处理完成"。返回 `[ _Obj(uuid_=ep.uuid) ]` |
| `graph.add(graph_id, type, data)` | 同上单个版本（memory updater 走这里） |
| `graph.episode.get(uuid_)` | 直接返回 `_Obj(uuid_=uuid_, processed=True)` —— graphiti 是同步摄取，没有异步队列 |
| `graph.node.get_by_graph_id(graph_id, limit, uuid_cursor)` | Cypher：`MATCH (n:Entity {group_id:$gid}) WHERE n.uuid > $cursor RETURN n ORDER BY n.uuid SKIP 0 LIMIT $limit`（uuid_cursor 语义与 zep_paging.py 的分页协议对齐，**必须先读 zep_paging.py 确认游标比较方式**） |
| `graph.edge.get_by_graph_id(...)` | `MATCH (s:Entity {group_id:$gid})-[r:RELATES_TO]->(t:Entity) ...` 同样游标分页 |
| `graph.node.get(uuid_)` | `MATCH (n:Entity {uuid:$uuid}) RETURN n` |
| `graph.node.get_entity_edges(node_uuid)` | `MATCH (n:Entity {uuid:$uuid})-[r:RELATES_TO]-() RETURN r` |
| `graph.search(graph_id, query, limit, scope, reranker)` | 见 5.4 |
| `graph.delete(graph_id)` | `MATCH (n {group_id:$gid}) DETACH DELETE n` + 删 GraphMeta/ontology 落盘文件 + 删本地缓存 JSON（现有 delete_graph 已有清缓存逻辑） |

**5.4 search 映射（质量关键）**

```python
# scope="edges" → graphiti.search(query, group_ids=[gid], num_results=limit)
#   返回 EntityEdge 列表（自带 fact/name/uuid/时间字段），直接包装成 _Obj
# scope="nodes" → graphiti 的节点检索（SearchConfig recipe，实施时按钉版 API 验证，
#   如 NODE_HYBRID_SEARCH_RRF）；返回 EntityNode 包装
# reranker="cross_encoder" 参数忽略（graphiti 默认 RRF 融合 BM25+向量）
```

返回对象需有 `.edges` / `.nodes` 两个列表属性（zep_tools.py 的解析逻辑依赖 `hasattr`）。

**5.5 动态 Pydantic 本体**

Zep 的 set_ontology 收的是 Pydantic 模型类；graphiti 的 `add_episode(entity_types={...})` 同样收 `{类型名: BaseModel子类}`。shim 内部把落盘的 JSON 本体经 `pydantic.create_model` 重建。**字段默认值必须给 `None`**，否则抽取时 LLM 漏字段会校验失败（graphiti 社区常见坑）。

**5.6 LLM/嵌入客户端构造**

```python
from graphiti_core.llm_client.openai_client import OpenAIClient, LLMConfig
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

llm = OpenAIClient(config=LLMConfig(
    api_key=os.environ["LLM_API_KEY"], base_url=os.environ["LLM_BASE_URL"],
    model=os.environ.get("LLM_MODEL_NAME"), small_model=...,  # 抽取用小模型档位
))
# 限流：子类化 OpenAIClient，generate_response 前后过 asyncio.Semaphore(GRAPHITI_LLM_MAX_CONCURRENCY)
# 嵌入：优先 GRAPHITI_EMBED_BASE_URL（OpenAI 兼容 /v1/embeddings），否则本地 sentence-transformers 自定义 Embedder
```

注意：graphiti 抽取对结构化输出能力敏感，MiniMax-M2.7 若 function-calling/JSON 模式不稳，会出现**抽取为空**——阶段 4 有专项验证。

**验收**：单测 mock Neo4j + 真机冒烟——`create → set_ontology → add("长鑫存储是一家DRAM制造商") → node.get_by_graph_id 拉回来 → search 命中 → delete` 全链路通。

---

### 阶段 2：现有图谱零 token 迁移（约 1 天）— `scripts/migrate_cache_to_graphiti.py`

核心洞察：**缓存 JSON 里已有全部实体/边/事实/时间字段**，不需要重新抽取，直接 Cypher 批量导入，uuid 原样保留（前端、模拟配置、报告引用的 uuid 不断链）。

```
1. 读 backend/uploads/graphs/mirofish_1069a964f83c4bef.json
2. 先跑 graphiti.build_indices_and_constraints()（保证索引名/属性布局与 graphiti 检索约定一致）
3. UNWIND 批量 MERGE 节点:
   MERGE (n:Entity {uuid: row.uuid})
   SET n += {name, summary, group_id, created_at}, 动态追加类型标签（labels 里非 Entity 的第一个）
4. UNWIND 批量建边:
   MATCH (s:Entity {uuid: row.source}), (t:Entity {uuid: row.target})
   MERGE (s)-[r:RELATES_TO {uuid: row.uuid}]->(t)
   SET r += {name, fact, group_id, created_at, valid_at, invalid_at, expired_at, episodes}
   （'None' 字符串归一化为 null）
5. 嵌入回填: 批处理 name+summary / fact 经嵌入模型生成向量，写回 name_embedding/fact_embedding 属性
   （向量索引在属性写入后自动生效；约 12000 条文本，本地 bge-small 几分钟）
6. 校验: 计数对齐（2734/9263）、抽样 10 条 fact 比对、graphiti.search 冒烟 5 个查询
```

**验收**：迁移后 `GRAPH_PROVIDER=graphiti` 下 `/api/graph/data/<id>` 返回节点/边数与原缓存一致；zep_tools 的 6 个检索方法全部有合理返回；`GRAPH_LOCAL_ONLY` 关闭也能工作。

---

### 阶段 3：新图构建全链路验证（约 1 天）

用小文档（< 5 万字）走完整新建流程，验证构建路径而不只是读取路径：

1. 前端新建项目 → 上传小文档 → 本体生成（LLM，不变）→ `set_ontology` → `add_batch` 摄取
2. **摄取速率控制**：graphiti 每 episode 产生 3~6 次 LLM 调用（实体抽取、边抽取、去重、属性填充、嵌入）。graph_builder 现有 batch_size=3 + sleep(1)。估算：38 万字 ÷ 500 字符/块 ≈ 770 episodes × ~4 调用 ≈ **3000 次 LLM 调用**，并发 4 限流下约 1.5~3 小时。MiniMax token 成本按 38 万字 × ~15 倍放大（prompt+输出）估算，需提前告知用户
3. `episode.get` 轮询立即返回 processed=True（同步摄取）——graph_builder 的等待逻辑自然通过，无需改
4. 验证图谱质量：实体数合理、类型标签正确（这检验本体映射和 MiniMax 抽取能力）
5. prepare 模拟（150 Agent 过滤链路）+ 小规模模拟（< 10 轮）+ 报告 Agent 全工具冒烟

**验收**：新图端到端跑通；抽取质量人工抽查 20 个实体/边。

---

### 阶段 4：收尾（约 0.5 天）

1. `backend/tests/` 增加 shim 契约测试：复用现有 `test_zep_*` 的断言形状（它们钉的是 zep-cloud 3.25 契约，shim 输出需满足同样的属性形状），新增 `test_graphiti_shim.py`
2. README/AGENTS.md 更新：新 env、新服务、切换/回滚步骤
3. docker-patches/README.md 增补：shim 文件的 compose 挂载、Dockerfile 说明
4. 文档化回滚：`GRAPH_PROVIDER=zep`（或删行）+ `docker compose up -d`

---

## 5. 风险清单与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| graphiti-core 版本 API 漂移（search recipe、entity_types 参数名近年改过多次） | 高 | 实施第一步钉版并把 `add_episode`/`search`/索引名写进契约测试；升级时必须全量跑测试 |
| MiniMax-M2.7 结构化输出不稳 → 抽取为空/属性缺失 | 高 | 阶段 3 小文档先行验证；抽取 prompt 层可在 shim 注入 system 提示；兜底换 LLM_BOOST 渠道或加 JSON 模式重试 |
| 检索质量下降（无 cross-encoder） | 中 | graphiti RRF 融合 BM25+向量已可用；必要时 shim 里加一道 LLM 重排（top-30 → top-10，复用现有 LLM） |
| 异步桥在 Flask debug reloader 下双实例/泄漏 | 中 | 模块级单例 + 幂等初始化；容器生产模式不起 reloader（run.py 确认） |
| Neo4j 内存压力（图谱规模增长后） | 低 | 当前规模 1.2 万元素毫无压力；heap 1G 足够支撑到 ~百万级 |
| 迁移后 uuid 冲突（MERGE 键） | 低 | 迁移脚本先 `MATCH (n {group_id:$gid}) DETACH DELETE n` 保证幂等可重跑 |
| zep_paging.py 游标语义与 Cypher 分页不匹配 | 中 | 实施时先读补丁版 zep_paging.py 确认游标比较方式（uuid 字符串字典序），Cypher 用同序 `ORDER BY uuid` |

## 6. 工作量汇总

| 阶段 | 内容 | 估时 |
|---|---|---|
| 0 | Neo4j 服务 + 薄镜像 + env | 0.5 天 |
| 1 | shim 核心（异步桥 + 11 方法 + 搜索 + 本体） | 1.5~2 天 |
| 2 | 缓存 JSON 零 token 迁移脚本 + 校验 | 1 天 |
| 3 | 新图构建 E2E 验证（小文档） | 1 天 |
| 4 | 契约测试 + 文档 | 0.5 天 |
| **合计** | | **4.5~5 天** |

## 7. 与现有体系的共存关系

- `GRAPH_PROVIDER`（zep/graphiti）与 `GRAPH_LOCAL_ONLY`（缓存离线）**正交**：两个开关独立，离线兜底在任何 provider 下都有效
- 缓存 JSON 机制原样保留：Graphiti 下 `get_graph_data` 全量拉取照样走缓存秒开
- 既有 10 个补丁全部不受影响（它们都在 shim 之上或与之无关）
- Zep 账号恢复后，`GRAPH_PROVIDER=zep` 一行切回；两套数据各自独立（Neo4j 里的图和 Zep 云端的图互不可见，由 graph_id 区分环境即可）
