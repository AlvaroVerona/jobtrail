from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine

from jobtrail import __version__
from jobtrail.config import Settings, get_settings
from jobtrail.database.session import init_db, make_engine, make_session_factory
from jobtrail.llm.base import LLMClient
from jobtrail.llm.ollama_client import OllamaClient
from jobtrail.web import security
from jobtrail.web.matching_routes import router as matching_router
from jobtrail.web.routes import register_error_pages, router

WEB_DIR = Path(__file__).parent / "web"


def create_app(
    engine: Engine | None = None,
    settings: Settings | None = None,
    llm_factory: Callable[[], LLMClient] | None = None,
) -> FastAPI:
    """Build the app. Pass an engine to use a specific database and an llm_factory to use a
    specific model client (tests do both); by default the configured SQLite file and the
    local Ollama model are used."""
    settings = settings or get_settings()
    if engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        engine = make_engine(settings.database_url)
    init_db(engine)

    app = FastAPI(title="jobtrail", version=__version__)
    app.state.settings = settings
    app.state.session_factory = make_session_factory(engine)
    app.state.llm_factory = llm_factory or (
        lambda: OllamaClient(
            settings.ollama_model,
            settings.ollama_host,
            settings.ollama_num_ctx,
            settings.ollama_timeout,
        )
    )
    security.install(app, extra_hosts=(settings.host,))
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(router)
    app.include_router(matching_router)
    register_error_pages(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
