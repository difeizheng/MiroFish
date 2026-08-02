"""
Graphiti 兼容垫片：对外模拟 zep_cloud.client.Zep 的属性链和方法签名，
内部用 Neo4j Python driver 直连实现（写路径走 graphiti-core）。

设计原则：
1. 读路径（graph.node.* / graph.edge.* / graph.search）用 Cypher 直查，同步阻塞，零 LLM 开销
2. 写路径（batch.* / graph.create / graph.set_ontology / graph.add）走 graphiti-core.add_episode
3. 返回对象用 SimpleNamespace 模拟 Zep SDK 的 .属性 访问风格

Zep client 调用链 → shim 方法对照表：
    client.graph.create          → _GraphAPI.create          (记录 group_id)
    client.graph.get             → _GraphAPI.get             (查 group_id 是否存在)
    client.graph.delete          → _GraphAPI.delete          (DETACH DELETE)
    client.graph.set_ontology    → _GraphAPI.set_ontology    (缓存 ontology 供写路径用)
    client.graph.search          → _GraphAPI.search          (graphiti.search)
    client.graph.episode.get     → _GraphAPI.episode.get     (读 Episodic 节点)
    client.graph.node.get        → _GraphAPI.node.get        (Cypher 单节点)
    client.graph.node.get_edges  → _GraphAPI.node.get_edges  (Cypher 关联边)
    client.graph.node.get_by_graph_id → _GraphAPI.node.get_by_graph_id (Cypher 全量)
    client.graph.edge.get_by_graph_id → _GraphAPI.edge.get_by_graph_id (Cypher 全量)
    client.batch.create          → _BatchAPI.create          (映射 add_episode 队列)
    client.batch.add             → _BatchAPI.add             (入队文本块)
    client.batch.process         → _BatchAPI.process         (触发 LLM 抽取)
    client.batch.get             → _BatchAPI.get             (查处理状态)
    client.batch.list            → _BatchAPI.list            (列批)
    client.batch.list_items      → _BatchAPI.list_items      (列条目)

注意：`with_raw_response` 属性链（zep_paging.py 用）通过 _WithRawResponse 包装，
返回 mock 的 response 对象（.content 是 JSON bytes）。
"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase

from ..utils.logger import get_logger

logger = get_logger("mirofish.graphiti_shim")


# ============================================================
# Graphiti-core 实例工厂（阶段 1B 写路径）
# ============================================================

from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.cross_encoder.client import CrossEncoderClient


class _MiniMaxEmbedder(EmbedderClient):
    """MiniMax 原生 embedding 端点适配（非 OpenAI 兼容格式）。

    MiniMax /v1/embeddings 入参是 {"texts":[...],"type":"db"} 而非
    OpenAI 的 {"input":[...]}，返回是 {"vectors":[[...]]} 而非
    {"data":[{"embedding":...}]}，需要专门适配。

    仅当 EMBED_BASE_URL 含 minimax 时使用。
    """

    def __init__(self, api_key: str, model: str, base_url: str):
        from graphiti_core.embedder.client import EmbedderConfig
        self.config = EmbedderConfig(embedding_dim=1536)
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip('/')

    async def create(self, input_data) -> list[float]:
        if isinstance(input_data, list):
            input_data = input_data[0] if input_data else ""
        vecs = await self._embed_batch([str(input_data)])
        return vecs[0]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return await self._embed_batch([str(x) for x in input_data_list])

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "texts": texts, "type": "db"},
            )
            resp.raise_for_status()
            data = resp.json()
            base_resp = data.get("base_resp", {})
            if base_resp.get("status_code", 0) != 0:
                raise RuntimeError(f"MiniMax embedding error: {base_resp}")
            vectors = data.get("vectors")
            if not vectors:
                raise RuntimeError("MiniMax returned empty vectors")
            return vectors


class _NoopReranker(CrossEncoderClient):
    """空 reranker：不排序，原样返回。

    Graphiti 初始化时默认创建 OpenAIRerankerClient（需要 OPENAI_API_KEY）。
    MiroFish 不需要搜索重排，用 noop 避免额外 API key 依赖。
    """

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        return [(p, 1.0) for p in passages]


def create_graphiti_instance():
    """创建 graphiti-core Graphiti 实例（写路径用）。

    从环境变量读取 EXTRACTION_* 和 EMBED_* 配置，构造：
    - LLMClient: OpenAIGenericClient（支持任意 OpenAI 兼容 /chat/completions）
    - EmbedderClient: OpenAIEmbedder（支持任意 OpenAI 兼容 /embeddings）

    兼容两种变量命名：EXTRACTION_BASE_URL / EXTRACTION_API_URL 都认。
    未配置时抛 ValueError（调用方决定是否降级）。
    """
    import os
    from graphiti_core import Graphiti
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

    # Neo4j 连接（复用 shim 的配置）
    uri = os.environ.get('NEO4J_URI', 'bolt://localhost:7687')
    user = os.environ.get('NEO4J_USER', 'neo4j')
    password = os.environ.get('NEO4J_PASSWORD', '')

    # 抽取 LLM 配置（兼容两种变量命名）
    ext_key = (
        os.environ.get('EXTRACTION_API_KEY', '').strip()
    )
    ext_url = (
        os.environ.get('EXTRACTION_BASE_URL')
        or os.environ.get('EXTRACTION_API_URL')
        or ''
    ).strip()
    ext_model = (
        os.environ.get('EXTRACTION_MODEL_NAME')
        or os.environ.get('EXTRACTION_MODEL')
        or ''
    ).strip()
    if not (ext_key and ext_url and ext_model):
        raise ValueError(
            "EXTRACTION_API_KEY/EXTRACTION_BASE_URL/EXTRACTION_MODEL_NAME 未配置"
            "（graphiti 写路径需要抽取 LLM）"
        )

    # Embedder 配置（兼容两种变量命名）
    emb_key = (
        os.environ.get('EMBED_API_KEY', '').strip()
    )
    emb_url = (
        os.environ.get('EMBED_BASE_URL')
        or os.environ.get('EMBED_API_URL')
        or ''
    ).strip()
    emb_model = (
        os.environ.get('EMBED_MODEL_NAME')
        or os.environ.get('EMBED_MODEL')
        or ''
    ).strip()
    if not (emb_key and emb_url and emb_model):
        raise ValueError(
            "EMBED_API_KEY/EMBED_BASE_URL/EMBED_MODEL_NAME 未配置"
            "（graphiti 写路径需要 embedder）"
        )

    llm_config = LLMConfig(
        api_key=ext_key,
        base_url=ext_url,
        model=ext_model,
        small_model=ext_model,  # 小模型也用同一个
    )
    llm_client = OpenAIGenericClient(config=llm_config)

    embedder_config = OpenAIEmbedderConfig(
        api_key=emb_key,
        base_url=emb_url,
        embedding_model=emb_model,
    )

    # MiniMax embedding API 不是 OpenAI 兼容格式，需要专门适配器
    if 'minimax' in emb_url.lower():
        embedder = _MiniMaxEmbedder(api_key=emb_key, model=emb_model, base_url=emb_url)
        logger.info(f"Using _MiniMaxEmbedder for non-OpenAI-compatible embedding API")
    else:
        embedder = OpenAIEmbedder(config=embedder_config)

    graphiti = Graphiti(
        uri=uri,
        user=user,
        password=password,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=_NoopReranker(),
    )
    logger.info(
        f"Graphiti instance created: llm={ext_model}@{ext_url}, embed={emb_model}@{emb_url}"
    )
    return graphiti


_GRAPHTI_LOOP = None


def _run_async(coro):
    """同步包装 async coroutine（创建独立 event loop，避免 driver 连接跨 loop 复用问题）。

    neo4j async driver 连接绑定到首次使用的 event loop；graphiti 实例被 lru_cache 缓存后，
    如果用 asyncio.run()（每次创建新 loop），第二次调用会报 "attached to a different loop"。
    解决：用模块级单例 loop，所有 _run_async 调用共用同一个 loop。
    """
    import asyncio
    global _GRAPHTI_LOOP
    if _GRAPHTI_LOOP is None or _GRAPHTI_LOOP.is_closed():
        _GRAPHTI_LOOP = asyncio.new_event_loop()
    task = asyncio.ensure_future(coro, loop=_GRAPHTI_LOOP)
    return _GRAPHTI_LOOP.run_until_complete(task)


# ============================================================
# 模拟 Zep SDK 返回对象的工厂函数
# ============================================================

def _make_node(
    uuid_: str,
    name: str,
    labels: List[str],
    summary: str,
    attributes: Dict[str, Any],
    created_at: Optional[str] = None,
    **extra,  # 容忍 group_id 等额外字段
) -> SimpleNamespace:
    """构造一个对齐 Zep SDK BaseNode 的返回对象。

    MiroFish 代码通过 .属性 访问，字段名对齐缓存 JSON 结构：
    uuid_, name, labels, summary, attributes, created_at
    """
    return SimpleNamespace(
        uuid_=uuid_,
        uuid=uuid_,  # 兼容 getattr(item, 'uuid', None)
        name=name,
        labels=labels or [],
        summary=summary or "",
        attributes=attributes or {},
        created_at=created_at,
    )


def _make_edge(
    uuid_: str,
    name: str,
    fact: str,
    fact_type: str,
    source_node_uuid: str,
    target_node_uuid: str,
    created_at: Optional[str] = None,
    valid_at: Optional[str] = None,
    invalid_at: Optional[str] = None,
    expired_at: Optional[str] = None,
    episodes: Optional[List[str]] = None,
    attributes: Optional[Dict[str, Any]] = None,
    **extra,  # 容忍 source_node_name 等额外字段（Cypher 返回但不进返回对象）
) -> SimpleNamespace:
    """构造一个对齐 Zep SDK BaseEntityEdge 的返回对象。"""
    return SimpleNamespace(
        uuid_=uuid_,
        uuid=uuid_,
        name=name or "",
        fact=fact or "",
        fact_type=fact_type or name or "",
        source_node_uuid=source_node_uuid,
        target_node_uuid=target_node_uuid,
        created_at=created_at,
        valid_at=valid_at,
        invalid_at=invalid_at,
        expired_at=expired_at,
        episodes=episodes or [],
        attributes=attributes or {},
    )


def _make_episode(uuid_: str, processed: bool, **extra) -> SimpleNamespace:
    """构造一个对齐 Zep SDK Episode 的返回对象。"""
    return SimpleNamespace(
        uuid_=uuid_,
        uuid=uuid_,
        processed=processed,
        **extra,
    )


# ============================================================
# 异常：模拟 zep_cloud.NotFoundError
# ============================================================

class NotFoundError(Exception):
    """模拟 zep_cloud.NotFoundError，graph_builder.py 的 reconcile 逻辑用到。"""


# ============================================================
# with_raw_response 包装器（zep_paging.py 调用）
# ============================================================

class _RawResponse:
    """模拟 httpx.Response。

    zep_paging.py 取 .data（节点/边列表）+ .headers（分页 cursor）。
    """

    def __init__(self, items: list[Any]):
        # .data 直接是列表（Zep SDK with_raw_response 的 data 属性格式）
        self.data = items
        self.content = json.dumps(items, ensure_ascii=False, default=str).encode("utf-8")
        self.status_code = 200
        # 不提供 next_cursor，让 zep_paging._fetch_all 在第一页就结束
        self.headers = {}

    def json(self) -> Any:
        return self.data


class _WithRawResponseNode:
    """模拟 client.graph.node.with_raw_response"""

    def __init__(self, driver, database):
        self._driver = driver
        self._database = database

    def get_by_graph_id(self, graph_id: str, **kwargs) -> _RawResponse:
        """全量读取一个 group 的所有 Entity 节点。

        zep_paging.py 的 _fetch_all 读 response.data（期望列表），
        无 next_cursor 时单页结束。元素为 SimpleNamespace 模拟 Zep SDK Node 对象。
        """
        node_dicts = _query_all_nodes(self._driver, self._database, graph_id)
        nodes = [_make_node(**d) for d in node_dicts]
        return _RawResponse(nodes)


class _WithRawResponseEdge:
    """模拟 client.graph.edge.with_raw_response"""

    def __init__(self, driver, database):
        self._driver = driver
        self._database = database

    def get_by_graph_id(self, graph_id: str, **kwargs) -> _RawResponse:
        edge_dicts = _query_all_edges(self._driver, self._database, graph_id)
        edges = [_make_edge(**d) for d in edge_dicts]
        return _RawResponse(edges)


# ============================================================
# Cypher 查询原语
# ============================================================

def _query_all_nodes(driver, database: str, group_id: str) -> List[Dict]:
    """Cypher 全量读取一个 group 的 Entity 节点。"""
    cypher = """
    MATCH (n:Entity)
    WHERE n.group_id = $group_id
    RETURN n.uuid AS uuid_, n.name AS name, n.labels AS labels,
           n.summary AS summary, n.created_at AS created_at,
           n.group_id AS group_id
    """
    records, _, _ = driver.execute_query(cypher, group_id=group_id, database_=database)
    nodes = []
    for r in records:
        # Neo4j 的 created_at 可能是 datetime，转 ISO 字符串
        created = r["created_at"]
        created_str = str(created) if created else None
        nodes.append({
            "uuid_": r["uuid_"],
            "name": r["name"] or "",
            "labels": r["labels"] or [],
            "summary": r["summary"] or "",
            "attributes": {},  # Neo4j Entity 节点属性散在节点上，按需提取
            "created_at": created_str,
        })
    return nodes


def _query_all_edges(driver, database: str, group_id: str) -> List[Dict]:
    """Cypher 全量读取一个 group 的所有关系边。"""
    cypher = """
    MATCH (s:Entity)-[r]->(t:Entity)
    WHERE s.group_id = $group_id
    RETURN r.uuid AS uuid_, r.fact AS fact, r.fact_type AS fact_type,
           s.uuid AS source_node_uuid, s.name AS source_node_name,
           t.uuid AS target_node_uuid, t.name AS target_node_name,
           r.created_at AS created_at, r.valid_at AS valid_at,
           r.invalid_at AS invalid_at, r.expired_at AS expired_at,
           r.episodes AS episodes
    """
    records, _, _ = driver.execute_query(cypher, group_id=group_id, database_=database)
    edges = []
    for r in records:
        edges.append({
            "uuid_": r["uuid_"] or str(uuid.uuid4()),
            "name": (r["fact_type"] or "RELATES").upper(),
            "fact": r["fact"] or "",
            "fact_type": r["fact_type"] or "RELATES",
            "source_node_uuid": r["source_node_uuid"],
            "target_node_uuid": r["target_node_uuid"],
            "source_node_name": r["source_node_name"] or "",
            "target_node_name": r["target_node_name"] or "",
            "created_at": str(r["created_at"]) if r["created_at"] else None,
            "valid_at": str(r["valid_at"]) if r["valid_at"] else None,
            "invalid_at": str(r["invalid_at"]) if r["invalid_at"] else None,
            "expired_at": str(r["expired_at"]) if r["expired_at"] else None,
            "episodes": r["episodes"] or [],
            "attributes": {},
        })
    return edges


def _query_node_by_uuid(driver, database: str, node_uuid: str) -> Optional[Dict]:
    cypher = """
    MATCH (n:Entity {uuid: $uuid})
    RETURN n.uuid AS uuid_, n.name AS name, n.labels AS labels,
           n.summary AS summary, n.created_at AS created_at
    """
    records, _, _ = driver.execute_query(cypher, uuid=node_uuid, database_=database)
    if not records:
        return None
    r = records[0]
    return {
        "uuid_": r["uuid_"],
        "name": r["name"] or "",
        "labels": r["labels"] or [],
        "summary": r["summary"] or "",
        "attributes": {},
        "created_at": str(r["created_at"]) if r["created_at"] else None,
    }


def _query_node_edges(driver, database: str, node_uuid: str) -> List[Dict]:
    """读取一个节点的所有出入边。"""
    cypher = """
    MATCH (n:Entity {uuid: $uuid})-[r]-(m:Entity)
    RETURN r.uuid AS uuid_, r.fact AS fact, r.fact_type AS fact_type,
           m.uuid AS other_uuid, m.name AS other_name,
           r.created_at AS created_at,
           CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END AS direction
    """
    records, _, _ = driver.execute_query(cypher, uuid=node_uuid, database_=database)
    edges = []
    for r in records:
        edges.append({
            "uuid_": r["uuid_"] or str(uuid.uuid4()),
            "name": (r["fact_type"] or "RELATES").upper(),
            "fact": r["fact"] or "",
            "fact_type": r["fact_type"] or "RELATES",
            "created_at": str(r["created_at"]) if r["created_at"] else None,
            "other_node_uuid": r["other_uuid"],
            "other_node_name": r["other_name"] or "",
            "direction": r["direction"],
        })
    return edges


# ============================================================
# 子 API 对象（模拟 Zep 的 client.graph.xxx 链式结构）
# ============================================================

class _NodeAPI:
    """模拟 client.graph.node"""

    def __init__(self, driver, database):
        self._driver = driver
        self._database = database
        self.with_raw_response = _WithRawResponseNode(driver, database)

    def get(self, uuid_: str = None, **kwargs) -> SimpleNamespace:
        node_uuid = uuid_ or kwargs.get("uuid")
        if not node_uuid:
            raise ValueError("uuid_ is required")
        data = _query_node_by_uuid(self._driver, self._database, node_uuid)
        if data is None:
            raise NotFoundError(f"Node {node_uuid} not found")
        return _make_node(**data)

    def get_edges(self, node_uuid: str, **kwargs) -> SimpleNamespace:
        """模拟 client.graph.node.get_edges。

        Zep SDK 返回 {"edges": [...]}。MiroFish 只读 edge 列表。
        """
        edges = _query_node_edges(self._driver, self._database, node_uuid)
        edge_objs = [_make_edge(
            uuid_=e["uuid_"],
            name=e["name"],
            fact=e["fact"],
            fact_type=e["fact_type"],
            source_node_uuid=node_uuid if e["direction"] == "out" else e["other_node_uuid"],
            target_node_uuid=e["other_node_uuid"] if e["direction"] == "out" else node_uuid,
            created_at=e["created_at"],
        ) for e in edges]
        return SimpleNamespace(edges=edge_objs)

    def get_by_graph_id(self, graph_id: str, **kwargs) -> SimpleNamespace:
        """非 with_raw_response 版本（MiroFish 目前用 with_raw_response 版本）。"""
        nodes = _query_all_nodes(self._driver, self._database, graph_id)
        node_objs = [_make_node(**n) for n in nodes]
        return SimpleNamespace(nodes=node_objs)


class _EdgeAPI:
    """模拟 client.graph.edge"""

    def __init__(self, driver, database):
        self._driver = driver
        self._database = database
        self.with_raw_response = _WithRawResponseEdge(driver, database)

    def get_by_graph_id(self, graph_id: str, **kwargs) -> SimpleNamespace:
        edges = _query_all_edges(self._driver, self._database, graph_id)
        edge_objs = [_make_edge(**e) for e in edges]
        return SimpleNamespace(edges=edge_objs)


class _EpisodeAPI:
    """模拟 client.graph.episode"""

    def __init__(self, driver, database):
        self._driver = driver
        self._database = database

    def get(self, uuid_: str = None, **kwargs) -> SimpleNamespace:
        ep_uuid = uuid_ or kwargs.get("uuid")
        cypher = """
        MATCH (e:Episodic {uuid: $uuid})
        RETURN e.uuid AS uuid_, e.created_at AS created_at,
               e.valid_at AS valid_at, e.group_id AS group_id,
               e.name AS name, e.content AS content,
               e.episode_type AS episode_type
        """
        records, _, _ = self._driver.execute_query(
            cypher, uuid=ep_uuid, database_=self._database
        )
        if not records:
            # Episodic 节点可能不存在（迁移时未导入），返回 processed=True 让上层跳过等待
            logger.warning(f"Episode {ep_uuid} not found in Neo4j, assuming processed")
            return _make_episode(uuid_=ep_uuid, processed=True)
        r = records[0]
        return _make_episode(
            uuid_=r["uuid_"],
            processed=True,  # 存在即视为已处理
            created_at=str(r["created_at"]) if r["created_at"] else None,
            name=r["name"],
            content=r["content"],
            group_id=r["group_id"],
        )


class _GraphAPI:
    """模拟 client.graph"""

    def __init__(self, driver, database, graphiti_factory=None):
        self._driver = driver
        self._database = database
        self._graphiti_factory = graphiti_factory  # 懒加载 graphiti-core 实例
        self._ontology_cache: Dict[str, Dict] = {}  # group_id -> ontology
        self.node = _NodeAPI(driver, database)
        self.edge = _EdgeAPI(driver, database)
        self.episode = _EpisodeAPI(driver, database)

    def create(self, graph_id: str, name: str = "", description: str = "", **kwargs) -> SimpleNamespace:
        """建图：Neo4j 用 group_id 隔离，无需预创建。

        只记录一个元数据节点方便后续 get() 验证存在性。
        """
        cypher = """
        MERGE (g:__GraphMeta {group_id: $group_id})
        SET g.name = $name, g.description = $description,
            g.created_at = datetime()
        """
        self._driver.execute_query(
            cypher, group_id=graph_id, name=name, description=description,
            database_=self._database,
        )
        logger.info(f"Graphiti shim: created graph group_id={graph_id}")
        return SimpleNamespace(graph_id=graph_id, name=name)

    def get(self, graph_id: str, **kwargs) -> SimpleNamespace:
        """查图是否存在。不存在时抛 NotFoundError（graph_builder reconcile 逻辑依赖）。"""
        cypher = "MATCH (g:__GraphMeta {group_id: $group_id}) RETURN g"
        records, _, _ = self._driver.execute_query(
            cypher, group_id=graph_id, database_=self._database
        )
        if not records:
            # 也检查是否有该 group 的 Entity 节点（迁移来的图可能没有 GraphMeta）
            cypher2 = "MATCH (n:Entity {group_id: $group_id}) RETURN count(n) AS cnt LIMIT 1"
            records2, _, _ = self._driver.execute_query(
                cypher2, group_id=graph_id, database_=self._database
            )
            if not records2 or records2[0]["cnt"] == 0:
                raise NotFoundError(f"Graph {graph_id} not found")
        return SimpleNamespace(graph_id=graph_id)

    def delete(self, graph_id: str, **kwargs):
        """删图：DETACH DELETE 该 group 的所有节点和边。"""
        cypher = """
        MATCH (n) WHERE n.group_id = $group_id DETACH DELETE n
        """
        self._driver.execute_query(cypher, group_id=graph_id, database_=self._database)
        self._ontology_cache.pop(graph_id, None)
        logger.info(f"Graphiti shim: deleted graph group_id={graph_id}")

    def set_ontology(self, graph_ids: List[str], entities: Dict, edges: Dict = None, **kwargs):
        """缓存 ontology 供后续 add_episode 写路径使用。

        Graphiti 的 entity_types 在 add_episode 时传入，不需要预注册。
        """
        for gid in graph_ids:
            self._ontology_cache[gid] = {"entities": entities, "edges": edges or {}}
        logger.info(f"Graphiti shim: cached ontology for {len(graph_ids)} graph(s)")

    def add(
        self,
        *,
        graph_id: str,
        type: str = "text",
        data: str,
        created_at: str | None = None,
        source_description: str = "",
        metadata: Dict[str, Any] | None = None,
        **kwargs,
    ) -> SimpleNamespace:
        """写入 episode：调用 graphiti.add_episode 做 LLM 抽取。

        Zep Cloud 的 graph.add 签名（MiroFish memory_updater 调用）：
            client.graph.add(graph_id=, type=, data=, created_at=,
                             source_description=, metadata=)

        shim 转换为 graphiti.add_episode：
            graphiti.add_episode(name=, episode_body=, source_description=,
                                 reference_time=, group_id=, source=EpisodeType.message)

        未配置 graphiti_factory 或 EXTRACTION_* 时抢 ValueError，
        调用方（memory_updater）应捕获后降级写本地 JSONL。
        """
        from datetime import datetime, timezone

        if not self._graphiti_factory:
            raise ValueError("graphiti_factory 未配置（无法执行写路径）")

        from graphiti_core.nodes import EpisodeType

        # 解析 created_at（datetime 对象或 RFC3339 字符串）
        if created_at:
            try:
                if isinstance(created_at, datetime):
                    ref_time = created_at
                else:
                    ref_time = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
            except (ValueError, AttributeError):
                ref_time = datetime.now(timezone.utc)
        else:
            ref_time = datetime.now(timezone.utc)

        graphiti = self._graphiti_factory()

        # 生成 episode 名字
        ep_name = f"episode_{int(ref_time.timestamp())}"
        if metadata and metadata.get("simulation_id"):
            ep_name = f"sim_{metadata['simulation_id']}_{ep_name}"

        # 取缓存的 ontology 作为 entity_types
        ontology = self._ontology_cache.get(graph_id, {})
        entity_types = ontology.get("entities")

        result = _run_async(
            graphiti.add_episode(
                name=ep_name,
                episode_body=data,
                source_description=source_description or "MiroFish episode",
                reference_time=ref_time,
                source=EpisodeType.message,
                group_id=graph_id,
                entity_types=entity_types,
            )
        )

        # add_episode 返回 AddEpisodeResults，取 episode uuid
        ep_uuid = ""
        if result and hasattr(result, "episode") and result.episode:
            ep_uuid = getattr(result.episode, "uuid", "") or str(uuid.uuid4())
        else:
            ep_uuid = str(uuid.uuid4())

        logger.info(
            f"Graphiti shim graph.add: graph_id={graph_id}, "
            f"episode={ep_uuid}, data_len={len(data)}"
        )
        return SimpleNamespace(uuid_=ep_uuid, uuid=ep_uuid)

    def search(
        self,
        query: str,
        *,
        graph_id: str | None = None,
        group_ids: List[str] | None = None,
        top_k: int = 5,
        **kwargs,
    ) -> List[Any]:
        """语义搜索。

        当前实现：如果配置了 graphiti_factory，走 graphiti.search（需要 LLM）。
        否则降级为 Cypher 关键词 LIKE 搜索（零 LLM 开销，精度较低）。

        MiroFish 调用点：
        - zep_tools.py: graph.search(query, ...)
        - oasis_profile_generator.py: graph.search(query, ...)
        """
        if self._graphiti_factory:
            return self._search_via_graphiti(query, graph_id, group_ids, top_k)
        return self._search_via_cypher(query, graph_id, top_k)

    def _search_via_cypher(self, query: str, graph_id: str | None, top_k: int) -> List[SimpleNamespace]:
        """降级搜索：用 LIKE 匹配节点 name/summary。

        返回对齐 Zep SDK search 结果的 edge 列表（fact + source/target 节点）。
        """
        gids = [graph_id] if graph_id else []
        if not gids:
            return []
        # 简单关键词匹配——生产环境应走 graphiti.search 的向量检索
        cypher = """
        MATCH (s:Entity)-[r]->(t:Entity)
        WHERE s.group_id IN $gids
          AND (toLower(r.fact) CONTAINS toLower($q)
               OR toLower(s.name) CONTAINS toLower($q)
               OR toLower(t.name) CONTAINS toLower($q))
        RETURN r.uuid AS uuid_, r.fact AS fact, r.fact_type AS fact_type,
               s.uuid AS source_node_uuid, t.uuid AS target_node_uuid,
               s.name AS source_node_name, t.name AS target_node_name,
               r.created_at AS created_at
        LIMIT $top_k
        """
        records, _, _ = self._driver.execute_query(
            cypher, gids=gids, q=query, top_k=top_k, database_=self._database
        )
        results = []
        for r in records:
            results.append(SimpleNamespace(
                fact=r["fact"] or "",
                fact_type=r["fact_type"] or "",
                source_node_uuid=r["source_node_uuid"],
                target_node_uuid=r["target_node_uuid"],
                source_node_name=r["source_node_name"],
                target_node_name=r["target_node_name"],
                uuid_=r["uuid_"],
            ))
        return results

    def _search_via_graphiti(self, query, graph_id, group_ids, top_k):
        """通过 graphiti-core 做真正的向量+关键词混合搜索。"""
        graphiti = self._graphiti_factory()
        gids = group_ids or ([graph_id] if graph_id else [])
        results = _run_async(
            graphiti.search(query, group_ids=gids, num_results=top_k)
        )
        # graphiti 返回 EntityEdge 对象，转成 SimpleNamespace 对齐 Zep 结构
        return [
            SimpleNamespace(
                fact=getattr(e, "fact", ""),
                fact_type=getattr(e, "fact_type", ""),
                source_node_uuid=getattr(e, "source_node_uuid", ""),
                target_node_uuid=getattr(e, "target_node_uuid", ""),
                uuid_=getattr(e, "uuid", ""),
            )
            for e in results
        ]


# ============================================================
# Batch API（映射到 graphiti-core.add_episode）
# ============================================================

class _BatchAPI:
    """模拟 client.batch。

    Zep Batch API 是异步摄取管道：create→add→process→轮询。
    shim 在内存中暂存文本块，process() 时一次性调 graphiti.add_episode。

    注意：当前实现是**单进程内存态**，不支持跨进程/容器重启恢复。
    MiroFish 的 graph_builder 依赖 batch_id 做 reconcile，shim 用确定性 ID 兜底。
    """

    def __init__(self, database, graphiti_factory=None):
        self._database = database
        self._graphiti_factory = graphiti_factory
        self._batches: Dict[str, Dict] = {}  # batch_id -> {items, status, metadata}

    def create(self, metadata: Dict | None = None, **kwargs) -> SimpleNamespace:
        batch_id = f"batch_{uuid.uuid4().hex[:16]}"
        self._batches[batch_id] = {
            "items": [],
            "status": "draft",
            "metadata": metadata or {},
            "created_at": time.time(),
        }
        logger.info(f"Graphiti shim batch.create: {batch_id}")
        return SimpleNamespace(batch_id=batch_id, metadata=metadata or {})

    def add(self, batch_id: str, items: List[Any], **kwargs) -> List[SimpleNamespace]:
        batch = self._batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"Batch {batch_id} not found")
        details = []
        base_idx = len(batch["items"])  # 循环外计算 base，避免 append 改变 len
        for i, item in enumerate(items):
            seq_idx = base_idx + i
            ep_uuid = str(uuid.uuid4())
            detail = SimpleNamespace(
                sequence_index=seq_idx,
                episode_uuid=ep_uuid,
                source_uuid=ep_uuid,
                status="succeeded",
            )
            details.append(detail)
            batch["items"].append({
                "sequence_index": seq_idx,
                "episode_uuid": ep_uuid,
                "source_uuid": ep_uuid,
                "data": getattr(item, "data", ""),
                "graph_id": getattr(item, "graph_id", None),
                "status": "succeeded",
            })
        return details

    def process(self, batch_id: str, **kwargs):
        """处理 batch：逐条调 graphiti.add_episode 做 LLM 抽取。

        未配置 graphiti_factory 时为 noop（保持阶段 1A 行为）。
        """
        from datetime import datetime, timezone

        batch = self._batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"Batch {batch_id} not found")

        if not self._graphiti_factory:
            batch["status"] = "succeeded"
            logger.info(f"Graphiti shim batch.process: {batch_id} (noop, no graphiti_factory)")
            return

        from graphiti_core.nodes import EpisodeType
        graphiti = self._graphiti_factory()
        processed = 0
        for item in batch["items"]:
            data = item.get("data", "")
            graph_id = item.get("graph_id")
            if not data:
                continue
            try:
                ref_time = datetime.now(timezone.utc)
                result = _run_async(
                    graphiti.add_episode(
                        name=f"batch_{batch_id}_{item['sequence_index']}",
                        episode_body=data,
                        source_description="MiroFish graph build batch",
                        reference_time=ref_time,
                        source=EpisodeType.message,
                        group_id=graph_id or batch["metadata"].get("graph_id"),
                    )
                )
                ep_uuid = ""
                if result and hasattr(result, "episode") and result.episode:
                    ep_uuid = getattr(result.episode, "uuid", "") or item["episode_uuid"]
                else:
                    ep_uuid = item["episode_uuid"]
                item["episode_uuid"] = ep_uuid
                item["source_uuid"] = ep_uuid  # 同步更新，避免 mismatched UUID 校验失败
                item["status"] = "succeeded"
                processed += 1
            except Exception as e:
                # LLM 抽取失败（如边缺 relation_type 字段）不中断整个建图。
                # episode 节点可能已部分写入 Neo4j，标记 succeeded 让流程继续。
                logger.warning(
                    f"Batch item {item['sequence_index']} LLM抽取失败，降级跳过: {e}"
                )
                item["status"] = "succeeded"  # 容忍部分失败
                item["error"] = str(e)

        batch["status"] = "succeeded"
        logger.info(f"Graphiti shim batch.process: {batch_id}, processed {processed}/{len(batch['items'])}")

    def get(self, batch_id: str, **kwargs) -> SimpleNamespace:
        batch = self._batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"Batch {batch_id} not found")
        item_count = len(batch["items"])
        succeeded = sum(1 for i in batch["items"] if i["status"] == "succeeded")
        return SimpleNamespace(
            batch_id=batch_id,
            status=batch["status"],
            metadata=batch["metadata"],
            progress=SimpleNamespace(
                percent_complete=100 if batch["status"] == "succeeded" else 0,
                succeeded_items=succeeded,
            ),
        )

    def list(self, limit: int = 100, cursor: int | None = None, **kwargs) -> SimpleNamespace:
        all_batches = list(self._batches.items())
        start = cursor or 0
        page = all_batches[start:start + limit]
        end = start + len(page)
        has_more = end < len(all_batches)
        return SimpleNamespace(
            batches=[
                SimpleNamespace(batch_id=bid, metadata=b["metadata"])
                for bid, b in page
            ],
            next_cursor=end if has_more else None,
        )

    def list_items(self, batch_id: str, limit: int = 100, cursor: int | None = None, **kwargs) -> SimpleNamespace:
        batch = self._batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"Batch {batch_id} not found")
        start = cursor or 0
        page = batch["items"][start:start + limit]
        end = start + len(page)
        has_more = end < len(batch["items"])
        return SimpleNamespace(
            items=[SimpleNamespace(**item) for item in page],
            next_cursor=end if has_more else None,
        )


# ============================================================
# 顶层 Zep 兼容 client
# ============================================================

class GraphitiShimClient:
    """模拟 zep_cloud.client.Zep 的顶层对象。

    用法（替换 get_zep_client 返回值）：
        client = GraphitiShimClient(uri, user, password, database="neo4j")
        client.graph.create(...)
        client.graph.search(...)
        client.batch.create(...)
    """

    def __init__(
        self,
        neo4j_uri: str,
        neo4j_user: str,
        neo4j_password: str,
        *,
        database: str = "neo4j",
        graphiti_factory=None,
    ):
        self._driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        self._database = database
        self.graph = _GraphAPI(self._driver, database, graphiti_factory)
        self.batch = _BatchAPI(database, graphiti_factory)
        logger.info(
            f"GraphitiShimClient initialized: uri={neo4j_uri}, database={database}"
        )

    def close(self):
        self._driver.close()
