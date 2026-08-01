# 方案 D 实施计划 — Graphiti 替换 Zep 后端

> 状态：阶段 0（基础设施）✅ 已完成 → 阶段 1（shim）进行中
> 创建：2026-08-01
> 关联：`docs/graphiti-migration-plan.md`（可行性评估）

## 已验证的关键事实（2026-08-01）

| 事实 | 来源 | 对方案的影响 |
|------|------|-------------|
| Graphiti 0.29.3 默认 `labels:["Entity"]`，无细分类型 | Neo4j e2e 测试 | **迁移脚本必须显式设置 entity label** |
| MiroFish 缓存 JSON node 字段：uuid/name/labels/summary/attributes/created_at | 11.8MB 缓存文件 | shim 的 `graph.node.get` 返回结构对齐 |
| MiroFish 缓存 JSON edge 字段：14 个字段含 fact/fact_type/source/target | 同上 | shim 的 `graph.edge.get_by_graph_id` 对齐 |
| 2734 节点中 1470 个 labels 为空（财务科目） | 缓存统计 | 迁移时空 labels → 不设置 label |
| labels 有值时是数组（如 `["Organization"]`） | 缓存统计 | 对应 Zep ontology entity_types |
| Neo4j Entity 属性：name/created_at/name_embedding/uuid/labels/group_id/summary | Neo4j 查询 | shim Cypher 字段映射 |
| graphiti-core `add_episode` 支持 entity_types/edge_types/edge_type_map | 源码 + e2e | 对应 Zep set_ontology |
| 双源 LLM 已验证：抽取=qwen3.7-plus(6实体/ep)，向量=MiniMax embo-01 | e2e 测试 | 镜像内置此配置 |

## 阶段 1：写 graphiti_shim.py（~1.5 天）

### 目标
实现一个模块，对外暴露与 `zep_cloud.client.Zep` **同名的属性链和方法签名**，内部用 graphiti-core + Neo4j 直连实现。

### 文件位置
`backend/app/services/graphiti_shim.py`

### 需实现的 11 个方法（按调用频次排序）

| # | Zep 调用链 | MiroFish 调用点 | shim 实现 |
|---|-----------|----------------|----------|
| 1 | `graph.add_batch(data_elements)` | graph_builder.py 摄取 | → graphiti `add_episode` 或直写 Neo4j |
| 2 | `graph.search(query, top_k)` | graph_api.py 搜索 | → graphiti `search` (COMBINED_HYBRID_SEARCH_RRF) |
| 3 | `graph.node.get_by_graph_id(graph_id)` | zep_paging.py 全量读节点 | → Cypher `MATCH (n:Entity {group_id})` |
| 4 | `graph.edge.get_by_graph_id(graph_id)` | zep_paging.py 全量读边 | → Cypher `MATCH (n)-[r]->(m)` 同 group |
| 5 | `graph.node.get(node_id)` | zep_entity_reader.py | → Cypher `MATCH (n {uuid})` |
| 6 | `graph.node.get_entity_edges(node_id)` | zep_entity_reader.py | → Cypher 取节点关联边 |
| 7 | `graph.create(graph_name)` | graph_builder.py 建图 | → 仅记录 group_id（Neo4j 用 group_id 隔离） |
| 8 | `graph.set_ontology(ontology)` | graph_builder.py 设置本体 | → 保存 entity_types 供 add_episode 用 |
| 9 | `graph.add(data_element)` | graph_builder.py 单条 | → graphiti `add_episode` 单条 |
| 10 | `graph.episode.get(episode_id)` | zep_tools.py | → Cypher 读 Episodic 节点 |
| 11 | `graph.delete(graph_id)` | 图谱管理 | → Cypher `DETACH DELETE` |

### shim 的返回对象设计

用 `types.SimpleNamespace` 或轻量 dataclass 模拟 Zep SDK 的返回对象（MiroFish 代码用 `.属性` 访问）：

```python
# ZepNode 对齐缓存 JSON 的 node 结构
class ShimNode:
    uuid: str
    name: str
    labels: list[str]
    summary: str
    attributes: dict
    created_at: str
```

### 阶段 1 验收标准
- [ ] 单元测试：mock Neo4j driver，验证 11 个方法的入参/出参
- [ ] 集成测试：连真实 Neo4j，写入 3 节点 + 2 边，读回验证字段一致

## 阶段 2：写迁移脚本（~1 天）

### 目标
把 `backend/uploads/graphs/mirofish_1069a964f83c4bef.json`（2734 节点/9263 边）零 token 导入 Neo4j。

### 文件位置
`backend/scripts/migrate_zep_to_graphiti.py`

### 策略：直写 Cypher（不走 LLM 抽取）
- 节点：`CREATE (n:Entity {uuid, name, summary, group_id, labels, created_at})`
- 边：`MATCH source/target CREATE (s)-[r:RELATES {uuid, fact, fact_type, ...}]->(t)`
- **embedding 字段**：先不写（search 走 graphiti 的 lazy embed 或后续补算）

### 阶段 2 验收标准
- [ ] 2734 节点全部进 Neo4j，`count` 一致
- [ ] 9263 边全部进 Neo4j，`count` 一致
- [ ] 随机抽 10 节点，字段值与缓存 JSON 一致

## 阶段 3：改 compose + Dockerfile（~0.5 天）

### docker-compose.yml
新增 `neo4j` 服务，network 与 mirofish 共享。

### Dockerfile
`uv add graphiti-core==0.29.3 neo4j>=5.14`，打包 `minimax_adapters.py`。

### config.py
新增 `GRAPHITI_NEO4J_URI` / `GRAPHITI_NEO4J_USER` / `GRAPHITI_NEO4J_PASSWORD`。

### 环境开关
`GRAPH_BACKEND=graphiti`（默认仍 `zep`），实现平滑切换。

## 阶段 4：切换 + 回归测试（~0.5 天）

- 设 `GRAPH_BACKEND=graphiti`
- 跑现有 156 个回归测试
- 手动验证：图谱页加载、搜索、新建模拟首轮抽取

## 风险登记

| 风险 | 等级 | 缓解 |
|------|------|------|
| Graphiti search 结果排序与 Zep 不同，影响 Agent 选角 | 中 | 阶段 4 用同一批 entity 做对比 |
| 2734 节点无 embedding，semantic search 失效 | 高 | 迁移脚本补算 embedding（批量调 MiniMax embo-01） |
| minimax_adapters.py 打进镜像后 LLM 双源配置复杂 | 低 | 已有 e2e 验证 |
| neo4j Python driver 与 graphiti-core 版本冲突 | 中 | 阶段 3 先在容器外验证 import |
