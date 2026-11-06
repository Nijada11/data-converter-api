"""Settings, read from environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    api_key: str
    max_upload_bytes: int = 10 * 1024 * 1024
    rate_limit: str = "30/minute"

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.environ.get("API_KEY")
        if not api_key:
            # Fail closed: without this check a misconfigured deploy would run wide open.
            raise RuntimeError("API_KEY environment variable is required.")
        return cls(
            api_key=api_key,
            max_upload_bytes=int(os.environ.get("MAX_UPLOAD_BYTES", cls.max_upload_bytes)),
            rate_limit=os.environ.get("RATE_LIMIT", cls.rate_limit),
        )
