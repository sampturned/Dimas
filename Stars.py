import os
import json
import asyncio
import re
import hashlib
import logging
import time
from pathlib import Path
from typing import Optional, Dict, List

import aiohttp
from playwright.async_api import async_playwright, BrowserContext, Page
from playwright_stealth import stealth_async
from colorama import Fore, Style

# --- Конфигурация ---
CHECK_INTERVAL = 2        # секунды между проверками на странице чата
CHAT_SYNC_INTERVAL = 30   # секунды между синхронизацией списка чатов
MAX_CHATS = 20            # число одновременно мониторящих чатов
API_RETRY_ATTEMPTS = 3    # попыток при ошибках API
API_BACKOFF_FACTOR = 2    # коэффициент экспоненциальной задержки

# --- Пути к файлам ---
BASE_DIR = Path(__file__).parent
LAST_MESSAGES_DIR = BASE_DIR / "last_messages"
SETTINGS_FILE = BASE_DIR / "settings.json"
COOKIES_FILE = BASE_DIR / "cookies.json"
BUYER_STATE_FILE = BASE_DIR / "buyer_states.json"
CHAT_STATES_FILE = BASE_DIR / "chat_states.json"
LOG_FILE = BASE_DIR / "playerok_errors.log"

# --- Логирование ошибок ---
logging.basicConfig(
    filename=str(LOG_FILE),
    filemode="a",
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.ERROR,
    encoding="utf-8"
)

def log_error(e: Exception, context: Optional[str] = None) -> None:
    msg = f"[{context}] {e!r}" if context else repr(e)
    logging.error(msg)

# --- Менеджер файлов состояния ---
class FileManager:
    def __init__(self):
        self.last_dir = LAST_MESSAGES_DIR
        self.state_file = BUYER_STATE_FILE
        self.chat_states_file = CHAT_STATES_FILE
        self.last_dir.mkdir(exist_ok=True)
        for f in (self.state_file, self.chat_states_file):
            if not f.exists():
                f.write_text("{}", encoding='utf-8')
        self.buyer_states = json.loads(self.state_file.read_text(encoding='utf-8'))
        self.chat_states = json.loads(self.chat_states_file.read_text(encoding='utf-8'))

    def save_states(self):
        self.state_file.write_text(
            json.dumps(self.buyer_states, ensure_ascii=False, indent=2), encoding='utf-8'
        )

    def save_chat_states(self):
        self.chat_states_file.write_text(
            json.dumps(self.chat_states, ensure_ascii=False, indent=2), encoding='utf-8'
        )

    def get_last(self, buyer: str, msg_type: str) -> Optional[str]:
        path = self.last_dir / f"last_{msg_type}_message_id_{buyer}.txt"
        return path.read_text(encoding='utf-8').strip() if path.exists() else None

    def set_last(self, buyer: str, msg_type: str, msg_id: str):
        (self.last_dir / f"last_{msg_type}_message_id_{buyer}.txt").write_text(
            msg_id, encoding='utf-8'
        )

    def get_last_count(self, buyer: str) -> int:
        return self.chat_states.get(buyer, 0)

    def set_last_count(self, buyer: str, count: int):
        self.chat_states[buyer] = count
        self.save_chat_states()

    def is_waiting(self, buyer: str) -> bool:
        return self.buyer_states.get(buyer, False)

    def set_waiting(self, buyer: str, flag: bool):
        self.buyer_states[buyer] = flag
        self.save_states()

# --- API-клиент с retry/backoff ---
class PlayerokAPI:
    def __init__(self, cookies: List[dict], fragment_cookies: str, seed: str):
        self.cookies = {c['name']: c['value'] for c in cookies}
        self.fragment_cookies = fragment_cookies
        self.seed = seed

    async def send_message(self, receiver: str, text: str) -> bool:
        url = "https://playerok.com/graphql"
        payload = {
            'operationName': 'sendMessage',
            'variables': {'receiver': receiver, 'text': text, 'markdown': False},
            'extensions': {'persistedQuery': {'version': 1, 'sha256Hash': 'b71a34633625588062280264b85b8c70495b0a7a901449343a851989c109f406'}}
        }
        for attempt in range(1, API_RETRY_ATTEMPTS + 1):
            try:
                async with aiohttp.ClientSession(cookies=self.cookies) as session:
                    resp = await session.post(url, json=payload)
                    if resp.status == 200:
                        return True
                    log_error(Exception(f"Status {resp.status}"), 'send_message')
            except Exception as e:
                log_error(e, 'send_message')
            await asyncio.sleep(API_BACKOFF_FACTOR ** (attempt - 1))
        return False

    async def buy_stars(self, username: str, amount: int) -> tuple:
        url = "https://fragmentapi.nightstranger.space/api/buyStars"
        payload = {'username': username, 'amount': amount, 'fragment_cookies': self.fragment_cookies, 'seed': self.seed, 'show_sender': False}
        for attempt in range(1, API_RETRY_ATTEMPTS + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    resp = await session.post(url, json=payload, timeout=30)
                    data = await resp.json()
                    if resp.status == 200 and data.get('success'):
                        return True, data
                    log_error(Exception(f"API buyStars failed: {data}"), 'buy_stars')
            except Exception as e:
                log_error(e, 'buy_stars')
            await asyncio.sleep(API_BACKOFF_FACTOR ** (attempt - 1))
        return False, {}

# --- Монитор одного чата с восстановлением после сбоев ---
class ChatMonitor:
    def __init__(self, context: BrowserContext, api: PlayerokAPI, fm: FileManager, buyer: str, url: str):
        self.context = context
        self.api = api
        self.fm = fm
        self.buyer = buyer
        self.url = url
        self.page: Optional[Page] = None
        self.last_count: int = 0
        self.payment_pending: bool = False

    async def start(self):
        while True:
            try:
                if not self.page or self.page.is_closed():
                    self.page = await self.context.new_page()
                    await stealth_async(self.page)
                    await self.page.goto(self.url)
                    await asyncio.sleep(1)
                    current = await self.page.query_selector_all(
                        "span.MuiTypography-root.MuiTypography-14.mui-style-hkwtqx"
                    )
                    saved = self.fm.get_last_count(self.buyer)
                    for elem in current[saved:]:
                        await self.handle(await elem.inner_text())
                    self.last_count = len(current)
                    self.fm.set_last_count(self.buyer, self.last_count)

                elems = await self.page.query_selector_all(
                    "span.MuiTypography-root.MuiTypography-14.mui-style-hkwtqx"
                )
                new_len = len(elems)
                if new_len > self.last_count:
                    for elem in elems[self.last_count:new_len]:
                        await self.handle(await elem.inner_text())
                    self.last_count = new_len
                    self.fm.set_last_count(self.buyer, self.last_count)

                await asyncio.sleep(CHECK_INTERVAL)
            except Exception as e:
                log_error(e, f'monitor {self.buyer}')
                try:
                    if self.page:
                        await self.page.close()
                except:
                    pass
                await asyncio.sleep(5)

    async def handle(self, sys_text: str):
        # Нормализация текста, «ё» → «е» и приведение к нижнему регистру
        normalized = sys_text.lower().replace('ё', 'е')

        text_lower = normalized
        # Фиксируем факт оплаты
        if 'оплатил покупку' in text_lower:
            self.payment_pending = True
            return

        # Извлечение количества звезд из другого селектора
        match = re.search(r"(\d+)\s*звезд", normalized)
        if not match or not self.payment_pending:
            return
        stars = int(match.group(1))
        self.payment_pending = False

        # Запрос Telegram-username
        await self.api.send_message(
            self.buyer,
            f"Пожалуйста, отправьте Telegram username в формате @username для получения {stars} звёзд."
        )
        self.fm.set_waiting(self.buyer, True)
        # Ожидание корректного username
        while self.fm.is_waiting(self.buyer):
            user_elems = await self.page.query_selector_all(
                ".MuiTypography-root.MuiTypography-16.mmui-style-1g3e91c"
            )
            if user_elems:
                last_user = await user_elems[-1].inner_text()
                uid = hashlib.sha256(last_user.encode('utf-8')).hexdigest()
                if uid == self.fm.get_last(self.buyer, 'user'):
                    await asyncio.sleep(1)
                    continue
                self.fm.set_last(self.buyer, 'user', uid)
                if last_user.startswith('@'):
                    telegram = last_user[1:]
                    ok, data = await self.api.buy_stars(telegram, stars)
                    if ok:
                        try:
                            # Переходим по ссылке на страницу заказа
                            await self.page.click("a.MuiTypography-root.MuiTypography-inherit.MuiLink-root.MuiLink-underlineAlways.mmui-style-hlanhi", timeout=5000)
                            # Дожидаемся загрузки страницы заказа
                            await self.page.wait_for_load_state('networkidle')
                            await asyncio.sleep(2)
                            await self.page.click("button:has-text('Я выполнил')", timeout=5000)
                            await asyncio.sleep(2)
                            await self.page.click("label.MMuiBox-root.mmui-style-70qvj9 input[name='confirmed']", timeout=5000)
                            await asyncio.sleep(1)
                            await self.page.click("button.MMUiBox-root.mmui-style-driib9[type='submit']", timeout=5000)
                        except Exception as e:
                            log_error(e, f'handle {self.buyer}')
                        await self.api.send_message(
                            self.buyer,
                            f"⭐ Заказ выполнен: {stars} звёзд отправлены на @{telegram}. Hash: {data.get('transaction_hash')}"
                        )
                        self.fm.set_waiting(self.buyer, False)
                        return
                else:
                    await self.api.send_message(self.buyer, "Неверный формат. Пожалуйста, отправьте Telegram username в формате @username.")
            await asyncio.sleep(1)

async def main_loop():
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.write_text(json.dumps({'fragment_cookies': '', 'seed': ''}), encoding='utf-8')
        print('Fill settings.json')
        return
    settings = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    cookies = json.loads(COOKIES_FILE.read_text(encoding='utf-8'))
    fm = FileManager()
    api = PlayerokAPI(cookies, settings['fragment_cookies'], settings['seed'])

    async with async_playwright() as pw:
        browser = await pw.webkit.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(cookies)
        monitors: Dict[str, asyncio.Task] = {}
        last_sync = 0
        while True:
            now = asyncio.get_event_loop().time()
            if now - last_sync > CHAT_SYNC_INTERVAL:
                page = await context.new_page()
                await stealth_async(page)
                await page.goto('https://playerok.com/chats')
                await asyncio.sleep(1)
                links = await page.query_selector_all('a[href^="/chats/"]')
                current_buyers = []
                for link in links[:MAX_CHATS]:
                    buyer = await link.query_selector("span.MMuiTypography-root.MMuiTypography-16").inner_text()
                    href = await link.get_attribute('href')
                    url = f"https://playerok.com{href}"
                    current_buyers.append(buyer)
                    if buyer not in monitors:
                        mon = ChatMonitor(context, api, fm, buyer, url)
                        monitors[buyer] = asyncio.create_task(mon.start())
                for buyer in list(monitors):
                    if buyer not in current_buyers and not fm.is_waiting(buyer):
                        monitors[buyer].cancel()
                        del monitors[buyer]
                await page.close()
                last_sync = now
            await asyncio.sleep(1)

if __name__ == '__main__':
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print(Fore.YELLOW + 'Stopped by user.' + Style.RESET_ALL)
