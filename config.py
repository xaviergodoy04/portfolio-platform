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

# --- Datos de mercado ---
# Proveedor de datos de mercado ("yfinance" es el único implementado hoy)
MARKET_PROVIDER = os.getenv("MARKET_PROVIDER", "yfinance")

# --- General ---
# APP_HOST=0.0.0.0 expone la app en la red local (necesario para usarla desde
# el celular). Explorar/Noticias/chat quedan visibles a cualquiera en esa red
# sin cuenta (a propósito); el portfolio, alertas y watchlist de cada usuario
# están detrás de login. Usá 127.0.0.1 si estás en una red compartida/pública.
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "5000"))
DEBUG_MODE = os.getenv("DEBUG_MODE", "true").lower() == "true"
BASE_CURRENCY = os.getenv("BASE_CURRENCY", "USD")

# --- Sesión / autenticación ---
# Firma la cookie de sesión. Si falta, se genera una al azar en memoria: la
# app funciona pero cada restart invalida las sesiones abiertas. Para que
# sobrevivan reinicios, fijá un valor propio en .env.
SECRET_KEY = os.getenv("SECRET_KEY", "")

# Bootstrap de la cuenta de Xavier (user_id=1) la primera vez que corre la
# migración multi-usuario. Si no están seteadas, se genera una contraseña
# random que se imprime una sola vez en consola.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
