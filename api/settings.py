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

    # Crystal auto-regeneration.
    # Auto-regen fires when (drift / threshold) >= red_ratio AND the last
    # regen was at least cooldown_seconds ago (guard against runaway).
    # Cooldown is deliberately long — a crystal is a durable self-description,
    # not an hourly event; multiple regens per day always indicates either
    # instability or a broken gate.
    crystal_auto_regen: bool = True
    # Anchor-based drift starts at 0 after every accepted regen (see
    # crystal_anchor.py), so centroid drift grows slowly and monotonically
    # in a lake that's being fed. 0.15 fires the red zone at ~0.135 —
    # tight enough to catch real topic drift, loose enough that routine
    # ingestion alone won't burn cooldown cycles.
    crystal_drift_threshold: float = 0.15
    crystal_drift_red_ratio: float = 0.9
    crystal_drift_poll_seconds: int = 60
    crystal_regen_cooldown_seconds: int = 259200  # 3 days

    # Feed-orient crystal (mood-shape regen, not identity-shape).
    # See docs/feed-spec.md. The min-signal guard is the cold-start
    # fail-open lesson from the 2026-04-19 auto-regen runaway.
    feed_crystal_cooldown_seconds: int = 21600  # 6 hours
    feed_drift_threshold: float = 0.35
    feed_confidence_floor: float = 0.55
    feed_min_signal_engagements: int = 10

    # Feed loop — per-directive-line budgets. Without a budget,
    # "until satisfied" is a runaway-cost grenade.
    feed_loop_budget_tool_calls: int = 8
    feed_loop_budget_seconds: int = 90
    feed_loop_visit_debounce_seconds: int = 600  # 10 min

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
