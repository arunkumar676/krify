"""Settings, read from backend/.env."""

import os

from dotenv import load_dotenv

load_dotenv()

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))  # safety limit per task
PORT = int(os.getenv("PORT", "8765"))
