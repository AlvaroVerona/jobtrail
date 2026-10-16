import pytest
from sqlalchemy.orm import Session

from jobtrail.database.session import init_db, make_engine
from jobtrail.services.tracker import TrackerService


@pytest.fixture
def session():
    engine = make_engine("sqlite://")
    init_db(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def tracker(session):
    return TrackerService(session)


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """Point the CLI at an empty temp directory so tests never touch real data."""
    monkeypatch.setenv("JOBTRAIL_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path
