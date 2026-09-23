from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    # Medical ID translation. The public instance needs a key from
    # portal.libretranslate.com; a self-hosted one usually does not.
    libretranslate_url: str = "https://libretranslate.com"
    libretranslate_api_key: str = ""

    @property
    def sqlalchemy_url(self) -> str:
        """Normalise Railway's DATABASE_URL for psycopg 3, or fall back to SQLite."""
        url = self.database_url.strip()
        if not url:
            return "sqlite:///./haemkaki.db"
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
