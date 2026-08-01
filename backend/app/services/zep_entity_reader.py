"""
Zep实体读取与过滤服务
从Zep图谱中读取节点，筛选出符合预定义实体类型的节点
"""

from typing import Dict, Any, List, Optional, Set, Callable, TypeVar
from dataclasses import dataclass, field
from zep_cloud import NotFoundError

from ..config import Config
from ..utils.logger import get_logger
from ..utils.zep_paging import fetch_all_nodes, fetch_all_edges
from ..utils.zep import call_zep_read_with_retry, get_zep_client

logger = get_logger('mirofish.zep_entity_reader')

# 用于泛型返回类型
T = TypeVar('T')


@dataclass
class EntityNode:
    """实体节点数据结构"""
    uuid: str
    name: str
    labels: List[str]
    summary: str
    attributes: Dict[str, Any]
    # 相关的边信息
    related_edges: List[Dict[str, Any]] = field(default_factory=list)
    # 相关的其他节点信息
    related_nodes: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "labels": self.labels,
            "summary": self.summary,
            "attributes": self.attributes,
            "related_edges": self.related_edges,
            "related_nodes": self.related_nodes,
        }
    
    def get_entity_type(self) -> Optional[str]:
        """获取实体类型（排除默认的Entity标签）"""
        for label in self.labels:
            if label not in ["Entity", "Node"]:
                return label
        return None


@dataclass
class FilteredEntities:
    """过滤后的实体集合"""
    entities: List[EntityNode]
    entity_types: Set[str]
    total_count: int
    filtered_count: int
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "entities": [e.to_dict() for e in self.entities],
            "entity_types": list(self.entity_types),
            "total_count": self.total_count,
            "filtered_count": self.filtered_count,
        }


class ZepEntityReader:
    """
    Zep实体读取与过滤服务
    
    主要功能：
    1. 从Zep图谱读取所有节点
    2. 筛选出符合预定义实体类型的节点（Labels不只是Entity的节点）
    3. 获取每个实体的相关边和关联节点信息
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or Config.ZEP_API_KEY
        if not self.api_key:
            raise ValueError("ZEP_API_KEY 未配置")
        
        self.client = get_zep_client(self.api_key)
    
    def _call_with_retry(
        self, 
        func: Callable[[], T], 
        operation_name: str,
        max_retries: int = 3,
        initial_delay: float = 2.0
    ) -> T:
        """
        带重试机制的Zep API调用
        
        Args:
            func: 要执行的函数（无参数的lambda或callable）
            operation_name: 操作名称，用于日志
            max_retries: 最大重试次数（默认3次，即最多尝试3次）
            initial_delay: 初始延迟秒数
            
        Returns:
            API调用结果
        """
        return call_zep_read_with_retry(
            func,
            operation_name=operation_name,
            max_attempts=max_retries,
            initial_delay=initial_delay,
        )
    
    def get_all_nodes(self, graph_id: str) -> List[Dict[str, Any]]:
        """
        获取图谱的所有节点（分页获取）

        Args:
            graph_id: 图谱ID

        Returns:
            节点列表
        """
        logger.info(f"获取图谱 {graph_id} 的所有节点...")

        # 本地补丁：GRAPH_LOCAL_ONLY 或 Zep 失败时从本地图谱缓存读取（Zep 额度耗尽离线模式）
        import os
        if os.environ.get('GRAPH_LOCAL_ONLY', '').lower() in ('1', 'true', 'yes'):
            cached = self._read_graph_cache_data(graph_id)
            if cached is not None:
                logger.info(f"GRAPH_LOCAL_ONLY: 从本地缓存读取 {len(cached.get('nodes', []))} 个节点")
                return cached.get('nodes', [])
            logger.warning("GRAPH_LOCAL_ONLY 已开启但本地缓存不存在，回退尝试 Zep")

        try:
            nodes = fetch_all_nodes(self.client, graph_id)
        except Exception as e:
            cached = self._read_graph_cache_data(graph_id)
            if cached is None:
                raise
            logger.warning(f"Zep 读取节点失败({e})，降级为本地缓存 ({len(cached.get('nodes', []))} 节点)")
            return cached.get('nodes', [])

        nodes_data = []
        for node in nodes:
            nodes_data.append({
                "uuid": getattr(node, 'uuid_', None) or getattr(node, 'uuid', ''),
                "name": node.name or "",
                "labels": node.labels or [],
                "summary": node.summary or "",
                "attributes": node.attributes or {},
            })

        logger.info(f"共获取 {len(nodes_data)} 个节点")
        return nodes_data

    def get_all_edges(self, graph_id: str) -> List[Dict[str, Any]]:
        """
        获取图谱的所有边（分页获取）

        Args:
            graph_id: 图谱ID

        Returns:
            边列表
        """
        logger.info(f"获取图谱 {graph_id} 的所有边...")

        # 本地补丁：GRAPH_LOCAL_ONLY 或 Zep 失败时从本地图谱缓存读取
        import os
        if os.environ.get('GRAPH_LOCAL_ONLY', '').lower() in ('1', 'true', 'yes'):
            cached = self._read_graph_cache_data(graph_id)
            if cached is not None:
                logger.info(f"GRAPH_LOCAL_ONLY: 从本地缓存读取 {len(cached.get('edges', []))} 条边")
                return cached.get('edges', [])
            logger.warning("GRAPH_LOCAL_ONLY 已开启但本地缓存不存在，回退尝试 Zep")

        try:
            edges = fetch_all_edges(self.client, graph_id)
        except Exception as e:
            cached = self._read_graph_cache_data(graph_id)
            if cached is None:
                raise
            logger.warning(f"Zep 读取边失败({e})，降级为本地缓存 ({len(cached.get('edges', []))} 边)")
            return cached.get('edges', [])

        edges_data = []
        for edge in edges:
            edges_data.append({
                "uuid": getattr(edge, 'uuid_', None) or getattr(edge, 'uuid', ''),
                "name": edge.name or "",
                "fact": edge.fact or "",
                "source_node_uuid": edge.source_node_uuid,
                "target_node_uuid": edge.target_node_uuid,
                "attributes": edge.attributes or {},
            })

        logger.info(f"共获取 {len(edges_data)} 条边")
        return edges_data
    
    # ===== 本地补丁：Agent 实体质量过滤 + Zep 离线降级 =====
    _GENERIC_NAME_DENYLIST = {
        '公司', '本公司', '发行人', '发行人子公司', '子公司', '集团', '公司集团',
        'company', 'the company', 'issuer', '目标公司', '标的', '标的公司',
        '关联方', '控股股东', '实际控制人', '实控人', '管理层', '员工',
        '承诺人', '本人', '董事会', '监事会', '股东大会',
    }

    @staticmethod
    def _read_graph_cache_data(graph_id: str):
        """读取本地图谱缓存（uploads/graphs/<id>.json），返回 data 字段或 None"""
        try:
            from .graph_builder import GraphBuilderService
            return GraphBuilderService.read_graph_cache(graph_id)
        except Exception:
            return None

    @classmethod
    def _is_generic_name(cls, name) -> bool:
        import re
        n = (name or '').strip().lower()
        if not n or len(n) <= 1:
            return True
        if n in cls._GENERIC_NAME_DENYLIST:
            return True
        if re.fullmatch(r'公司[a-z]', n):   # 公司A / 公司B 等占位符
            return True
        if re.fullmatch(r'[\d\W]+', n):    # 纯数字/符号
            return True
        return False

    def _get_degree_map(self, graph_id: str, all_edges=None):
        """节点度数表。优先用传入的边；否则读本地图谱缓存；都没有返回 None（跳过度数过滤）"""
        edges = all_edges
        if edges is None:
            edges = self._read_graph_cache_data(graph_id)
            edges = edges.get('edges') if edges else None
        if edges is None:
            return None
        deg = {}
        for e in edges:
            deg[e['source_node_uuid']] = deg.get(e['source_node_uuid'], 0) + 1
            deg[e['target_node_uuid']] = deg.get(e['target_node_uuid'], 0) + 1
        return deg

    def get_node_edges(
        self,
        node_uuid: str,
        *,
        graph_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取指定节点的相关边。

        Zep Cloud 3.25 的 ``graph.node.get_edges`` 实测只返回节点作为
        source 的边，尽管文档将其描述为“all edges”。需要完整上下文时必须
        提供 graph_id，以全图分页后同时筛选 incoming 和 outgoing 边。
        
        Args:
            node_uuid: 节点UUID
            graph_id: 图谱ID；提供时保证返回双向完整关系
            
        Returns:
            边列表
        """
        try:
            if graph_id:
                return [
                    edge
                    for edge in self.get_all_edges(graph_id)
                    if edge["source_node_uuid"] == node_uuid
                    or edge["target_node_uuid"] == node_uuid
                ]

            # 使用重试机制调用Zep API
            edges = self._call_with_retry(
                func=lambda: self.client.graph.node.get_edges(node_uuid=node_uuid),
                operation_name=f"获取节点边(node={node_uuid[:8]}...)"
            )
            
            edges_data = []
            for edge in edges:
                edges_data.append({
                    "uuid": getattr(edge, 'uuid_', None) or getattr(edge, 'uuid', ''),
                    "name": edge.name or "",
                    "fact": edge.fact or "",
                    "source_node_uuid": edge.source_node_uuid,
                    "target_node_uuid": edge.target_node_uuid,
                    "attributes": edge.attributes or {},
                })
            
            return edges_data
        except Exception as e:
            # An empty edge list is valid data. Authentication, permission and
            # transport failures must not be made indistinguishable from it.
            logger.error(f"获取节点 {node_uuid} 的边失败: {str(e)}")
            raise
    
    def filter_defined_entities(
        self, 
        graph_id: str,
        defined_entity_types: Optional[List[str]] = None,
        enrich_with_edges: bool = True,
        min_degree: Optional[int] = None,
        max_entities: Optional[int] = None
    ) -> FilteredEntities:
        """
        筛选出符合预定义实体类型的节点
        
        筛选逻辑：
        - 如果节点的Labels只有一个"Entity"，说明这个实体不符合我们预定义的类型，跳过
        - 如果节点的Labels包含除"Entity"和"Node"之外的标签，说明符合预定义类型，保留
        
        Args:
            graph_id: 图谱ID
            defined_entity_types: 预定义的实体类型列表（可选，如果提供则只保留这些类型）
            enrich_with_edges: 是否获取每个实体的相关边信息
            
        Returns:
            FilteredEntities: 过滤后的实体集合
        """
        logger.info(f"开始筛选图谱 {graph_id} 的实体...")
        
        # 获取所有节点
        all_nodes = self.get_all_nodes(graph_id)
        total_count = len(all_nodes)
        
        # 获取所有边（用于后续关联查找）
        all_edges = self.get_all_edges(graph_id) if enrich_with_edges else []
        
        # 构建节点UUID到节点数据的映射
        node_map = {n["uuid"]: n for n in all_nodes}
        
        # ===== 本地补丁：质量过滤参数（env 可调；调用方可显式覆盖）=====
        import os
        if min_degree is None:
            try:
                min_degree = max(0, int(os.environ.get('AGENT_MIN_DEGREE', '3')))
            except ValueError:
                min_degree = 3
        if max_entities is None:
            try:
                max_entities = int(os.environ.get('AGENT_MAX_ENTITIES', '150'))
            except ValueError:
                max_entities = 150
        extra_deny = {x.strip().lower() for x in os.environ.get('AGENT_EXCLUDE_NAMES', '').split(',') if x.strip()}
        
        # ===== 第一遍：类型过滤，收集候选节点 =====
        candidates = []  # [(node, entity_type)]
        entity_types_found = set()
        
        for node in all_nodes:
            labels = node.get("labels", [])
            
            # 筛选逻辑：Labels必须包含除"Entity"和"Node"之外的标签
            custom_labels = [l for l in labels if l not in ["Entity", "Node"]]
            
            if not custom_labels:
                # 只有默认标签，跳过
                continue
            
            # 如果指定了预定义类型，检查是否匹配
            if defined_entity_types:
                matching_labels = [l for l in custom_labels if l in defined_entity_types]
                if not matching_labels:
                    continue
                entity_type = matching_labels[0]
            else:
                entity_type = custom_labels[0]
            
            entity_types_found.add(entity_type)
            candidates.append((node, entity_type))
        
        # ===== 第二遍：质量过滤（通用名黑名单 → 度数阈值 → 按度数排序截断）=====
        quality_before = len(candidates)
        candidates = [
            (n, t) for n, t in candidates
            if not self._is_generic_name(n.get('name'))
            and (n.get('name') or '').strip().lower() not in extra_deny
        ]
        deny_removed = quality_before - len(candidates)
        
        deg_map = self._get_degree_map(graph_id, all_edges if enrich_with_edges else None)
        degree_note = "度数数据不可用，跳过度数过滤"
        if deg_map is not None:
            before = len(candidates)
            if min_degree > 0:
                candidates = [(n, t) for n, t in candidates if deg_map.get(n["uuid"], 0) >= min_degree]
            candidates.sort(key=lambda nt: -deg_map.get(nt[0]["uuid"], 0))
            degree_note = f"度>={min_degree} 过滤 {before} -> {len(candidates)}"
        if max_entities > 0 and len(candidates) > max_entities:
            candidates = candidates[:max_entities]
        # 类型集合按最终幸存者重算
        entity_types_found = {t for _, t in candidates}
        logger.info(
            f"质量过滤: 黑名单剔除 {deny_removed}, {degree_note}, "
            f"上限 {max_entities}, 最终 {len(candidates)}"
        )
        
        # ===== 第三遍：为幸存者构建 EntityNode 并补充边信息 =====
        filtered_entities = []
        
        for node, entity_type in candidates:
            # 创建实体节点对象
            entity = EntityNode(
                uuid=node["uuid"],
                name=node["name"],
                labels=node.get("labels", []),
                summary=node["summary"],
                attributes=node["attributes"],
            )
            
            # 获取相关边和节点
            if enrich_with_edges:
                related_edges = []
                related_node_uuids = set()
                
                for edge in all_edges:
                    if edge["source_node_uuid"] == node["uuid"]:
                        related_edges.append({
                            "direction": "outgoing",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "target_node_uuid": edge["target_node_uuid"],
                        })
                        related_node_uuids.add(edge["target_node_uuid"])
                    elif edge["target_node_uuid"] == node["uuid"]:
                        related_edges.append({
                            "direction": "incoming",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "source_node_uuid": edge["source_node_uuid"],
                        })
                        related_node_uuids.add(edge["source_node_uuid"])
                
                entity.related_edges = related_edges
                
                # 获取关联节点的基本信息
                related_nodes = []
                for related_uuid in related_node_uuids:
                    if related_uuid in node_map:
                        related_node = node_map[related_uuid]
                        related_nodes.append({
                            "uuid": related_node["uuid"],
                            "name": related_node["name"],
                            "labels": related_node["labels"],
                            "summary": related_node.get("summary", ""),
                        })
                
                entity.related_nodes = related_nodes
            
            filtered_entities.append(entity)
        
        logger.info(f"筛选完成: 总节点 {total_count}, 符合条件 {len(filtered_entities)}, "
                   f"实体类型: {entity_types_found}")
        
        return FilteredEntities(
            entities=filtered_entities,
            entity_types=entity_types_found,
            total_count=total_count,
            filtered_count=len(filtered_entities),
        )
    
    def get_entity_with_context(
        self, 
        graph_id: str, 
        entity_uuid: str
    ) -> Optional[EntityNode]:
        """
        获取单个实体及其完整上下文（边和关联节点，带重试机制）
        
        Args:
            graph_id: 图谱ID
            entity_uuid: 实体UUID
            
        Returns:
            EntityNode或None
        """
        try:
            # 使用重试机制获取节点
            node = self._call_with_retry(
                func=lambda: self.client.graph.node.get(uuid_=entity_uuid),
                operation_name=f"获取节点详情(uuid={entity_uuid[:8]}...)"
            )
            
            if not node:
                return None
            
            # 获取节点的边
            edges = self.get_node_edges(entity_uuid, graph_id=graph_id)
            
            # 获取所有节点用于关联查找
            all_nodes = self.get_all_nodes(graph_id)
            node_map = {n["uuid"]: n for n in all_nodes}
            
            # 处理相关边和节点
            related_edges = []
            related_node_uuids = set()
            
            for edge in edges:
                if edge["source_node_uuid"] == entity_uuid:
                    related_edges.append({
                        "direction": "outgoing",
                        "edge_name": edge["name"],
                        "fact": edge["fact"],
                        "target_node_uuid": edge["target_node_uuid"],
                    })
                    related_node_uuids.add(edge["target_node_uuid"])
                else:
                    related_edges.append({
                        "direction": "incoming",
                        "edge_name": edge["name"],
                        "fact": edge["fact"],
                        "source_node_uuid": edge["source_node_uuid"],
                    })
                    related_node_uuids.add(edge["source_node_uuid"])
            
            # 获取关联节点信息
            related_nodes = []
            for related_uuid in related_node_uuids:
                if related_uuid in node_map:
                    related_node = node_map[related_uuid]
                    related_nodes.append({
                        "uuid": related_node["uuid"],
                        "name": related_node["name"],
                        "labels": related_node["labels"],
                        "summary": related_node.get("summary", ""),
                    })
            
            return EntityNode(
                uuid=getattr(node, 'uuid_', None) or getattr(node, 'uuid', ''),
                name=node.name or "",
                labels=node.labels or [],
                summary=node.summary or "",
                attributes=node.attributes or {},
                related_edges=related_edges,
                related_nodes=related_nodes,
            )
            
        except NotFoundError:
            return None
        except Exception as e:
            # Only an actual Zep 404 means "entity not found". Propagate 401,
            # 403 and exhausted transport errors so callers cannot prepare a
            # simulation with silently incomplete graph context.
            logger.error(f"获取实体 {entity_uuid} 失败: {str(e)}")
            raise
    
    def get_entities_by_type(
        self, 
        graph_id: str, 
        entity_type: str,
        enrich_with_edges: bool = True
    ) -> List[EntityNode]:
        """
        获取指定类型的所有实体
        
        Args:
            graph_id: 图谱ID
            entity_type: 实体类型（如 "Student", "PublicFigure" 等）
            enrich_with_edges: 是否获取相关边信息
            
        Returns:
            实体列表
        """
        result = self.filter_defined_entities(
            graph_id=graph_id,
            defined_entity_types=[entity_type],
            enrich_with_edges=enrich_with_edges
        )
        return result.entities
