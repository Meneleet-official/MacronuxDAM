"""Coordination with the Akemi moderation bot.

Если нарушение уже обработал Akemi (тайм-аут/кик/бан/удаление сообщения),
PMX не дублирует его: не создаёт запись о нарушении, не шлёт панель и не наказывает.
"""
import time

import discord

AKEMI_WINDOW = 30  # секунд — действие Akemi в этом окне считается «обработанным»

_AKEMI_ID = None
_CACHE = {}  # user_id -> timestamp действия Akemi

_PUNISH_ACTIONS = (
    discord.AuditLogAction.kick,
    discord.AuditLogAction.ban,
    discord.AuditLogAction.member_update,     # тайм-аут
)
_DELETE_ACTIONS = (
    discord.AuditLogAction.message_delete,
    discord.AuditLogAction.message_bulk_delete,
)


def resolved_id() -> int | None:
    return _AKEMI_ID


def _is_punish_entry(entry: discord.AuditLogEntry) -> bool:
    if entry.action in (discord.AuditLogAction.kick, discord.AuditLogAction.ban):
        return True
    if entry.action == discord.AuditLogAction.member_update:
        after = getattr(entry, "after", None)
        if after is not None and hasattr(after, "timed_out_until"):
            return after.timed_out_until is not None
        return True
    return False


def _extract_channel_id(entry: discord.AuditLogEntry) -> int | None:
    extra = getattr(entry, "extra", None)
    ch = None
    if isinstance(extra, dict):
        ch = extra.get("channel")
    elif extra is not None:
        ch = getattr(extra, "channel", None)
    if ch is None:
        ch = getattr(entry.target, "channel", None)
    return getattr(ch, "id", None)


def punished_user(entry: discord.AuditLogEntry) -> int | None:
    """Если Akemi наказал участника (тайм-аут/кик/бан) — возвращает его id, иначе None."""
    if _AKEMI_ID is None or entry.user_id != _AKEMI_ID:
        return None
    if not _is_punish_entry(entry):
        return None
    target = entry.target
    if target is not None and not getattr(target, "bot", False):
        return getattr(target, "id", None)
    return None


def _prune(now: float) -> None:
    for uid, ts in list(_CACHE.items()):
        if now - ts > AKEMI_WINDOW:
            del _CACHE[uid]


def cache_from_entry(entry: discord.AuditLogEntry) -> None:
    """Питает кэш из события on_audit_log_entry_create."""
    if _AKEMI_ID is None or entry.user_id != _AKEMI_ID:
        return
    now = time.time()
    if _is_punish_entry(entry):
        target = entry.target
        if target is not None and not getattr(target, "bot", False):
            tid = getattr(target, "id", None)
            if tid is not None:
                _CACHE[tid] = now
    elif entry.action == discord.AuditLogAction.message_delete:
        target = entry.target
        if target is not None and not getattr(target, "bot", False):
            tid = getattr(target, "id", None)
            if tid is not None:
                _CACHE[tid] = now
    _prune(now)


async def handled_by_akemi(guild, author, channel_id) -> bool:
    """True, если Akemi уже наказал этого участника / удалил его сообщение в этом окне."""
    if _AKEMI_ID is None:
        return False
    now = time.time()
    _prune(now)
    if _CACHE.get(author.id, 0) >= now - AKEMI_WINDOW:
        return True

    # Фолбэк по audit-log (закрывает разрыв, пока кэш не заполнен).
    try:
        async for entry in guild.audit_logs(limit=40):
            if now - entry.created_at.timestamp() > AKEMI_WINDOW:
                break
            if entry.user_id != _AKEMI_ID:
                continue
            if _is_punish_entry(entry):
                target = entry.target
                if target is not None and getattr(target, "id", None) == author.id:
                    return True
            elif entry.action == discord.AuditLogAction.message_delete:
                target = entry.target
                if getattr(target, "id", None) != author.id:
                    continue
                ch_id = _extract_channel_id(entry)
                if ch_id is None or ch_id == channel_id:
                    return True
            elif entry.action == discord.AuditLogAction.message_bulk_delete:
                ch_id = _extract_channel_id(entry) or getattr(entry.target, "id", None)
                if ch_id is not None and ch_id == channel_id:
                    return True
    except (discord.Forbidden, discord.HTTPException):
        pass
    return False


async def detect(bot) -> None:
    """Находит Akemi по ID из конфига (приоритет) или по имени участника сервера."""
    global _AKEMI_ID
    if _AKEMI_ID is not None:
        return
    from config import AKEMI_BOT_ID
    if AKEMI_BOT_ID:
        _AKEMI_ID = AKEMI_BOT_ID
        print(f"[Akemi] id из конфига: {AKEMI_BOT_ID}")
        return
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot and member.name.lower().startswith("akemi"):
                _AKEMI_ID = member.id
                print(f"[Akemi] найден: {member} (id={member.id}) — нарушение им не дублируем")
                return
    print("[Akemi] НЕ найден — PMX обрабатывает все нарушения сам.")