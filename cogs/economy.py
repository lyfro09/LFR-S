from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.utils import EMBED_COLOR, ERROR_COLOR, OwnerView, discord_timestamp


class ShopSelect(discord.ui.Select):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        options = [
            discord.SelectOption(
                label=item["label"],
                value=item_id,
                description=f"Цена: {item['price']} points",
                emoji="💎",
            )
            for item_id, item in config.SHOP_ITEMS.items()
        ]
        super().__init__(placeholder="Выбери товар", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        item = config.SHOP_ITEMS[self.values[0]]
        success, balance, vip_until = await self.bot.db.purchase_vip(
            interaction.user.id,
            int(item["price"]),
            int(item["days"]) * 24 * 60 * 60,
        )
        if not success:
            embed = discord.Embed(
                title="❌ Недостаточно баллов",
                description=(
                    f"Нужно **{item['price']} points**, на балансе **{balance}**."
                ),
                color=ERROR_COLOR,
            )
        else:
            embed = discord.Embed(
                title="✅ Покупка завершена",
                description=(
                    f"Активирован товар **{item['label']}**.\n"
                    f"Баланс: **{balance} points**\n"
                    f"VIP действует до {discord_timestamp(vip_until)}."
                ),
                color=EMBED_COLOR,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


class ShopView(OwnerView):
    def __init__(self, bot: commands.Bot, owner_id: int) -> None:
        super().__init__(owner_id)
        self.add_item(ShopSelect(bot))


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="shop", description="Открыть магазин LFR")
    async def shop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        user = await self.bot.db.get_user(interaction.user.id)
        products = "\n".join(
            f"**{item['label']}** — `{item['price']} points`"
            for item in config.SHOP_ITEMS.values()
        )
        embed = discord.Embed(
            title="🛒 LFR Shop",
            description=f"{products}\n\nТвой баланс: **{user['points']} points**",
            color=EMBED_COLOR,
        )
        embed.set_footer(text="Если VIP уже активен, срок будет продлён")
        view = ShopView(self.bot, interaction.user.id)
        view.message = await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True,
            wait=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EconomyCog(bot))
