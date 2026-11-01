from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, echo=False)
# Out-of-process code (RQ workers, CLI, and out-of-band request writes) has no
# ambient session to bind to and must create its own via new_session(), which
# resolves SessionLocal at call time so tests redirect every consumer through
# the single patch point app.db.SessionLocal instead of per-module attributes.
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    # No-op for engine binds; lets tests rebind to a connection already inside
    # an outer transaction without committing it on session.commit().
    join_transaction_mode="create_savepoint",
)


def new_session() -> Session:
    return SessionLocal()


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
