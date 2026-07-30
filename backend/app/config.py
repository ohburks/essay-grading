"""
Provider configuration — server-side only.

Ported from Performative_Assessment_V5 config.py; the hardcoded key
placeholders are replaced by environment variables (loaded from a repo-root
.env when present), so keys never live in source or reach the browser.
Providers without a configured key are hidden from in-app pickers
automatically (see core.llm.get_configured_providers).
"""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv():
    """Minimal .env loader (KEY=VALUE lines, # comments) — no dependency."""
    env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


PROVIDERS = {
    "OpenAI": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "api_key": _env("OPENAI_API_KEY"),
    },
    "Claude": {
        "base_url": "https://api.anthropic.com",
        "model": "claude-opus-4-8",
        "api_key": _env("ANTHROPIC_API_KEY"),
    },
    "Gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.5-flash",
        "api_key": _env("GEMINI_API_KEY"),
    },
    "Groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "api_key": _env("GROQ_API_KEY"),
    },
    "Mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
        "api_key": _env("MISTRAL_API_KEY"),
    },
    "GitHub Models": {
        # Free OpenAI-compatible endpoint for prototyping. Auth with a GitHub
        # personal access token that has the `models: read` permission.
        # Rate limited (free tier ~15 req/min, ~150 req/day) — light testing only.
        "base_url": "https://models.github.ai/inference",
        "model": "openai/gpt-4o-mini",
        "api_key": _env("GITHUB_MODELS_TOKEN"),
    },
    "TAMU AI": {
        # Texas A&M's OpenAI-compatible AI gateway (chat.tamu.ai). One key fans
        # out to OpenAI, Anthropic, and Gemini models behind campus SSO/licensing,
        # so institutional data-handling rules apply instead of per-vendor terms.
        # Get a key from chat.tamu.ai → Settings → API Keys.
        "base_url": os.environ.get("TAMU_AI_BASE_URL", "https://chat-api.tamu.ai/openai"),
        "model": "protected.gpt-4o",
        # TAMU_CHAT_API_KEY is the name TAMU's own client library uses; accepted
        # as an alias so an existing campus .env works unchanged.
        "api_key": _env("TAMU_AI_API_KEY") or _env("TAMU_CHAT_API_KEY"),
    },
    "Ollama": {
        "base_url": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "model": "llama3.2",
        "api_key": "ollama",  # Ollama needs no real key
    },
}

DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "Claude")

# ── Citation fact-checking (optional; pip install -e backend[citations]) ─────
# Crossref's "polite pool": an email in every request gets faster, more
# reliable responses per Crossref's etiquette policy. Only used as a
# fallback when a citation has no URL of its own.
CROSSREF_MAILTO = _env("CROSSREF_MAILTO")
CITATIONS_FETCH_TIMEOUT_SECS = float(_env("CITATIONS_FETCH_TIMEOUT_SECS") or 15)
CROSSREF_MIN_INTERVAL_SECS = float(_env("CROSSREF_MIN_INTERVAL_SECS") or 0.2)


def provider_config(name: str):
    """Resolve a provider by name; returns None when unknown."""
    return PROVIDERS.get(name)


def resolve_provider(preferred: str = "") -> tuple[str, dict] | tuple[None, None]:
    """Pick the user's preferred provider if it has a key, else the default,
    else the first configured one. Returns (name, cfg) or (None, None)."""
    from .core.llm import llm_is_available

    candidates = [preferred, DEFAULT_PROVIDER, *PROVIDERS.keys()]
    for name in candidates:
        cfg = PROVIDERS.get(name or "")
        if cfg and llm_is_available(cfg["api_key"]) and name != "Ollama":
            return name, cfg
    return None, None
