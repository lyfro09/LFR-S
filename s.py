"""Entry point for LFR Spam."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Local modules are imported after dotenv so role ID sets can be configured via
# ADMIN_USER_IDS and MOD_USER_IDS environment variables.
import config  # noqa: E402
from database import Database  # noqa: E402


TOKEN = os.getenv("SPAM_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "SPAM_TOKEN не найден. Добавь SPAM_TOKEN в .env "
        "или переменные окружения хостинга."
    )


def load_messages() -> list[str]:
    if not config.MESSAGES_FILE.exists():
        raise RuntimeError(
            "messages.txt не найден. Создай его рядом с s.py: одна строка — "
            "одно сообщение."
        )
    messages = [
        line.strip()
        for line in config.MESSAGES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not messages:
        raise RuntimeError("messages.txt пуст.")
    return messages


def load_message_packs(default_messages: list[str]) -> dict[str, list[str]]:
    packs = {"random": default_messages}
    for mode in ("duplicate", "unicode", "caps", "long"):
        path = config.MESSAGES_DIR / f"{mode}.txt"
        if not path.exists():
            continue
        messages = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if messages:
            packs[mode] = messages
    return packs


EXTENSIONS = (
    "cogs.spam",
    "cogs.profile",
    "cogs.economy",
    "cogs.vip",
    "cogs.payments",
    "cogs.admin",
    "cogs.general",
)


class LFRClient(commands.Bot):
    db: Database
    loaded_messages: list[str]
    message_packs: dict[str, list[str]]

    def __init__(self) -> None:
        intents = discord.Intents.default()
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="LFR Community",
        )
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            activity=activity,
            allowed_installs=app_commands.AppInstallationType(
                guild=False,
                user=True,
            ),
            allowed_contexts=app_commands.AppCommandContext(
                guild=True,
                dm_channel=True,
                private_channel=True,
            ),
        )
        self.db = Database(config.DATABASE_PATH)
        self.loaded_messages = load_messages()
        self.message_packs = load_message_packs(self.loaded_messages)

    async def setup_hook(self) -> None:
        await self.db.initialize()
        for owner_id in config.OWNER_USER_IDS:
            await self.db.set_permanent_vip(
                owner_id, config.PERMANENT_VIP_UNTIL
            )
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            print(f"[COG] loaded {extension}")
        synced = await self.tree.sync()
        print(f"[SYNC] {len(synced)} commands synced")

    async def on_ready(self) -> None:
        if self.user is None:
            return
        print("=" * 60)
        print(f"LFR SPAM ONLINE: {self.user}")
        print(f"BOT ID: {self.user.id}")
        print(f"MESSAGES LOADED: {len(self.loaded_messages)}")
        print(f"DATABASE: {config.DATABASE_PATH}")
        print("=" * 60)


client = LFRClient()


@client.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    original = getattr(error, "original", error)
    print(f"[COMMAND ERROR] {type(original).__name__}: {original}")

    if isinstance(original, sqlite3.Error):
        message = "❌ Ошибка базы данных. Попробуй позже."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"⏳ Попробуй снова через {error.retry_after:.1f} сек."
    elif isinstance(error, app_commands.TransformerError):
        message = "❌ Указаны неверные значения параметров команды."
    else:
        message = "❌ Не удалось выполнить команду. Попробуй ещё раз."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        pass


def main() -> None:
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
