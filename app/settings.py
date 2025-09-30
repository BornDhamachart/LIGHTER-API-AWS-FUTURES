from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    SECRET_KEY: str | None = None  # fallback if .env missing
    BASE_URL: str | None = None
    LINE_BOT_TOKEN: str | None = None
    TARGET_IDS: str | None = None

    class Config:
        env_file = ".env"

settings = Settings()
