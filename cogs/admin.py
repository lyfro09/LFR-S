from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.utils import (
    EMBED_COLOR,
    ERROR_COLOR,
    OwnerView,
    discord_timestamp,
    is_admin,
    is_moderator,
    parse_user_id,
    profile_embed,
    send_ephemeral,
)


def stats_embed(stats: dict[str, int], title: str = "📊 Статистика LFR") -> discord.Embed:
    embed = discord.Embed(title=title, color=EMBED_COLOR)
    embed.add_field(name="Пользователей", value=f"`{stats['users']}`", inline=True)
    embed.add_field(name="Запусков", value=f"`{stats['runs']}`", inline=True)
    embed.add_field(name="Сообщений", value=f"`{stats['messages']}`", inline=True)
    embed.add_field(name="Активных VIP", value=f"`{stats['active_vip']}`", inline=True)
    return embed


class AdminModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, *, title: str) -> None:
        super().__init__(title=title, timeout=300)
        self.bot = bot

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user.id):
            return True
        await send_ephemeral(interaction, "❌ Недостаточно прав.")
        return False

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        print(f"[ADMIN MODAL ERROR] {type(error).__name__}: {error}")
        try:
            await send_ephemeral(
                interaction, "❌ Не удалось выполнить действие. Данные некорректны."
            )
        except discord.HTTPException:
            pass


class CreateKeyModal(AdminModal):
    duration = discord.ui.TextInput(
        label="Duration days", placeholder="Например: 7", max_length=5
    )
    amount = discord.ui.TextInput(
        label="Amount", placeholder="От 1 до 25", default="1", max_length=2
    )

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot, title="Создать VIP key")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        try:
            days = int(self.duration.value)
            amount = int(self.amount.value)
        except ValueError:
            await send_ephemeral(interaction, "❌ Дни и количество должны быть целыми.")
            return
        if not 1 <= days <= 3650 or not 1 <= amount <= 25:
            await send_ephemeral(
                interaction, "❌ Допустимо: 1–3650 дней и 1–25 ключей."
            )
            return
        await interaction.response.defer(ephemeral=True)
        keys = await self.bot.db.create_keys(days * 86400, amount)
        await interaction.followup.send(
            f"✅ Создано ключей: **{len(keys)}** на **{days} дн.**\n```\n"
            + "\n".join(keys)
            + "\n```",
            ephemeral=True,
        )


class GrantVipModal(AdminModal):
    target = discord.ui.TextInput(
        label="User ID или упоминание", placeholder="123456789012345678"
    )
    duration = discord.ui.TextInput(
        label="Duration days", placeholder="Например: 30", max_length=5
    )

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot, title="Выдать VIP")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        user_id = parse_user_id(self.target.value)
        try:
            days = int(self.duration.value)
        except ValueError:
            days = 0
        if user_id is None or not 1 <= days <= 3650:
            await send_ephemeral(interaction, "❌ Некорректные ID или срок (1–3650 дней).")
            return
        await interaction.response.defer(ephemeral=True)
        vip_until = await self.bot.db.grant_vip(user_id, days * 86400)
        await interaction.followup.send(
            f"✅ Пользователю <@{user_id}> выдан VIP до "
            f"{discord_timestamp(vip_until)}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class RemoveVipModal(AdminModal):
    target = discord.ui.TextInput(
        label="User ID или упоминание", placeholder="123456789012345678"
    )

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot, title="Снять VIP")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        user_id = parse_user_id(self.target.value)
        if user_id is None:
            await send_ephemeral(interaction, "❌ Некорректный ID пользователя.")
            return
        if user_id in config.OWNER_USER_IDS:
            await send_ephemeral(
                interaction, "❌ Постоянный VIP владельца отключить нельзя."
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.remove_vip(user_id)
        await interaction.followup.send(
            f"✅ VIP пользователя <@{user_id}> отключён.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class PointsModal(AdminModal):
    target = discord.ui.TextInput(
        label="User ID или упоминание", placeholder="123456789012345678"
    )
    amount = discord.ui.TextInput(label="Количество points", placeholder="Например: 500")

    def __init__(self, bot: commands.Bot, *, remove: bool) -> None:
        self.remove = remove
        super().__init__(bot, title="Снять points" if remove else "Добавить points")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        user_id = parse_user_id(self.target.value)
        try:
            amount = int(self.amount.value)
        except ValueError:
            amount = 0
        if user_id is None or not 1 <= amount <= 1_000_000_000:
            await send_ephemeral(interaction, "❌ Некорректные ID или сумма.")
            return
        await interaction.response.defer(ephemeral=True)
        delta = -amount if self.remove else amount
        balance = await self.bot.db.change_points(user_id, delta)
        action = "Снято" if self.remove else "Добавлено"
        await interaction.followup.send(
            f"✅ {action} **{amount} points** для <@{user_id}>. "
            f"Новый баланс: **{balance}**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class AdminView(OwnerView):
    def __init__(self, bot: commands.Bot, owner_id: int) -> None:
        super().__init__(owner_id)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if is_admin(interaction.user.id):
            return True
        await send_ephemeral(interaction, "❌ Права администратора больше не доступны.")
        return False

    @discord.ui.button(label="Создать ключ", emoji="🔑", style=discord.ButtonStyle.primary)
    async def create_key(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(CreateKeyModal(self.bot))

    @discord.ui.button(label="Выдать VIP", emoji="💎", style=discord.ButtonStyle.success)
    async def grant_vip(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(GrantVipModal(self.bot))

    @discord.ui.button(label="Снять VIP", emoji="🚫", style=discord.ButtonStyle.danger)
    async def remove_vip(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(RemoveVipModal(self.bot))

    @discord.ui.button(label="Добавить points", emoji="➕", style=discord.ButtonStyle.secondary)
    async def add_points(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(PointsModal(self.bot, remove=False))

    @discord.ui.button(label="Снять points", emoji="➖", style=discord.ButtonStyle.secondary)
    async def remove_points(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(PointsModal(self.bot, remove=True))

    @discord.ui.button(label="Статистика", emoji="📊", style=discord.ButtonStyle.secondary)
    async def project_stats(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        stats = await self.bot.db.get_global_stats()
        await interaction.followup.send(
            embed=stats_embed(stats, "📊 Админ-статистика"), ephemeral=True
        )


class InspectUserModal(discord.ui.Modal, title="Данные пользователя"):
    target = discord.ui.TextInput(
        label="User ID или упоминание", placeholder="123456789012345678"
    )

    def __init__(self, bot: commands.Bot, *, stats_only: bool = False) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.stats_only = stats_only

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_moderator(interaction.user.id):
            await send_ephemeral(interaction, "❌ Недостаточно прав.")
            return
        user_id = parse_user_id(self.target.value)
        if user_id is None:
            await send_ephemeral(interaction, "❌ Некорректный ID пользователя.")
            return
        await interaction.response.defer(ephemeral=True)
        user = await self.bot.db.get_user(user_id)
        embed = profile_embed(user_id, user, moderator=True)
        if self.stats_only:
            embed.title = "📈 Статистика пользователя"
            embed.description = (
                f"ID: `{user_id}`\nПробный VIP использован: "
                f"**{'да' if user['vip_trial_used'] else 'нет'}**"
            )
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        print(f"[MOD MODAL ERROR] {type(error).__name__}: {error}")
        try:
            await send_ephemeral(
                interaction, "❌ Не удалось получить данные пользователя."
            )
        except discord.HTTPException:
            pass


class ModView(OwnerView):
    def __init__(self, bot: commands.Bot, owner_id: int) -> None:
        super().__init__(owner_id)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if is_moderator(interaction.user.id):
            return True
        await send_ephemeral(interaction, "❌ Права модератора больше не доступны.")
        return False

    @discord.ui.button(label="Профиль", emoji="👤", style=discord.ButtonStyle.primary)
    async def inspect_profile(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(InspectUserModal(self.bot))

    @discord.ui.button(label="Статистика", emoji="📈", style=discord.ButtonStyle.secondary)
    async def inspect_stats(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            InspectUserModal(self.bot, stats_only=True)
        )


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="adminmenu", description="Открыть панель администратора")
    async def adminmenu(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🛠️ LFR Admin Menu",
            description="Управление VIP, ключами, баллами и статистикой проекта.",
            color=EMBED_COLOR,
        )
        view = AdminView(self.bot, interaction.user.id)
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @app_commands.command(
        name="vipforever",
        description="Выдать пользователю постоянный VIP",
    )
    @app_commands.describe(user="Пользователь, которому нужно выдать VIP навсегда")
    async def vipforever(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(
                "❌ Недостаточно прав.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_permanent_vip(
            user.id, config.PERMANENT_VIP_UNTIL
        )
        await interaction.followup.send(
            f"✅ Пользователю <@{user.id}> выдан VIP **навсегда**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="modmenu", description="Открыть панель модератора")
    async def modmenu(self, interaction: discord.Interaction) -> None:
        if not is_moderator(interaction.user.id):
            await interaction.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🛡️ LFR Mod Menu",
            description=(
                "Просмотр профиля и статистики пользователя. "
                "Изменение VIP и баланса недоступно."
            ),
            color=EMBED_COLOR,
        )
        view = ModView(self.bot, interaction.user.id)
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
