"""Система жалоб от участников: слэш-команда /report и ПКМ по сообщению «🚨 Пожаловаться».

Позволяет участникам отправить жалобу на сообщение, профиль/аватарку (п. 2.2, 2.3, 3.7)
или поведение в голосовом канале (п. 3.6 — саундпад, крики).
В панель модерации автоматически добавляется кнопка «⚠️ Ложный вызов (п. 2.5)» для
наказания автора жалобы при спаме или клевете.
"""
import time
from typing import Optional

import discord
from discord import app_commands, ui
from discord.ext import commands

from bot.database import get_db
from bot.rules import RULES, build_suggest
from bot.views.mod_buttons import ModActionView

BRAND_ORANGE = 0xED8936
REPORT_COOLDOWN = 60  # секунд между жалобами от одного участника
_LAST_REPORT_TS: dict[tuple[int, int], float] = {}


class ReportMessageModal(ui.Modal, title="🚨 Пожаловаться модерации"):
    reason = ui.TextInput(
        label="Причина жалобы (кратко)",
        placeholder="Например: оскорбление, скам-ссылка, деанон, провокация...",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=3,
        max_length=400,
    )

    def __init__(self, cog: "ReportsCog", target_message: discord.Message):
        super().__init__()
        self.cog = cog
        self.target_message = target_message

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._submit_report(
            interaction,
            target_user=self.target_message.author,
            reason_text=self.reason.value.strip(),
            target_message=self.target_message,
        )


class ReportsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(
            name="🚨 Пожаловаться",
            callback=self.report_message_ctx,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    async def report_message_ctx(self, interaction: discord.Interaction, message: discord.Message):
        """Контекстное меню (ПКМ по сообщению -> Приложения -> 🚨 Пожаловаться)."""
        if not interaction.guild:
            await interaction.response.send_message("Жалобы работают только на сервере.", ephemeral=True)
            return
        if message.author.bot:
            await interaction.response.send_message("Нельзя пожаловаться на сообщение бота.", ephemeral=True)
            return
        if message.author.id == interaction.user.id:
            await interaction.response.send_message("Нельзя отправить жалобу на самого себя.", ephemeral=True)
            return
        await interaction.response.send_modal(ReportMessageModal(self, message))

    @app_commands.command(
        name="report",
        description="Отправить жалобу модераторам на участника (войс, профиль, чат)",
    )
    @app_commands.describe(
        user="Участник, нарушающий правила",
        reason="Описание нарушения (например: саундпад в войсе п. 3.6, 18+ аватарка п. 2.3)",
        rule="Предполагаемый пункт правил (необязательно, например 3.1, 3.6, 2.3)",
    )
    async def report_slash(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str,
        rule: Optional[str] = None,
    ):
        if not interaction.guild:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if user.bot:
            await interaction.response.send_message("Нельзя пожаловаться на бота.", ephemeral=True)
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message("Нельзя пожаловаться на самого себя.", ephemeral=True)
            return
        await self._submit_report(
            interaction,
            target_user=user,
            reason_text=reason.strip(),
            target_message=None,
            rule_hint=(rule or "").strip(),
        )

    async def _submit_report(
        self,
        interaction: discord.Interaction,
        target_user: discord.abc.User,
        reason_text: str,
        target_message: Optional[discord.Message] = None,
        rule_hint: str = "",
    ):
        from bot.client import _resolve_mod_channel, snip

        now = time.time()
        cd_key = (interaction.guild.id, interaction.user.id)
        last_ts = _LAST_REPORT_TS.get(cd_key, 0.0)
        if now - last_ts < REPORT_COOLDOWN:
            wait_s = int(REPORT_COOLDOWN - (now - last_ts))
            await interaction.response.send_message(
                f"⏳ Подождите **{wait_s} сек.** перед отправкой следующей жалобы.\n"
                f"*Напоминание: ложные вызовы модерации наказываются по п. 2.5.*",
                ephemeral=True,
            )
            return
        _LAST_REPORT_TS[cd_key] = now

        db = get_db()
        server = db.get_server(interaction.guild.id)
        mod_channel = _resolve_mod_channel(interaction.guild, server.get("mod_channel_id"))
        if mod_channel is None:
            await interaction.response.send_message(
                "Канал модерации ещё не настроен администратором (`/setup`).",
                ephemeral=True,
            )
            return

        rule_id = rule_hint if rule_hint in RULES else "3.1"
        rule_title = RULES.get(rule_id, {}).get("title", "Жалоба участника")

        try:
            db.record_discord_member(interaction.guild.id, interaction.user)
            target_meta = db.record_discord_member(interaction.guild.id, target_user)
        except Exception:
            target_meta = {}

        msg_content = (target_message.content if target_message else "") or ""
        snapshot = (
            f"[Жалоба от {interaction.user} (reporter_id={interaction.user.id})]: {reason_text}"
            + (f" | Сообщение: {msg_content}" if msg_content else "")
        )
        orig_ch = target_message.channel if target_message else interaction.channel
        orig_ch_id = orig_ch.id if orig_ch else 0
        orig_ch_name = getattr(orig_ch, "name", None)
        orig_msg_id = target_message.id if target_message else 0
        jump_url = getattr(target_message, "jump_url", None) if target_message else None

        violation_id = db.add_violation(
            interaction.guild.id,
            target_user.id,
            guild_name=interaction.guild.name,
            user_name=target_meta.get("username"),
            user_display_name=target_meta.get("display_name"),
            user_avatar_url=target_meta.get("avatar_url"),
            rule_id=rule_id,
            severity="medium",
            method="user_report",
            reason=reason_text[:300],
            message_snapshot=snapshot[:500],
            original_text=snapshot[:500],
            translated_text=None,
            detected_language="ru",
            channel_id=orig_ch_id,
            channel_name=orig_ch_name,
            message_id=orig_msg_id,
            jump_url=jump_url,
        )

        violations_n = db.count_real_violations(interaction.guild.id, target_user.id)
        suggest = build_suggest(rule_id, violations_n, "medium")

        embed = discord.Embed(
            title=f"📨 Жалоба от участника (№{violation_id})",
            color=discord.Color(BRAND_ORANGE),
        )
        if getattr(target_user, "display_avatar", None):
            embed.set_thumbnail(url=target_user.display_avatar.url)
            embed.set_author(
                name=f"{target_user.display_name} ({target_user})",
                icon_url=target_user.display_avatar.url,
            )
        embed.add_field(
            name="На кого жалоба",
            value=f"{target_user.mention} (`{target_user.id}`)\nКанал: <#{orig_ch_id}>",
            inline=True,
        )
        embed.add_field(
            name="Автор жалобы",
            value=f"{interaction.user.mention} (`{interaction.user.id}`)",
            inline=True,
        )
        embed.add_field(
            name="Причина жалобы",
            value=snip(reason_text, limit=800),
            inline=False,
        )
        if target_message:
            jump_url = target_message.jump_url
            msg_preview = snip(target_message.content or "(вложение / без текста)", limit=700)
            embed.add_field(
                name="Сообщение",
                value=f"{msg_preview}\n[🔗 Перейти к сообщению]({jump_url})",
                inline=False,
            )
        embed.add_field(
            name=f"Предварительное правило: {rule_id} ({rule_title})",
            value=f"Страйков у участника: **{violations_n}**\n"
                  f"Если вызов ложный — нажмите **«⚠️ Ложный вызов (п. 2.5)»**.",
            inline=False,
        )
        embed.set_footer(text=f"Жалоба · Нарушение №{violation_id}")

        view = ModActionView(
            interaction.guild.id,
            target_user.id,
            violation_id,
            target_msg_id=orig_msg_id or None,
            channel_id=orig_ch_id,
            lang=server.get("target_language") or "ru",
            suggest=suggest,
            reporter_id=interaction.user.id,
        )

        try:
            await mod_channel.send(embed=embed, view=view)
            await interaction.response.send_message(
                f"✅ Ваша жалоба **№{violation_id}** отправлена модераторам.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Не удалось отправить жалобу в канал модерации: {e}",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ReportsCog(bot))
