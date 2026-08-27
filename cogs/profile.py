from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.utils import (
    EMBED_COLOR,
    OwnerView,
    discord_timestamp,
    format_duration,
    profile_embed,
)


LEADERBOARDS = {
    "points": ("🏆 Топ по баллам", "points"),
    "total_runs": ("🧪 Топ по запускам", "запусков"),
    "total_messages": ("💬 Топ по сообщениям", "сообщений"),
}


async def make_top_embed(bot: commands.Bot, metric: str) -> discord.Embed:
    title, unit = LEADERBOARDS[metric]
    rows = await bot.db.get_top(metric, 10)
    if rows:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [
            f"{medals.get(index, f'`{index}.`')} <@{row['user_id']}> — "
            f"**{row['value']}** {unit}"
            for index, row in enumerate(rows, start=1)
        ]
        description = "\n".join(lines)
    else:
        description = "Пока в рейтинге никого нет."
    return discord.Embed(title=title, description=description, color=EMBED_COLOR)


class TopSelect(discord.ui.Select):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__(
            placeholder="Выбери категорию рейтинга",
            options=[
                discord.SelectOption(
                    label="По баллам", value="points", emoji="🏆", default=True
                ),
                discord.SelectOption(
                    label="По количеству запусков", value="total_runs", emoji="🧪"
                ),
                discord.SelectOption(
                    label="По количеству сообщений",
                    value="total_messages",
                    emoji="💬",
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        metric = self.values[0]
        for option in self.options:
            option.default = option.value == metric
        embed = await make_top_embed(self.bot, metric)
        await interaction.response.edit_message(embed=embed, view=self.view)


class TopView(OwnerView):
    def __init__(self, bot: commands.Bot, owner_id: int) -> None:
        super().__init__(owner_id)
        self.add_item(TopSelect(bot))


class ProfileCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="profile", description="Показать профиль LFR")
    async def profile(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        user = await self.bot.db.get_user(interaction.user.id)
        await interaction.followup.send(
            embed=profile_embed(interaction.user.id, user), ephemeral=True
        )

    @app_commands.command(name="daily", description="Получить ежедневную награду")
    async def daily(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        claimed, remaining = await self.bot.db.claim_daily(
            interaction.user.id, config.DAILY_REWARD, config.DAILY_COOLDOWN
        )
        if claimed:
            embed = discord.Embed(
                title="🎁 Ежедневная награда",
                description=f"Ты получил **+{config.DAILY_REWARD} points**!",
                color=EMBED_COLOR,
            )
        else:
            next_daily = int(discord.utils.utcnow().timestamp()) + remaining
            embed = discord.Embed(
                title="⏳ Награда уже получена",
                description=(
                    f"До следующей награды: **{format_duration(remaining)}**\n"
                    f"Можно забрать {discord_timestamp(next_daily, 'R')}."
                ),
                color=EMBED_COLOR,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="top", description="Показать рейтинг пользователей LFR")
    async def top(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = await make_top_embed(self.bot, "points")
        view = TopView(self.bot, interaction.user.id)
        view.message = await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True,
            wait=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProfileCog(bot))
