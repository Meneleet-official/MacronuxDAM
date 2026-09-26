"""User violation statistics slash-command."""
import discord
from discord import app_commands
from discord.ext import commands

from bot.database import get_db
from bot.rules import RULES, suggest_punishment, action_label, fmt_duration

BRAND_PURPLE = 0x7F5AF0


class StatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="stats", description="Статистика нарушений пользователя")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def stats(self, interaction: discord.Interaction, user: discord.User):
        db = get_db()
        gid = interaction.guild.id
        violations = db.list_violations(gid, user.id)
        punishments = db.list_punishments(gid, user.id)
        real_n = db.count_real_violations(gid, user.id)
        skipped_ids = db.skipped_violation_ids(gid, user.id)
        total = len(violations)

        # рекомендация по последнему нарушению
        last = violations[0] if violations else None
        if last:
            act, secs = suggest_punishment(last["rule_id"], real_n,
                                           last.get("severity") or "medium")
            suggested = f"{action_label(act)} {fmt_duration(secs)}" if secs else action_label(act)
        else:
            suggested = "—"

        embed = discord.Embed(
            title=f"📊 Статистика — {user.display_name} ({user.name})",
            color=discord.Color(BRAND_PURPLE),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Нарушений (реально)", value=str(real_n), inline=True)
        if total != real_n:
            embed.add_field(name="Всего зафиксировано", value=str(total), inline=True)
        if len(skipped_ids):
            embed.add_field(name="Пропущено модератором", value=str(len(skipped_ids)), inline=True)
        embed.add_field(name="Рекомендация", value=suggested, inline=True)

        if violations:
            lines = []
            for v in violations[:15]:
                r = RULES.get(v.get("rule_id"))
                title = r["title"] if r else "—"
                skip = " ⏭️" if v["id"] in skipped_ids else ""
                lines.append(
                    f"• **{v.get('rule_id')}** {title} "
                    f"({v.get('method')}, {v.get('created_at','')[:16]}){skip}"
                )
            embed.add_field(name="Нарушения", value="\n".join(lines) or "нет", inline=False)
        else:
            embed.add_field(name="Нарушения", value="Нет нарушений", inline=False)

        if punishments:
            plines = []
            for p in punishments[:10]:
                plines.append(f"• {p.get('action')} · {p.get('created_at','')[:16]} "
                              f"(модератор {p.get('moderator_id')})")
            embed.add_field(name="История решений", value="\n".join(plines), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stats_reset", description="Обнулить статистику нарушений пользователя")
    @app_commands.checks.has_permissions(administrator=True)
    async def stats_reset(self, interaction: discord.Interaction, user: discord.User):
        get_db().reset_user_stats(interaction.guild.id, user.id)
        await interaction.response.send_message(f"Статистика {user.mention} обнулена.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(StatsCog(bot))
