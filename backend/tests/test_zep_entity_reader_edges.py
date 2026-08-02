from types import SimpleNamespace

from app.services.zep_entity_reader import ZepEntityReader
from zep_cloud.graph.node.client import NodeClient


def test_pinned_zep_sdk_exposes_get_edges_not_get_entity_edges():
    assert hasattr(NodeClient, "get_edges")
    assert not hasattr(NodeClient, "get_entity_edges")


def test_get_node_edges_uses_the_supported_sdk_method():
    calls = []

    class NodeApi:
        def get_edges(self, *, node_uuid):
            calls.append(node_uuid)
            return [SimpleNamespace(
                uuid_="edge-1",
                name="KNOWS",
                fact="Alice knows Bob",
                source_node_uuid="node-1",
                target_node_uuid="node-2",
                attributes={"since": "2024"},
            )]

    class GraphApi:
        node = NodeApi()

    class Client:
        graph = GraphApi()

    reader = object.__new__(ZepEntityReader)
    reader.client = Client()

    assert reader.get_node_edges("node-1") == [{
        "uuid": "edge-1",
        "name": "KNOWS",
        "fact": "Alice knows Bob",
        "source_node_uuid": "node-1",
        "target_node_uuid": "node-2",
        "attributes": {"since": "2024"},
    }]
    assert calls == ["node-1"]


def test_filter_defined_entities_graphiti_fallback_all_entity_labels():
    """回归测试：graphiti 模式下所有节点只有 'Entity' label 时降级为全部纳入候选。

    场景：graphiti-core 不按 ontology 给节点打细分标签（如 AutomotiveCompany），
    所有实体 labels=['Entity']。ZepEntityReader.filter_defined_entities 应降级为
    不做类型过滤，靠 entity_selector 的 LLM 智能筛选环节决定哪些实体适合作 Agent。
    """
    reader = object.__new__(ZepEntityReader)

    # 模拟 graphiti 模式的图谱数据：全部 labels=['Entity']
    graph_id = "test_graphiti_graph"
    all_nodes = [
        {"uuid": "n1", "name": "小米SU7", "labels": ["Entity"], "summary": "小米SU7是小米汽车的首款轿车", "attributes": {}},
        {"uuid": "n2", "name": "雷军", "labels": ["Entity"], "summary": "小米集团创始人", "attributes": {}},
        # 通用名应被过滤
        {"uuid": "n3", "name": "北京", "labels": ["Entity"], "summary": "城市", "attributes": {}},
    ]
    all_edges = []

    # mock get_all_nodes / get_all_edges
    reader.get_all_nodes = lambda gid: all_nodes if gid == graph_id else []
    reader.get_all_edges = lambda gid: all_edges if gid == graph_id else []
    reader.client = None  # 不需要调用

    result = reader.filter_defined_entities(
        graph_id=graph_id,
        defined_entity_types=None,
        enrich_with_edges=False,
    )

    # 降级后应保留全部节点（type 过滤被跳过；通用名过滤在降级路径也会调 _is_generic_name）
    result_entities = result.entities
    names = [e.name for e in result_entities]
    assert "小米SU7" in names
    assert "雷军" in names
    # 降级路径同样会过滤通用名，但"北京"不在 _is_generic_name 黑名单里 → 会保留
    assert len(result_entities) >= 2


def test_filter_defined_entities_zep_mode_preserves_type_filter():
    """回归测试：Zep Cloud 模式下节点有细分 label 时仍做类型过滤。"""
    reader = object.__new__(ZepEntityReader)

    graph_id = "test_zep_graph"
    all_nodes = [
        # 有细分类型 → 保留
        {"uuid": "n1", "name": "小米集团", "labels": ["Entity", "AutomotiveCompany"], "summary": "", "attributes": {}},
        {"uuid": "n2", "name": "雷军", "labels": ["Entity", "CorporateExecutive"], "summary": "", "attributes": {}},
        # 只有 Entity → 跳过（Zep 模式不做降级）
        {"uuid": "n3", "name": "某路人", "labels": ["Entity"], "summary": "", "attributes": {}},
    ]

    reader.get_all_nodes = lambda gid: all_nodes if gid == graph_id else []
    reader.get_all_edges = lambda gid: [] if gid == graph_id else []
    reader.client = None

    result = reader.filter_defined_entities(
        graph_id=graph_id,
        defined_entity_types=None,
        enrich_with_edges=False,
    )

    names = [e.name for e in result.entities]
    assert "小米集团" in names
    assert "雷军" in names
    assert "某路人" not in names  # 纯 Entity label 被跳过
    assert len(result.entities) == 2
