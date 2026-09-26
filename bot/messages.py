"""UI strings for the bot, multilingual."""

MESSAGES = {
    "ru": {
        "violation_title": "🚨 Нарушение правил",
        "original": "📝 Сообщение",
        "translated": "🌐 Перевод ({target})",
        "member_field": "👤 Участник",
        "by_rule": "Правило **{rule}** — {title}",
        "recommendation": "Рекомендация:",
        "violations_count": "нарушений: {n}",
        "no_violations": "Нарушений нет",
        "footer_stamp": "Панель №{violation_id}",
        "delete": "🗑 Удалить",
        "timeout": "⏳ Мут (тайм-аут)",
        "kick": "👢 Кик",
        "ban": "🔨 Бан",
        "skip": "➡️ Пропустить",
        "timeout_1h": "1 час",
        "timeout_1d": "1 день",
        "timeout_7d": "7 дней",
        "punishment_applied": "Наказание применено к {user}: **{action}**",
        "punishment_cancel": "Действие отменено",
        "chose_action": "Выберите длительность тайм-аута",
        "delete_confirm": "Сообщение удалено.",
        "skip_confirm": "Нарушение пропущено.",
    },
    "en": {
        "violation_title": "🚨 Rule violation",
        "original": "📝 Message",
        "translated": "🌐 Translation ({target})",
        "member_field": "👤 Member",
        "by_rule": "Rule **{rule}** — {title}",
        "recommendation": "Recommendation:",
        "violations_count": "violations: {n}",
        "no_violations": "No violations",
        "footer_stamp": "Panel #{violation_id}",
        "delete": "🗑 Delete",
        "timeout": "⏳ Timeout (mute)",
        "kick": "👢 Kick",
        "ban": "🔨 Ban",
        "skip": "➡️ Skip",
        "timeout_1h": "1 hour",
        "timeout_1d": "1 day",
        "timeout_7d": "7 days",
        "punishment_applied": "Punishment applied to {user}: **{action}**",
        "punishment_cancel": "Action canceled",
        "chose_action": "Choose timeout duration",
        "delete_confirm": "Message deleted.",
        "skip_confirm": "Violation skipped.",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    """Get a message in the given language (falls back to English/Russian)."""
    strings = MESSAGES.get(lang) or MESSAGES.get("ru") or MESSAGES["en"]
    template = strings.get(key) or MESSAGES["en"].get(key, key)
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template
