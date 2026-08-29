from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.admin import stats_embed
from cogs.utils import EMBED_COLOR, is_admin, is_moderator


class GeneralCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="stats", description="Показать статистику LFR")
    async def stats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        stats = await self.bot.db.get_global_stats()
        await interaction.followup.send(embed=stats_embed(stats), ephemeral=True)

    @app_commands.command(name="help", description="Показать список команд LFR")
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="📚 LFR Spam — помощь",
            color=EMBED_COLOR,
        )
        embed.add_field(
            name="🧪 Spam",
            value=(
                "`/spam` — открыть многоразовую панель\n"
                "`/status` — активная отправка\n"
                "`/stop` — остановить свою отправку\n"
                "`/history` — последние запуски"
            ),
            inline=False,
        )
        embed.add_field(
            name="👤 Profile",
            value=(
                "`/profile` — профиль\n"
                "`/daily` — ежедневная награда\n"
                "`/top` — рейтинг"
            ),
            inline=False,
        )
        embed.add_field(
            name="💎 VIP",
            value=(
                "`/vip` — статус и преимущества\n"
                "`/buyvip` — купить VIP за USDT\n"
                "`/paymentstatus` — обновить статус оплаты\n"
                "`/keyuse` — активировать ключ\n"
                "`/shop` — магазин\n"
                "`/viptrial` — пробный VIP"
            ),
            inline=False,
        )
        embed.add_field(
            name="ℹ️ Other",
            value="`/stats` — общая статистика\n`/spamping` — статус приложения",
            inline=False,
        )
        if is_admin(interaction.user.id):
            embed.add_field(
                name="🛠️ Admin",
                value=(
                    "`/adminmenu` — админ-панель\n"
                    "`/vipforever` — постоянный VIP\n"
                    "`/modmenu` — мод-панель"
                ),
                inline=False,
            )
        elif is_moderator(interaction.user.id):
            embed.add_field(
                name="🛡️ Moderator",
                value="`/modmenu` — мод-панель",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="spamping", description="Показать состояние LFR")
    async def spamping(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        database_ok = await self.bot.db.is_healthy()
        embed = discord.Embed(
            title="✅ LFR Spam online",
            color=EMBED_COLOR,
        )
        embed.add_field(name="Версия", value=f"`{config.APP_VERSION}`", inline=True)
        embed.add_field(
            name="Загружено строк",
            value=f"`{len(self.bot.loaded_messages)}`",
            inline=True,
        )
        embed.add_field(
            name="SQLite",
            value="`online`" if database_ok else "`error`",
            inline=True,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GeneralCog(bot))
