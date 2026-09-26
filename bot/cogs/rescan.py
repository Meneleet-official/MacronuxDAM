"""Ручной рескан: перепроверить сообщения канала по тем же детекторам (regex + LLM).

Команда /rescan для модераторов:
  /rescan [channel] [count]          — проверить последние `count` сообщений канала
  /rescan [message_ids=...]          — проверить конкретные сообщения по ID
Панель нарушений уходит в канал модерации, исходники НЕ удаляются (это
перепроверка, а не автонаказание) — только панель + логи.

Important: import из bot.client делаем лениво, внутри метода. client.py сам
подгружает этот ког через load_extension в setup_hook, поэтому импорт на уровне
модуля дал бы циклическую зависимость.
"""
import discord
from discord import app_commands
from discord.ext import commands

from bot.database import get_db

RESCAN_MAX = 200


class RescanCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="rescan", description="Перепроверить сообщения канала на нарушения")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        channel="Канал для перепроверки (по умолчанию — текущий)",
        count="Сколько последних сообщений проверить (1-200, по умолчанию 20)",
        message_ids="ID конкретных сообщений через запятую (перекроет count)",
    )
    async def rescan(self, interaction: discord.Interaction,
                     channel: discord.TextChannel = None,
                     count: int = 20,
                     message_ids: str = None):
        await interaction.response.defer(ephemeral=True)
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Рескан доступен только в текстовых каналах.", ephemeral=True)
            return

        # Ленивый импорт: см. комментарий в докстринге модуля.
        from bot.client import detect_violation, post_violation_panel

        count = max(1, min(count, RESCAN_MAX))
        db = get_db()
        server = db.get_server(interaction.guild.id)
        gid_marker = (interaction.guild.id, channel.id)

        # Собираем сообщения: либо по ID, либо последние N.
        messages = []
        if message_ids:
            for mid in (x.strip() for x in message_ids.split(",") if x.strip().isdigit()):
                try:
                    messages.append(await channel.fetch_message(int(mid)))
                except (discord.NotFound, discord.HTTPException):
                    continue
        else:
            async for msg in channel.history(limit=count):
                messages.append(msg)

        if not messages:
            await interaction.followup.send("Проверять нечего.", ephemeral=True)
            return

        # Тот же фильтр, что в автосканировании: боты/пустые пропускаются.
        checked = 0
        violations = 0
        for msg in reversed(messages):
            if msg.author.bot or not msg.content or not msg.content.strip():
                continue
            checked += 1
            try:
                detect = await detect_violation(msg, server)
            except Exception as e:
                print(f"[Rescan] детект {msg.id}: {type(e).__name__}: {e}")
                continue
            if detect is None:
                continue
            rule_id, method, severity, reason, translation = detect
            violations += 1
            try:
                await post_violation_panel(
                    msg, server, rule_id, method, severity, reason,
                    server.get("target_language") or "ru",
                    delete_msg=False,
                    mod_channel_id=server.get("mod_channel_id"),
                    translation=translation,
                )
            except Exception as e:
                print(f"[Rescan] панель {msg.id}: {type(e).__name__}: {e}")

        await interaction.followup.send(
            f"Готово: проверено **{checked}**, нарушений **{violations}**."
            f"{' (ИИ недоступен — только regex)' if not server.get('llm_enabled') else ''}",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(RescanCog(bot))
