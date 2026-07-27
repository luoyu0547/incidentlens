"""Database engine creation and schema management."""

from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy import create_engine as sa_create_engine

from incidentlens_telemetry.models import Base


def create_engine(url: str = "sqlite:///telemetry.db") -> Engine:
    """Create a SQLAlchemy engine and initialise all tables.

    For production use, pass a persistent database URL.
    For testing, pass ``"sqlite:///:memory:"`` to get an in-memory DB.
    """
    engine = sa_create_engine(url, echo=False)
    # Schema creation lives here (not in TelemetryRepository) because this
    # function owns the engine and is the single point of entry for DB setup.
    Base.metadata.create_all(engine)
    return engine
