from __future__ import annotations

import asyncio
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.utils import EMBED_COLOR, ERROR_COLOR, OwnerView, is_vip, send_ephemeral


SpamMode = Literal["random", "duplicate", "unicode", "caps", "long", "mixed"]

MODE_LABELS = {
    "random": "Random",
    "duplicate": "Duplicate",
    "unicode": "Unicode",
    "caps": "CAPS",
    "long": "Long",
    "mixed": "Mixed",
}

STATUS_LABELS = {
    "running": "🟢 Выполняется",
    "completed": "✅ Completed",
    "failed": "❌ Failed",
    "stopped": "⏹️ Stopped",
}

BUILTIN_UNICODE_MESSAGES = [
    "LFR Unicode: Кириллица — сообщение",
    "LFR Unicode: Ελληνικά — μήνυμα",
    "LFR Unicode: 日本語 — メッセージ",
    "LFR Unicode: 한국어 — 메시지",
    "LFR Unicode: символы ◇ ◆ ○ ● △ ▲",
]

BUILTIN_CAPS_MESSAGES = [
    "LFR CAPS MESSAGE",
    "ANTISPAM CAPS MESSAGE",
    "CONTROLLED SPAM MESSAGE",
    "CAPITAL LETTERS MESSAGE",
    "LFR COMMUNITY MESSAGE",
]

BUILTIN_LONG_MESSAGES = [
    "LFR LONG • " + ("controlled anti-spam message " * 12).strip(),
    "LFR LONG • " + ("payload validation message " * 14).strip(),
    "LFR LONG • " + ("message length sequence " * 16).strip(),
]


@dataclass(slots=True)
class SpamSession:
    history_id: int
    user_id: int
    started_at: int
    started_monotonic: float
    requested_messages: int
    delay: float
    mode: str
    sent_messages: int = 0
    failed_messages: int = 0
    status: str = "running"
    finished_at: int | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)


class SpamView(OwnerView):
    def __init__(
        self,
        cog: SpamCog,
        owner_id: int,
        count: int,
        delay: float,
        mode: str,
    ) -> None:
        super().__init__(owner_id, timeout=600)
        self.cog = cog
        self.count = count
        self.delay = delay
        self.mode = mode
        self.running = False

    @discord.ui.button(
        label="Старт",
        emoji="🧪",
        style=discord.ButtonStyle.primary,
    )
    async def start_spam(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.running:
            await send_ephemeral(interaction, "⚠️ Отправка уже выполняется")
            return

        # There is no await between reading and assigning the state, so rapid
        # double clicks on this panel cannot start two callback loops.
        self.running = True
        try:
            # A deferred message update acknowledges the click without putting
            # the ephemeral panel into a long "thinking" state. The unchanged
            # Start button therefore remains clickable while messages are sent.
            await interaction.response.defer()
            session = await self.cog.begin_session(
                interaction.user.id,
                self.count,
                self.delay,
                self.mode,
            )
            if session is None:
                await interaction.followup.send(
                    "⚠️ Отправка уже выполняется", ephemeral=True
                )
                return
            await self.cog.execute_session(interaction, session)
        finally:
            self.running = False

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.label = "Истекло"
        await super().on_timeout()


class SpamCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.active_sessions: dict[int, SpamSession] = {}
        self._session_lock = asyncio.Lock()

    def _mode_pack(self, mode: str) -> list[str]:
        packs = self.bot.message_packs
        random_pack = packs["random"]
        if mode in {"random", "duplicate"}:
            return packs.get(mode, random_pack)
        if mode == "unicode":
            return packs.get(
                mode,
                [
                    message
                    for message in random_pack
                    if any(ord(character) > 127 for character in message)
                ]
                or BUILTIN_UNICODE_MESSAGES,
            )
        if mode == "caps":
            return packs.get(mode, BUILTIN_CAPS_MESSAGES)
        if mode == "long":
            return packs.get(mode, BUILTIN_LONG_MESSAGES)
        if mode == "mixed":
            combined = (
                random_pack
                + self._mode_pack("unicode")
                + self._mode_pack("caps")
                + self._mode_pack("long")
            )
            return list(dict.fromkeys(combined))
        raise ValueError("Unsupported spam mode")

    def available_messages(self, mode: str) -> int:
        return len(self._mode_pack(mode))

    @staticmethod
    def _neutralize_mentions(message: str) -> str:
        return re.sub(
            r"@(everyone|here)",
            lambda match: "@\u200b" + match.group(1),
            message,
            flags=re.IGNORECASE,
        )

    def select_messages(self, mode: str, count: int) -> list[str]:
        pack = self._mode_pack(mode)
        if not pack:
            raise RuntimeError("Message pack is empty")
        if mode == "duplicate":
            selected = [random.choice(pack)] * count
        elif len(pack) >= count:
            selected = random.sample(pack, count)
        else:
            selected = [random.choice(pack) for _ in range(count)]
        return [self._neutralize_mentions(message) for message in selected]

    async def begin_session(
        self,
        user_id: int,
        requested_messages: int,
        delay: float,
        mode: str,
    ) -> SpamSession | None:
        async with self._session_lock:
            if user_id in self.active_sessions:
                return None
            started_at = int(time.time())
            history_id = await self.bot.db.create_spam_session(
                user_id,
                started_at,
                requested_messages,
                delay,
                mode,
            )
            session = SpamSession(
                history_id=history_id,
                user_id=user_id,
                started_at=started_at,
                started_monotonic=time.monotonic(),
                requested_messages=requested_messages,
                delay=delay,
                mode=mode,
            )
            self.active_sessions[user_id] = session
            return session

    async def _release_session(self, session: SpamSession) -> None:
        async with self._session_lock:
            if self.active_sessions.get(session.user_id) is session:
                self.active_sessions.pop(session.user_id, None)

    async def execute_session(
        self, interaction: discord.Interaction, session: SpamSession
    ) -> None:
        error_message: str | None = None
        database_error = False
        cancelled = False
        try:
            selected_messages = self.select_messages(
                session.mode, session.requested_messages
            )
            for number, message_text in enumerate(selected_messages, start=1):
                if session.stop_event.is_set():
                    session.status = "stopped"
                    break
                try:
                    await interaction.followup.send(
                        message_text,
                        ephemeral=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                        suppress_embeds=True,
                    )
                    session.sent_messages += 1
                except discord.Forbidden:
                    session.status = "failed"
                    error_message = "Discord запретил отправку в этом канале."
                    break
                except discord.NotFound:
                    session.status = "failed"
                    error_message = "Канал или interaction больше недоступен."
                    break
                except discord.HTTPException as error:
                    session.status = "failed"
                    error_message = f"Ошибка Discord при отправке ({error.status})."
                    break

                if number < session.requested_messages:
                    try:
                        await asyncio.wait_for(
                            session.stop_event.wait(), timeout=session.delay
                        )
                    except TimeoutError:
                        pass
                    if session.stop_event.is_set():
                        session.status = "stopped"
                        break

            if session.status == "running":
                session.status = "completed"
        except asyncio.CancelledError:
            session.status = "stopped"
            cancelled = True
        except Exception as error:
            session.status = "failed"
            error_message = f"Внутренняя ошибка: {type(error).__name__}."
            print(f"[SPAM SESSION ERROR] {type(error).__name__}: {error}")
        finally:
            session.finished_at = int(time.time())
            session.failed_messages = max(
                0, session.requested_messages - session.sent_messages
            )
            reward = config.SPAM_REWARD if session.sent_messages > 0 else 0
            try:
                await self.bot.db.finish_spam_session(
                    session.history_id,
                    session.user_id,
                    session.finished_at,
                    session.sent_messages,
                    session.failed_messages,
                    session.status,
                    reward,
                )
            except sqlite3.Error as error:
                database_error = True
                print(f"[SPAM DATABASE ERROR] {type(error).__name__}: {error}")
            finally:
                await self._release_session(session)

        print(
            f"[SPAM RUN] id={session.history_id} user={session.user_id} "
            f"status={session.status} sent={session.sent_messages}/"
            f"{session.requested_messages} delay={session.delay} mode={session.mode}"
        )

        if cancelled:
            raise asyncio.CancelledError

        if session.status == "completed":
            summary = (
                f"✅ Запуск #{session.history_id} завершён: отправлено "
                f"**{session.sent_messages}/{session.requested_messages}** сообщений."
            )
        elif session.status == "stopped":
            summary = (
                f"⏹️ Запуск #{session.history_id} остановлен: отправлено "
                f"**{session.sent_messages}/{session.requested_messages}** сообщений."
            )
        else:
            summary = (
                f"❌ Запуск #{session.history_id} завершён с ошибкой: отправлено "
                f"**{session.sent_messages}/{session.requested_messages}** сообщений."
            )
        if session.sent_messages > 0 and not database_error:
            summary += f" Получено **+{config.SPAM_REWARD} points**."
        if error_message:
            summary += f"\n⚠️ {error_message}"
        if database_error:
            summary += "\n⚠️ Не удалось сохранить статистику SQLite."
        try:
            await interaction.followup.send(summary, ephemeral=True)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    @app_commands.command(
        name="spam", description="Отправить серию антиспам-сообщений"
    )
    @app_commands.describe(
        count="Количество сообщений (Free: до 5, VIP: до 20)",
        delay="Задержка между сообщениями в секундах",
        mode="Режим сообщений",
    )
    async def spam(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, config.VIP_MAX_MESSAGES] = 5,
        delay: app_commands.Range[
            float, config.VIP_MIN_DELAY, config.MAX_DELAY
        ] = 0.5,
        mode: SpamMode = "random",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        user = await self.bot.db.get_user(interaction.user.id)
        vip = is_vip(user)
        max_messages = config.VIP_MAX_MESSAGES if vip else config.FREE_MAX_MESSAGES
        min_delay = config.VIP_MIN_DELAY if vip else config.FREE_MIN_DELAY

        if int(count) > max_messages:
            embed = discord.Embed(
                title="❌ Превышен лимит",
                description=(
                    f"Для статуса **{'VIP' if vip else 'Free'}** доступно не более "
                    f"**{max_messages}** сообщений за запуск."
                ),
                color=ERROR_COLOR,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if float(delay) < min_delay:
            embed = discord.Embed(
                title="❌ Слишком маленькая задержка",
                description=(
                    f"Для статуса **{'VIP' if vip else 'Free'}** минимальная "
                    f"задержка — **{min_delay:.1f}s**."
                ),
                color=ERROR_COLOR,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(title="🧪 LFR Spam", color=EMBED_COLOR)
        embed.add_field(name="Сообщений", value=f"`{int(count)}`", inline=True)
        embed.add_field(name="Задержка", value=f"`{float(delay):.2f}s`", inline=True)
        embed.add_field(name="Статус", value="💎 VIP" if vip else "Free", inline=True)
        embed.add_field(name="Лимит", value=f"`{max_messages}`", inline=True)
        embed.add_field(
            name="Режим", value=f"`{MODE_LABELS[str(mode)]}`", inline=True
        )
        embed.add_field(
            name="Загружено сообщений",
            value=f"`{self.available_messages(str(mode))}`",
            inline=True,
        )
        embed.set_footer(text="Панель активна 10 минут • LFR Anti-Spam")
        view = SpamView(
            self, interaction.user.id, int(count), float(delay), str(mode)
        )
        view.message = await interaction.followup.send(
            embed=embed, view=view, ephemeral=True, wait=True
        )

    @app_commands.command(
        name="history", description="Показать последние запуски пользователя"
    )
    async def history(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await self.bot.db.get_spam_history(interaction.user.id, 10)
        if not rows:
            description = "История запусков пока пуста."
        else:
            blocks = []
            for row in rows:
                blocks.append(
                    f"**#{row['id']} • {MODE_LABELS.get(row['mode'], row['mode'].title())}**\n"
                    f"{row['sent_messages']}/{row['requested_messages']} messages • "
                    f"{row['delay']:.2f}s\n"
                    f"{STATUS_LABELS.get(row['status'], row['status'].title())}"
                )
            description = "\n\n".join(blocks)
        embed = discord.Embed(
            title="🕒 История запусков", description=description, color=EMBED_COLOR
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="status", description="Показать активную отправку")
    async def status(self, interaction: discord.Interaction) -> None:
        session = self.active_sessions.get(interaction.user.id)
        if session is None or session.status != "running":
            await interaction.response.send_message(
                "⚪ Активных отправок нет", ephemeral=True
            )
            return
        embed = discord.Embed(title="🟢 Активная отправка", color=EMBED_COLOR)
        embed.add_field(
            name="Отправлено",
            value=f"`{session.sent_messages}/{session.requested_messages}`",
            inline=True,
        )
        embed.add_field(name="Delay", value=f"`{session.delay:.2f}s`", inline=True)
        embed.add_field(
            name="Elapsed", value=f"`{session.elapsed:.1f}s`", inline=True
        )
        embed.add_field(
            name="Режим", value=f"`{MODE_LABELS[session.mode]}`", inline=True
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stop", description="Остановить свою активную отправку")
    async def stop(self, interaction: discord.Interaction) -> None:
        session = self.active_sessions.get(interaction.user.id)
        if session is None or session.status != "running":
            await interaction.response.send_message(
                "⚪ Активных отправок нет", ephemeral=True
            )
            return
        session.stop_event.set()
        await interaction.response.send_message(
            f"⏹️ Остановка запуска #{session.history_id} запрошена.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SpamCog(bot))
