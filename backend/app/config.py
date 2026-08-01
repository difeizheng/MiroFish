"""
配置管理
统一从项目根目录的 .env 文件加载配置
"""

import os
import threading
from pathlib import Path
from dotenv import load_dotenv, dotenv_values

# 加载项目根目录的 .env 文件
# 路径: MiroFish/.env (相对于 backend/app/config.py)
project_root_env = os.path.join(os.path.dirname(__file__), '../../.env')

if os.path.exists(project_root_env):
    load_dotenv(project_root_env, override=False)
else:
    # 如果根目录没有 .env，尝试加载环境变量（用于生产环境）
    load_dotenv(override=False)


# ---------- LLM 参数热更新支持 ----------
# .env 文件路径（绝对路径，供 config_api 写入用）
ENV_FILE_PATH = Path(project_root_env).resolve() if os.path.exists(project_root_env) else None

# 运行时 LLM 覆盖值（由 config_api POST 写入，优先级最高）
# 为 None 表示未覆盖，回退到 .env / os.environ
_llm_overrides: dict = {}
_llm_override_lock = threading.Lock()

# .env 文件读取缓存（按 mtime 失效，避免每次访问都读文件）
_env_cache: dict = {"mtime": None, "values": {}}
_env_cache_lock = threading.Lock()


def _read_env_file():
    """读取 .env 文件内容（带 mtime 缓存）。文件不存在时回退空 dict。"""
    if ENV_FILE_PATH is None or not ENV_FILE_PATH.exists():
        return {}
    try:
        mtime = ENV_FILE_PATH.stat().st_mtime
    except OSError:
        return _env_cache["values"]
    with _env_cache_lock:
        if _env_cache["mtime"] != mtime:
            _env_cache["values"] = dotenv_values(ENV_FILE_PATH)
            _env_cache["mtime"] = mtime
        return _env_cache["values"]


def _get_llm_config(key, default=''):
    """
    动态读取一个 LLM 配置项，优先级：
    运行时覆盖（_llm_overrides） > os.environ > .env 文件 > default

    每次调用都重新求值，实现改完即生效（无需重启）。
    """
    with _llm_override_lock:
        override = _llm_overrides.get(key)
    if override is not None:
        return override
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    return _read_env_file().get(key, default)


def set_llm_override(key, value):
    """设置运行时 LLM 覆盖值（供 config_api 调用）。None 表示清除覆盖。

    同时写入 os.environ：模拟主流程 run_parallel_simulation.py 是子进程，
    直接读 os.environ（LLM_MAX_CONCURRENCY / LLM_BOOST_* 等），子进程只能
    通过环境变量继承热更新后的值。
    """
    with _llm_override_lock:
        if value is None:
            _llm_overrides.pop(key, None)
            os.environ.pop(key, None)
        else:
            _llm_overrides[key] = value
            os.environ[key] = value


def get_all_llm_config():
    """返回当前生效的全部 LLM 配置（供前端展示）。"""
    return {
        "LLM_API_KEY": _get_llm_config("LLM_API_KEY"),
        "LLM_BASE_URL": _get_llm_config("LLM_BASE_URL", "https://api.openai.com/v1"),
        "LLM_MODEL_NAME": _get_llm_config("LLM_MODEL_NAME", "gpt-4o-mini"),
        "LLM_BOOST_API_KEY": _get_llm_config("LLM_BOOST_API_KEY"),
        "LLM_BOOST_BASE_URL": _get_llm_config("LLM_BOOST_BASE_URL"),
        "LLM_BOOST_MODEL_NAME": _get_llm_config("LLM_BOOST_MODEL_NAME"),
        "LLM_MAX_CONCURRENCY": _get_llm_config("LLM_MAX_CONCURRENCY", "4"),
        "LLM_RATE_LIMIT_RETRIES": _get_llm_config("LLM_RATE_LIMIT_RETRIES", "8"),
    }


# ---- Config metaclass：让 LLM 字段动态求值 ----
# 关键：访问 Config.LLM_MODEL_NAME 走的是 type(Config).__getattribute__，
# 所以必须用 metaclass 的 property 才能拦截类属性访问。
class _ConfigMeta(type):
    """Config 的元类，将 LLM_* 字段变为每次访问动态求值的 property。"""

    @property
    def LLM_API_KEY(cls):
        return _get_llm_config("LLM_API_KEY")

    @property
    def LLM_BASE_URL(cls):
        return _get_llm_config("LLM_BASE_URL", "https://api.openai.com/v1")

    @property
    def LLM_MODEL_NAME(cls):
        return _get_llm_config("LLM_MODEL_NAME", "gpt-4o-mini")


class Config(metaclass=_ConfigMeta):
    """Flask配置类

    注意：LLM 相关字段（LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_NAME）通过 metaclass
    的 property 动态读取，运行时通过 config_api 修改后无需重启即生效。
    其它非 LLM 配置仍是模块加载时的静态值（ZEP_API_KEY 等改了要重建连接，不该热更）。
    """

    # Flask配置
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mirofish-secret-key')
    DEBUG = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'

    # JSON配置 - 禁用ASCII转义，让中文直接显示
    JSON_AS_ASCII = False

    # Zep配置
    ZEP_API_KEY = os.environ.get('ZEP_API_KEY')

    # Graphiti 后端配置（方案 D：用 Neo4j 替换 Zep）
    # GRAPH_BACKEND: 'zep'（默认，走 Zep Cloud）或 'graphiti'（走本地 Neo4j）
    GRAPH_BACKEND = os.environ.get('GRAPH_BACKEND', 'zep')
    NEO4J_URI = os.environ.get('NEO4J_URI', 'bolt://localhost:7687')
    NEO4J_USER = os.environ.get('NEO4J_USER', 'neo4j')
    NEO4J_PASSWORD = os.environ.get('NEO4J_PASSWORD', '')
    NEO4J_DATABASE = os.environ.get('NEO4J_DATABASE', 'neo4j')

    # Graphiti 写路径 LLM 配置（方案 D 阶段 1B）
    # 抽取 LLM：用于 graphiti.add_episode 时从文本抽取实体/关系
    EXTRACTION_API_KEY = os.environ.get('EXTRACTION_API_KEY', '')
    EXTRACTION_BASE_URL = os.environ.get('EXTRACTION_BASE_URL', '')
    EXTRACTION_MODEL_NAME = os.environ.get('EXTRACTION_MODEL_NAME', '')
    # Embedder：用于图谱节点向量化（搜索时用）
    EMBED_API_KEY = os.environ.get('EMBED_API_KEY', '')
    EMBED_BASE_URL = os.environ.get('EMBED_BASE_URL', '')
    EMBED_MODEL_NAME = os.environ.get('EMBED_MODEL_NAME', '')

    # 文件上传配置
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}

    # 文本处理配置
    DEFAULT_CHUNK_SIZE = 500  # 默认切块大小
    DEFAULT_CHUNK_OVERLAP = 50  # 默认重叠大小

    # OASIS模拟配置
    OASIS_DEFAULT_MAX_ROUNDS = int(os.environ.get('OASIS_DEFAULT_MAX_ROUNDS', '10'))
    OASIS_SIMULATION_DATA_DIR = os.path.join(os.path.dirname(__file__), '../uploads/simulations')

    # OASIS平台可用动作配置
    OASIS_TWITTER_ACTIONS = [
        'CREATE_POST', 'LIKE_POST', 'REPOST', 'FOLLOW', 'DO_NOTHING', 'QUOTE_POST'
    ]
    OASIS_REDDIT_ACTIONS = [
        'LIKE_POST', 'DISLIKE_POST', 'CREATE_POST', 'CREATE_COMMENT',
        'LIKE_COMMENT', 'DISLIKE_COMMENT', 'SEARCH_POSTS', 'SEARCH_USER',
        'TREND', 'REFRESH', 'DO_NOTHING', 'FOLLOW', 'MUTE'
    ]

    # Report Agent配置
    REPORT_AGENT_MAX_TOOL_CALLS = int(os.environ.get('REPORT_AGENT_MAX_TOOL_CALLS', '5'))
    REPORT_AGENT_MAX_REFLECTION_ROUNDS = int(os.environ.get('REPORT_AGENT_MAX_REFLECTION_ROUNDS', '2'))
    REPORT_AGENT_TEMPERATURE = float(os.environ.get('REPORT_AGENT_TEMPERATURE', '0.5'))

    @classmethod
    def validate(cls) -> list[str]:
        """验证必要配置"""
        errors: list[str] = []
        if not _get_llm_config("LLM_API_KEY"):
            errors.append("LLM_API_KEY 未配置")
        # ZEP_API_KEY 仅在 GRAPH_BACKEND=zep 时必须
        if cls.GRAPH_BACKEND == 'zep':
            if not cls.ZEP_API_KEY:
                errors.append("ZEP_API_KEY 未配置（或设置 GRAPH_BACKEND=graphiti 切换到 Neo4j 后端）")
            if os.environ.get("ZEP_API_URL"):
                errors.append("ZEP_API_URL 不受支持；MiroFish 仅连接 Zep Cloud")
        elif cls.GRAPH_BACKEND == 'graphiti':
            if not cls.NEO4J_PASSWORD:
                errors.append("NEO4J_PASSWORD 未配置（GRAPH_BACKEND=graphiti 需要）")
        if cls.DEBUG:
            import warnings
            warnings.warn("Flask DEBUG mode is enabled. Do not use in production.", RuntimeWarning)
        return errors
