"""Rules + banword management slash-commands."""
import discord
from discord import app_commands
from discord.ext import commands

from bot import rules
from bot.database import get_db
from bot.moderation import _variant_key

_RULE_ORDER = ("4.1", "4.2", "4.3", "4.4", "4.5", "3.1")
_PAGE_SIZE = 24  # строк на страницу
BRAND_PURPLE = 0x7F5AF0


class BanwordPager(discord.ui.View):
    """Кнопки навигации по страницам списка банвордов."""

    def __init__(self, pages: list):
        super().__init__(timeout=180)
        self.pages = pages
        self.index = 0

    async def _flip(self, interaction: discord.Interaction, delta: int):
        self.index = (self.index + delta) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.index])

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._flip(interaction, -1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._flip(interaction, 1)


def _collect_banwords(guild_id: int) -> list[tuple[str, str]]:
    """Все применяемые банворды: (rule_id, word). RU + иностранные + серверные."""
    seen = set()
    rows = []
    for rule_id in _RULE_ORDER:
        words = rules._BANWORD_BASE.get(rule_id, []) + rules._BANWORD_FOREIGN.get(rule_id, [])
        for w in words:
            key = _variant_key(w)
            if key in seen:
                continue
            seen.add(key)
            rows.append((rule_id, w))
    for w in get_db().list_banwords(guild_id):
        key = _variant_key(w)
        if key in seen:
            continue
        seen.add(key)
        rows.append(("server", w))
    return rows


def _build_pages(rows: list[tuple[str, str]]) -> list[discord.Embed]:
    from collections import Counter
    counts = Counter(rule for rule, _ in rows)
    summary = " · ".join(f"{r}: {counts.get(r, 0)}" for r in _RULE_ORDER)
    server_n = counts.get("server", 0)
    lines = [f"`{rule}` {word}" for rule, word in rows]
    chunks = [lines[i:i + _PAGE_SIZE] for i in range(0, len(lines), _PAGE_SIZE)]
    pages = []
    for i, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(title="📛 Банворды сервера", color=discord.Color(BRAND_PURPLE))
        embed.description = "\n".join(chunk)
        embed.set_footer(
            text=f"Стр. {i}/{len(chunks)} · Всего {len(rows)} "
                 f"({summary}, серверные: {server_n})"
        )
        pages.append(embed)
    return pages


class RulesCmdCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="rules", description="Показать правила сервера")
    async def rules_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(title="📜 Правила сервера", color=discord.Color(BRAND_PURPLE))
        for section_id, ids in rules.RULES_BY_SECTION.items():
            lines = []
            for rid in ids:
                r = rules.RULES[rid]
                pun = f" — *{r['punishment']}*" if r.get("punishment") else ""
                lines.append(f"**{rid}** {r['title']}{pun}")
            embed.add_field(name=f"Раздел {section_id}", value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="add_banword", description="Добавить слово в список банвордов сервера")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def add_banword(self, interaction: discord.Interaction, word: str):
        get_db().add_banword(interaction.guild.id, word.lower())
        await interaction.response.send_message(f"Добавлено слово: **{word.lower()}**", ephemeral=True)

    @app_commands.command(name="remove_banword", description="Убрать слово из списка банвордов сервера")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def remove_banword(self, interaction: discord.Interaction, word: str):
        ok = get_db().remove_banword(interaction.guild.id, word.lower())
        if ok:
            await interaction.response.send_message(f"Убрано слово: **{word.lower()}**", ephemeral=True)
        else:
            await interaction.response.send_message("Слово не найдено. Встроенные банворды можно "
                                                    "заблокировать отдельно — их список смотрим в "
                                                    "/list_banwords.", ephemeral=True)

    @app_commands.command(name="list_banwords", description="Показать все банворды, применяемые ботом")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def list_banwords(self, interaction: discord.Interaction):
        rows = _collect_banwords(interaction.guild.id)
        if not rows:
            await interaction.response.send_message("Список банвордов пуст.", ephemeral=True)
            return
        pages = _build_pages(rows)
        view = BanwordPager(pages)
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(RulesCmdCog(bot))
