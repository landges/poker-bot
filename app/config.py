from dataclasses import dataclass, field
from os import getenv
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(default_factory=lambda: getenv("BOT_TOKEN", ""), repr=False)
    database_url: str = field(
        default_factory=lambda: getenv("DATABASE_URL", "sqlite:///poker.db"), repr=False,
    )
    proxy_url: Optional[str] = field(
        default_factory=lambda: getenv("PROXY_URL", "").strip() or None, repr=False,
    )


settings = Settings()
