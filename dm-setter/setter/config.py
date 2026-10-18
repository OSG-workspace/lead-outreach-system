"""Settings, read once from the environment and dm-setter/.env. Nothing else in the package reads os.environ."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    f = ROOT / ".env"
    if f.is_file():
        for k, v in re.findall(r"^([A-Z_0-9]+)=(.*)$", f.read_text(), re.M):
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))


_load_dotenv()
# The Jev client reads JEV_CACHE at import: keep its answers (lead text) inside the gitignored data folder.
os.environ.setdefault("JEV_CACHE", str(ROOT / "data" / "jev-answers.db"))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def flag(name: str, default: bool = False) -> bool:
    v = env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


def num(name: str, default: float) -> float:
    try:
        return float(env(name) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    public_base_url: str = field(default_factory=lambda: env("PUBLIC_BASE_URL").rstrip("/"))
    db_path: Path = field(default_factory=lambda: Path(env("SETTER_DB") or ROOT / "data" / "setter.db"))
    coaches_dir: Path = field(default_factory=lambda: Path(env("COACHES_DIR") or ROOT / "coaches"))

    # Safety switches. DRY_RUN defaults ON: nothing reaches Instagram until you set DRY_RUN=0.
    dry_run: bool = field(default_factory=lambda: flag("DRY_RUN", True))
    enabled: bool = field(default_factory=lambda: flag("SETTER_ENABLED", True))   # global kill switch

    # One key for every AI call: Claude (through OpenRouter's Anthropic-format endpoint), Jev and voice notes.
    openrouter_key: str = field(default_factory=lambda: env("OPEN_ROUTER_API_KEY") or env("OPENROUTER_API_KEY"))
    openrouter_base: str = field(default_factory=lambda: env("OPENROUTER_BASE_URL", "https://openrouter.ai/api"))

    # Claude. The reply model writes every DM; the cheap model summarises, describes photos, plays eval leads.
    reply_model: str = field(default_factory=lambda: env("REPLY_MODEL", "anthropic/claude-sonnet-5.5"))
    reply_effort: str = field(default_factory=lambda: env("REPLY_EFFORT", "low"))
    reply_thinking: str = field(default_factory=lambda: env("REPLY_THINKING", "adaptive"))  # adaptive | off
    cheap_model: str = field(default_factory=lambda: env("CHEAP_MODEL", "anthropic/claude-haiku-5.5"))
    llm_fallbacks: bool = field(default_factory=lambda: flag("LLM_FALLBACKS", True))
    llm_fallback_model: str = field(default_factory=lambda: env("LLM_FALLBACK_MODEL", "anthropic/claude-opus-4.8"))
    llm_provider_order: list = field(default_factory=lambda: [p.strip() for p in env(
        "LLM_PROVIDER_ORDER", "anthropic").split(",") if p.strip()])
    system_cache_ttl: str = field(default_factory=lambda: env("SYSTEM_CACHE_TTL", "1h"))     # 1h | 5m

    # Instagram API with Instagram Login (Meta app → Instagram product).
    ig_app_id: str = field(default_factory=lambda: env("IG_APP_ID"))
    ig_app_secret: str = field(default_factory=lambda: env("IG_APP_SECRET"))
    ig_verify_token: str = field(default_factory=lambda: env("IG_VERIFY_TOKEN"))
    ig_graph_version: str = field(default_factory=lambda: env("IG_GRAPH_VERSION", "v23.0"))
    ig_graph_host: str = field(default_factory=lambda: env("IG_GRAPH_HOST", "graph.instagram.com"))
    human_agent_enabled: bool = field(default_factory=lambda: flag("HUMAN_AGENT_ENABLED"))   # after App Review
    skip_signature: bool = field(default_factory=lambda: flag("SKIP_SIGNATURE"))             # local tests only

    admin_user: str = field(default_factory=lambda: env("ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: env("ADMIN_PASSWORD"))
    booking_secret: str = field(default_factory=lambda: env("BOOKING_WEBHOOK_SECRET"))
    notify_webhook_url: str = field(default_factory=lambda: env("NOTIFY_WEBHOOK_URL"))

    # Voice notes: OpenRouter's transcription endpoint on the same key (Whisper large-v3 turbo). Any other
    # OpenAI-compatible endpoint works by setting all three STT_* values.
    stt_api_url: str = field(default_factory=lambda: env("STT_API_URL",
                                                          "https://openrouter.ai/api/v1/audio/transcriptions"))
    stt_api_key: str = field(default_factory=lambda: env("STT_API_KEY") or env("OPEN_ROUTER_API_KEY")
                             or env("OPENROUTER_API_KEY"))
    stt_model: str = field(default_factory=lambda: env("STT_MODEL", "openai/whisper-large-v3-turbo"))

    # Conversation mechanics.
    debounce_s: float = field(default_factory=lambda: num("DEBOUNCE_SECONDS", 20))
    debounce_max_s: float = field(default_factory=lambda: num("DEBOUNCE_MAX_SECONDS", 75))
    history_keep: int = field(default_factory=lambda: int(num("HISTORY_KEEP", 16)))
    history_summarize_at: int = field(default_factory=lambda: int(num("HISTORY_SUMMARIZE_AT", 36)))
    workers: int = field(default_factory=lambda: int(num("WORKERS", 8)))
    humanize_timing: bool = field(default_factory=lambda: flag("HUMANIZE_TIMING", True))
    comment_poll_minutes: float = field(default_factory=lambda: num("COMMENT_POLL_MINUTES", 10))


settings = Settings()
