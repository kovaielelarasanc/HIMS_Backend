from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URI,
    pool_pre_ping=True,  # avoids "MySQL server has gone away"
    pool_recycle=280,  # refresh stale connections
    pool_size=10,
    max_overflow=20,
    future=True,
)
SessionLocal = sessionmaker(autocommit=False,
                            autoflush=False,
                            bind=engine,
                            future=True)
