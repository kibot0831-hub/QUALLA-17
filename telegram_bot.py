"""Envío de mensajes a Telegram (HTML) con reintentos, límite de longitud y manejo de 429."""
import asyncio
import logging

import aiohttp

log = logging.getLogger("telegram")

MAX_LEN = 4000


def _split(text):
    if len(text) <= MAX_LEN:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > MAX_LEN:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


class Telegram:
    def __init__(self, token, chat_id, thread_id=""):
        self.token = token
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.enabled = bool(token and chat_id)
        self.session = None

    async def start(self):
        if self.enabled:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        else:
            log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID sin configurar: los mensajes solo se verán en los logs.")

    async def close(self):
        if self.session:
            await self.session.close()

    async def send(self, text):
        if not self.enabled:
            log.info("[Telegram desactivado]\n%s", text)
            return False
        ok = True
        for chunk in _split(text):
            ok = (await self._post(chunk)) and ok
            await asyncio.sleep(0.35)
        return ok

    async def _post(self, text):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if self.thread_id:
            try:
                payload["message_thread_id"] = int(self.thread_id)
            except ValueError:
                pass
        for attempt in range(4):
            try:
                async with self.session.post(url, json=payload) as r:
                    data = await r.json(content_type=None)
                    if r.status == 200 and data.get("ok"):
                        return True
                    if r.status == 429:
                        wait = int(data.get("parameters", {}).get("retry_after", 5))
                        log.warning("Telegram 429, esperando %ss", wait)
                        await asyncio.sleep(wait + 1)
                        continue
                    if r.status == 400 and "parse_mode" in payload:
                        log.warning("Telegram rechazó el HTML (%s); reenvío en texto plano", data.get("description"))
                        payload.pop("parse_mode", None)
                        continue
                    log.error("Telegram error %s: %s", r.status, data)
                    return False
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Telegram red: %s", e)
                await asyncio.sleep(2 * (attempt + 1))
        return False
