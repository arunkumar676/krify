"""Settings, read from backend/.env."""

import os

from dotenv import load_dotenv

load_dotenv()

# Free Groq models Krify can use, best first. Each has its own daily limit, so when one runs out
# Krify switches to the next. "extra" = model-specific settings (less thinking = fewer tokens).
MODELS = {
    "openai/gpt-oss-120b": {"label": "GPT-OSS 120B (smartest)", "extra": {"reasoning_effort": "low"}},
    "qwen/qwen3.8-27b": {"label": "Qwen 3.8 27B", "extra": {"reasoning_effort": "none"}},
    "openai/gpt-oss-20b": {"label": "GPT-OSS 20B (fastest)", "extra": {"reasoning_effort": "low"}},
}

MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))  # safety limit per task
PORT = int(os.getenv("PORT", "8765"))
