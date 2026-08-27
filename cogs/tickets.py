from __future__ import annotations

import asyncio
import io
import re
import sqlite3
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.utils import EMBED_COLOR, ERROR_COLOR, SUCCESS_COLOR, parse_user_id


SUPPORT_CATEGORY = "━━ 🎫 SUPPORT ━━"
OPEN_TICKETS_CATEGORY = "━━ 🎫 OPEN TICKETS ━━"
REPORT_TICKETS_CATEGORY = "━━ 🚨 REPORT TICKETS ━━"
STAFF_CATEGORY = "━━ 🛡️ STAFF ━━"

OWNER_ROLE = "👑・Owner"
ADMIN_ROLE = "⚜️・Administration"
MODERATOR_ROLE = "🛡️・Moderator"
SUPPORT_ROLE = "🎫・Support"

SUPPORT_TYPES = {
    "anti_nuke": "Anti-Nuke",
    "nuke": "Nuke",
    "spam": "Spam",
    "other": "Other",
}
REPORT_TYPES = {
    "violation": "Нарушение",
    "user": "Пользователь",
    "abuse": "Abuse LFR",
    "security": "Security issue",
    "other": "Other",
}


def compact(value: str | None, limit: int = 1024) -> str:
    value = (value or "—").strip() or "—"
    return value if len(value) <= limit else value[: limit - 1] + "…"


def ticket_service(interaction: discord.Interaction) -> TicketService:
    service = getattr(interaction.client, "ticket_service", None)
    if service is None:
        raise RuntimeError("Ticket service is not initialized")
    return service


async def ephemeral(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    file: discord.File | None = None,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(
            content, embed=embed, view=view, file=file, ephemeral=True
        )
    else:
        await interaction.response.send_message(
            content, embed=embed, view=view, file=file, ephemeral=True
        )


class SafeView(discord.ui.View):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        print(
            f"[TICKET VIEW ERROR] custom_id={getattr(item, 'custom_id', None)} "
            f"{type(error).__name__}: {error}",
            flush=True,
        )
        try:
            await ephemeral(interaction, "❌ Не удалось выполнить действие.")
        except discord.HTTPException:
            pass


class OwnedView(SafeView):
    def __init__(self, owner_id: int, *, timeout: float = 180) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await ephemeral(interaction, "❌ Эта панель открыта для другого пользователя.")
        return False


class SupportPanelView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Создать тикет",
        emoji="🎫",
        style=discord.ButtonStyle.primary,
        custom_id="lfr_support_ticket_create",
    )
    async def create(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        print(
            f"[TICKET UI] support panel user={interaction.user.id} "
            f"guild={interaction.guild_id}",
            flush=True,
        )
        embed = discord.Embed(
            title="🎫 Категория обращения",
            description="Выбери направление, с которым нужна помощь.",
            color=EMBED_COLOR,
        )
        await interaction.followup.send(
            embed=embed,
            view=TicketCategoryView(interaction.user.id, "support"),
            ephemeral=True,
        )


class ReportPanelView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Создать жалобу",
        emoji="🚨",
        style=discord.ButtonStyle.danger,
        custom_id="lfr_report_ticket_create",
    )
    async def create(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        print(
            f"[TICKET UI] report panel user={interaction.user.id} "
            f"guild={interaction.guild_id}",
            flush=True,
        )
        embed = discord.Embed(
            title="🚨 Причина обращения",
            description="Выбери наиболее подходящую категорию.",
            color=ERROR_COLOR,
        )
        await interaction.followup.send(
            embed=embed,
            view=TicketCategoryView(interaction.user.id, "report"),
            ephemeral=True,
        )


class TicketCategorySelect(discord.ui.Select[Any]):
    def __init__(self, ticket_type: str) -> None:
        self.ticket_type = ticket_type
        if ticket_type == "support":
            options = [
                discord.SelectOption(label="Anti-Nuke", value="anti_nuke", emoji="🛡️"),
                discord.SelectOption(label="Nuke", value="nuke", emoji="💣"),
                discord.SelectOption(label="Spam", value="spam", emoji="💬"),
                discord.SelectOption(label="Other", value="other", emoji="⚙️"),
            ]
            placeholder = "Выберите категорию обращения"
        else:
            options = [
                discord.SelectOption(label="Нарушение", value="violation", emoji="⚠️"),
                discord.SelectOption(label="Пользователь", value="user", emoji="👤"),
                discord.SelectOption(label="Abuse", value="abuse", emoji="🛡️"),
                discord.SelectOption(label="Security", value="security", emoji="🐛"),
                discord.SelectOption(label="Other", value="other", emoji="📌"),
            ]
            placeholder = "Выберите причину жалобы"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.ticket_type == "support":
            await interaction.response.send_modal(SupportTicketModal(self.values[0]))
        else:
            await interaction.response.send_modal(ReportTicketModal(self.values[0]))


class TicketCategoryView(OwnedView):
    def __init__(self, owner_id: int, ticket_type: str) -> None:
        super().__init__(owner_id)
        self.add_item(TicketCategorySelect(ticket_type))


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        print(f"[TICKET MODAL ERROR] {type(error).__name__}: {error}")
        try:
            await ephemeral(interaction, "❌ Не удалось выполнить действие.")
        except discord.HTTPException:
            pass


class SupportTicketModal(SafeModal):
    subject = discord.ui.TextInput(
        label="Тема", style=discord.TextStyle.short, max_length=100
    )
    description = discord.ui.TextInput(
        label="Описание проблемы", style=discord.TextStyle.paragraph, max_length=1000
    )

    def __init__(self, category: str) -> None:
        super().__init__(title="Создание тикета", timeout=300)
        self.category = category

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await ticket_service(interaction).create_ticket(
            interaction,
            ticket_type="support",
            category=self.category,
            subject=str(self.subject),
            description=str(self.description),
        )


class ReportTicketModal(SafeModal):
    target = discord.ui.TextInput(
        label="User ID / объект жалобы",
        style=discord.TextStyle.short,
        required=False,
        max_length=100,
    )
    subject = discord.ui.TextInput(
        label="Краткая причина", style=discord.TextStyle.short, max_length=100
    )
    description = discord.ui.TextInput(
        label="Описание", style=discord.TextStyle.paragraph, max_length=1000
    )
    evidence = discord.ui.TextInput(
        label="Доказательства / ссылки",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, category: str) -> None:
        super().__init__(title="Создание жалобы", timeout=300)
        self.category = category

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await ticket_service(interaction).create_ticket(
            interaction,
            ticket_type="report",
            category=self.category,
            subject=str(self.subject),
            description=str(self.description),
            target=str(self.target) or None,
            evidence=str(self.evidence) or None,
        )

class TicketControlsView(SafeView):
    def __init__(self, *, claimed: bool = False) -> None:
        super().__init__(timeout=None)
        if claimed:
            self.claim.label = "Claimed"
            self.claim.emoji = "✅"
            self.claim.disabled = True

    @discord.ui.button(
        label="Claim",
        emoji="👤",
        style=discord.ButtonStyle.primary,
        custom_id="lfr_ticket_claim",
    )
    async def claim(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await ticket_service(interaction).claim(interaction)

    @discord.ui.button(
        label="Add User",
        emoji="➕",
        style=discord.ButtonStyle.secondary,
        custom_id="lfr_ticket_add_user",
    )
    async def add_user(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await ticket_service(interaction).open_add_user(interaction)

    @discord.ui.button(
        label="Close",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="lfr_ticket_close",
    )
    async def close(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await ticket_service(interaction).close_prompt(interaction)

    @discord.ui.button(
        label="More",
        emoji="⋯",
        style=discord.ButtonStyle.secondary,
        custom_id="lfr_ticket_more",
    )
    async def more(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await ticket_service(interaction).more_menu(interaction)


class ClosedTicketView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Reopen",
        emoji="🔓",
        style=discord.ButtonStyle.success,
        custom_id="lfr_ticket_reopen",
    )
    async def reopen(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await ticket_service(interaction).reopen(interaction)

    @discord.ui.button(
        label="Transcript",
        emoji="📄",
        style=discord.ButtonStyle.secondary,
        custom_id="lfr_ticket_transcript",
    )
    async def transcript(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await ticket_service(interaction).transcript(interaction)

    @discord.ui.button(
        label="Delete",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="lfr_ticket_delete",
    )
    async def delete(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await ticket_service(interaction).delete_prompt(interaction)


class ConfirmCloseView(OwnedView):
    def __init__(self, owner_id: int) -> None:
        super().__init__(owner_id)

    @discord.ui.button(label="Закрыть", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await ticket_service(interaction).close_confirmed(interaction)

    @discord.ui.button(label="Отмена", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await interaction.response.edit_message(content="Действие отменено.", embed=None, view=None)


class ConfirmDeleteView(OwnedView):
    def __init__(self, owner_id: int) -> None:
        super().__init__(owner_id)

    @discord.ui.button(label="Удалить", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await ticket_service(interaction).delete_confirmed(interaction)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await interaction.response.edit_message(content="Действие отменено.", embed=None, view=None)


class AddUserModal(SafeModal, title="Добавить пользователя"):
    user_id = discord.ui.TextInput(label="User ID", placeholder="123456789012345678")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await ticket_service(interaction).add_user(interaction, str(self.user_id))


class RemoveUserModal(SafeModal, title="Удалить пользователя"):
    user_id = discord.ui.TextInput(label="User ID", placeholder="123456789012345678")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await ticket_service(interaction).remove_user(interaction, str(self.user_id))


class RenameTicketModal(SafeModal, title="Переименовать тикет"):
    name = discord.ui.TextInput(label="Новое имя", max_length=80)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await ticket_service(interaction).rename(interaction, str(self.name))


class MoreSelect(discord.ui.Select[Any]):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Дополнительные действия",
            options=[
                discord.SelectOption(label="Transcript", value="transcript", emoji="📄"),
                discord.SelectOption(label="Rename", value="rename", emoji="✏️"),
                discord.SelectOption(label="Remove User", value="remove", emoji="➖"),
                discord.SelectOption(label="Ticket Info", value="info", emoji="📋"),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        service = ticket_service(interaction)
        action = self.values[0]
        if action == "transcript":
            await service.transcript(interaction)
        elif action == "rename":
            await service.open_rename(interaction)
        elif action == "remove":
            await service.open_remove_user(interaction)
        else:
            await service.info(interaction)


class MoreView(OwnedView):
    def __init__(self, owner_id: int) -> None:
        super().__init__(owner_id)
        self.add_item(MoreSelect())


class TicketService:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.create_lock = asyncio.Lock()

    @staticmethod
    def role(member: discord.Member, name: str) -> bool:
        return any(role.name == name for role in member.roles)

    def is_admin(self, member: discord.Member) -> bool:
        return (
            member.id in config.ADMIN_USER_IDS
            or member.guild_permissions.administrator
            or self.role(member, OWNER_ROLE)
            or self.role(member, ADMIN_ROLE)
        )

    def is_moderator(self, member: discord.Member) -> bool:
        return (
            self.is_admin(member)
            or member.id in config.MOD_USER_IDS
            or self.role(member, MODERATOR_ROLE)
        )

    def is_ticket_staff(self, member: discord.Member, ticket_type: str) -> bool:
        if ticket_type == "report":
            return self.is_moderator(member)
        return self.is_moderator(member) or self.role(member, SUPPORT_ROLE)

    @staticmethod
    def find_category(guild: discord.Guild, name: str) -> discord.CategoryChannel | None:
        return discord.utils.get(guild.categories, name=name)

    @staticmethod
    def find_role(guild: discord.Guild, name: str) -> discord.Role | None:
        return discord.utils.get(guild.roles, name=name)

    async def ensure_role(
        self, guild: discord.Guild, name: str, color: discord.Color
    ) -> discord.Role:
        role = self.find_role(guild, name)
        if role is None:
            role = await guild.create_role(name=name, color=color, reason="LFR ticket setup")
        return role

    def bot_overwrite(self, guild: discord.Guild) -> dict[Any, discord.PermissionOverwrite]:
        if guild.me is None:
            return {}
        return {
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
            )
        }

    async def ensure_category(
        self,
        guild: discord.Guild,
        name: str,
        overwrites: dict[Any, discord.PermissionOverwrite],
    ) -> discord.CategoryChannel:
        category = self.find_category(guild, name)
        if category is None:
            return await guild.create_category(name, overwrites=overwrites, reason="LFR ticket setup")
        await category.edit(overwrites=overwrites, reason="LFR ticket setup")
        return category

    async def ensure_text_channel(
        self,
        category: discord.CategoryChannel,
        name: str,
        overwrites: dict[Any, discord.PermissionOverwrite],
    ) -> discord.TextChannel:
        channel = discord.utils.get(category.guild.text_channels, name=name)
        if channel is None:
            return await category.create_text_channel(
                name, overwrites=overwrites, reason="LFR ticket setup"
            )
        await channel.edit(
            category=category,
            overwrites=overwrites,
            sync_permissions=False,
            reason="LFR ticket setup",
        )
        return channel

    async def setup_guild(self, guild: discord.Guild) -> tuple[discord.TextChannel, discord.TextChannel]:
        owner = await self.ensure_role(guild, OWNER_ROLE, discord.Color.gold())
        admin = await self.ensure_role(guild, ADMIN_ROLE, discord.Color.red())
        moderator = await self.ensure_role(guild, MODERATOR_ROLE, discord.Color.blue())
        support = await self.ensure_role(guild, SUPPORT_ROLE, discord.Color.green())

        bot_ow = self.bot_overwrite(guild)
        public_overwrites: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=False, read_message_history=True
            ),
            **bot_ow,
        }
        for role in (owner, admin, moderator, support):
            public_overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
            )
        support_category = await self.ensure_category(guild, SUPPORT_CATEGORY, public_overwrites)
        support_channel = await self.ensure_text_channel(
            support_category, "🆘・support", public_overwrites
        )
        report_channel = await self.ensure_text_channel(
            support_category, "🚨・report", public_overwrites
        )

        private_base: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            **bot_ow,
        }
        support_overwrites = dict(private_base)
        for role in (owner, admin, moderator, support):
            support_overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                manage_channels=role in (owner, admin),
                attach_files=True,
                embed_links=True,
            )
        report_overwrites = dict(private_base)
        for role in (owner, admin, moderator):
            report_overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                manage_channels=role in (owner, admin),
                attach_files=True,
                embed_links=True,
            )
        await self.ensure_category(guild, OPEN_TICKETS_CATEGORY, support_overwrites)
        await self.ensure_category(guild, REPORT_TICKETS_CATEGORY, report_overwrites)

        staff_overwrites = dict(private_base)
        for role in (owner, admin, moderator, support):
            staff_overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=True
            )
        staff_category = await self.ensure_category(guild, STAFF_CATEGORY, staff_overwrites)
        await self.ensure_text_channel(staff_category, "🎫・ticket-logs", staff_overwrites)

        await self.replace_panel(support_channel, self.support_panel_embed(), SupportPanelView())
        await self.replace_panel(report_channel, self.report_panel_embed(), ReportPanelView())
        return support_channel, report_channel

    async def replace_panel(
        self, channel: discord.TextChannel, embed: discord.Embed, view: discord.ui.View
    ) -> None:
        if self.bot.user is not None:
            async for message in channel.history(limit=None):
                if message.author.id == self.bot.user.id:
                    try:
                        await message.delete()
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                        pass
        await channel.send(embed=embed, view=view)

    @staticmethod
    def support_panel_embed() -> discord.Embed:
        embed = discord.Embed(
            title="🎫 LFR Support",
            description=(
                "Нужна помощь с LFR?\n\n"
                "Выбери тип обращения ниже, и система создаст приватный тикет "
                "между тобой и командой поддержки."
            ),
            color=EMBED_COLOR,
        )
        embed.add_field(name="🛡️ Anti-Nuke", value="Настройка, защита и ошибки Anti-Nuke", inline=False)
        embed.add_field(name="💣 Nuke", value="Вопросы по LFR Nuke и его функциям", inline=False)
        embed.add_field(name="💬 Spam", value="Помощь с LFR Spam и anti-spam", inline=False)
        embed.add_field(name="⚙️ Other", value="Другие вопросы по LFR", inline=False)
        embed.set_footer(text="LFR Community • Support Center")
        return embed

    @staticmethod
    def report_panel_embed() -> discord.Embed:
        embed = discord.Embed(
            title="🚨 LFR Report Center",
            description="Используй эту панель для приватных обращений к администрации.",
            color=ERROR_COLOR,
        )
        embed.add_field(
            name="Возможные причины",
            value=(
                "⚠️ Нарушение правил\n👤 Жалоба на пользователя\n"
                "🛡️ Abuse LFR\n🐛 Security issue\n📌 Другое"
            ),
            inline=False,
        )
        embed.set_footer(text="LFR Community • Report Center")
        return embed

    async def ticket_context(
        self, interaction: discord.Interaction
    ) -> tuple[discord.TextChannel, dict[str, Any], discord.Member] | None:
        if not isinstance(interaction.channel, discord.TextChannel) or interaction.guild is None:
            await ephemeral(interaction, "❌ Это действие работает только внутри тикета.")
            return None
        if not isinstance(interaction.user, discord.Member):
            await ephemeral(interaction, "❌ Не удалось определить права участника.")
            return None
        row = await self.bot.db.get_ticket_by_channel(interaction.channel.id)
        if row is None:
            await ephemeral(interaction, "❌ Этот канал не зарегистрирован как тикет.")
            return None
        return interaction.channel, row, interaction.user

    async def ensure_ticket_categories(
        self, guild: discord.Guild
    ) -> tuple[discord.CategoryChannel, discord.CategoryChannel]:
        support = self.find_category(guild, OPEN_TICKETS_CATEGORY)
        report = self.find_category(guild, REPORT_TICKETS_CATEGORY)
        if support is None or report is None:
            await self.setup_guild(guild)
            support = self.find_category(guild, OPEN_TICKETS_CATEGORY)
            report = self.find_category(guild, REPORT_TICKETS_CATEGORY)
        if support is None or report is None:
            raise RuntimeError("Ticket categories were not created")
        return support, report

    async def create_ticket(
        self,
        interaction: discord.Interaction,
        *,
        ticket_type: str,
        category: str,
        subject: str,
        description: str,
        target: str | None = None,
        evidence: str | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("❌ Тикеты доступны только на сервере.", ephemeral=True)
            return
        guild = interaction.guild
        async with self.create_lock:
            existing = await self.bot.db.get_open_ticket(guild.id, interaction.user.id, ticket_type)
            if existing is not None:
                channel = guild.get_channel(int(existing["channel_id"]))
                if isinstance(channel, discord.TextChannel):
                    await interaction.followup.send(
                        f"⚠️ У тебя уже есть открытый тикет: {channel.mention}",
                        ephemeral=True,
                    )
                    return
                await self.bot.db.delete_ticket(int(existing["id"]))

            support_category, report_category = await self.ensure_ticket_categories(guild)
            parent = support_category if ticket_type == "support" else report_category
            overwrites = dict(parent.overwrites)
            overwrites[interaction.user] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
            slug = re.sub(r"[^a-z0-9-]", "", interaction.user.name.lower().replace("_", "-"))
            slug = slug.strip("-") or str(interaction.user.id)
            channel: discord.TextChannel | None = None
            try:
                channel = await guild.create_text_channel(
                    f"{ticket_type}-{slug}"[:100],
                    category=parent,
                    overwrites=overwrites,
                    topic=f"LFR {ticket_type} ticket • owner {interaction.user.id}",
                    reason=f"Ticket opened by {interaction.user}",
                )
                row = await self.bot.db.create_ticket(
                    channel_id=channel.id,
                    guild_id=guild.id,
                    owner_id=interaction.user.id,
                    ticket_type=ticket_type,
                    category=category,
                    subject=subject,
                    description=description,
                    target=target,
                    evidence=evidence,
                )
            except sqlite3.IntegrityError:
                if channel is not None:
                    await channel.delete(reason="Duplicate ticket rollback")
                duplicate = await self.bot.db.get_open_ticket(guild.id, interaction.user.id, ticket_type)
                mention = f"<#{duplicate['channel_id']}>" if duplicate else "существующий канал"
                await interaction.followup.send(
                    f"⚠️ У тебя уже есть открытый тикет: {mention}", ephemeral=True
                )
                return
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"[TICKET CREATE ERROR] {type(error).__name__}: {error}")
                await interaction.followup.send(
                    "❌ Не удалось создать канал. Проверь права бота.", ephemeral=True
                )
                return

            message = await channel.send(
                embed=await self.ticket_embed(row),
                view=TicketControlsView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.bot.db.set_ticket_panel(int(row["id"]), message.id)
            row["panel_message_id"] = message.id
            await self.log_event(guild, "created", row, interaction.user)
            await interaction.followup.send(
                f"✅ Тикет создан: {channel.mention}", ephemeral=True
            )

    async def ticket_embed(self, ticket: dict[str, Any]) -> discord.Embed:
        category_names = SUPPORT_TYPES if ticket["ticket_type"] == "support" else REPORT_TYPES
        embed = discord.Embed(
            title=f"🎫 LFR Ticket #{ticket['id']}",
            color=SUCCESS_COLOR if ticket["status"] == "open" else ERROR_COLOR,
        )
        embed.add_field(
            name="Тип", value="Support" if ticket["ticket_type"] == "support" else "Report", inline=True
        )
        embed.add_field(name="Категория", value=category_names.get(ticket["category"], ticket["category"]), inline=True)
        embed.add_field(name="Автор", value=f"<@{ticket['owner_id']}>", inline=True)
        embed.add_field(
            name="Статус",
            value="🟢 Open" if ticket["status"] == "open" else "🔴 Closed",
            inline=True,
        )
        embed.add_field(
            name="Ответственный",
            value=f"<@{ticket['claimed_by']}>" if ticket.get("claimed_by") else "Не назначен",
            inline=True,
        )
        embed.add_field(name="Создан", value=f"<t:{ticket['created_at']}:F>", inline=True)
        embed.add_field(name="Тема", value=compact(ticket["subject"]), inline=False)
        embed.add_field(name="Описание", value=compact(ticket["description"]), inline=False)
        if ticket.get("target"):
            embed.add_field(name="Объект жалобы", value=compact(ticket["target"]), inline=False)
        if ticket.get("evidence"):
            embed.add_field(name="Доказательства", value=compact(ticket["evidence"]), inline=False)
        members = await self.bot.db.get_ticket_members(int(ticket["id"]))
        if members:
            embed.add_field(
                name="Additional Users",
                value=compact(", ".join(f"<@{user_id}>" for user_id in members)),
                inline=False,
            )
        if ticket.get("closed_by"):
            embed.add_field(name="Закрыл", value=f"<@{ticket['closed_by']}>", inline=True)
        embed.set_footer(text=f"LFR Ticket System • ID: {ticket['id']}")
        return embed

    async def refresh_panel(
        self, channel: discord.TextChannel, ticket: dict[str, Any]
    ) -> None:
        view: discord.ui.View
        if ticket["status"] == "closed":
            view = ClosedTicketView()
        else:
            view = TicketControlsView(claimed=ticket.get("claimed_by") is not None)
        message: discord.Message | None = None
        if ticket.get("panel_message_id"):
            try:
                message = await channel.fetch_message(int(ticket["panel_message_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            message = await channel.send(embed=await self.ticket_embed(ticket), view=view)
            await self.bot.db.set_ticket_panel(int(ticket["id"]), message.id)
        else:
            await message.edit(embed=await self.ticket_embed(ticket), view=view)

    async def claim(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        context = await self.ticket_context(interaction)
        if context is None:
            return
        channel, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await interaction.followup.send("❌ Claim доступен только Staff.", ephemeral=True)
            return
        changed, updated = await self.bot.db.claim_ticket(int(ticket["id"]), member.id)
        if not changed:
            responsible = f"<@{updated['claimed_by']}>" if updated.get("claimed_by") else "другим Staff"
            await interaction.followup.send(
                f"⚠️ Тикет уже закреплён: {responsible}.", ephemeral=True
            )
            return
        await self.refresh_panel(channel, updated)
        await self.log_event(channel.guild, "claimed", updated, member)
        await interaction.followup.send("✅ Тикет закреплён за вами.", ephemeral=True)

    async def close_prompt(self, interaction: discord.Interaction) -> None:
        context = await self.ticket_context(interaction)
        if context is None:
            return
        _, ticket, member = context
        if member.id != int(ticket["owner_id"]) and not self.is_ticket_staff(member, ticket["ticket_type"]):
            await ephemeral(interaction, "❌ Ты не можешь закрыть этот тикет.")
            return
        embed = discord.Embed(
            title="🔒 Закрытие тикета",
            description="Вы уверены, что хотите закрыть этот тикет?",
            color=ERROR_COLOR,
        )
        await ephemeral(interaction, embed=embed, view=ConfirmCloseView(member.id))

    async def close_confirmed(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        context = await self.ticket_context(interaction)
        if context is None:
            return
        channel, ticket, member = context
        if member.id != int(ticket["owner_id"]) and not self.is_ticket_staff(member, ticket["ticket_type"]):
            await interaction.followup.send("❌ Недостаточно прав.", ephemeral=True)
            return
        if ticket["status"] == "closed":
            await interaction.followup.send("⚠️ Тикет уже закрыт.", ephemeral=True)
            return
        updated = await self.bot.db.close_ticket(int(ticket["id"]), member.id)
        await self.set_participant_send(channel, updated, False)
        slug = channel.name.removeprefix("support-").removeprefix("report-").removeprefix("closed-")
        await channel.edit(name=f"closed-{ticket['ticket_type']}-{slug}"[:100], reason="Ticket closed")
        await self.refresh_panel(channel, updated)
        await self.log_event(channel.guild, "closed", updated, member)
        await interaction.followup.send("✅ Тикет закрыт.", ephemeral=True)

    async def reopen(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        context = await self.ticket_context(interaction)
        if context is None:
            return
        channel, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await interaction.followup.send("❌ Reopen доступен только Staff.", ephemeral=True)
            return
        try:
            updated = await self.bot.db.reopen_ticket(int(ticket["id"]))
        except sqlite3.IntegrityError:
            await interaction.followup.send(
                "❌ У автора уже есть другой открытый тикет этого типа.", ephemeral=True
            )
            return
        await self.set_participant_send(channel, updated, True)
        base = channel.name
        prefix = f"closed-{ticket['ticket_type']}-"
        if base.startswith(prefix):
            base = base[len(prefix) :]
        await channel.edit(name=f"{ticket['ticket_type']}-{base}"[:100], reason="Ticket reopened")
        await self.refresh_panel(channel, updated)
        await self.log_event(channel.guild, "reopened", updated, member)
        await interaction.followup.send("✅ Тикет снова открыт.", ephemeral=True)

    async def set_participant_send(
        self, channel: discord.TextChannel, ticket: dict[str, Any], enabled: bool
    ) -> None:
        ids = [int(ticket["owner_id"])] + await self.bot.db.get_ticket_members(int(ticket["id"]))
        for user_id in ids:
            member = channel.guild.get_member(user_id)
            if member is None:
                try:
                    member = await channel.guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            overwrite = channel.overwrites_for(member)
            overwrite.view_channel = True
            overwrite.read_message_history = True
            overwrite.send_messages = enabled
            overwrite.attach_files = enabled
            overwrite.embed_links = enabled
            await channel.set_permissions(member, overwrite=overwrite, reason="Ticket status changed")

    async def open_add_user(self, interaction: discord.Interaction) -> None:
        context = await self.ticket_context(interaction)
        if context is None:
            return
        _, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await ephemeral(interaction, "❌ Добавлять пользователей может только Staff.")
            return
        await interaction.response.send_modal(AddUserModal())

    async def add_user(self, interaction: discord.Interaction, raw_user_id: str) -> None:
        context = await self.ticket_context(interaction)
        if context is None:
            return
        channel, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await interaction.followup.send("❌ Недостаточно прав.", ephemeral=True)
            return
        user_id = parse_user_id(raw_user_id)
        if user_id is None or user_id == int(ticket["owner_id"]):
            await interaction.followup.send("❌ Укажи корректный ID другого пользователя.", ephemeral=True)
            return
        target = channel.guild.get_member(user_id)
        if target is None:
            try:
                target = await channel.guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await interaction.followup.send("❌ Участник не найден на сервере.", ephemeral=True)
                return
        await channel.set_permissions(
            target,
            view_channel=True,
            send_messages=ticket["status"] == "open",
            read_message_history=True,
            attach_files=ticket["status"] == "open",
            embed_links=ticket["status"] == "open",
            reason=f"Added to ticket by {member}",
        )
        await self.bot.db.add_ticket_member(int(ticket["id"]), target.id)
        await self.refresh_panel(channel, ticket)
        await interaction.followup.send(f"✅ Пользователь {target.mention} добавлен.", ephemeral=True)

    async def open_remove_user(self, interaction: discord.Interaction) -> None:
        context = await self.ticket_context(interaction)
        if context is None:
            return
        _, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await ephemeral(interaction, "❌ Удалять пользователей может только Staff.")
            return
        await interaction.response.send_modal(RemoveUserModal())

    async def remove_user(self, interaction: discord.Interaction, raw_user_id: str) -> None:
        context = await self.ticket_context(interaction)
        if context is None:
            return
        channel, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await interaction.followup.send("❌ Недостаточно прав.", ephemeral=True)
            return
        user_id = parse_user_id(raw_user_id)
        if user_id is None or user_id == int(ticket["owner_id"]):
            await interaction.followup.send("❌ Автора тикета удалить нельзя.", ephemeral=True)
            return
        if not await self.bot.db.remove_ticket_member(int(ticket["id"]), user_id):
            await interaction.followup.send("⚠️ Пользователь не добавлен в этот тикет.", ephemeral=True)
            return
        target = channel.guild.get_member(user_id)
        if target is not None:
            await channel.set_permissions(target, overwrite=None, reason=f"Removed by {member}")
        await self.refresh_panel(channel, ticket)
        await interaction.followup.send(f"✅ <@{user_id}> удалён из тикета.", ephemeral=True)

    async def more_menu(self, interaction: discord.Interaction) -> None:
        context = await self.ticket_context(interaction)
        if context is None:
            return
        embed = discord.Embed(
            title="⋯ Ticket Actions",
            description="Выбери дополнительное действие.",
            color=EMBED_COLOR,
        )
        await ephemeral(interaction, embed=embed, view=MoreView(interaction.user.id))

    async def open_rename(self, interaction: discord.Interaction) -> None:
        context = await self.ticket_context(interaction)
        if context is None:
            return
        _, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await ephemeral(interaction, "❌ Переименовывать тикет может только Staff.")
            return
        await interaction.response.send_modal(RenameTicketModal())

    async def rename(self, interaction: discord.Interaction, raw_name: str) -> None:
        context = await self.ticket_context(interaction)
        if context is None:
            return
        channel, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await interaction.followup.send("❌ Недостаточно прав.", ephemeral=True)
            return
        name = re.sub(r"[^a-z0-9-]", "-", raw_name.lower().replace("_", "-"))
        name = re.sub(r"-+", "-", name).strip("-")
        if not name:
            await interaction.followup.send("❌ Имя должно содержать латинские буквы или цифры.", ephemeral=True)
            return
        prefix = "closed-" if ticket["status"] == "closed" else ""
        await channel.edit(name=f"{prefix}{ticket['ticket_type']}-{name}"[:100], reason=f"Renamed by {member}")
        await interaction.followup.send(f"✅ Канал переименован: {channel.mention}", ephemeral=True)

    async def info(self, interaction: discord.Interaction) -> None:
        context = await self.ticket_context(interaction)
        if context is None:
            return
        _, ticket, member = context
        if member.id != int(ticket["owner_id"]) and not self.is_ticket_staff(member, ticket["ticket_type"]):
            await ephemeral(interaction, "❌ Недостаточно прав.")
            return
        await ephemeral(interaction, embed=await self.ticket_embed(ticket))

    async def transcript(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        context = await self.ticket_context(interaction)
        if context is None:
            return
        channel, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await interaction.followup.send("❌ Transcript доступен только Staff.", ephemeral=True)
            return
        lines = [
            f"LFR Ticket #{ticket['id']}",
            f"Type: {ticket['ticket_type']}",
            f"Owner: {ticket['owner_id']}",
            "",
        ]
        async for message in channel.history(limit=500, oldest_first=True):
            stamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            content = message.content or "[embed/system message]"
            lines.append(f"[{stamp}] {message.author} ({message.author.id}): {content}")
            for attachment in message.attachments:
                lines.append(f"  Attachment: {attachment.url}")
        data = "\n".join(lines).encode("utf-8")
        if len(data) > 7_500_000:
            data = data[-7_500_000:]
        filename = f"transcript-{ticket['ticket_type']}-{ticket['owner_id']}.txt"
        await interaction.followup.send(
            file=discord.File(io.BytesIO(data), filename=filename), ephemeral=True
        )

    async def delete_prompt(self, interaction: discord.Interaction) -> None:
        context = await self.ticket_context(interaction)
        if context is None:
            return
        _, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await ephemeral(interaction, "❌ Удалять тикеты может только Staff.")
            return
        embed = discord.Embed(
            title="🗑️ Удаление тикета",
            description="Это действие невозможно отменить.",
            color=ERROR_COLOR,
        )
        await ephemeral(interaction, embed=embed, view=ConfirmDeleteView(member.id))

    async def delete_confirmed(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        context = await self.ticket_context(interaction)
        if context is None:
            return
        channel, ticket, member = context
        if not self.is_ticket_staff(member, ticket["ticket_type"]):
            await interaction.followup.send("❌ Недостаточно прав.", ephemeral=True)
            return
        await self.log_event(channel.guild, "deleted", ticket, member, channel_id=channel.id)
        await self.bot.db.delete_ticket(int(ticket["id"]))
        await interaction.followup.send("✅ Тикет удаляется.", ephemeral=True)
        await channel.delete(reason=f"Ticket deleted by {member}")

    async def log_event(
        self,
        guild: discord.Guild,
        event: str,
        ticket: dict[str, Any],
        actor: discord.abc.User,
        *,
        channel_id: int | None = None,
    ) -> None:
        log_channel = discord.utils.get(guild.text_channels, name="🎫・ticket-logs")
        if log_channel is None:
            return
        titles = {
            "created": ("🎫 Ticket Created", SUCCESS_COLOR),
            "claimed": ("👤 Ticket Claimed", EMBED_COLOR),
            "closed": ("🔒 Ticket Closed", ERROR_COLOR),
            "reopened": ("🔓 Ticket Reopened", SUCCESS_COLOR),
            "deleted": ("🗑️ Ticket Deleted", ERROR_COLOR),
        }
        title, color = titles[event]
        category_names = SUPPORT_TYPES if ticket["ticket_type"] == "support" else REPORT_TYPES
        actual_channel_id = channel_id or int(ticket["channel_id"])
        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name="Type", value=ticket["ticket_type"].title(), inline=True)
        embed.add_field(name="Category", value=category_names.get(ticket["category"], ticket["category"]), inline=True)
        embed.add_field(name="User", value=f"<@{ticket['owner_id']}>\n`{ticket['owner_id']}`", inline=False)
        embed.add_field(name="Staff / Actor", value=f"{actor.mention}\n`{actor.id}`", inline=False)
        embed.add_field(name="Channel", value=f"<#{actual_channel_id}>\n`{actual_channel_id}`", inline=False)
        embed.set_footer(text=f"LFR Ticket System • ID: {ticket['id']}")
        try:
            await log_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as error:
            print(f"[TICKET LOG ERROR] {type(error).__name__}: {error}")


class TicketsCog(commands.Cog):
    ticket = app_commands.Group(name="ticket", description="Резервные команды тикетов")

    def __init__(self, bot: commands.Bot, service: TicketService) -> None:
        self.bot = bot
        self.service = service

    def admin_check(self, member: discord.Member) -> bool:
        return self.service.is_admin(member) or member.guild_permissions.manage_guild

    @commands.command(name="setup")
    @commands.guild_only()
    async def setup_prefix(self, ctx: commands.Context[Any]) -> None:
        if not isinstance(ctx.author, discord.Member) or not self.admin_check(ctx.author):
            await ctx.send("❌ Недостаточно прав.", delete_after=8)
            return
        status = await ctx.send("⏳ Настраиваю LFR Ticket System…")
        try:
            await self.service.setup_guild(ctx.guild)
        except (discord.Forbidden, discord.HTTPException) as error:
            await status.edit(content=f"❌ Ошибка Discord: `{type(error).__name__}`. Проверь права бота.")
            return
        await status.edit(content="✅ SUPPORT и REPORT готовы.")

    @app_commands.command(name="ticket-setup", description="Создать панели и каналы тикетов")
    @app_commands.guild_only()
    async def setup_slash(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not self.admin_check(interaction.user):
            await interaction.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            support, report = await self.service.setup_guild(interaction.guild)
        except (discord.Forbidden, discord.HTTPException) as error:
            await interaction.followup.send(
                f"❌ Ошибка Discord: `{type(error).__name__}`. Проверь права бота.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ Панели обновлены: {support.mention} и {report.mention}.", ephemeral=True
        )

    @ticket.command(name="claim", description="Закрепить текущий тикет за собой")
    @app_commands.guild_only()
    async def ticket_claim(self, interaction: discord.Interaction) -> None:
        await self.service.claim(interaction)

    @ticket.command(name="close", description="Закрыть текущий тикет")
    @app_commands.guild_only()
    async def ticket_close(self, interaction: discord.Interaction) -> None:
        await self.service.close_prompt(interaction)

    @ticket.command(name="add", description="Добавить пользователя в текущий тикет")
    @app_commands.describe(user="Участник сервера")
    @app_commands.guild_only()
    async def ticket_add(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.service.add_user(interaction, str(user.id))

    @ticket.command(name="remove", description="Удалить пользователя из текущего тикета")
    @app_commands.describe(user="Участник сервера")
    @app_commands.guild_only()
    async def ticket_remove(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.service.remove_user(interaction, str(user.id))

    @ticket.command(name="rename", description="Переименовать текущий тикет")
    @app_commands.describe(name="Новое имя без префикса")
    @app_commands.guild_only()
    async def ticket_rename(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.service.rename(interaction, name)

    @ticket.command(name="transcript", description="Получить transcript текущего тикета")
    @app_commands.guild_only()
    async def ticket_transcript(self, interaction: discord.Interaction) -> None:
        await self.service.transcript(interaction)


async def setup(bot: commands.Bot) -> None:
    service = TicketService(bot)
    bot.ticket_service = service
    bot.add_view(SupportPanelView())
    bot.add_view(ReportPanelView())
    bot.add_view(TicketControlsView())
    bot.add_view(ClosedTicketView())
    await bot.add_cog(TicketsCog(bot, service))
