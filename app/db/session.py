# app/db/session.py
from typing import Dict

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

# ---------- MASTER (Tenant Management) DB ----------

master_engine: Engine = create_engine(
    settings.MASTER_SQLALCHEMY_DATABASE_URI,
    pool_pre_ping=True,
    pool_recycle=280,
    pool_size=10,
    max_overflow=20,
    future=True,
)

MasterSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=master_engine,
    future=True,
)

# Backward-compatible names (some old code may still import these)
engine = master_engine
SessionLocal = MasterSessionLocal

# ---------- TENANT DB ENGINES (one per hospital) ----------

_tenant_engines: Dict[str, Engine] = {}


def get_or_create_tenant_engine(db_uri: str) -> Engine:
    eng = _tenant_engines.get(db_uri)
    if eng is None:
        eng = create_engine(
            db_uri,
            pool_pre_ping=True,
            pool_recycle=280,
            pool_size=10,
            max_overflow=20,
            future=True,
        )
        _tenant_engines[db_uri] = eng
    return eng


def create_tenant_session(db_uri: str):
    """
    Return a new SQLAlchemy Session bound to the given tenant DB URI.
    """
    eng = get_or_create_tenant_engine(db_uri)
    TenantSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=eng,
        future=True,
    )   
    return TenantSessionLocal()
