from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine

from jobtrail import __version__
from jobtrail.config import Settings, get_settings
from jobtrail.database.session import init_db, make_engine, make_session_factory
from jobtrail.web import security
from jobtrail.web.routes import register_error_pages, router

WEB_DIR = Path(__file__).parent / "web"


def create_app(engine: Engine | None = None, settings: Settings | None = None) -> FastAPI:
    """Build the app. Pass an engine to use a specific database (tests do); otherwise the
    configured SQLite file is created on first use."""
    settings = settings or get_settings()
    if engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        engine = make_engine(settings.database_url)
    init_db(engine)

    app = FastAPI(title="jobtrail", version=__version__)
    app.state.settings = settings
    app.state.session_factory = make_session_factory(engine)
    security.install(app, extra_hosts=(settings.host,))
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(router)
    register_error_pages(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
