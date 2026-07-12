"""Engine + session setup. Call init_db() once at startup."""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker

import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = scoped_session(
    sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
)
Base = declarative_base()


def init_db() -> None:
    """Create tables if they don't exist and seed ig_credentials from env."""
    import models  # imported here to avoid a circular import at module load

    Base.metadata.create_all(engine)
    models.seed_credentials(SessionLocal())
