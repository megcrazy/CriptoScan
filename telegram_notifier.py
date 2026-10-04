"""Envio opcional de alertas para o Telegram Bot API."""
from __future__ import annotations

import asyncio
import os

import httpx


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.enabled = bool(self.token and self.chat_id)
        self.url = f"https://api.telegram.org/bot{self.token}/sendMessage" if self.token else ""

    async def send_text(self, text: str, silent: bool = False) -> bool:
        """Envia HTML. silent=True chega sem som. Respeita o 429 (retry_after) até 3 tentativas.
        Sem credenciais retorna False e não interrompe o scanner."""
        if not self.enabled:
            return False
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        for _ in range(3):
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(self.url, json=payload)
            if r.status_code == 429:
                espera = 5
                try:
                    espera = int(r.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    pass
                await asyncio.sleep(min(espera, 30) + 0.5)
                continue
            r.raise_for_status()
            return True
        return False
