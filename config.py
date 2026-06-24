import os
from dotenv import load_dotenv

load_dotenv()

# --- IBKR Flex Query (opcional) ---
IBKR_FLEX_TOKEN = os.getenv("IBKR_FLEX_TOKEN", "")
IBKR_FLEX_QUERY_ID = os.getenv("IBKR_FLEX_QUERY_ID", "")

# --- Proveedor de IA ---
# "groq" | "anthropic" | "auto"
# "auto" intenta Groq primero (gratis), fallback a Anthropic
AI_PROVIDER = os.getenv("AI_PROVIDER", "auto")

# --- Groq (gratis) ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL_ANALYSIS = os.getenv("GROQ_MODEL_ANALYSIS", "llama-3.3-70b-versatile")
GROQ_MODEL_FAST = os.getenv("GROQ_MODEL_FAST", "llama-3.1-8b-instant")

# --- Anthropic (pago) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# --- General ---
APP_PORT = int(os.getenv("APP_PORT", "5000"))
DEBUG_MODE = os.getenv("DEBUG_MODE", "true").lower() == "true"
BASE_CURRENCY = os.getenv("BASE_CURRENCY", "USD")
