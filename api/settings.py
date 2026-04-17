"""Configuration from environment variables."""
from __future__ import annotations

from pydantic_settings import BaseSettings

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.5-flash",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1/",
        "model": "gpt-4o",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1/",
        "model": "llama3.1",
    },
}


class Settings(BaseSettings):
    # LLM provider
    provider: str = "gemini"
    api_key: str = ""
    base_url: str = ""       # overrides provider default if set
    model: str = ""          # overrides provider default if set

    # Delta store
    delta_store_url: str = "http://localhost:8100"
    delta_api_key: str = ""

    # Source runner
    source_runner_url: str = "http://localhost:4260"

    # Paths (container defaults)
    crystal_path: str = "/data/crystal.json"
    feed_directive_path: str = "/data/feed-directive.txt"
    tokens_path: str = "/data/tokens.json"
    mood_state_path: str = "/data/mood-state.json"

    # Mood layer (carrier wave) — pressure thresholds
    mood_pressure_threshold: float = 5.0
    mood_decay_half_life_seconds: int = 14400  # 4 hours
    mood_contrast_wake_seconds: int = 21600    # 6 hours

    # Server
    host: str = "0.0.0.0"
    port: int = 8200

    model_config = {"env_prefix": "FATHOM_"}

    @property
    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        return PROVIDER_DEFAULTS.get(self.provider, {}).get("base_url", "")

    @property
    def resolved_model(self) -> str:
        if self.model:
            return self.model
        return PROVIDER_DEFAULTS.get(self.provider, {}).get("model", "")


settings = Settings()
