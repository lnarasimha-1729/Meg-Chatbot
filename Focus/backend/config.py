from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    APP_NAME:    str  = "Focus – Meghalaya Producer Group (PG) NLP-to-SQL"
    APP_VERSION: str  = "1.0.0"
    ENVIRONMENT: str  = "development"
    DEBUG:       bool = False
    LOG_LEVEL:   str  = "INFO"

    # ── Neon PostgreSQL (data queries only — no RAG) ───────────
    NEON_DATABASE_URL:  str = ""          # postgresql://...neon.tech/...
    DATA_TABLE:         str = "focus_pg"   # the Producer Group register
    MAX_SQL_ROWS:       int = 1000
    NEON_POOL_SIZE:     int = 2
    NEON_MAX_OVERFLOW:  int = 2

    # ── Gemini AI ──────────────────────────────────────────────
    GEMINI_API_KEY: str = ""

    # ── App ────────────────────────────────────────────────────
    SECRET_KEY:   str = ""
    HOST:         str = "0.0.0.0"
    PORT:         int = 8200          # Focus runs on its own port (Unified-Data 8000, CM-Elevate 8100)
    CORS_ORIGINS: str = "http://localhost:8000,http://localhost:8100,http://localhost:8200,http://localhost:3000"
    BACKEND_ENABLED: bool = True

    REDIS_URL:         str = "redis://localhost:6379"
    CACHE_TTL_SECONDS: int = 300

    class Config:
        env_file       = ".env"
        case_sensitive = True
        extra          = "allow"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]


settings = Settings()
