from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from tracker.db import init_db, make_engine, session_factory


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    engine = make_engine(tmp_path / "triage.db")
    init_db(engine)
    factory = session_factory(engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
