"""Quick check that the Groq key works. Run: uv run python hello_groq.py"""

from dotenv import load_dotenv
from groq import Groq

load_dotenv()  # reads GROQ_API_KEY from .env

client = Groq()
reply = client.chat.completions.create(
    model="openai/gpt-oss-120b",
    messages=[{"role": "user", "content": "Say hello to Krify in one short sentence."}],
)
print(reply.choices[0].message.content)
