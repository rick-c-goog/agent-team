"""Telegram surface — gateway + client adapters (DESIGN.md §2, §3.1)."""

from .client import TelegramClient, MockTelegramClient, Button
from .gateway import Gateway, Update, Callback

__all__ = ["TelegramClient", "MockTelegramClient", "Button", "Gateway", "Update", "Callback"]
