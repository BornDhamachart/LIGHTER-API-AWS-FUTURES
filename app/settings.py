from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    SECRET_KEY: str | None = None  # fallback if .env missing
    API_PUBLIC_KEY: str | None = None
    API_SECRET_KEY: str | None = None
    WALLET_ADDRESS: str | None = None
    API_KEY_INDEX: int | None = None
    BASE_URL: str | None = None
    LINE_BOT_TOKEN: str | None = None
    TARGET_IDS: str | None = None

    class Config:
        env_file = ".env"

settings = Settings()
