# ==============================================================
# shared/database.py
# Used by ALL microservices to connect to RDS via Secrets Manager
# ==============================================================

import boto3
import json
import os
import time
import logging
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import redis

logger = logging.getLogger(__name__)

# ── Secrets Manager ───────────────────────────────────────────
def get_secret(secret_name: str) -> dict:
    """Fetch secret from AWS Secrets Manager."""
    client = boto3.client(
        "secretsmanager",
        region_name=os.getenv("AWS_REGION", "eu-north-1")
    )
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


# ── RDS PostgreSQL ────────────────────────────────────────────
def get_database_url() -> str:
    """Build DB URL (local OR AWS Secrets Manager)."""

    # LOCAL / DOCKER
    if os.getenv("ENV") == "local":
        return os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/shivamhospital"
        )

    # PRODUCTION (AWS)
    secret = get_secret("shivam-hospital/production/rds")

    password = quote_plus(secret["password"])  # 🔥 FIX for special chars (# etc.)

    return (
        f"postgresql://{secret['username']}:{password}"
        f"@{secret['host']}:{secret['port']}/{secret['dbname']}"
    )


# ── Create Engine ─────────────────────────────────────────────
engine = create_engine(
    get_database_url(),
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── DB Retry (IMPORTANT for Docker/K8s startup) ───────────────
def wait_for_db():
    for i in range(10):
        try:
            conn = engine.connect()
            conn.close()
            logger.info("✅ Database connected")
            return
        except Exception as e:
            logger.warning(f"⏳ DB not ready, retry {i+1}/10")
            time.sleep(3)

    raise Exception("❌ Database not available after retries")


wait_for_db()


def get_db():
    """FastAPI dependency — yields DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Redis ─────────────────────────────────────────────────────
def get_redis_client():
    """Connect to Redis (local OR ElastiCache)."""

    # LOCAL / DOCKER
    if os.getenv("ENV") == "local":
        return redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True
        )

    # PRODUCTION (AWS)
    secret = get_secret("shivam-hospital/production/redis")

    return redis.Redis(
        host=secret["host"],
        port=int(secret["port"]),
        decode_responses=True,
        ssl=True,
        ssl_cert_reqs=None  # 🔥 avoids TLS cert issues
    )


redis_client = get_redis_client()
