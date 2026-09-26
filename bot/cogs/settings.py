"""Configuration slash-commands for the bot."""
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.database import get_db

BRAND_PURPLE = 0x7F5AF0

LANGUAGES = [
    app_commands.Choice(name="Русский (ru)", value="ru"),
    app_commands.Choice(name="English (en)", value="en"),
    app_commands.Choice(name="Українська (uk)", value="uk"),
    app_commands.Choice(name="Deutsch (de)", value="de"),
    app_commands.Choice(name="Français (fr)", value="fr"),
    app_commands.Choice(name="Español (es)", value="es"),
    app_commands.Choice(name="Polski (pl)", value="pl"),
]


class SettingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    exception = app_commands.Group(name="exception", description="Исключения правил в каналах")

    def _valid_rule(self, rule: str) -> bool:
        from bot.rules import RULES
        return rule.lower() in RULES or rule.lower() == "all"

    @exception.command(name="add", description="Исключить правило в канале (не детектится)")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        channel="Канал, где правило не должно работать",
        rule="Правило (3.1, 4.5, ...) или all — все правила",
    )
    async def exception_add(self, interaction: discord.Interaction,
                            channel: discord.TextChannel, rule: str):
        rule = rule.lower()
        if not self._valid_rule(rule):
            from bot.rules import RULES
            known = ", ".join(sorted(RULES.keys()))
            await interaction.response.send_message(
                f"Неизвестное правило `{rule}`. Доступные: `{known}` или `all`.",
                ephemeral=True,
            )
            return
        get_db().add_rule_exception(interaction.guild.id, channel.id, rule)
        await interaction.response.send_message(
            f"Правило `{rule}` исключено в {channel.mention}.",
            ephemeral=True,
        )

    @exception.command(name="remove", description="Вернуть правило в канале")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        channel="Канал, где правило нужно вернуть",
        rule="Правило (3.1, 4.5, ...) или all",
    )
    async def exception_remove(self, interaction: discord.Interaction,
                               channel: discord.TextChannel, rule: str):
        removed = get_db().remove_rule_exception(interaction.guild.id, channel.id, rule.lower())
        await interaction.response.send_message(
            f"Правило `{rule.lower()}` {'снято с исключений' if removed else 'не было исключено'} "
            f"в {channel.mention}.",
            ephemeral=True,
        )

    @exception.command(name="list", description="Показать исключения правил")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(channel="Канал (по умолчанию — все каналы сервера)")
    async def exception_list(self, interaction: discord.Interaction,
                             channel: discord.TextChannel = None):
        rows = get_db().list_rule_exceptions(interaction.guild.id) \
            if channel is None else \
            [(channel.id, r) for r in get_db().list_rule_exceptions(interaction.guild.id, channel.id)]
        if not rows:
            await interaction.response.send_message(
                "Исключений нет.", ephemeral=True,
            )
            return
        by_channel: dict = {}
        for ch_id, rule in rows:
            by_channel.setdefault(ch_id, []).append(rule)
        lines = []
        for ch_id, rules in sorted(by_channel.items()):
            ch = interaction.guild.get_channel(ch_id)
            name = ch.mention if ch else f"`{ch_id}`"
            lines.append(f"{name}: `{'`, `'.join(sorted(rules))}`")
        embed = discord.Embed(title="🚫 Исключения правил по каналам",
                              color=discord.Color(BRAND_PURPLE))
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="setup", description="Настроить канал модерации и параметры")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(language=LANGUAGES)
    @app_commands.describe(
        channel="Канал, куда бот будет слать нарушения",
        language="Язык перевода сообщений о нарушении",
        delete="Удалять ли исходное сообщение",
        llm="Включить/выключить ИИ-анализ",
        modrole="Роль модерации — пингуется при экстренных нарушениях",
    )
    async def setup(self, interaction: discord.Interaction,
                    channel: Optional[discord.TextChannel] = None,
                    language: Optional[str] = None,
                    delete: Optional[bool] = None,
                    llm: Optional[bool] = None,
                    modrole: Optional[discord.Role] = None):
        db = get_db()
        gid = interaction.guild.id
        updates = {}
        if channel is not None:
            updates["mod_channel_id"] = channel.id
        if language is not None:
            updates["target_language"] = language.value if hasattr(language, "value") else str(language)
        if delete is not None:
            updates["delete_message"] = 1 if delete else 0
        if llm is not None:
            updates["llm_enabled"] = 1 if llm else 0
        if modrole is not None:
            updates["mod_role_id"] = modrole.id
        db.update_server(gid, **updates)
        server = db.get_server(gid)
        embed = discord.Embed(title="⚙️ Настройки сервера", color=discord.Color(BRAND_PURPLE))
        emb_channel = interaction.guild.get_channel(server["mod_channel_id"]) if server["mod_channel_id"] else None
        emb_role = interaction.guild.get_role(server["mod_role_id"]) if server["mod_role_id"] else None
        embed.add_field(name="Канал модерации", value=emb_channel.mention if emb_channel else "не задан")
        embed.add_field(name="Роль модерации (экстренный пинг)",
                        value=emb_role.mention if emb_role else "не задана")
        embed.add_field(name="Язык перевода", value=server["target_language"])
        embed.add_field(name="Удалять сообщение", value="да" if server["delete_message"] else "нет")
        embed.add_field(name="ИИ-анализ", value="вкл" if server["llm_enabled"] else "выкл")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SettingsCog(bot))
