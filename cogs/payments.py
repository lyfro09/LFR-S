from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.utils import ERROR_COLOR, VIP_COLOR, OwnerView, discord_timestamp


ASSET = "USDT"


class CryptoPayError(RuntimeError):
    pass


class CryptoPayClient:
    def __init__(self, token: str, session: aiohttp.ClientSession, testnet: bool) -> None:
        self.token = token
        self.session = session
        host = "testnet-pay.crypt.bot" if testnet else "pay.crypt.bot"
        self.base_url = f"https://{host}/api"

    async def request(self, method: str, data: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{method}"
        headers = {"Crypto-Pay-API-Token": self.token}
        try:
            async with self.session.post(
                url,
                json=data or {},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            raise CryptoPayError(f"Crypto Pay connection error: {type(error).__name__}") from error

        if response.status != 200 or not payload.get("ok"):
            error_data = payload.get("error")
            api_error = (
                error_data.get("name", "UNKNOWN_API_ERROR")
                if isinstance(error_data, dict)
                else str(error_data or "UNKNOWN_API_ERROR")
            )
            raise CryptoPayError(f"Crypto Pay API error: {api_error}")
        return payload.get("result")

    async def create_invoice(
        self, *, amount: str, description: str, payload: str
    ) -> dict[str, Any]:
        result = await self.request(
            "createInvoice",
            {
                "currency_type": "crypto",
                "asset": ASSET,
                "amount": amount,
                "description": description,
                "payload": payload,
                "allow_comments": False,
                "allow_anonymous": True,
                "expires_in": 3600,
            },
        )
        if not isinstance(result, dict):
            raise CryptoPayError("Crypto Pay returned an invalid invoice")
        return result

    async def get_invoices(self, invoice_ids: list[int]) -> list[dict[str, Any]]:
        if not invoice_ids:
            return []
        result = await self.request(
            "getInvoices",
            {
                "invoice_ids": ",".join(str(invoice_id) for invoice_id in invoice_ids),
                "count": len(invoice_ids),
            },
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        return [item for item in items if isinstance(item, dict)]

    async def delete_invoice(self, invoice_id: int) -> None:
        try:
            await self.request("deleteInvoice", {"invoice_id": invoice_id})
        except CryptoPayError:
            pass


class InvoiceLinkView(discord.ui.View):
    def __init__(self, pay_url: str) -> None:
        super().__init__(timeout=3600)
        self.add_item(
            discord.ui.Button(
                label="Оплатить в Crypto Bot",
                emoji="💳",
                style=discord.ButtonStyle.link,
                url=pay_url,
            )
        )


class VipPurchaseView(OwnerView):
    def __init__(self, cog: PaymentsCog, owner_id: int) -> None:
        super().__init__(owner_id, timeout=600)
        self.cog = cog

    async def create_invoice(
        self, interaction: discord.Interaction, plan_days: int
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payment = await self.cog.create_or_get_invoice(
                interaction.user.id, plan_days
            )
        except CryptoPayError as error:
            print(f"[CRYPTO PAY ERROR] {error}")
            await interaction.followup.send(
                "❌ Crypto Pay временно недоступен. Попробуй позже.",
                ephemeral=True,
            )
            return
        except sqlite3.Error as error:
            print(f"[PAYMENT DATABASE ERROR] {type(error).__name__}: {error}")
            await interaction.followup.send(
                "❌ Не удалось сохранить счёт. Попробуй позже.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="💎 Оплата LFR VIP",
            description=(
                f"Тариф: **{plan_days} дн. VIP**\n"
                f"К оплате: **{payment['amount']} {payment['asset']}**\n\n"
                "После оплаты VIP будет начислен автоматически. "
                "Обычно это занимает до 15 секунд."
            ),
            color=VIP_COLOR,
        )
        if payment.get("status") == "paid":
            embed.title = "✅ VIP активирован"
            embed.description = (
                "Оплата подтверждена. VIP действует до "
                f"{discord_timestamp(payment['vip_until'])}."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        embed.add_field(
            name="Invoice ID", value=f"`{payment['invoice_id']}`", inline=False
        )
        embed.set_footer(text="Счёт действует 1 час • Не оплачивай один счёт дважды")
        await interaction.followup.send(
            embed=embed,
            view=InvoiceLinkView(payment["pay_url"]),
            ephemeral=True,
        )

    @discord.ui.button(label="1 день • 1 USDT", emoji="💎", style=discord.ButtonStyle.primary)
    async def one_day(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self.create_invoice(interaction, 1)

    @discord.ui.button(label="7 дней • 6 USDT", emoji="💎", style=discord.ButtonStyle.primary)
    async def seven_days(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self.create_invoice(interaction, 7)

    @discord.ui.button(label="30 дней • 25 USDT", emoji="💎", style=discord.ButtonStyle.success)
    async def thirty_days(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self.create_invoice(interaction, 30)


class PaymentsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.token = os.getenv("CRYPTO_PAY_TOKEN", "").strip()
        self.testnet = os.getenv("CRYPTO_PAY_TESTNET", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.http_session: aiohttp.ClientSession | None = None
        self.crypto: CryptoPayClient | None = None
        self.invoice_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last_watcher_error = 0.0

    async def cog_load(self) -> None:
        if not self.token:
            print("[CRYPTO PAY] Disabled: CRYPTO_PAY_TOKEN is not configured")
            return
        self.http_session = aiohttp.ClientSession()
        self.crypto = CryptoPayClient(self.token, self.http_session, self.testnet)
        self.payment_watcher.start()
        network = "testnet" if self.testnet else "mainnet"
        print(f"[CRYPTO PAY] Enabled: {network}")

    async def cog_unload(self) -> None:
        if self.payment_watcher.is_running():
            self.payment_watcher.cancel()
        if self.http_session is not None:
            await self.http_session.close()

    def ensure_enabled(self) -> CryptoPayClient:
        if self.crypto is None:
            raise CryptoPayError("CRYPTO_PAY_TOKEN is not configured")
        return self.crypto

    @staticmethod
    def payload_for(user_id: int, plan_days: int) -> str:
        return json.dumps(
            {"discord_user_id": user_id, "plan_days": plan_days},
            separators=(",", ":"),
        )

    async def create_or_get_invoice(
        self, user_id: int, plan_days: int
    ) -> dict[str, Any]:
        crypto = self.ensure_enabled()
        if plan_days not in config.CRYPTO_VIP_PLANS:
            raise CryptoPayError("Unknown VIP plan")

        async with self.invoice_locks[user_id]:
            existing = await self.bot.db.get_active_crypto_payment(
                user_id, plan_days
            )
            if existing is not None:
                activated = await self.refresh_payments([existing])
                if activated:
                    user = await self.bot.db.get_user(user_id)
                    return {
                        "status": "paid",
                        "vip_until": int(user["vip_until"]),
                        "amount": existing["amount"],
                        "asset": existing["asset"],
                        "invoice_id": existing["invoice_id"],
                    }
                existing = await self.bot.db.get_active_crypto_payment(
                    user_id, plan_days
                )
                if existing is not None:
                    return existing

            amount = config.CRYPTO_VIP_PLANS[plan_days]
            payload = self.payload_for(user_id, plan_days)
            invoice = await crypto.create_invoice(
                amount=amount,
                description=f"LFR VIP на {plan_days} дн. • Discord ID {user_id}",
                payload=payload,
            )
            invoice_id = int(invoice["invoice_id"])
            pay_url = (
                invoice.get("bot_invoice_url")
                or invoice.get("mini_app_invoice_url")
                or invoice.get("web_app_invoice_url")
            )
            if not pay_url:
                await crypto.delete_invoice(invoice_id)
                raise CryptoPayError("Invoice has no payment URL")

            try:
                await self.bot.db.create_crypto_payment(
                    invoice_id=invoice_id,
                    user_id=user_id,
                    plan_days=plan_days,
                    amount=amount,
                    asset=ASSET,
                    pay_url=pay_url,
                    created_at=int(time.time()),
                )
            except sqlite3.Error:
                await crypto.delete_invoice(invoice_id)
                raise

            return {
                "invoice_id": invoice_id,
                "user_id": user_id,
                "plan_days": plan_days,
                "amount": amount,
                "asset": ASSET,
                "pay_url": pay_url,
                "status": "active",
            }

    async def validate_and_activate(
        self, payment: dict[str, Any], invoice: dict[str, Any]
    ) -> bool:
        invoice_id = int(payment["invoice_id"])
        status = invoice.get("status")
        if status == "expired":
            await self.bot.db.set_crypto_payment_status(invoice_id, "expired")
            return False
        if status != "paid":
            return False

        try:
            amount_matches = Decimal(str(invoice.get("amount"))) == Decimal(
                payment["amount"]
            )
            payload = json.loads(invoice.get("payload") or "{}")
            valid = (
                int(invoice.get("invoice_id", 0)) == invoice_id
                and invoice.get("asset") == payment["asset"]
                and amount_matches
                and int(payload.get("discord_user_id", 0))
                == int(payment["user_id"])
                and int(payload.get("plan_days", 0))
                == int(payment["plan_days"])
            )
        except (
            InvalidOperation,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            valid = False
        if not valid:
            await self.bot.db.set_crypto_payment_status(invoice_id, "invalid")
            print(f"[CRYPTO PAY] Rejected invoice {invoice_id}: validation mismatch")
            return False

        activated, vip_until = await self.bot.db.activate_crypto_payment(
            invoice_id,
            expected_user_id=int(payment["user_id"]),
            expected_plan_days=int(payment["plan_days"]),
            expected_amount=payment["amount"],
            expected_asset=payment["asset"],
        )
        if not activated or vip_until is None:
            return False

        print(
            f"[CRYPTO PAY] Paid invoice={invoice_id} user={payment['user_id']} "
            f"plan={payment['plan_days']}d"
        )
        await self.notify_user(
            int(payment["user_id"]), int(payment["plan_days"]), vip_until
        )
        return True

    async def refresh_payments(self, payments: list[dict[str, Any]]) -> int:
        crypto = self.ensure_enabled()
        activated = 0
        for offset in range(0, len(payments), 100):
            batch = payments[offset : offset + 100]
            invoices = await crypto.get_invoices(
                [int(payment["invoice_id"]) for payment in batch]
            )
            payment_by_id = {
                int(payment["invoice_id"]): payment for payment in batch
            }
            for invoice in invoices:
                payment = payment_by_id.get(int(invoice.get("invoice_id", 0)))
                if payment and await self.validate_and_activate(payment, invoice):
                    activated += 1
        return activated

    async def notify_user(self, user_id: int, plan_days: int, vip_until: int) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            embed = discord.Embed(
                title="✅ VIP автоматически активирован",
                description=(
                    f"Оплата получена. Добавлено **{plan_days} дн. VIP**.\n"
                    f"VIP действует до {discord_timestamp(vip_until)}."
                ),
                color=VIP_COLOR,
            )
            await user.send(embed=embed)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    @tasks.loop(seconds=config.CRYPTO_PAY_POLL_SECONDS)
    async def payment_watcher(self) -> None:
        try:
            payments = await self.bot.db.get_active_crypto_payments()
            if payments:
                await self.refresh_payments(payments)
        except (CryptoPayError, sqlite3.Error) as error:
            now = time.monotonic()
            if now - self.last_watcher_error >= 300:
                self.last_watcher_error = now
                print(
                    f"[CRYPTO PAY WATCHER ERROR] "
                    f"{type(error).__name__}: {error}"
                )

    @payment_watcher.before_loop
    async def before_payment_watcher(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="buyvip", description="Купить LFR VIP за USDT")
    async def buyvip(self, interaction: discord.Interaction) -> None:
        if self.crypto is None:
            await interaction.response.send_message(
                "❌ Автоматическая оплата пока не настроена.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="💎 Купить LFR VIP",
            description=(
                "Выбери срок VIP. После оплаты Crypto Pay автоматически "
                "обновит твой статус."
            ),
            color=VIP_COLOR,
        )
        embed.add_field(name="1 день", value="`1 USDT`", inline=True)
        embed.add_field(name="7 дней", value="`6 USDT`", inline=True)
        embed.add_field(name="30 дней", value="`25 USDT`", inline=True)
        if self.testnet:
            embed.add_field(
                name="⚠️ Демо-режим",
                value="Реальные деньги не используются.",
                inline=False,
            )
        embed.set_footer(text="Оплата обрабатывается через Telegram Crypto Bot")
        view = VipPurchaseView(self, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(
        name="paymentstatus",
        description="Обновить статус оплаты VIP",
    )
    async def paymentstatus(self, interaction: discord.Interaction) -> None:
        if self.crypto is None:
            await interaction.response.send_message(
                "❌ Автоматическая оплата пока не настроена.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        payments = []
        for plan_days in config.CRYPTO_VIP_PLANS:
            payment = await self.bot.db.get_active_crypto_payment(
                interaction.user.id, plan_days
            )
            if payment:
                payments.append(payment)
        try:
            activated = await self.refresh_payments(payments)
        except CryptoPayError as error:
            print(f"[CRYPTO PAY ERROR] {error}")
            await interaction.followup.send(
                "❌ Не удалось получить статус Crypto Pay.", ephemeral=True
            )
            return

        user = await self.bot.db.get_user(interaction.user.id)
        if activated:
            message = f"✅ Оплата подтверждена. VIP до {discord_timestamp(user['vip_until'])}."
        elif int(user["vip_until"]) > int(time.time()):
            message = f"💎 VIP активен до {discord_timestamp(user['vip_until'])}."
        elif payments:
            message = "⏳ Оплата пока не поступила. Попробуй снова через несколько секунд."
        else:
            message = "⚪ Активных счетов и действующего VIP нет. Используй `/buyvip`."
        await interaction.followup.send(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PaymentsCog(bot))
