"""Configuration from environment variables."""
from __future__ import annotations

from pydantic import Field
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
    # LLM provider — these use the LLM_ prefix (not FATHOM_) to keep them
    # distinct from Fathom's own bearer tokens (ftm_…), which the client
    # tools (mcp, cli, hooks) read from FATHOM_API_KEY.
    provider: str = Field("gemini", validation_alias="LLM_PROVIDER")
    api_key: str = Field("", validation_alias="LLM_API_KEY")
    base_url: str = Field("", validation_alias="LLM_BASE_URL")   # overrides provider default
    model: str = Field("", validation_alias="LLM_MODEL")         # overrides provider default

    # Delta store
    delta_store_url: str = "http://localhost:8100"
    delta_api_key: str = ""

    # Source runner
    source_runner_url: str = "http://localhost:4260"

    # Paths (container defaults). Crystal is lake-backed — no file path.
    feed_directive_path: str = "/data/feed-directive.txt"
    tokens_path: str = "/data/tokens.json"
    mood_state_path: str = "/data/mood-state.json"
    pair_codes_path: str = "/data/pair-codes.json"

    # Mood layer (carrier wave) — pressure thresholds
    # Threshold tuned against a real lake. With ~50 deltas/hour, pressure
    # builds to ~30 within a few hours; 25 fires roughly every 2-3 hours
    # of sustained activity unless a contrast-wake intervenes.
    mood_pressure_threshold: float = 25.0
    mood_decay_half_life_seconds: int = 14400  # 4 hours
    mood_contrast_wake_seconds: int = 21600    # 6 hours

    # Crystal auto-regeneration — same defaults as fathom2 dashboard.
    # Auto-regen fires when (drift / threshold) >= red_ratio AND the last
    # regen was at least cooldown_seconds ago (guard against runaway).
    crystal_auto_regen: bool = True
    crystal_drift_threshold: float = 0.55
    crystal_drift_red_ratio: float = 0.9
    crystal_drift_poll_seconds: int = 60
    crystal_regen_cooldown_seconds: int = 1800  # 30 min

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
