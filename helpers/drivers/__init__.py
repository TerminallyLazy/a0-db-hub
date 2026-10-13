"""Driver registry: static metadata plus a lazy driver factory.

This module must not import any third-party database library — availability
is probed with find_spec and driver modules import lazily on first use.
"""

from __future__ import annotations

import importlib

from usr.plugins.db_hub.helpers import bootstrap
from usr.plugins.db_hub.helpers.base import DbHubError

# These adapters implement the complete automatic-memory lifecycle, including
# collection creation and deletion. Vector search alone is insufficient.
MEMORY_DRIVERS = frozenset({"postgres", "qdrant", "lancedb", "chromadb"})

DRIVERS: dict[str, dict] = {
    "postgres": {
        "title": "Postgres (Postgres / Neon / Supabase)",
        "description": "Postgres-wire SQL plus pgvector search where available.",
        "capabilities": {"sql", "vector"},
        "import": "asyncpg",
        "pip": ["asyncpg>=0.29"],
        "dsn_example": "postgresql://app:§§secret(PG_PASS)@localhost:5432/app",
        "score": "cosine_distance",
    },
    "neo4j": {
        "title": "Neo4j",
        "description": "OpenCypher over Bolt, incl. Neo4j 5.x vector indexes.",
        "capabilities": {"cypher", "vector"},
        "import": "neo4j",
        "pip": ["neo4j>=5.20"],
        "dsn_example": "bolt://neo4j:§§secret(NEO4J_PASS)@localhost:7687/neo4j",
        "score": "cosine_similarity",
    },
    "hydradb": {
        "title": "HydraDB",
        "description": "Distributed graph DB; Bolt-compatible (default) or HTTPS query API.",
        "capabilities": {"cypher"},
        "import": "neo4j",
        "pip": ["neo4j>=5.20"],
        "dsn_example": "bolt://root:§§secret(HYDRA_PASS)@localhost:7687/graph",
    },
    "qdrant": {
        "title": "Qdrant",
        "description": "Vector collections over HTTP/gRPC.",
        "capabilities": {"vector"},
        "import": "qdrant_client",
        "pip": ["qdrant-client>=1.9"],
        "dsn_example": "http://localhost:6333",
        "score": "cosine_similarity",
    },
    "lancedb": {
        "title": "LanceDB",
        "description": "Embedded or cloud vector tables. Heavy wheel, installs on first use.",
        "capabilities": {"vector"},
        "import": "lancedb",
        "pip": ["lancedb>=0.9"],
        "dsn_example": "/data/lancedb",
        "score": "l2_distance",
    },
    "snowflake": {
        "title": "Snowflake",
        "description": "Snowflake SQL. Heavy connector, installs on first use.",
        "capabilities": {"sql"},
        "import": "snowflake.connector",
        "pip": ["snowflake-connector-python>=3.10"],
        "dsn_example": "snowflake://user:§§secret(SNOWFLAKE_PASS)@account/warehouse/db/schema",
    },
    "convex": {
        "title": "Convex",
        "description": "Convex query/mutation/action functions over HTTPS. No extra install.",
        "capabilities": {"api"},
        "import": "httpx",
        "pip": [],
        "dsn_example": "https://polished-panda-123.convex.cloud",
    },
    "sqlalchemy": {
        "title": "Generic SQL (SQLAlchemy)",
        "description": "Catch-all SQL access via a SQLAlchemy URL. Installs on first use.",
        "capabilities": {"sql"},
        "import": "sqlalchemy",
        "pip": ["sqlalchemy>=2.0"],
        "dsn_example": "sqlite:////data/app.db",
    },
    "redis": {
        "title": "Redis",
        "description": "Key/value commands plus RediSearch vector search where available.",
        "capabilities": {"kv", "vector"},
        "import": "redis",
        "pip": ["redis>=5.0"],
        "dsn_example": "redis://localhost:6379/0",
        "score": "cosine_distance",
    },
    "mongodb": {
        "title": "MongoDB",
        "description": "Document operations plus Atlas Vector Search where configured.",
        "capabilities": {"api", "vector"},
        "import": "pymongo",
        "pip": ["pymongo>=4.0"],
        "dsn_example": "mongodb://app:§§secret(MONGO_PASS)@localhost:27017/appdb",
        "score": "cosine_similarity",
    },
    "pinecone": {
        "title": "Pinecone",
        "description": "Serverless vector indexes. Needs options.api_key.",
        "capabilities": {"vector"},
        "import": "pinecone",
        "pip": ["pinecone>=3.0"],
        "dsn_example": "pinecone://my-index",
        "score": "cosine_similarity",
    },
    "chromadb": {
        "title": "ChromaDB",
        "description": "Embedded or client/server vector collections.",
        "capabilities": {"vector"},
        "import": "chromadb",
        "pip": ["chromadb>=0.5"],
        "dsn_example": "~/.chromadb",
        "score": "l2_distance",
    },
    "milvus": {
        "title": "Milvus",
        "description": "Scalable vector collections. Needs an existing collection.",
        "capabilities": {"vector"},
        "import": "pymilvus",
        "pip": ["pymilvus>=2.4"],
        "dsn_example": "http://localhost:19530",
        "score": "cosine_distance",
    },
    "duckdb": {
        "title": "DuckDB",
        "description": "Embedded analytical SQL over files or :memory:.",
        "capabilities": {"sql"},
        "import": "duckdb",
        "pip": ["duckdb>=0.10"],
        "dsn_example": "/data/analytics.duckdb",
    },
    "bigquery": {
        "title": "BigQuery",
        "description": "Google Cloud SQL analytics. Needs credentials in options.",
        "capabilities": {"sql"},
        "import": "google.cloud.bigquery",
        "pip": ["google-cloud-bigquery>=3.0"],
        "dsn_example": "bigquery://my-project/my_dataset",
    },
    "weaviate": {
        "title": "Weaviate",
        "description": "Vector collections with hybrid-ready search.",
        "capabilities": {"vector"},
        "import": "weaviate",
        "pip": ["weaviate-client>=4.0"],
        "dsn_example": "http://localhost:8080",
        "score": "cosine_similarity",
    },
    "fluree": {
        "title": "Fluree",
        "description": "Ledger graph queries over HTTPS. No extra install.",
        "capabilities": {"api"},
        "import": "httpx",
        "pip": [],
        "dsn_example": "http://localhost:8090",
    },
}


def driver_names() -> list[str]:
    return sorted(DRIVERS)


def get_driver_info(name: str) -> dict:
    info = DRIVERS.get(name or "")
    if info is None:
        raise DbHubError(
            f"Unknown db_hub driver '{name}'. "
            f"Valid drivers: {', '.join(driver_names())}."
        )
    return info


def create_driver(name: str):
    """Instantiate a driver, ensuring its third-party package first."""
    info = get_driver_info(name)
    bootstrap.ensure_driver_deps(name)
    module = importlib.import_module(f"usr.plugins.db_hub.helpers.drivers.{name}")
    return module.create()


def driver_availability() -> dict[str, dict]:
    availability = {}
    for name, info in DRIVERS.items():
        availability[name] = {
            "title": info["title"],
            "capabilities": sorted(info["capabilities"]),
            "memory_supported": name in MEMORY_DRIVERS,
            "package": info["import"],
            "available": bootstrap.is_importable(info["import"]),
            "dsn_example": info["dsn_example"],
        }
    return availability
