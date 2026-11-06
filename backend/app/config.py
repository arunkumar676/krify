"""Settings, read from backend/.env."""

import os

from dotenv import load_dotenv

load_dotenv()

# Free AI providers. All speak the same (OpenAI-style) API, so one client works for all.
# A provider without a key in .env is skipped.
PROVIDERS = {
    "groq": {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY"},
    "gemini": {
        "name": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key": "GEMINI_API_KEY",
    },
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY"},
}

# Models Krify can use, in auto-switch order. Each has its own free limit.
# "strong" models handle long tasks well; "backup" models are used only when every strong model
# is out for a while (they tend to wander on long tasks).
# "extra" = model-specific settings (less thinking = fewer tokens).
# "boost" = extra settings merged over "extra" for a step the agent loop flags as needing more
# thought (interpreting the task, resolving ambiguous elements) - omitted for models whose "extra"
# has nothing worth raising.
# "vision" = can look at screenshots (tested with a sample image).
_ALL_MODELS = {
    "groq:openai/gpt-oss-120b": {"label": "GPT-OSS 120B (smartest)", "tier": "strong", "vision": False, "extra": {"reasoning_effort": "low"}, "boost": {"reasoning_effort": "medium"}},
    "gemini:gemini-3.8-flash": {"label": "Gemini 3.8 Flash", "tier": "strong", "vision": True, "extra": {"reasoning_effort": "low"}, "boost": {"reasoning_effort": "medium"}},
    "gemini:gemini-3.5-flash": {"label": "Gemini 3.5 Flash", "tier": "strong", "vision": True, "extra": {"reasoning_effort": "none"}, "boost": {"reasoning_effort": "low"}},
    "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free": {"label": "Nemotron Ultra", "tier": "strong", "vision": False, "extra": {}},
    "groq:qwen/qwen3.8-27b": {"label": "Qwen 3.8 27B (backup)", "tier": "backup", "vision": True, "extra": {"reasoning_effort": "none"}},
    "groq:openai/gpt-oss-20b": {"label": "GPT-OSS 20B (backup)", "tier": "backup", "vision": False, "extra": {"reasoning_effort": "low"}},
    "gemini:gemini-flash-lite-latest": {"label": "Gemini Flash Lite (backup)", "tier": "backup", "vision": True, "extra": {}},
    "openrouter:nvidia/nemotron-3.5-super-vl:free": {"label": "Nemotron Super (backup)", "tier": "backup", "vision": True, "extra": {}},
    "gemini:gemini-4-argon": {"label": "Gemini 4 Argon (backup, paid)", "tier": "backup", "vision": True, "extra": {}},
}


def provider_of(model_id):
    return model_id.split(":", 1)[0]


def model_name(model_id):
    """The provider's own name for the model, e.g. 'openai/gpt-oss-120b'."""
    return model_id.split(":", 1)[1]


MODELS = {m: info for m, info in _ALL_MODELS.items() if os.getenv(PROVIDERS[provider_of(m)]["key"])}

MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))  # safety limit per task
PORT = int(os.getenv("PORT", "8765"))
