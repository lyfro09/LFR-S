from __future__ import annotations

import re
import sqlite3
import time

import discord

import config


EMBED_COLOR = discord.Color.from_rgb(88, 101, 242)
VIP_COLOR = discord.Color.from_rgb(241, 196, 15)
ERROR_COLOR = discord.Color.from_rgb(231, 76, 60)
SUCCESS_COLOR = discord.Color.from_rgb(46, 204, 113)


def is_vip(user: dict, now: int | None = None) -> bool:
    return int(user["vip_until"]) > (now or int(time.time()))


def is_permanent_vip(user: dict) -> bool:
    return int(user["vip_until"]) >= config.PERMANENT_VIP_UNTIL


def discord_timestamp(timestamp: int, style: str = "f") -> str:
    return f"<t:{int(timestamp)}:{style}>"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч. {minutes} мин."
    if minutes:
        return f"{minutes} мин. {secs} сек."
    return f"{secs} сек."


def parse_user_id(value: str) -> int | None:
    match = re.fullmatch(r"\s*<@!?(\d+)>\s*|\s*(\d+)\s*", value)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_USER_IDS


def is_moderator(user_id: int) -> bool:
    return is_admin(user_id) or user_id in config.MOD_USER_IDS


async def send_ephemeral(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(content, embed=embed, ephemeral=True)


class OwnerView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_ephemeral(
            interaction,
            "❌ Этой панелью может пользоваться только создавший её пользователь.",
        )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        print(f"[VIEW ERROR] {type(error).__name__}: {error}")
        try:
            message = "❌ Не удалось выполнить действие. Попробуй ещё раз."
            if isinstance(error, sqlite3.Error):
                message = "❌ Ошибка базы данных. Попробуй позже."
            await send_ephemeral(interaction, message)
        except discord.HTTPException:
            pass


def profile_embed(user_id: int, user: dict, *, moderator: bool = False) -> discord.Embed:
    vip = is_vip(user)
    embed = discord.Embed(
        title="👤 LFR Profile" if not moderator else "🛡️ Профиль пользователя",
        color=VIP_COLOR if vip else EMBED_COLOR,
    )
    embed.add_field(name="Пользователь", value=f"<@{user_id}>", inline=False)
    embed.add_field(name="Статус", value="💎 VIP" if vip else "Free", inline=True)
    embed.add_field(name="Баллы", value=f"`{user['points']}`", inline=True)
    embed.add_field(name="Всего запусков", value=f"`{user['total_runs']}`", inline=True)
    embed.add_field(
        name="Всего сообщений", value=f"`{user['total_messages']}`", inline=True
    )
    embed.add_field(
        name="Использует LFR с",
        value=discord_timestamp(int(user["created_at"]), "D"),
        inline=True,
    )
    if vip:
        embed.add_field(
            name="VIP действует до",
            value=(
                "**Навсегда**"
                if is_permanent_vip(user)
                else discord_timestamp(int(user["vip_until"]))
            ),
            inline=False,
        )
    return embed
