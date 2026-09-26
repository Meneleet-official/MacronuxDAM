"""User violation statistics, digest report, and CSV export slash-commands."""
import csv
import io
import time

import discord
from discord import app_commands
from discord.ext import commands

from bot.database import get_db
from bot.rules import RULES, suggest_punishment, action_label, fmt_duration

BRAND_PURPLE = 0x7F5AF0


def build_digest_embed(guild: discord.Guild, days: int = 7) -> discord.Embed:
    """Формирует сводный отчёт (дайджест) для Главной Администрации за `days` дней."""
    data = get_db().get_digest_stats(guild.id, days=days)
    embed = discord.Embed(
        title=f"📈 Сводный отчёт модерации за {data['days']} дн. — {guild.name}",
        color=discord.Color(BRAND_PURPLE),
    )
    embed.add_field(
        name="Всего срабатываний",
        value=f"**{data['total_violations']}** (реальных: **{data['real_violations']}**)",
        inline=True,
    )
    embed.add_field(
        name="Снято («Не нарушение»)",
        value=f"**{data['dismissed']}**",
        inline=True,
    )
    embed.add_field(
        name="Пропущено без наказания",
        value=f"**{data['skipped']}**",
        inline=True,
    )

    actions = data.get("actions") or {}
    if actions:
        act_lines = [f"• **{action_label(k)}**: {v}" for k, v in actions.items()]
        embed.add_field(name="Вынесенные наказания", value="\n".join(act_lines), inline=False)
    else:
        embed.add_field(name="Вынесенные наказания", value="За этот период наказаний не выносилось", inline=False)

    top_rules = data.get("top_rules") or []
    if top_rules:
        rule_lines = []
        for rid, cnt in top_rules:
            r_title = RULES.get(rid, {}).get("title", "—")
            rule_lines.append(f"• **{rid}** ({r_title}) — **{cnt}**")
        embed.add_field(name="Топ нарушаемых правил", value="\n".join(rule_lines), inline=False)

    top_mods = data.get("top_mods") or []
    if top_mods:
        mod_lines = [
            f"{idx}. <@{mid}> — **{cnt}** реш."
            for idx, (mid, cnt) in enumerate(top_mods, start=1)
        ]
        embed.add_field(name="🏆 Топ активных модераторов", value="\n".join(mod_lines), inline=False)

    embed.set_footer(text=f"Macronux™ Авто-дайджест · Период: {data['days']} дн.")
    return embed


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
            suggested = (
                f"{action_label(act)} {fmt_duration(secs)}"
                if (secs or act == "ban") else action_label(act)
            )
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
            embed.add_field(name="Пропущено/снято", value=str(len(skipped_ids)), inline=True)
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
                              f"(модератор <@{p.get('moderator_id')}>)")
            embed.add_field(name="История решений", value="\n".join(plines), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stats_reset", description="Обнулить статистику нарушений пользователя")
    @app_commands.checks.has_permissions(administrator=True)
    async def stats_reset(self, interaction: discord.Interaction, user: discord.User):
        get_db().reset_user_stats(interaction.guild.id, user.id)
        await interaction.response.send_message(f"Статистика {user.mention} обнулена.", ephemeral=True)

    @app_commands.command(name="digest", description="Сводный отчёт модерации за период (по умолчанию 7 дней)")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(days="За сколько последних дней собрать отчёт (1-90, по умолчанию 7)")
    async def digest_cmd(self, interaction: discord.Interaction, days: int = 7):
        days = max(1, min(days, 90))
        embed = build_digest_embed(interaction.guild, days=days)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="export", description="Выгрузить таблицу всех нарушений и наказаний в CSV (Excel)")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(days="За сколько последних дней выгрузить данные (1-365, по умолчанию 30)")
    async def export_cmd(self, interaction: discord.Interaction, days: int = 30):
        await interaction.response.defer(ephemeral=True)
        days = max(1, min(days, 365))
        rows = get_db().export_guild_records(interaction.guild.id, days=days)
        if not rows:
            await interaction.followup.send(f"За последние {days} дн. записей о нарушениях нет.", ephemeral=True)
            return

        buf = io.StringIO()
        # UTF-8 BOM (\ufeff), чтобы Microsoft Excel корректно открывал кириллицу
        buf.write("\ufeff")
        writer = csv.writer(buf, delimiter=";")
        writer.writerow([
            "ID нарушения",
            "Дата нарушения (UTC)",
            "ID пользователя",
            "ID канала",
            "Правило",
            "Название правила",
            "Тяжесть",
            "Метод детекта",
            "Оригинальный текст",
            "Перевод",
            "Решение модератора",
            "Длительность (сек)",
            "ID модератора",
            "Статус наказания",
            "Дата решения (UTC)",
        ])
        for r in rows:
            rid = r.get("rule_id") or ""
            r_title = RULES.get(rid, {}).get("title", "")
            writer.writerow([
                r.get("violation_id"),
                r.get("violation_time") or "",
                r.get("user_id") or "",
                r.get("channel_id") or "",
                rid,
                r_title,
                r.get("severity") or "",
                r.get("method") or "",
                (r.get("original_text") or "").replace("\n", " "),
                (r.get("translated_text") or "").replace("\n", " "),
                r.get("mod_action") or "ожидает",
                r.get("duration_seconds") if r.get("duration_seconds") is not None else "",
                r.get("moderator_id") or "",
                r.get("punishment_status") or "",
                r.get("decision_time") or "",
            ])

        data_bytes = io.BytesIO(buf.getvalue().encode("utf-8"))
        stamp = time.strftime("%Y%m%d_%H%M")
        filename = f"macronux_moderation_{interaction.guild.id}_{stamp}.csv"
        await interaction.followup.send(
            content=f"📥 Выгружено записей за **{days} дн.**: **{len(rows)}** (разделитель `;`, кодировка UTF-8 для Excel).",
            file=discord.File(fp=data_bytes, filename=filename),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(StatsCog(bot))

