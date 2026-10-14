"""Load the sample runs into a local SQLite file and serve the UI."""

from __future__ import annotations

from sqlalchemy import func, select

import uvicorn

from tracker.db import default_database_path, init_db, make_engine, session_factory
from tracker.ingest import load_sample_runs
from tracker.models import RunRow
from tracker.seed import seed_demo
from tracker.web import create_app


def main() -> None:
    database = default_database_path()
    engine = make_engine(database)
    init_db(engine)
    factory = session_factory(engine)
    session = factory()
    try:
        stored = session.scalar(select(func.count()).select_from(RunRow)) or 0
        if stored == 0:
            seed_demo(session)
            session.commit()
            print(f"Loaded {len(load_sample_runs())} sample runs into {database}")
        else:
            print(f"Using existing database {database}")
    finally:
        session.close()
    uvicorn.run(create_app(engine), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
