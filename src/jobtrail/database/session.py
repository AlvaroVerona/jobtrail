from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from jobtrail.database.models import Base, Channel

DEFAULT_CHANNELS = ("Easy Apply", "Company website", "Referral", "Recruiter", "Direct email")


def make_engine(url: str) -> Engine:
    return create_engine(url)


def init_db(engine: Engine) -> None:
    """Create tables and seed the default channels. Safe to call repeatedly."""
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        existing = set(session.scalars(select(Channel.name)))
        session.add_all(Channel(name=n) for n in DEFAULT_CHANNELS if n not in existing)
        session.commit()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
