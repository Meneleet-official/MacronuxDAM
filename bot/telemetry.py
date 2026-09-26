"""Телеметрия бота: сырые события в JSONL для последующего обучения.

Каждая строка — самостоятельный JSON-объект: {"t": ISO-метка, "event": имя, ...поля}.
Потом эти данные можно кормить нейронке/LLM, чтобы научить бота действовать так,
как реально решают модераторы (какое наказание, что считать не нарушением и т.п.).
"""
from collections import deque
from datetime import datetime, timezone
import json
import os

TELEMETRY_DIR = os.environ.get("PMX_TELEMETRY_DIR", "data")
TELEMETRY_PATH = os.path.join(TELEMETRY_DIR, "telemetry.jsonl")
MAX_TELEMETRY_SIZE = 20 * 1024 * 1024  # ротация при 20 МБ

# Кэш последних фраз, отмеченных модераторами как «Не нарушение» (dismiss):
# guild_id (или None для общих) -> deque[str]
_DISMISSED_CACHE: dict[int | None, deque[str]] = {}
_DISMISSED_LOADED = False


def _record_dismissed_in_cache(guild_id: int | None, text: str) -> None:
    clean = " ".join((text or "").split()).strip()
    if len(clean) < 3 or len(clean) > 200:
        return
    for key in (guild_id, None):
        buf = _DISMISSED_CACHE.setdefault(key, deque(maxlen=25))
        if clean not in buf:
            buf.append(clean)


def _ensure_loaded_from_disk() -> None:
    global _DISMISSED_LOADED
    if _DISMISSED_LOADED:
        return
    _DISMISSED_LOADED = True
    if not os.path.exists(TELEMETRY_PATH):
        return
    try:
        with open(TELEMETRY_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("event") == "moderator_action" and obj.get("action") == "dismiss":
                    txt = obj.get("text")
                    gid = obj.get("guild_id")
                    if txt:
                        _record_dismissed_in_cache(gid, txt)
    except Exception:
        pass


def load_dismissed_examples(guild_id: int | None = None, limit: int = 12) -> list[str]:
    """Возвращает последние фразы, помеченные модераторами как «Не нарушение» (dismiss),
    из телеметрии и БД для подстановки в Few-Shot промпт LLM и белый список."""
    _ensure_loaded_from_disk()
    seen: set[str] = set()
    result: list[str] = []

    # 1. Из БД (таблица punishments + violations)
    try:
        from bot.database import get_db
        for txt in get_db().list_dismissed_examples(guild_id=guild_id, limit=limit):
            norm = txt.lower().strip()
            if norm and norm not in seen:
                seen.add(norm)
                result.append(txt)
    except Exception:
        pass

    # 2. Из кэша телеметрии (сначала для конкретного сервера, затем общие)
    for key in (guild_id, None):
        buf = _DISMISSED_CACHE.get(key)
        if not buf:
            continue
        for txt in reversed(buf):
            norm = txt.lower().strip()
            if norm not in seen:
                seen.add(norm)
                result.append(txt)
            if len(result) >= limit:
                return result[:limit]

    return result[:limit]


def log_event(event: str, **data) -> None:
    """Записать одну строку телеметрии. Никогда не роняет бота."""
    try:
        if event == "moderator_action" and data.get("action") == "dismiss" and data.get("text"):
            _record_dismissed_in_cache(data.get("guild_id"), data["text"])

        os.makedirs(TELEMETRY_DIR, exist_ok=True)
        try:
            if os.path.getsize(TELEMETRY_PATH) > MAX_TELEMETRY_SIZE:
                os.replace(TELEMETRY_PATH, TELEMETRY_PATH + ".old")
        except FileNotFoundError:
            pass
        row = {
            "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **data,
        }
        with open(TELEMETRY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[TELEMETRY] ошибка: {e}")