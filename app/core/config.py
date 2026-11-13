"""app/core/config.py — 配置读取。

用 pydantic-settings 从 .env 读配置，好处是：
1. 字段有类型，读错类型当场报错（而不是使用时才发现）
2. 有默认值，缺配置时能跑（而不是 KeyError）
3. IDE 能补全 settings.qdrant_url，不用记字符串键名

关于 .env 的路径：不能用相对路径 ".env"，因为那依赖"从哪个目录启动进程"。
PyCharm 里跑 pytest 的 cwd 是项目根，Docker 里可能是 /app，
用 __file__ 反推项目根目录才稳定。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/core -> app -> kb-agent（项目根）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """全部配置项。字段名与 .env 里的键对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",          # .env 里有多余的键不报错（比如从主项目复制过来的）
        case_sensitive=False,
    )

    # ---- LLM ----
    llm_base_url: str = "https://api.deepseek.com"
    llm_model_id: str = "deepseek-flash"
    llm_api_key: str = ""
    llm_temperature: float = 0.0
    llm_max_tokens: int = 2048

    # ---- Qdrant ----
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "kb_chunks"

    # ---- Embedding ----
    embed_model: str = "BAAI/bge-small-zh-v1.5"
    embed_dim: int = 512
    # BGE 中文模型的 query 侧前缀。passage 侧不加，加了反而降低效果。
    embed_query_instruction: str = "为这个句子生成表示以用于检索相关文章："

    # ---- 分块 ----
    chunk_size: int = 400
    chunk_overlap: int = 80

    # ---- Agent 内核 ----
    token_budget: int = 30000
    max_steps: int = 6
    token_estimate_cn: float = 1.0
    token_estimate_other: float = 0.3
    tool_timeout: float = 30.0
    tool_result_max_chars: int = 2000

    # ---- 访问控制 ----
    # 为空 = 不鉴权。本地开发/测试保持空，部署时在 .env 里设一个长口令。
    #
    # 为什么默认空而不是默认开：测试和本地开发不想背一个口令。
    # 代价是“忘配 = 裸奔”，所以启动时如果为空会打一行 WARN（见 app/api/auth.py）。
    access_token: str = ""
    # 每 IP 每分钟请求上限。只在 access_token 非空时生效 ——
    # 因为它是用来防“拿到口令之后滥用”和“暴力猜口令”的。
    rate_limit_per_minute: int = 60

    # ---- 数据库后端 ----
    # sqlite（默认）| mysql
    #
    # 为什么默认必须是 sqlite：190 个测试和本地开发不能依赖一个必须起容器的服务。
    # MySQL 只在部署时用，靠 .env 里一个变量切换。
    db_backend: str = "sqlite"

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "kb"
    mysql_password: str = ""
    mysql_database: str = "kb_agent"
    # 连接池大小。固定 5：够用，而且不会把 MySQL 的 max_connections 吃满。
    mysql_pool_size: int = 5
    # 从池里拿连接的等待上限。没这个上限的话，数据库卡住会让请求永久阻塞。
    mysql_acquire_timeout: float = 3.0

    # ---- 存储 ----
    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def trace_dir(self) -> Path:
        return self.data_dir / "traces"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "kb.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。lru_cache 保证 .env 只读一次。

    测试里要换配置时，用 get_settings.cache_clear() 配合 monkeypatch.setenv。
    """
    return Settings()


def reload_settings() -> Settings:
    """强制重读 .env（改完配置不想重启进程时用）。"""
    get_settings.cache_clear()
    return get_settings()
