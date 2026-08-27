from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.utils import (
    ERROR_COLOR,
    VIP_COLOR,
    discord_timestamp,
    is_permanent_vip,
    is_vip,
)


class VipCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="vip", description="Показать информацию о LFR VIP")
    async def vip(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        user = await self.bot.db.get_user(interaction.user.id)
        vip_active = is_vip(user)
        status = "💎 VIP" if vip_active else "Free"
        description = (
            "**Преимущества:**\n"
            f"• до {config.VIP_MAX_MESSAGES} сообщений за запуск\n"
            f"• задержка от {config.VIP_MIN_DELAY:.1f}s\n"
            "• дополнительные будущие функции\n\n"
            f"**Твой статус:** {status}"
        )
        if vip_active:
            expiry = (
                "**Навсегда**"
                if is_permanent_vip(user)
                else discord_timestamp(int(user["vip_until"]))
            )
            description += f"\n**VIP действует до:** {expiry}"
        await interaction.followup.send(
            embed=discord.Embed(
                title="💎 LFR VIP", description=description, color=VIP_COLOR
            ),
            ephemeral=True,
        )

    @app_commands.command(name="keyuse", description="Активировать VIP-ключ")
    @app_commands.describe(key="Ключ формата LFR-XXXX-XXXX-XXXX")
    async def keyuse(self, interaction: discord.Interaction, key: str) -> None:
        await interaction.response.defer(ephemeral=True)
        success, reason, vip_until = await self.bot.db.redeem_key(
            interaction.user.id, key
        )
        if success:
            embed = discord.Embed(
                title="✅ VIP активирован",
                description=f"VIP действует до {discord_timestamp(vip_until)}.",
                color=VIP_COLOR,
            )
        else:
            message = (
                "Такого ключа не существует."
                if reason == "not_found"
                else "Этот ключ уже был использован."
            )
            embed = discord.Embed(
                title="❌ Ключ не активирован",
                description=message,
                color=ERROR_COLOR,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="viptrial", description="Один раз получить пробный VIP на 1 день"
    )
    async def viptrial(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        success, vip_until = await self.bot.db.use_vip_trial(
            interaction.user.id, config.VIP_TRIAL_DURATION
        )
        if success:
            embed = discord.Embed(
                title="💎 Пробный VIP активирован",
                description=f"VIP действует до {discord_timestamp(vip_until)}.",
                color=VIP_COLOR,
            )
        else:
            embed = discord.Embed(
                title="❌ Пробный VIP уже использован",
                description="Пробный период доступен только один раз.",
                color=ERROR_COLOR,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VipCog(bot))
