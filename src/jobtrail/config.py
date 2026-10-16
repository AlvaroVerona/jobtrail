from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every value can be overridden with a JOBTRAIL_* env var
    or a local .env file (never committed)."""

    model_config = SettingsConfigDict(env_prefix="JOBTRAIL_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    db_file: str = "jobtrail.db"
    host: str = "127.0.0.1"
    port: int = 8000
    ollama_model: str = "qwen3:8b"
    ollama_host: str = "http://localhost:11434"
    ollama_num_ctx: int = 8192
    ollama_timeout: int = 300
    stale_after_days: int = 21
    min_sample: int = 20

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / self.db_file}"


def get_settings() -> Settings:
    return Settings()
