"""Bot configuration loaded from environment variables."""
from pydantic_settings import BaseSettings
from typing import List, Optional

class Settings(BaseSettings):
    bot_token: str
    admin_ids: List[int] = []
    database_url: str = "sqlite:///data/flashcart.db"
    redis_url: str = "redis://redis:6379/0"
    webhook_url: Optional[str] = None
    webhook_secret: str = "change-me"
    encryption_key: str
    captcha_api_key: Optional[str] = None
    proxy_list: List[str] = []
    check_interval_seconds: int = 5
    max_retries: int = 5

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()