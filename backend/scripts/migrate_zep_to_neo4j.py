#!/usr/bin/env python
"""
Zep 图谱缓存 → Neo4j 迁移脚本（方案 D 阶段 2）。

把 MiroFish 的 Zep 缓存 JSON（2734 节点 / 9263 边）零 LLM token 导入 Neo4j，
供 graphiti_shim.py 读路径使用。

用法：
    cd backend
    python -m scripts.migrate_zep_to_neo4j                          # 默认参数
    python -m scripts.migrate_zep_to_neo4j --dry-run                # 只打印不写
    python -m scripts.migrate_zep_to_neo4j --group-id mygroup       # 指定 group_id
    python -m scripts.migrate_zep_to_neo4j --cache-file path.json   # 指定缓存文件
    python -m scripts.migrate_zep_to_neo4j --batch-size 500         # 调整批量大小

设计原则：
1. 零 LLM token：直接 Cypher UNWIND 批量插入，不调任何 LLM/embedding
2. 幂等：MERGE 而非 CREATE，重复运行不报错
3. 保留原始 labels：存到 n.labels 属性（对齐 Graphiti schema）
4. 原生 Neo4j label：给所有节点加 :Entity 标签（Graphiti 查询模式）
5. 细分类型作为额外原生 label：如 ["Organization"] → 同时加 :Organization
6. name_embedding 留空：迁移时不算 embedding（搜索降级为 Cypher LIKE）
7. 索引优化：迁移后自动建 name/group_id 索引

Graphiti Entity 节点 schema 对齐：
    uuid: string (主键)
    name: string
    labels: string[] (原 Zep 的实体类型，如 ["Organization"])
    summary: string
    group_id: string (图谱隔离键)
    created_at: datetime
    name_embedding: float[] (留空)
    + 所有 attributes 字段平铺到节点属性

Graphiti 关系边 schema 对齐：
    uuid: string (主键)
    fact: string
    fact_type: string
    episodes: string[]
    created_at: datetime
    valid_at: datetime (可选)
    invalid_at: datetime (可选)
    expired_at: datetime (可选)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from neo4j import GraphDatabase


def parse_args():
    p = argparse.ArgumentParser(
        description="Zep 缓存 → Neo4j 迁移工具（零 LLM token）"
    )
    p.add_argument(
        "--cache-file",
        default="uploads/graphs/mirofish_1069a964f83c4bef.json",
        help="Zep 缓存 JSON 路径（默认 MiroFish 主图谱）",
    )
    p.add_argument(
        "--group-id",
        default=None,
        help="Neo4j group_id（默认用缓存文件里的 graph_id）",
    )
    p.add_argument("--neo4j-uri", default=None, help="Neo4j bolt URI")
    p.add_argument("--neo4j-user", default=None)
    p.add_argument("--neo4j-password", default=None)
    p.add_argument("--neo4j-database", default=None)
    p.add_argument("--batch-size", type=int, default=500, help="UNWIND 批大小")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析和打印统计，不写 Neo4j",
    )
    p.add_argument(
        "--skip-index",
        action="store_true",
        help="跳过索引创建（如果已存在会加速重复运行）",
    )
    p.add_argument(
        "--clear-existing",
        action="store_true",
        help="迁移前删除该 group_id 的所有现有节点（慎用）",
    )
    return p.parse_args()


def load_cache(cache_path: str) -> Dict[str, Any]:
    """加载 Zep 缓存 JSON。"""
    path = Path(cache_path)
    if not path.exists():
        raise FileNotFoundError(f"缓存文件不存在: {path.absolute()}")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # 兼容两种结构：顶层就是 {nodes, edges} 或嵌套在 data 里
    if "data" in data and "nodes" not in data:
        inner = data["data"]
    else:
        inner = data

    nodes = inner.get("nodes", [])
    edges = inner.get("edges", [])
    graph_id = inner.get("graph_id") or data.get("graph_id", "unknown")

    return {"nodes": nodes, "edges": edges, "graph_id": graph_id}


def get_neo4j_config(args):
    """从命令行参数或环境变量获取 Neo4j 连接配置。"""
    return {
        "uri": args.neo4j_uri or os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        "user": args.neo4j_user or os.getenv("NEO4J_USER", "neo4j"),
        "password": args.neo4j_password or os.getenv("NEO4J_PASSWORD", "testpass"),
        "database": args.neo4j_database or os.getenv("NEO4J_DATABASE", "neo4j"),
    }


def _to_label_list(labels_raw: Any) -> List[str]:
    """规范化 labels 为字符串列表。"""
    if not labels_raw:
        return []
    if isinstance(labels_raw, str):
        return [labels_raw]
    if isinstance(labels_raw, list):
        return [str(l) for l in labels_raw if l]
    return []


def _sanitize_label(label: str) -> str:
    """把实体类型转为合法的 Neo4j label（只能字母数字下划线）。

    例如 "SemiconductorCompany" → "SemiconductorCompany"（已合法）
    例如 "政府机构" → 需要 sanitize 但这里假设类型已是英文
    """
    if not label:
        return ""
    # Neo4j label 只接受字母数字和下划线，不能以数字开头
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in label)
    if safe and safe[0].isdigit():
        safe = "_" + safe
    return safe


def migrate_nodes(
    driver,
    database: str,
    nodes: List[Dict],
    group_id: str,
    batch_size: int,
) -> int:
    """批量插入节点。返回插入数量。"""
    # 预处理：把 attributes 平铺 + 规范化 labels + 转换 created_at
    prepared = []
    for n in nodes:
        labels = _to_label_list(n.get("labels"))
        attrs = n.get("attributes") or {}

        row = {
            "uuid": n.get("uuid"),
            "name": n.get("name", ""),
            "labels": labels,
            "summary": n.get("summary", ""),
            "group_id": group_id,
            "created_at": n.get("created_at"),
            # 把 attributes 平铺到节点属性（Neo4j 节点可以有很多属性）
            "attrs_json": json.dumps(attrs, ensure_ascii=False),
        }
        prepared.append(row)

    cypher = """
    UNWIND $batch AS row
    MERGE (n:Entity {uuid: row.uuid})
    SET n.name = row.name,
        n.labels = row.labels,
        n.summary = row.summary,
        n.group_id = row.group_id,
        n.created_at = CASE
            WHEN row.created_at IS NOT NULL THEN datetime(replace(row.created_at, 'Z', '+00:00'))
            ELSE datetime()
        END,
        n.attrs_json = row.attrs_json
    """

    count = 0
    for i in range(0, len(prepared), batch_size):
        batch = prepared[i : i + batch_size]
        driver.execute_query(cypher, batch=batch, database_=database)
        count += len(batch)
        print(f"  节点进度: {count}/{len(prepared)}")

    # 给有细分类型的节点加额外的原生 Neo4j label
    # 例如 labels=["Organization"] 的节点加上 :Organization
    print("  添加细分类型原生 label...")
    all_subtypes = set()
    for n in nodes:
        for l in _to_label_list(n.get("labels")):
            safe = _sanitize_label(l)
            if safe:
                all_subtypes.add(safe)

    for subtype in sorted(all_subtypes):
        cypher_sub = f"""
        MATCH (n:Entity)
        WHERE n.group_id = $group_id AND $subtype IN n.labels
        CALL apoc.cypher.doIt('SET n:`{subtype}`', {{n: n}}) YIELD value
        RETURN count(value) AS cnt
        """
        try:
            records, _, _ = driver.execute_query(
                cypher_sub, group_id=group_id, subtype=subtype, database_=database
            )
            cnt = records[0]["cnt"] if records else 0
            if cnt > 0:
                print(f"    :{subtype} → {cnt} 个节点")
        except Exception as e:
            # APOC 可能不可用，降级为逐条 SET（较慢但兼容）
            print(f"    APOC 不可用，降级逐条设置 :{subtype}...")
            cypher_fallback = f"""
            MATCH (n:Entity {{group_id: $group_id}})
            WHERE $subtype IN n.labels
            SET n:`{subtype}`
            """
            driver.execute_query(
                cypher_fallback, group_id=group_id, subtype=subtype, database_=database
            )

    return count


def migrate_edges(
    driver,
    database: str,
    edges: List[Dict],
    group_id: str,
    batch_size: int,
) -> int:
    """批量插入边。返回插入数量。"""
    prepared = []
    for e in edges:
        fact_type = e.get("fact_type") or e.get("name") or "RELATES"
        row = {
            "uuid": e.get("uuid"),
            "fact": e.get("fact", ""),
            "fact_type": fact_type,
            "name": e.get("name", fact_type),
            "source_node_uuid": e.get("source_node_uuid"),
            "target_node_uuid": e.get("target_node_uuid"),
            "source_node_name": e.get("source_node_name", ""),
            "target_node_name": e.get("target_node_name", ""),
            "episodes": e.get("episodes") or [],
            "created_at": e.get("created_at"),
            "valid_at": e.get("valid_at"),
            "invalid_at": e.get("invalid_at"),
            "expired_at": e.get("expired_at"),
            "attrs_json": json.dumps(e.get("attributes") or {}, ensure_ascii=False),
        }
        prepared.append(row)

    # 用 fact_type 作为关系类型。Neo4j 关系类型必须大写，且只能字母数字下划线
    # 先统一创建 generic RELATES 关系，再把 fact_type 存属性
    # 原因：Neo4j 关系类型在建图时确定，动态类型需要 APOC 或逐条 cypher
    cypher = """
    UNWIND $batch AS row
    MATCH (s:Entity {uuid: row.source_node_uuid, group_id: $group_id})
    MATCH (t:Entity {uuid: row.target_node_uuid, group_id: $group_id})
    MERGE (s)-[r:RELATES {uuid: row.uuid}]->(t)
    SET r.fact = row.fact,
        r.fact_type = row.fact_type,
        r.name = row.name,
        r.episodes = row.episodes,
        r.created_at = CASE
            WHEN row.created_at IS NOT NULL THEN datetime(replace(row.created_at, 'Z', '+00:00'))
            ELSE datetime()
        END,
        r.valid_at = CASE WHEN row.valid_at IS NOT NULL THEN datetime(replace(row.valid_at, 'Z', '+00:00')) ELSE NULL END,
        r.invalid_at = CASE WHEN row.invalid_at IS NOT NULL THEN datetime(replace(row.invalid_at, 'Z', '+00:00')) ELSE NULL END,
        r.expired_at = CASE WHEN row.expired_at IS NOT NULL THEN datetime(replace(row.expired_at, 'Z', '+00:00')) ELSE NULL END,
        r.attrs_json = row.attrs_json,
        r.group_id = $group_id
    """

    count = 0
    for i in range(0, len(prepared), batch_size):
        batch = prepared[i : i + batch_size]
        result = driver.execute_query(
            cypher, batch=batch, group_id=group_id, database_=database
        )
        count += len(batch)
        print(f"  边进度: {count}/{len(prepared)}")

    return count


def create_indexes(driver, database: str, group_id: str):
    """创建索引加速查询。"""
    indexes = [
        ("Entity_uuid", "CREATE INDEX entity_uuid IF NOT EXISTS FOR (n:Entity) ON (n.uuid)"),
        ("Entity_group_id", "CREATE INDEX entity_group_id IF NOT EXISTS FOR (n:Entity) ON (n.group_id)"),
        ("Entity_name", "CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)"),
        ("RELATES_uuid", "CREATE INDEX relates_uuid IF NOT EXISTS FOR ()-[r:RELATES]-() ON (r.uuid)"),
        ("RELATES_group_id", "CREATE INDEX relates_group_id IF NOT EXISTS FOR ()-[r:RELATES]-() ON (r.group_id)"),
        ("Entity_summary_fulltext", "CREATE FULLTEXT INDEX entity_summary_fulltext IF NOT EXISTS FOR (n:Entity) ON EACH [n.name, n.summary]"),
    ]
    for name, cypher in indexes:
        try:
            driver.execute_query(cypher, database_=database)
            print(f"  ✅ 索引 {name}")
        except Exception as e:
            # fulltext 索引可能需要特定语法，降级跳过
            print(f"  ⚠️  索引 {name} 跳过: {e}")


def clear_existing(driver, database: str, group_id: str):
    """删除该 group_id 的所有节点和边。"""
    cypher = "MATCH (n) WHERE n.group_id = $group_id DETACH DELETE n"
    driver.execute_query(cypher, group_id=group_id, database_=database)
    print(f"  已清空 group_id={group_id} 的所有节点和边")


def verify_migration(driver, database: str, group_id: str, expected_nodes: int, expected_edges: int):
    """迁移后验证数量。"""
    node_cypher = "MATCH (n:Entity {group_id: $group_id}) RETURN count(n) AS cnt"
    edge_cypher = "MATCH ()-[r:RELATES {group_id: $group_id}]->() RETURN count(r) AS cnt"

    _, _, _ = driver.execute_query(node_cypher, group_id=group_id, database_=database)
    records, _, _ = driver.execute_query(node_cypher, group_id=group_id, database_=database)
    actual_nodes = records[0]["cnt"] if records else 0

    records, _, _ = driver.execute_query(edge_cypher, group_id=group_id, database_=database)
    actual_edges = records[0]["cnt"] if records else 0

    print(f"\n===== 验证 =====")
    print(f"  节点: {actual_nodes} / 期望 {expected_nodes} {'✅' if actual_nodes == expected_nodes else '❌'}")
    print(f"  边:   {actual_edges} / 期望 {expected_edges} {'✅' if actual_edges == expected_edges else '❌'}")

    if actual_nodes != expected_nodes or actual_edges != expected_edges:
        print("  ⚠️  数量不匹配，可能部分数据已存在或有孤儿边")
        return False

    # 采样验证：随机取一个节点和一条边
    sample_node = driver.execute_query(
        "MATCH (n:Entity {group_id: $group_id}) RETURN n.name AS name, n.labels AS labels, n.summary AS summary LIMIT 1",
        group_id=group_id, database_=database,
    )
    if sample_node[0]:
        r = sample_node[0][0]
        print(f"  样例节点: name={r['name'][:30]}, labels={r['labels']}, summary={r['summary'][:50]}...")

    return True


def main():
    args = parse_args()
    start_time = time.time()

    # 1. 加载缓存
    print(f"===== 加载缓存: {args.cache_file} =====")
    cache = load_cache(args.cache_file)
    nodes = cache["nodes"]
    edges = cache["edges"]
    graph_id = cache["graph_id"]
    group_id = args.group_id or graph_id

    print(f"  图谱 ID: {graph_id}")
    print(f"  group_id: {group_id}")
    print(f"  节点数: {len(nodes)}")
    print(f"  边数:   {len(edges)}")

    if args.dry_run:
        print("\n===== DRY RUN — 不写入 Neo4j =====")
        # 打印 labels 分布
        from collections import Counter
        label_dist = Counter()
        for n in nodes:
            labels = _to_label_list(n.get("labels"))
            key = labels[0] if labels else "(empty)"
            label_dist[key] += 1
        print("  labels 分布:")
        for k, v in label_dist.most_common():
            print(f"    {k}: {v}")
        return

    # 2. 连接 Neo4j
    config = get_neo4j_config(args)
    print(f"\n===== 连接 Neo4j: {config['uri']} (db={config['database']}) =====")
    driver = GraphDatabase.driver(
        config["uri"], auth=(config["user"], config["password"])
    )

    # 验证连接
    try:
        records, _, _ = driver.execute_query("RETURN 1 AS test", database_=config["database"])
        if not records or records[0]["test"] != 1:
            raise RuntimeError("Neo4j 连接验证失败")
        print("  ✅ 连接成功")
    except Exception as e:
        print(f"  ❌ 连接失败: {e}")
        sys.exit(1)

    # 3. 清空现有（可选）
    if args.clear_existing:
        print(f"\n===== 清空现有数据 =====")
        clear_existing(driver, config["database"], group_id)

    # 4. 迁移节点
    print(f"\n===== 迁移节点（batch_size={args.batch_size}）=====")
    node_count = migrate_nodes(driver, config["database"], nodes, group_id, args.batch_size)

    # 5. 迁移边
    print(f"\n===== 迁移边（batch_size={args.batch_size}）=====")
    edge_count = migrate_edges(driver, config["database"], edges, group_id, args.batch_size)

    # 6. 建索引
    if not args.skip_index:
        print(f"\n===== 创建索引 =====")
        create_indexes(driver, config["database"], group_id)

    # 7. 验证
    verify_migration(driver, config["database"], group_id, len(nodes), len(edges))

    # 8. 创建 __GraphMeta 元数据节点（shim.graph.get 会查）
    driver.execute_query(
        """
        MERGE (g:__GraphMeta {group_id: $group_id})
        SET g.name = $name, g.created_at = datetime()
        """,
        group_id=group_id, name=f"MiroFish migrated graph",
        database_=config["database"],
    )

    elapsed = time.time() - start_time
    print(f"\n===== 完成 =====")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"  节点: {node_count}")
    print(f"  边:   {edge_count}")
    print(f"  group_id: {group_id}")
    print(f"  零 LLM token 消耗 ✅")

    driver.close()


if __name__ == "__main__":
    main()
