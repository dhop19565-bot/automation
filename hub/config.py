"""Server configuration, read from environment variables only."""
from __future__ import annotations

import os
from dataclasses import dataclass

from .providers.claude import DEFAULT_EFFORT, DEFAULT_MODEL


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/hub.sqlite3"
    claude_model: str = DEFAULT_MODEL
    claude_effort: str = DEFAULT_EFFORT
    provider_timeout_seconds: float = 300.0
    demo_latency_seconds: float = 0.6
    # The dashboard's ask-a-question box is interactive, so it runs at a lower effort than a close workflow.
    noi_chat_effort: str = "medium"
    noi_chat_timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("HUB_DB_PATH", cls.db_path),
            claude_model=os.environ.get("HUB_CLAUDE_MODEL", cls.claude_model),
            claude_effort=os.environ.get("HUB_CLAUDE_EFFORT", cls.claude_effort),
            provider_timeout_seconds=float(os.environ.get("HUB_PROVIDER_TIMEOUT_SECONDS", cls.provider_timeout_seconds)),
            demo_latency_seconds=float(os.environ.get("HUB_DEMO_LATENCY_SECONDS", cls.demo_latency_seconds)),
            noi_chat_effort=os.environ.get("HUB_NOI_CHAT_EFFORT", cls.noi_chat_effort),
            noi_chat_timeout_seconds=float(os.environ.get("HUB_NOI_CHAT_TIMEOUT_SECONDS",
                                                          cls.noi_chat_timeout_seconds)),
        )
