import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DB_PATH = os.getenv("DB_PATH", "data/modbot.db")
LOG_PATH = os.getenv("LOG_PATH", "data/modbot.log")

# Срок давности страйков в днях (для расчёта эскалации наказания; 0 = бессрочно)
STRIKE_DECAY_DAYS = int(os.getenv("STRIKE_DECAY_DAYS", "60"))

# ID бота Akemi (пин/override; если пусто — находится автоматически по имени "Akemi")
AKEMI_BOT_ID = int(os.getenv("AKEMI_BOT_ID") or 0) or None

# LLM provider selection: deepseek | gemini | groq | openrouter
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek").lower()

# Google Gemini (free tier: gemini-2.0-flash-lite / gemini-2.5-flash)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")

# Groq (free tier)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# OpenRouter (free models like meta-llama/llama-3.3-70b-instruct:free)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Default values for automod / rules tuning
DEFAULT_TARGET_LANGUAGE = "ru"
DEFAULT_STRIKE_THRESHOLDS = {
    "1": "timeout",
    "2": "timeout",
    "3": "timeout",
    "4": "ban",
    "5": "ban",
    "6": "ban",
}

# Интеграция с веб-панелью плагина Pulse (включая серверы в Pterodactyl)
PULSE_ENABLED = os.getenv("PULSE_ENABLED", "1").strip().lower() not in ("0", "false", "no")
PULSE_API_HOST = os.getenv("PULSE_API_HOST", "0.0.0.0").strip()
PULSE_API_PORT = int(os.getenv("PULSE_API_PORT") or "8765")
PULSE_API_TOKEN = os.getenv("PULSE_API_TOKEN", "").strip()
# Дополнительные пути к папкам контейнеров Pterodactyl (через запятую или ;)
PULSE_BRIDGE_DIRS = os.getenv("PULSE_BRIDGE_DIRS", "").strip()

# Подключение к удалённому серверу Minecraft через Pterodactyl Client API (если сервер на другом хостинге)
PTERODACTYL_URL = os.getenv("PTERODACTYL_URL", "").strip().rstrip("/")
PTERODACTYL_API_KEY = os.getenv("PTERODACTYL_API_KEY", "").strip()
PTERODACTYL_SERVER_ID = os.getenv("PTERODACTYL_SERVER_ID", "").strip()

