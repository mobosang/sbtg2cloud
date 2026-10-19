import asyncio
import time
from telethon import TelegramClient, events, utils
from telethon.errors import SessionPasswordNeededError, PasswordHashInvalidError, FloodWaitError
from .extract import extract, resource_title


class Telegram:
    def __init__(self, store, directory):
        self.store, self.directory = store, directory
        self.client = None
        self.ready_at = None
        self.state = 'not_configured'
        self.error = ''
        self.qr_url = None
        self.login_task = None
        self.lock = asyncio.Lock()

    async def ensure(self):
        async with self.lock:
            config = self.store.get('telegram')
            if not config:
                return
            if self.client is None:
                self.client = TelegramClient(str(self.directory / 'telegram'), config['api_id'], config['api_hash'],
                                             auto_reconnect=False, connection_retries=1, catch_up=False,
                                             flood_sleep_threshold=0)
                self.client.add_event_handler(self.message, events.NewMessage(incoming=True))
            if not self.client.is_connected():
                self.ready_at = None
                self.state = 'connecting'
                await self.client.connect()
                if await self.client.is_user_authorized():
                    self.ready_at = int(time.time())
                    self.state = 'connected'
                    self.error = ''
                else:
                    self.state = 'login_required'

    async def run(self):
        while True:
            try:
                await self.ensure()
            except Exception:
                self.ready_at = None
                self.state = 'disconnected'
                self.error = 'Telegram 连接失败，10 秒后重连'
            await asyncio.sleep(10)

    async def message(self, event):
        if self.ready_at is None:
            return
        sources = self.store.rows('SELECT * FROM sources WHERE id=? AND enabled=1', (event.chat_id,))
        if not sources:
            return
        source = sources[0]
        if event.message.date.timestamp() < max(self.ready_at, source['enabled_at']):
            return
        urls = []
        for entity in event.message.entities or []:
            if getattr(entity, 'url', None):
                urls.append(entity.url)
        markup = event.message.reply_markup
        for row in getattr(markup, 'rows', []):
            for button in row.buttons:
                if getattr(button, 'url', None):
                    urls.append(button.url)
        links = extract(event.raw_text, urls)
        title = resource_title(event.raw_text) if len(links) == 1 else ''
        for link in links:
            self.store.enqueue(link, source, event.id, title)

    async def start_qr(self):
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            try:
                await self.login_task
            except asyncio.CancelledError:
                pass
        await self.ensure()
        if not self.client:
            raise ValueError('请先填写 Telegram API ID 和 API Hash')
        if await self.client.is_user_authorized():
            self.state = 'connected'
            return
        qr = await self.client.qr_login()
        self.qr_url = qr.url
        self.state = 'scan_qr'
        self.error = ''
        self.login_task = asyncio.create_task(self.wait_qr(qr))

    async def wait_qr(self, qr):
        try:
            await qr.wait()
            self.ready_at = int(time.time())
            self.state = 'connected'
        except SessionPasswordNeededError:
            self.state = 'password_required'
        except asyncio.TimeoutError:
            self.state = 'qr_expired'
        except Exception:
            self.state = 'login_required'
            self.error = '扫码登录失败，请重新生成二维码'
        finally:
            self.qr_url = None

    async def password(self, password):
        if self.state != 'password_required':
            raise ValueError('当前无需两步验证密码')
        try:
            await self.client.sign_in(password=password)
        except PasswordHashInvalidError:
            raise ValueError('两步验证密码错误') from None
        except FloodWaitError as error:
            raise ValueError(f'Telegram 要求等待 {error.seconds} 秒后重试') from None
        self.ready_at = int(time.time())
        self.state = 'connected'

    async def dialogs(self):
        if self.state != 'connected':
            raise ValueError('请先登录 Telegram')
        async for dialog in self.client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                self.store.execute('''INSERT INTO sources (id,title) VALUES (?,?)
                  ON CONFLICT(id) DO UPDATE SET title=excluded.title''', (utils.get_peer_id(dialog.entity), dialog.name))

    async def close(self):
        if self.login_task:
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)
        if self.client:
            await self.client.disconnect()
